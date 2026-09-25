// AD-29: research (Wiki tools + decide; model text discarded) then compose (no tools;
// its deltas are the only model text a user sees). Fresh Agent per Run.
import { readFileSync } from "node:fs";
import { Agent } from "@earendil-works/pi-agent-core";
import { createAssistantMessageEventStream, getCurrentSystemPrompt, getCurrentTools, normalizeContext } from "@earendil-works/pi-ai";
import { LABELS, createTools, emitStep, limitReached, markLimit, newRunState } from "./wiki-tools.mjs";
import { failureFrom } from "./model.mjs";
import { UNSAFE_TEXT } from "./protocol.mjs";

export const COMPOSE_INSTRUCTION =
  "Answer in Korean using only the documents below. Do not add facts, names or numbers that are not in them. "
  + "If the documents disagree, say so.";
const SOURCED = new Set(["grounded", "partial"]);
const OUTCOMES = new Set(["grounded", "partial", "wiki_gap", "out_of_scope", "meta"]);
const DEFAULT_UNCOVERED = "질문의 나머지 부분";
const ZERO_USAGE = () => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });

export class RunFailure extends Error {
  constructor(cause, httpStatus = null) { super(cause); this.cause = cause; this.httpStatus = httpStatus; }
}
export class RunAborted extends Error {}

export function toAgentMessages(history, model) {
  return history.map((item) => item.role === "user"
    ? { role: "user", content: item.text, timestamp: Date.now() }
    : { role: "assistant", content: [{ type: "text", text: item.text }], api: model.api,
      provider: model.provider, model: model.id, usage: ZERO_USAGE(), stopReason: "stop", timestamp: Date.now() });
}

function countContext(tokenizer, context) {
  return tokenizer.count(JSON.stringify({
    system: context.systemPrompt ?? "",
    messages: context.messages ?? [],
    tools: (context.tools ?? []).map((tool) => ({ name: tool.name, description: tool.description, parameters: tool.parameters })),
  }));
}

// The library's normalizeContext() folds systemPrompt/tools into the transcript's
// leading system message and returns only { messages } (see @earendil-works/pi-ai
// utils/transcript.js): there is no top-level systemPrompt/tools on the object a
// StreamFn actually receives. countContext() re-counts an already-folded context by
// its messages alone, so the prompt/tool text baked into that system message is
// counted exactly once instead of twice.
function countMessages(tokenizer, context) {
  return countContext(tokenizer, { messages: context.messages });
}

function refusedStream(model, reason) {
  const stream = createAssistantMessageEventStream();
  const message = { role: "assistant", content: [], api: model.api, provider: model.provider, model: model.id,
    usage: ZERO_USAGE(), stopReason: "error", errorMessage: reason, timestamp: Date.now() };
  queueMicrotask(() => {
    stream.push({ type: "start", partial: message });
    stream.push({ type: "error", reason: "error", error: message });
  });
  return stream;
}

function cleanUncovered(value) {
  if (typeof value !== "string") return DEFAULT_UNCOVERED;
  const trimmed = value.trim().slice(0, 200);
  return trimmed && !UNSAFE_TEXT.test(trimmed) ? trimmed : DEFAULT_UNCOVERED;
}

function decisionOf(state, wiki) {
  const decision = state.decision;
  if (!decision || !OUTCOMES.has(decision.outcome)) return { outcome: "wiki_gap", sources: [], uncovered: null };
  if (!SOURCED.has(decision.outcome)) return { outcome: decision.outcome, sources: [], uncovered: null };
  const named = new Set((decision.used_paths ?? []).map((path) => wiki.resolve(path)?.id).filter(Boolean));
  const sources = [...state.reads.entries()]
    .filter(([id]) => named.has(id))
    .map(([path, meta]) => ({ path, title: meta.title, confidence: meta.confidence, contested: meta.contested }));
  if (sources.length === 0) return { outcome: "wiki_gap", sources: [], uncovered: null };
  return { outcome: decision.outcome, sources,
    uncovered: decision.outcome === "partial" ? cleanUncovered(decision.uncovered) : null };
}

// Returns the block and the paths actually in it. A document that does not fit is
// skipped (a later, smaller one may still fit), and the caller cuts `sources` to
// `paths`, so an answer never cites a document compose did not see (AD-29).
function documentsBlock(sources, wiki, tokenizer, budget) {
  const parts = [];
  const paths = new Set();
  let used = 0;
  for (const source of sources) {
    const resolved = wiki.resolve(source.path);
    if (!resolved) continue;
    const text = `[문서: ${source.path}]\n${readFileSync(resolved.real, "utf8")}`;
    const cost = tokenizer.count(text);
    if (used + cost > budget) continue;
    parts.push(text);
    paths.add(source.path);
    used += cost;
  }
  return { text: parts.join("\n\n"), paths };
}

export async function runAgent({ frame, binding, wiki, tokenizer, model, streamFn, emit, signal }) {
  if (!wiki || !wiki.indexReadable()) throw new RunFailure("wiki_unavailable");
  const limits = binding.limits;
  const window = limits.model_context_window - limits.max_output_tokens;
  const state = newRunState();
  const tools = createTools({ wiki, tokenizer, limits, state, emit });
  const decideTool = tools[3];
  const history = frame.messages.slice(0, -1);
  const question = frame.messages.at(-1).text;
  let overflow = false;
  let runaway = false;

  if (countContext(tokenizer, { systemPrompt: frame.system_instruction, tools,
    messages: frame.messages.map((message) => ({ role: message.role, content: message.text })) }) > window) {
    throw new RunFailure("context_overflow");
  }

  // Every research model call is re-counted before it is sent (AD-30), and the
  // number of calls is capped so a model that never calls decide cannot loop.
  //
  // `context` here is the library's already-normalized TranscriptContext (just
  // `{ messages }`, with the system prompt and tool declarations folded into a
  // leading system message -- see countMessages() above). We hand the *test
  // double* / real streamFn an augmented copy that also carries systemPrompt and
  // tools as plain top-level fields, replayed from that folded system message
  // via getCurrentSystemPrompt/getCurrentTools. Real providers (openai-completions
  // stream/streamSimple) only ever read `.messages`, so the extra fields are inert
  // in production; they exist so callers (and this Run's own compose step, and
  // tests) can introspect what was actually declared to the model for a given call.
  const guarded = (callModel, context, options = {}) => {
    if (countMessages(tokenizer, context) > window) { overflow = true; return refusedStream(callModel, "context_overflow"); }
    state.modelCalls += 1;
    if (state.modelCalls > limits.max_tool_calls + 2) { runaway = true; return refusedStream(callModel, "runaway"); }
    const observed = { ...context, systemPrompt: getCurrentSystemPrompt(context.messages), tools: getCurrentTools(context.messages) };
    return streamFn(callModel, observed, { ...options, maxTokens: limits.max_output_tokens });
  };

  const agent = new Agent({
    initialState: { systemPrompt: frame.system_instruction, model, thinkingLevel: "off", tools,
      messages: toAgentMessages(history, model) },
    streamFn: guarded,
    toolExecution: "sequential",
    prepareNextTurnWithContext: ({ context }) => {
      if (state.decision || !limitReached(state, limits)) return undefined;
      markLimit(state, emit);
      return { context: { ...context, tools: [decideTool] } };
    },
  });
  const onAbort = () => agent.abort();
  signal.addEventListener("abort", onAbort, { once: true });
  try {
    if (signal.aborted) throw new RunAborted();
    await agent.prompt(question);
  } finally {
    signal.removeEventListener("abort", onAbort);
  }
  if (signal.aborted) throw new RunAborted();
  if (overflow) throw new RunFailure("context_overflow");
  const last = agent.state.messages.at(-1);
  if (!runaway && last?.role === "assistant" && last.stopReason === "error") {
    const failure = failureFrom(last.errorMessage);
    throw new RunFailure(failure.cause, failure.http_status);
  }

  let { outcome, sources, uncovered } = runaway
    ? { outcome: "wiki_gap", sources: [], uncovered: null }
    : decisionOf(state, wiki);
  let finish = "stop";
  let documents = "";
  if (SOURCED.has(outcome)) {
    const block = documentsBlock(sources, wiki, tokenizer, limits.max_tool_output_tokens);
    documents = block.text;
    sources = sources.filter((source) => block.paths.has(source.path));
    if (sources.length === 0) { outcome = "wiki_gap"; uncovered = null; }
  }
  if (SOURCED.has(outcome)) {
    emitStep(state, emit, "compose", LABELS.compose, null);
    const system = outcome === "partial"
      ? `${COMPOSE_INSTRUCTION} End with one sentence stating that the Wiki does not cover: ${uncovered}`
      : COMPOSE_INSTRUCTION;
    const composeMessages = [
      ...toAgentMessages(history, model),
      { role: "user", content: `다음은 Wiki 문서예요.\n\n${documents}\n\n질문: ${question}`, timestamp: Date.now() },
    ];
    // normalizeContext() folds systemPrompt/tools into composeMessages' leading
    // system message (so a real provider call actually sees the compose
    // instruction); the extra top-level systemPrompt/tools fields below are the
    // same introspection convenience guarded() adds for research calls.
    const folded = normalizeContext({ systemPrompt: system, tools: [], messages: composeMessages });
    const context = { ...folded, systemPrompt: system, tools: [] };
    if (countMessages(tokenizer, context) > window) throw new RunFailure("context_overflow");
    const stream = await streamFn(model, context, { signal, maxTokens: limits.max_output_tokens });
    for await (const event of stream) {
      if (event.type === "text_delta" && event.delta) emit("delta", { text: event.delta });
      else if (event.type === "done") finish = event.reason === "length" ? "length" : "stop";
      else if (event.type === "error") {
        if (event.reason === "aborted" || signal.aborted) throw new RunAborted();
        const failure = failureFrom(event.error?.errorMessage);
        throw new RunFailure(failure.cause, failure.http_status);
      }
    }
    if (signal.aborted) throw new RunAborted();
  }
  return { outcome, uncovered, sources, search_truncated: state.limitHit, finish };
}
