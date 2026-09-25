// agent/test-helpers.mjs -- scripted pi streams for node:test. Never imported by the sidecar.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export const fakeModel = {
  id: "fake", name: "fake", api: "openai-completions", provider: "ollama", baseUrl: "http://127.0.0.1:9/v1",
  reasoning: true, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 32768, maxTokens: 2048, thinkingLevelMap: { off: "none" },
  compat: { maxTokensField: "max_tokens", supportsDeveloperRole: false },
};

export const usage = () => ({ input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } });

const base = (model) => ({ role: "assistant", content: [], api: model.api, provider: model.provider,
  model: model.id, usage: usage(), stopReason: "pending", timestamp: Date.now() });

export function scripted(turns) {
  const calls = [];
  const fn = (model, context, options = {}) => {
    calls.push({ context, options });
    const stream = createAssistantMessageEventStream();
    const turn = turns.shift();
    if (!turn) throw new Error("script exhausted");
    queueMicrotask(() => turn(stream, base(model), options));
    return stream;
  };
  return { fn, calls };
}

let callCounter = 0;
export const toolCallTurn = (name, args) => (s, msg) => {
  s.push({ type: "start", partial: msg });
  callCounter += 1;
  const call = { type: "toolCall", id: `call_${callCounter}`, name, arguments: args };
  msg.content.push(call);
  s.push({ type: "toolcall_start", contentIndex: 0, partial: msg });
  s.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: msg });
  msg.stopReason = "toolUse";
  s.push({ type: "done", reason: "toolUse", message: msg });
};

export const textTurn = (text, reason = "stop") => (s, msg) => {
  s.push({ type: "start", partial: msg });
  msg.content.push({ type: "text", text: "" });
  s.push({ type: "text_start", contentIndex: 0, partial: msg });
  msg.content[0].text = text;
  s.push({ type: "text_delta", contentIndex: 0, delta: text, partial: msg });
  s.push({ type: "text_end", contentIndex: 0, content: text, partial: msg });
  msg.stopReason = reason;
  s.push({ type: "done", reason, message: msg });
};

export const errorTurn = (message, reason = "error") => (s, msg) => {
  s.push({ type: "start", partial: msg });
  msg.stopReason = reason;
  msg.errorMessage = message;
  s.push({ type: "error", reason, error: msg });
};

export const hangTurn = () => (s, msg, options) => {
  s.push({ type: "start", partial: msg });
  options.signal?.addEventListener("abort", () => {
    msg.stopReason = "aborted";
    msg.errorMessage = "Request aborted";
    s.push({ type: "error", reason: "aborted", error: msg });
  }, { once: true });
};
