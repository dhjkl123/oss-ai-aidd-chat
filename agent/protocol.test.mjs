import { test } from "node:test";
import assert from "node:assert/strict";
import { parseInbound, serialize, ProtocolError, logLine, UNSAFE_TEXT } from "./protocol.mjs";

const RUN = "3f2b7c1e-8a4d-4c2b-9e1f-0a1b2c3d4e5f";
const run = (over = {}) => JSON.stringify({ v: 1, type: "run", run_id: RUN, correlation_id: RUN,
  system_instruction: "s", messages: [{ role: "user", text: "q" }], ...over });

test("accepts the three inbound types", () => {
  assert.equal(parseInbound(run()).type, "run");
  assert.equal(parseInbound(JSON.stringify({ v: 1, type: "abort", run_id: RUN })).type, "abort");
  assert.equal(parseInbound(JSON.stringify({ v: 1, type: "probe", run_id: null, probe_id: RUN })).type, "probe");
});

for (const [name, line] of [
  ["not json", "{"],
  ["array", "[]"],
  ["unknown version", run({ v: 2 })],
  ["unknown type", JSON.stringify({ v: 1, type: "exec", run_id: RUN })],
  ["bad run id", run({ run_id: "x" })],
  ["last message not user", run({ messages: [{ role: "assistant", text: "a" }] })],
  ["bad role", run({ messages: [{ role: "system", text: "a" }] })],
  ["extra field", run({ tools: [] })],
  ["probe without probe_id", JSON.stringify({ v: 1, type: "probe", run_id: null })],
  ["bad probe_id", JSON.stringify({ v: 1, type: "probe", run_id: null, probe_id: "x" })],
]) {
  test(`refuses ${name}`, () => assert.throws(() => parseInbound(line), ProtocolError));
}

test("serialize writes one line with v and run_id", () => {
  const line = serialize("delta", RUN, { text: "가\n나" });
  assert.equal(line.endsWith("\n"), true);
  assert.equal(line.split("\n").length, 2);
  assert.deepEqual(JSON.parse(line), { v: 1, type: "delta", run_id: RUN, text: "가\n나" });
});

test("log lines keep only allowlisted fields", () => {
  const parsed = JSON.parse(logLine({ correlation_id: RUN, tool_name: "wiki_read", path: "secret.md", text: "x" }));
  assert.deepEqual(Object.keys(parsed).sort(), ["correlation_id", "tool_name"]);
});

// Probe list and expected booleans generated once from the real Python predicate
// (aidd_chat.contracts.has_unsafe_code_point) -- see task-7-report.md Fix round 1
// for the exact `uv run python -c ...` command and its output this table was copied from.
const UNSAFE_TABLE = [
  ["0x00", 0x0, true],
  ["0x08", 0x8, true],
  ["0x09", 0x9, false],
  ["0x0A", 0xA, false],
  ["0x0B", 0xB, true],
  ["0x0C", 0xC, true],
  ["0x0D", 0xD, false],
  ["0x0E", 0xE, true],
  ["0x1F", 0x1F, true],
  ["0x20", 0x20, false],
  ["0x7E", 0x7E, false],
  ["0x7F", 0x7F, true],
  ["0x9F", 0x9F, true],
  ["0xA0", 0xA0, false],
  ["0xAD", 0xAD, true],
  ["0x61C", 0x61C, true],
  ["0x180E", 0x180E, true],
  ["0x200B", 0x200B, true],
  ["0x200C", 0x200C, false],
  ["0x200D", 0x200D, false],
  ["0x200E", 0x200E, true],
  ["0x200F", 0x200F, true],
  ["0x2028", 0x2028, true],
  ["0x2029", 0x2029, true],
  ["0x202A", 0x202A, true],
  ["0x202E", 0x202E, true],
  ["0x202F", 0x202F, false],
  ["0x2060", 0x2060, true],
  ["0x2064", 0x2064, true],
  ["0x2065", 0x2065, false],
  ["0x2066", 0x2066, true],
  ["0x2069", 0x2069, true],
  ["0xFE0F", 0xFE0F, false],
  ["0xFEFF", 0xFEFF, true],
  ["0xD800 (lone surrogate)", 0xD800, true],
  ["0xE0000", 0xE0000, true],
  ["0xE007F", 0xE007F, true],
  ["0xE0080", 0xE0080, false],
  ["'A'", 0x41, false],
  ["U+AC00 (Korean GA)", 0xAC00, false],
  ["' ' (space)", 0x20, false],
];

test("UNSAFE_TEXT matches Python has_unsafe_code_point on every range boundary", () => {
  for (const [label, cp, expected] of UNSAFE_TABLE) {
    const actual = UNSAFE_TEXT.test(String.fromCodePoint(cp));
    assert.equal(actual, expected, `${label}: expected ${expected}, got ${actual}`);
  }
  assert.equal(UNSAFE_TEXT.test("hello world"), false);
});
