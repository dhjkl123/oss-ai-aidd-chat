// tests/scripted_sidecar.mjs -- speaks the AD-27 protocol with canned behaviour
// chosen by the current question text. Used only by tests/test_wiki_agent_adapter.py.
import { createHash } from "node:crypto";
import { appendFileSync } from "node:fs";
import { createInterface } from "node:readline";

const bindingJson = process.env.AIDD_AGENT_BINDING ?? "";
const digest = process.env.SCRIPTED_WRONG_DIGEST ? "0".repeat(64)
  : createHash("sha256").update(bindingJson, "utf8").digest("hex");
const out = (frame) => process.stdout.write(`${JSON.stringify({ v: 1, ...frame })}\n`);
const waiting = new Map();
let probes = 0;
const SOURCE = { path: "concepts/alpha.md", title: "Alpha 개념", confidence: "low", contested: true };
// Non-disclosure test only: record the key the child got through env, then every stdin line.
const stdinLog = process.env.SCRIPTED_STDIN_LOG;
if (stdinLog) appendFileSync(stdinLog, `ENV ${process.env.OLLAMA_API_KEY ?? ""}\n`);

// SCRIPTED_CONTEXT_OK: unset = ok, "0" = shortfall, "none" = never report.
if (process.env.SCRIPTED_CONTEXT_OK !== "none") {
  out({ type: "context_probe", run_id: null, binding_digest: digest,
    effective_context_ok: process.env.SCRIPTED_CONTEXT_OK !== "0" });
}
process.stderr.write(`${JSON.stringify({ correlation_id: "boot", tool_name: "wiki_read" })}\n`);

createInterface({ input: process.stdin }).on("line", (line) => {
  if (stdinLog) appendFileSync(stdinLog, `${line}\n`);
  const frame = JSON.parse(line);
  if (frame.type === "probe") {
    probes += 1;
    const reply = { type: "probe_ok", run_id: null, probe_id: frame.probe_id, binding_digest: digest, wiki_ok: true };
    // SCRIPTED_STALE_PROBE: the first reply is late (700 ms) and not ok; later ones take 300 ms.
    if (process.env.SCRIPTED_STALE_PROBE) {
      setTimeout(() => out({ ...reply, provider_ok: probes > 1 }), probes === 1 ? 700 : 300);
    } else {
      out({ ...reply, provider_ok: true });
    }
    return;
  }
  if (frame.type === "abort") {
    const resolve = waiting.get(frame.run_id);
    if (resolve) { waiting.delete(frame.run_id); resolve(); }
    return;
  }
  const id = frame.run_id;
  const question = frame.messages.at(-1).text;
  const step = (index, kind, label, docPath = null) =>
    out({ type: "step", run_id: id, step: { step_index: index, kind, label, doc_path: docPath } });
  const hang = () => waiting.set(id, () => out({ type: "aborted", run_id: id }));
  const [command, cause, status] = question.split(":");
  if (command === "grounded") {
    step(1, "wiki_read", "문서 읽는 중: Alpha 개념", "concepts/alpha.md");
    step(2, "decide", "근거 판단 중");
    step(3, "compose", "답변 작성 중");
    out({ type: "delta", run_id: id, text: "알파는 " });
    out({ type: "delta", run_id: id, text: "개념이에요." });
    out({ type: "final", run_id: id, binding_digest: digest, outcome: "grounded", uncovered: null,
      sources: [SOURCE], search_truncated: false, finish: "stop" });
  } else if (command === "gap") {
    step(1, "decide", "근거 판단 중");
    out({ type: "final", run_id: id, binding_digest: digest, outcome: "wiki_gap", uncovered: null,
      sources: [], search_truncated: false, finish: "stop" });
  } else if (command === "error") {
    out({ type: "error", run_id: id, binding_digest: digest, cause,
      http_status: status === undefined ? null : Number(status) });
  } else if (command === "badcause") {
    out({ type: "error", run_id: id, binding_digest: digest, cause: ["provider_http"], http_status: null });
  } else if (command === "hang") {
    step(1, "wiki_index", "Wiki 목록 확인");
    hang();
  } else if (command === "exit") {
    process.exit(3);
  } else if (command === "badstep") {
    out({ type: "step", run_id: id, step: { step_index: 1, kind: "wiki_search", label: "Wiki 검색 중: 비밀", doc_path: null } });
    hang();
  } else if (command === "unknown") {
    out({ type: "surprise", run_id: id });
    hang();
  } else if (command === "length") {
    out({ type: "delta", run_id: id, text: "잘" });
    out({ type: "final", run_id: id, binding_digest: digest, outcome: "grounded", uncovered: null,
      sources: [SOURCE], search_truncated: false, finish: "length" });
  }
});
