// AD-27 sidecar: one child process, many concurrent Runs keyed by run_id. stdout
// carries protocol frames only; stderr carries allowlisted log fields only.
import { createHash } from "node:crypto";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { normalizeContext } from "@earendil-works/pi-ai";
import { ProtocolError, logLine, parseInbound, serialize } from "./protocol.mjs";
import { loadTokenizer } from "./tokenizer.mjs";
import { WikiRoot } from "./wiki-tools.mjs";
import { buildModel, failureFrom, productionStreamFn, providerKey } from "./model.mjs";
import { RunAborted, RunFailure, runAgent, toAgentMessages } from "./run.mjs";

const PROBE_TIMEOUT_MS = 1500;
const CONTEXT_PROBE_TIMEOUT_MS = 60_000; // off the /ready path (AD-30)

export function createSidecar({ bindingJson, apiKey, streamFn, fetchFn = fetch, write, log }) {
  const binding = JSON.parse(bindingJson);
  const digest = createHash("sha256").update(bindingJson, "utf8").digest("hex");
  const tokenizer = loadTokenizer(binding.tokenizer_authority.path);
  let wiki = null;
  try { wiki = new WikiRoot(binding.wiki_root); } catch { wiki = null; }
  const model = buildModel(binding);
  const callModel = streamFn ?? productionStreamFn(apiKey);
  const runs = new Map();
  const pending = new Set();
  // AD-30 context check: once per binding when it passes or finds a real shortfall;
  // a Provider error (down or cold at boot) is retried by the next healthy probe.
  let contextSettled = false;
  let contextFlight = null;
  const send = (type, runId, fields) => write(serialize(type, runId, fields));

  function track(promise) {
    pending.add(promise);
    promise.finally(() => pending.delete(promise));
    return promise;
  }

  async function run(frame) {
    if (runs.has(frame.run_id)) return;
    const controller = new AbortController();
    runs.set(frame.run_id, controller);
    const started = Date.now();
    let steps = 0;
    let lastTool;
    let terminal = "completed";
    let errorClass;
    const emit = (type, fields) => {
      if (controller.signal.aborted) return;
      if (type === "step") { steps += 1; lastTool = fields.step.kind; }
      send(type, frame.run_id, fields);
    };
    try {
      const result = await runAgent({ frame, binding, wiki, tokenizer, model, streamFn: callModel, emit,
        signal: controller.signal });
      if (controller.signal.aborted) { terminal = "cancelled"; send("aborted", frame.run_id, {}); }
      else send("final", frame.run_id, { binding_digest: digest, ...result });
    } catch (error) {
      if (controller.signal.aborted || error instanceof RunAborted) {
        terminal = "cancelled";
        send("aborted", frame.run_id, {});
      } else {
        const cause = error instanceof RunFailure ? error.cause : "internal";
        terminal = "failed";
        errorClass = cause;
        send("error", frame.run_id, { binding_digest: digest, cause,
          http_status: cause === "provider_http" ? error.httpStatus : null });
      }
    } finally {
      runs.delete(frame.run_id);
      log(logLine({ correlation_id: frame.correlation_id, run_stage: "terminal", duration_ms: Date.now() - started,
        terminal_state: terminal, error_class: errorClass, step_count: steps, tool_name: lastTool }));
    }
  }

  // A failed pi stream resolves stream.result() with stopReason "error"/"aborted"
  // (it does not throw -- see AssistantMessageEventStream.extractResult); throw here
  // so contextProbe() can classify the failure via failureFrom. On our own
  // timeout the errorMessage text is whatever the aborted fetch happened to report
  // (e.g. "Request aborted"), not necessarily anything timeout-shaped, so force the
  // classification explicitly instead of trusting that text.
  async function oneTokenCall(messages, timeoutMs) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    try {
      const stream = await callModel(model, normalizeContext({ systemPrompt: "", tools: [], messages }),
        { signal: controller.signal, maxTokens: 1 });
      const message = await stream.result();
      if (message.stopReason === "error" || message.stopReason === "aborted") {
        throw new Error(timedOut ? "timeout" : message.errorMessage);
      }
      return message;
    } finally { clearTimeout(timer); }
  }

  function logProbeFailure(runStage, error) {
    const { cause, http_status } = failureFrom(error?.message);
    const errorClass = error?.message === "model_missing" ? "provider_model_missing"
      : http_status != null ? `${cause}_${http_status}` : cause;
    log(logLine({ run_stage: runStage, error_class: errorClass }));
    return cause;
  }

  // AD-11 cheap health probe: the OpenAI-compatible model list, not a completion, so
  // it never queues behind a generation on a single-slot Ollama.
  async function modelListed() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
    try {
      const response = await fetchFn(`${binding.endpoint_origin}/v1/models`,
        { headers: { Authorization: `Bearer ${providerKey(apiKey)}` }, signal: controller.signal });
      if (!response.ok) throw new Error(`${response.status} models`);
      const body = await response.json();
      if (!Array.isArray(body?.data) || !body.data.some((entry) => entry?.id === binding.model_revision)) {
        throw new Error("model_missing");
      }
    } catch (error) {
      throw controller.signal.aborted ? new Error("timeout") : error;
    } finally { clearTimeout(timer); }
  }

  async function probe(probeId) {
    let providerOk = false;
    try {
      await modelListed();
      providerOk = true;
    } catch (error) {
      logProbeFailure("probe", error);
    }
    send("probe_ok", null, { probe_id: probeId, binding_digest: digest, provider_ok: providerOk,
      wiki_ok: Boolean(wiki?.indexReadable()) });
    if (providerOk) startContextProbe();
  }

  function startContextProbe() {
    if (contextSettled || contextFlight) return contextFlight;
    contextFlight = track(contextProbe().finally(() => { contextFlight = null; }));
    return contextFlight;
  }

  async function contextProbe() {
    // AD-30: Ollama silently drops the oldest messages beyond its num_ctx. Send one
    // request near the window and compare the reported input with our own count.
    const target = binding.model_context_window - binding.max_output_tokens - 512;
    const history = [];
    let counted = 0;
    while (counted < target) {
      const text = "위키 문맥 길이 확인 문장입니다. ".repeat(50);
      history.push({ role: "user", text }, { role: "assistant", text: "확인" });
      counted += tokenizer.count(text) + tokenizer.count("확인");
    }
    let ok = false;
    try {
      const message = await oneTokenCall([...toAgentMessages(history, model),
        { role: "user", content: ".", timestamp: Date.now() }], CONTEXT_PROBE_TIMEOUT_MS);
      const reported = (message.usage?.input ?? 0) + (message.usage?.cacheRead ?? 0);
      ok = reported >= counted;
      contextSettled = true;
    } catch (error) {
      ok = false;
      contextSettled = !logProbeFailure("context_probe", error).startsWith("provider_");
    }
    send("context_probe", null, { binding_digest: digest, effective_context_ok: ok });
  }

  return {
    handleLine(line) {
      let frame;
      try { frame = parseInbound(line); } catch (error) {
        if (!(error instanceof ProtocolError)) throw error;
        send("error", null, { binding_digest: digest, cause: "protocol", http_status: null });
        return;
      }
      if (frame.type === "run") track(run(frame));
      else if (frame.type === "abort") runs.get(frame.run_id)?.abort();
      else track(probe(frame.probe_id));
    },
    contextProbe: startContextProbe,
    async idle() { while (pending.size) await Promise.allSettled([...pending]); },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const sidecar = createSidecar({
    bindingJson: process.env.AIDD_AGENT_BINDING ?? "",
    apiKey: process.env.OLLAMA_API_KEY ?? "",
    write: (line) => process.stdout.write(line),
    log: (line) => process.stderr.write(`${line}\n`),
  });
  createInterface({ input: process.stdin })
    .on("line", (line) => sidecar.handleLine(line))
    // The parent is gone: flush what is already written, then exit rather than orphan.
    .on("close", () => process.stdout.write("", () => process.exit(0)));
  void sidecar.contextProbe();
}
