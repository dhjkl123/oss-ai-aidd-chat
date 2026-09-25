import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { createSidecar } from "./sidecar.mjs";
import { errorTurn, hangTurn, scripted, textTurn, toolCallTurn } from "./test-helpers.mjs";

const RUN = "3f2b7c1e-8a4d-4c2b-9e1f-0a1b2c3d4e5f";
const bindingJson = JSON.stringify({
  endpoint_origin: "http://127.0.0.1:9", model_revision: "fake", model_context_window: 32768,
  max_output_tokens: 2048, wiki_root: fileURLToPath(new URL("./test-fixtures/wiki/", import.meta.url)),
  tokenizer_authority: { name: "hf-tokenizers", sha256: "x",
    path: fileURLToPath(new URL("../tests/fixtures/tiny-tokenizer.json", import.meta.url)) },
  limits: { max_tool_calls: 8, max_read_tokens: 3000, max_tool_output_tokens: 12000, max_output_tokens: 2048,
    fixed_overhead_tokens: 1500, model_context_window: 32768 },
});
const DIGEST = createHash("sha256").update(bindingJson, "utf8").digest("hex");
const runLine = JSON.stringify({ v: 1, type: "run", run_id: RUN, correlation_id: RUN, system_instruction: "s",
  messages: [{ role: "user", text: "alpha?" }] });

const PROBE = JSON.stringify({ v: 1, type: "probe", run_id: null, probe_id: RUN });
const listing = (ids) => async () => new Response(JSON.stringify({ object: "list", data: ids.map((id) => ({ id })) }));

function harness(turns, fetchFn = listing(["fake"])) {
  const out = [];
  const logs = [];
  const { fn, calls } = scripted(turns);
  const sidecar = createSidecar({ bindingJson, apiKey: "", streamFn: fn, fetchFn,
    write: (line) => out.push(JSON.parse(line)), log: (line) => logs.push(line) });
  return { sidecar, out, logs, calls };
}

// A context-probe reply whose reported input covers the whole window: no shortfall.
const fullContextTurn = () => (s, msg) => {
  msg.usage = { ...msg.usage, input: 1e9 };
  textTurn(".")(s, msg);
};

test("run ends with exactly one final carrying the digest", async () => {
  const { sidecar, out } = harness([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md"] }),
    textTurn("답"),
  ]);
  sidecar.handleLine(runLine);
  await sidecar.idle();
  assert.equal(out.at(-1).type, "final");
  assert.equal(out.at(-1).binding_digest, DIGEST);
  assert.equal(out.filter((frame) => ["final", "error", "aborted"].includes(frame.type)).length, 1);
  for (const frame of out) assert.equal(frame.v, 1);
});

test("abort produces aborted as the last frame", async () => {
  const { sidecar, out } = harness([hangTurn()]);
  sidecar.handleLine(runLine);
  await new Promise((resolve) => setTimeout(resolve, 20));
  sidecar.handleLine(JSON.stringify({ v: 1, type: "abort", run_id: RUN }));
  await sidecar.idle();
  assert.equal(out.at(-1).type, "aborted");
});

test("malformed line yields a protocol error frame with null run_id", () => {
  const { sidecar, out } = harness([]);
  sidecar.handleLine("{nope");
  assert.deepEqual(out[0], { v: 1, type: "error", run_id: null, binding_digest: DIGEST, cause: "protocol", http_status: null });
});

test("probe lists models, echoes probe_id and never calls the model", async () => {
  const seen = [];
  const { sidecar, out, calls } = harness([textTurn(".")], async (url, init) => {
    seen.push({ url, auth: init.headers.Authorization });
    return listing(["other", "fake"])();
  });
  await sidecar.contextProbe();  // settles (a shortfall), so the probe below is the only other call
  out.length = 0;
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  assert.deepEqual(out[0], { v: 1, type: "probe_ok", run_id: null, probe_id: RUN, binding_digest: DIGEST,
    provider_ok: true, wiki_ok: true });
  assert.deepEqual(seen, [{ url: "http://127.0.0.1:9/v1/models", auth: "Bearer ollama" }]);
  assert.equal(calls.length, 1);
});

test("probe: a model missing from the list is not ok, with its own error_class", async () => {
  const { sidecar, out, logs } = harness([], listing(["other"]));
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  assert.equal(out[0].provider_ok, false);
  assert.deepEqual(JSON.parse(logs[0]), { run_stage: "probe", error_class: "provider_model_missing" });
});

test("probe failure logs a classified error_class with no message text or endpoint", async () => {
  const { sidecar, out, logs } = harness([], async () => new Response('"model not found"', { status: 404 }));
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  assert.equal(out[0].provider_ok, false);
  assert.equal(logs.length, 1);
  const fields = JSON.parse(logs[0]);
  assert.equal(fields.run_stage, "probe");
  assert.equal(fields.error_class, "provider_http_404");
  assert.equal(logs[0].includes("model not found"), false);
  assert.equal(logs[0].includes("127.0.0.1"), false);
});

test("context probe failure logs run_stage context_probe", async () => {
  const { sidecar, logs } = harness([errorTurn('503 "busy"')]);
  await sidecar.contextProbe();
  assert.equal(logs.length, 1);
  const fields = JSON.parse(logs[0]);
  assert.equal(fields.run_stage, "context_probe");
  assert.equal(fields.error_class, "provider_http_503");
});

test("probe timeout classifies as provider_timeout", async () => {
  const hang = (url, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("This operation was aborted", "AbortError")));
  });
  const { sidecar, out, logs } = harness([], hang);
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  assert.equal(out[0].provider_ok, false);
  assert.equal(logs.length, 1);
  const fields = JSON.parse(logs[0]);
  assert.equal(fields.run_stage, "probe");
  assert.equal(fields.error_class, "provider_timeout");
});

test("a context probe that hit a Provider error is retried by the next healthy probe, once", async () => {
  const { sidecar, out, calls } = harness([errorTurn("fetch failed: ECONNREFUSED"), fullContextTurn()]);
  await sidecar.contextProbe();
  sidecar.handleLine(PROBE);
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  sidecar.handleLine(PROBE);  // settled now: no third context probe
  await sidecar.idle();
  assert.equal(calls.length, 2);
  assert.deepEqual(out.filter((frame) => frame.type === "context_probe").map((frame) => frame.effective_context_ok),
    [false, true]);
});

test("a genuine context shortfall is sticky: no retry", async () => {
  const { sidecar, out, calls } = harness([textTurn(".")]);  // reports 10 input tokens
  await sidecar.contextProbe();
  sidecar.handleLine(PROBE);
  await sidecar.idle();
  assert.equal(calls.length, 1);
  assert.deepEqual(out.filter((frame) => frame.type === "context_probe").map((frame) => frame.effective_context_ok),
    [false]);
});

test("the sidecar process exits when its stdin closes, even mid context probe", async () => {
  // A Provider that never answers keeps the boot context probe (and the loop) alive.
  const server = createServer(() => {});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const binding = { ...JSON.parse(bindingJson), endpoint_origin: `http://127.0.0.1:${server.address().port}` };
  const child = spawn(process.execPath, [fileURLToPath(new URL("./sidecar.mjs", import.meta.url))], {
    env: { ...process.env, AIDD_AGENT_BINDING: JSON.stringify(binding) }, stdio: ["pipe", "ignore", "ignore"] });
  const exited = new Promise((resolve) => child.on("exit", (code) => resolve(code)));
  await new Promise((resolve) => setTimeout(resolve, 300));
  child.stdin.end();
  const timer = setTimeout(() => child.kill(), 5_000);
  try {
    assert.equal(await exited, 0);
  } finally {
    clearTimeout(timer);
    server.closeAllConnections();
    server.close();
  }
});

test("logs carry only allowlisted fields and no content", async () => {
  const { sidecar, logs } = harness([toolCallTurn("decide", { outcome: "meta", used_paths: [] })]);
  sidecar.handleLine(runLine);
  await sidecar.idle();
  assert.ok(logs.length > 0);
  for (const line of logs) {
    for (const key of Object.keys(JSON.parse(line))) {
      assert.ok(["correlation_id", "run_stage", "duration_ms", "terminal_state", "error_class", "step_count", "tool_name"].includes(key));
    }
    assert.equal(line.includes("alpha"), false);
  }
});
