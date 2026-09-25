export const PROTOCOL_VERSION = 1;
export class ProtocolError extends Error {}

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// Same code points as aidd_chat.contracts._UNSAFE_TEXT (src/aidd_chat/contracts/__init__.py;
// tests/test_wiki_agent_adapter.py asserts both reject the same probe strings).
// Every non-ASCII code point below is written ONLY as a \uXXXX / \u{XXXXX} escape --
// no literal special/invisible characters in this source line, on purpose: a literal
// copy-paste of an invisible character here would be exactly the kind of smuggled
// code point this pattern exists to catch, and would be invisible in a diff too.
export const UNSAFE_TEXT =
  /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u00ad\u180e\u061c\u200b\u200e-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u2028\u2029\ud800-\udfff\ufeff\u{e0000}-\u{e007f}]/u;
export const ALLOWED_LOG_FIELDS = ["correlation_id", "run_stage", "duration_ms", "terminal_state",
  "error_class", "step_count", "tool_name"];

const FIELDS = {
  run: ["v", "type", "run_id", "correlation_id", "system_instruction", "messages"],
  abort: ["v", "type", "run_id"],
  probe: ["v", "type", "run_id", "probe_id"],
};

function exactKeys(value, keys) {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  return actual.length === expected.length && expected.every((key, index) => key === actual[index]);
}

export function parseInbound(line) {
  let frame;
  try { frame = JSON.parse(line); } catch { throw new ProtocolError("json"); }
  if (!frame || typeof frame !== "object" || Array.isArray(frame)) throw new ProtocolError("shape");
  if (frame.v !== PROTOCOL_VERSION || !Object.hasOwn(FIELDS, frame.type)) throw new ProtocolError("type");
  if (!exactKeys(frame, FIELDS[frame.type])) throw new ProtocolError("fields");
  if (frame.type === "probe") {
    if (frame.run_id !== null) throw new ProtocolError("run_id");
    // Echoed on probe_ok so a late reply to a timed-out probe answers nobody.
    if (typeof frame.probe_id !== "string" || !UUID_V4.test(frame.probe_id)) throw new ProtocolError("probe_id");
    return frame;
  }
  if (typeof frame.run_id !== "string" || !UUID_V4.test(frame.run_id)) throw new ProtocolError("run_id");
  if (frame.type === "abort") return frame;
  if (typeof frame.correlation_id !== "string" || !UUID_V4.test(frame.correlation_id)
    || typeof frame.system_instruction !== "string" || !Array.isArray(frame.messages)
    || frame.messages.length === 0) throw new ProtocolError("run");
  for (const message of frame.messages) {
    if (!message || typeof message !== "object" || !exactKeys(message, ["role", "text"])
      || !["user", "assistant"].includes(message.role) || typeof message.text !== "string") {
      throw new ProtocolError("message");
    }
  }
  if (frame.messages.at(-1).role !== "user") throw new ProtocolError("current");
  return frame;
}

export function serialize(type, runId, fields = {}) {
  return `${JSON.stringify({ v: PROTOCOL_VERSION, type, run_id: runId, ...fields })}\n`;
}

export function logLine(fields) {
  const kept = {};
  for (const key of ALLOWED_LOG_FIELDS) if (fields[key] !== undefined) kept[key] = fields[key];
  return JSON.stringify(kept);
}
