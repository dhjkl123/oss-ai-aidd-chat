import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { normalizeContext } from "@earendil-works/pi-ai";
import { buildModel, failureFrom, productionStreamFn } from "./model.mjs";

test("outbound body: max_tokens, reasoning_effort none, system role, no retries", async () => {
  const bodies = [];
  const auth = [];
  const server = createServer((request, response) => {
    auth.push(request.headers.authorization);
    let raw = "";
    request.on("data", (chunk) => { raw += chunk; });
    request.on("end", () => {
      bodies.push(JSON.parse(raw));
      response.writeHead(503, { "content-type": "application/json" });
      response.end('{"error":"busy"}');
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const model = buildModel({ endpoint_origin: origin, model_revision: "qwen3.5:9b",
    model_context_window: 32768, max_output_tokens: 2048 });
  const stream = productionStreamFn("key")(model, normalizeContext({ systemPrompt: "sys", tools: [],
    messages: [{ role: "user", content: "q", timestamp: Date.now() }] }), { maxTokens: 2048 });
  const final = await stream.result();
  server.close();
  assert.equal(bodies.length, 1);
  assert.deepEqual(auth, ["Bearer key"]);  // the header sidecar.mjs's /v1/models probe copies
  assert.equal(bodies[0].max_tokens, 2048);
  assert.equal(bodies[0].reasoning_effort, "none");
  assert.deepEqual(bodies[0].messages.map((message) => message.role), ["system", "user"]);
  assert.equal(final.stopReason, "error");
  assert.deepEqual(failureFrom(final.errorMessage), { cause: "provider_http", http_status: 503 });
});

test("failure mapping from error text", () => {
  assert.deepEqual(failureFrom("401: unauthorized"), { cause: "provider_http", http_status: 401 });
  assert.deepEqual(failureFrom("Request timed out"), { cause: "provider_timeout", http_status: null });
  assert.deepEqual(failureFrom("fetch failed: ECONNREFUSED"), { cause: "provider_transport", http_status: null });
  assert.deepEqual(failureFrom("weird"), { cause: "internal", http_status: null });
});
