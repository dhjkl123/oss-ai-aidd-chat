import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

// markdown.js is the classic script the page loads; require() reads the same file.
const { parseBlocks, parseInline } = createRequire(import.meta.url)("../src/aidd_chat/web/markdown.js");

test("a longer outer fence keeps inner fences as code", () => {
  const src = "예시:\n\n````markdown\n# 제목\n\n```bash\necho hi\n```\n\n끝\n````\n\n다음 문단";
  const b = parseBlocks(src);
  assert.deepEqual(b.map(x => x.type), ["p", "code", "p"]);
  assert.equal(b[1].lang, "markdown");
  assert.equal(b[1].text, "# 제목\n\n```bash\necho hi\n```\n\n끝");
  assert.equal(b[1].open, false);
});

test("tilde fences are not closed by backticks and vice versa", () => {
  const b = parseBlocks("~~~\n```\nx\n```\n~~~");
  assert.equal(b.length, 1);
  assert.equal(b[0].text, "```\nx\n```");
});

test("closing fence must be at least as long and have nothing after it", () => {
  const b = parseBlocks("````\n``` not a close\n```\nstill code\n`````\nafter");
  assert.equal(b[0].text, "``` not a close\n```\nstill code");
  assert.equal(b[1].type, "p");
});

test("unclosed fence while streaming is an open code block", () => {
  const b = parseBlocks("설명\n\n```python\nprint(1)\nprint(2");
  assert.deepEqual(b.at(-1), { type: "code", lang: "python", text: "print(1)\nprint(2", open: true });
});

test("fence indentation is stripped up to the opening indent", () => {
  assert.equal(parseBlocks("  ```\n  a\n    b\n  ```")[0].text, "a\n  b");
});

test("backtick fence info string may not contain a backtick", () => {
  assert.equal(parseBlocks("```a`b\ncode")[0].type, "p");
});

test("inline code spans match equal backtick runs", () => {
  assert.deepEqual(parseInline("use `` a`b `` here"), [
    { type: "text", text: "use " }, { type: "code", text: "a`b" }, { type: "text", text: " here" }]);
  assert.deepEqual(parseInline("lone ` tick"), [{ type: "text", text: "lone ` tick" }]);
});

test("markup-looking text stays plain text nodes", () => {
  const b = parseBlocks("<script>alert(1)</script> **굵게**");
  assert.deepEqual(b[0].inline, [{ type: "text", text: "<script>alert(1)</script> " }, { type: "strong", text: "굵게" }]);
});

test("lists and headings", () => {
  const b = parseBlocks("## 정리\n- 하나\n- 둘\n1. 첫째\n2. 둘째");
  assert.deepEqual(b.map(x => x.type), ["heading", "ul", "ol"]);
  assert.equal(b[1].items.length, 2);
});

test("empty and CRLF input", () => {
  assert.deepEqual(parseBlocks(""), []);   // a run with no delta yet renders nothing
  assert.deepEqual(parseBlocks("a\r\nb").map(x => x.inline), [[{ type: "text", text: "a b" }]]);
});
