import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";

export function buildModel(binding) {
  return {
    id: binding.model_revision, name: binding.model_revision, api: "openai-completions", provider: "ollama",
    baseUrl: `${binding.endpoint_origin}/v1`, reasoning: true, input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: binding.model_context_window, maxTokens: binding.max_output_tokens,
    // AD-27: thinking stays off on every call; `off` sends reasoning_effort "none".
    thinkingLevelMap: { off: "none" },
    compat: { maxTokensField: "max_tokens", supportsDeveloperRole: false },
  };
}

// pi-ai requires some key even for Ollama; the health probe sends the same one.
export const providerKey = (apiKey) => apiKey || "ollama";

export function productionStreamFn(apiKey) {
  // Retries stay at zero (AD-8).
  return (model, context, options = {}) =>
    streamSimple(model, context, { ...options, apiKey: providerKey(apiKey), maxRetries: 0 });
}

export function failureFrom(errorMessage) {
  const message = String(errorMessage ?? "");
  const status = /^(\d{3})[\s:]/.exec(message);
  if (status) return { cause: "provider_http", http_status: Number(status[1]) };
  if (/timed?\s*out|timeout/i.test(message)) return { cause: "provider_timeout", http_status: null };
  if (/fetch failed|ECONN|ENOTFOUND|EAI_AGAIN|socket|network/i.test(message)) {
    return { cause: "provider_transport", http_status: null };
  }
  return { cause: "internal", http_status: null };
}
