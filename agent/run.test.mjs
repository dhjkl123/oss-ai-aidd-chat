import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { loadTokenizer } from "./tokenizer.mjs";
import { WikiRoot } from "./wiki-tools.mjs";
import { RunAborted, RunFailure, runAgent } from "./run.mjs";
import { errorTurn, fakeModel, hangTurn, scripted, textTurn, toolCallTurn } from "./test-helpers.mjs";

const tokenizer = loadTokenizer(fileURLToPath(new URL("../tests/fixtures/tiny-tokenizer.json", import.meta.url)));
const wiki = new WikiRoot(fileURLToPath(new URL("./test-fixtures/wiki/", import.meta.url)));
const LIMITS = { max_tool_calls: 8, max_read_tokens: 3000, max_tool_output_tokens: 12000,
  max_output_tokens: 2048, fixed_overhead_tokens: 1500, model_context_window: 32768 };
const binding = { limits: LIMITS };
const frame = { run_id: "r", correlation_id: "c", system_instruction: "research",
  messages: [{ role: "user", text: "이전 질문" }, { role: "assistant", text: "이전 답" }, { role: "user", text: "alpha는?" }] };

async function go(turns, over = {}) {
  const frames = [];
  const { fn, calls } = scripted(turns);
  const result = await runAgent({ frame, binding, wiki, tokenizer, model: fakeModel, streamFn: fn,
    emit: (type, fields) => frames.push({ type, ...fields }), signal: new AbortController().signal, ...over });
  return { result, frames, calls };
}
const kinds = (frames) => frames.filter((f) => f.type === "step").map((f) => f.step.kind);
const deltas = (frames) => frames.filter((f) => f.type === "delta").map((f) => f.text);

test("grounded: research tools, decide, then compose deltas", async () => {
  const { result, frames, calls } = await go([
    toolCallTurn("wiki_index", {}),
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md"] }),
    textTurn("Alpha는 개념이에요."),
  ]);
  assert.deepEqual(kinds(frames), ["wiki_index", "wiki_read", "decide", "compose"]);
  assert.deepEqual(deltas(frames), ["Alpha는 개념이에요."]);
  assert.deepEqual(result, { outcome: "grounded", uncovered: null, finish: "stop", search_truncated: false,
    sources: [{ path: "concepts/alpha.md", title: "Alpha 개념", confidence: "low", contested: true }] });
  const compose = calls.at(-1).context;
  assert.equal((compose.tools ?? []).length, 0);
  assert.ok(!compose.systemPrompt.includes("alphaword"));
  assert.ok(JSON.stringify(compose.messages).includes("alphaword"));
});

test("research text is discarded", async () => {
  const { frames } = await go([textTurn("모델 잡담")]);
  assert.deepEqual(deltas(frames), []);
});

test("no decide becomes wiki_gap without compose", async () => {
  const { result, frames } = await go([toolCallTurn("wiki_search", { query: "zzz" }), textTurn("모름")]);
  assert.equal(result.outcome, "wiki_gap");
  assert.deepEqual(result.sources, []);
  assert.ok(!kinds(frames).includes("compose"));
});

test("used paths that were never read are dropped; grounded with none becomes wiki_gap", async () => {
  const { result } = await go([toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md"] })]);
  assert.equal(result.outcome, "wiki_gap");
});

test("partial keeps uncovered and asks compose to state it", async () => {
  const { result, calls } = await go([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "partial", used_paths: ["./concepts/alpha.md"], uncovered: "가격 정보" }),
    textTurn("일부 답"),
  ]);
  assert.equal(result.outcome, "partial");
  assert.equal(result.uncovered, "가격 정보");
  assert.match(calls.at(-1).context.systemPrompt, /가격 정보/);
});

for (const outcome of ["wiki_gap", "out_of_scope", "meta"]) {
  test(`${outcome} never composes`, async () => {
    const { result, frames } = await go([toolCallTurn("decide", { outcome, used_paths: [] })]);
    assert.equal(result.outcome, outcome);
    assert.deepEqual(deltas(frames), []);
  });
}

test("limit leaves only decide for the next turn and sets search_truncated", async () => {
  const { result, frames, calls } = await go([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md"] }),
    textTurn("답"),
  ], { binding: { limits: { ...LIMITS, max_tool_calls: 1 } } });
  assert.equal(result.search_truncated, true);
  assert.ok(kinds(frames).includes("limit_reached"));
  assert.deepEqual(calls[1].context.tools.map((tool) => tool.name), ["decide"]);
});

test("provider http error becomes RunFailure with status", async () => {
  await assert.rejects(go([errorTurn("429: slow down")]),
    (error) => error instanceof RunFailure && error.cause === "provider_http" && error.httpStatus === 429);
});

test("compose length finish is reported", async () => {
  const { result } = await go([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md"] }),
    textTurn("잘린 답", "length"),
  ]);
  assert.equal(result.finish, "length");
});

test("missing index fails wiki_unavailable before any model call", async () => {
  const broken = Object.assign(Object.create(Object.getPrototypeOf(wiki)), wiki, { indexReadable: () => false });
  const { fn, calls } = scripted([]);
  await assert.rejects(runAgent({ frame, binding, wiki: broken, tokenizer, model: fakeModel, streamFn: fn,
    emit: () => {}, signal: new AbortController().signal }),
  (error) => error instanceof RunFailure && error.cause === "wiki_unavailable");
  assert.equal(calls.length, 0);
});

test("abort during research ends with RunAborted", async () => {
  const controller = new AbortController();
  const pending = go([hangTurn()], { signal: controller.signal });
  setTimeout(() => controller.abort(), 20);
  await assert.rejects(pending, (error) => error instanceof RunAborted);
});

test("oversized request is refused before sending", async () => {
  const huge = { ...frame, messages: [{ role: "user", text: "가 ".repeat(40000) }] };
  const { fn, calls } = scripted([]);
  await assert.rejects(runAgent({ frame: huge, binding, wiki, tokenizer, model: fakeModel, streamFn: fn,
    emit: () => {}, signal: new AbortController().signal }),
  (error) => error instanceof RunFailure && error.cause === "context_overflow");
  assert.equal(calls.length, 0);
});

test("runaway model calls end research as wiki_gap", async () => {
  const turns = Array.from({ length: 20 }, () => toolCallTurn("wiki_index", {}));
  const { result } = await go(turns, { binding: { limits: { ...LIMITS, max_tool_calls: 2 } } });
  assert.equal(result.outcome, "wiki_gap");
});

// A document block that starts with `[문서: <path>]` counts as over budget for `huge`.
const inflated = (huge) => ({ count: (text) => (huge.some((path) => text.startsWith(`[문서: ${path}]`))
  ? 1e9 : tokenizer.count(text)) });

test("an oversized document is skipped, not the rest; sources list only what compose saw", async () => {
  const { result, calls } = await go([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("wiki_read", { path: "comparisons/beta.md" }),
    toolCallTurn("decide", { outcome: "grounded", used_paths: ["concepts/alpha.md", "comparisons/beta.md"] }),
    textTurn("답"),
  ], { tokenizer: inflated(["concepts/alpha.md"]) });
  assert.deepEqual(result.sources.map((source) => source.path), ["comparisons/beta.md"]);
  const composed = JSON.stringify(calls.at(-1).context.messages);
  assert.ok(composed.includes("[문서: comparisons/beta.md]"));
  assert.ok(!composed.includes("[문서: concepts/alpha.md]"));
});

test("no document fits the budget: wiki_gap, no compose", async () => {
  const { result, frames, calls } = await go([
    toolCallTurn("wiki_read", { path: "concepts/alpha.md" }),
    toolCallTurn("decide", { outcome: "partial", used_paths: ["concepts/alpha.md"], uncovered: "x" }),
  ], { tokenizer: inflated(["concepts/alpha.md"]) });
  assert.deepEqual(result, { outcome: "wiki_gap", uncovered: null, sources: [], search_truncated: false, finish: "stop" });
  assert.ok(!kinds(frames).includes("compose"));
  assert.equal(calls.length, 2);
});
