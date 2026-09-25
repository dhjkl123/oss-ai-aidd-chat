import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { Tokenizer } from "@huggingface/tokenizers";

export function loadTokenizer(path) {
  const raw = readFileSync(path);
  const tokenizer = new Tokenizer(JSON.parse(raw.toString("utf8")), {});
  return {
    sha256: createHash("sha256").update(raw).digest("hex"),
    count: (text) => tokenizer.encode(text, { add_special_tokens: false }).ids.length,
  };
}
