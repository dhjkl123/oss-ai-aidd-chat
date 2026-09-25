import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { loadTokenizer } from "./tokenizer.mjs";

const fixture = (name) => fileURLToPath(new URL(`../tests/fixtures/${name}`, import.meta.url));

test("node counts match the python vectors", () => {
  const tokenizer = loadTokenizer(fixture("tiny-tokenizer.json"));
  for (const { text, count } of JSON.parse(readFileSync(fixture("token_vectors.json"), "utf8"))) {
    assert.equal(tokenizer.count(text), count, JSON.stringify(text));
  }
  assert.match(tokenizer.sha256, /^[0-9a-f]{64}$/);
});
