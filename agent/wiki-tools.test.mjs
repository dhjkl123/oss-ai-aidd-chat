import { test } from "node:test";
import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { loadTokenizer } from "./tokenizer.mjs";
import { WikiRoot, createTools, newRunState, parseFrontmatter, safeTitle } from "./wiki-tools.mjs";

const FIXTURE = fileURLToPath(new URL("./test-fixtures/wiki/", import.meta.url));
const tokenizer = loadTokenizer(fileURLToPath(new URL("../tests/fixtures/tiny-tokenizer.json", import.meta.url)));
const LIMITS = { max_tool_calls: 8, max_read_tokens: 3000, max_tool_output_tokens: 12000 };

function setup(limits = LIMITS, root = FIXTURE) {
  const frames = [];
  const state = newRunState();
  const wiki = new WikiRoot(root);
  const [wikiIndex, wikiSearch, wikiRead, decide] = createTools({
    wiki, tokenizer, limits, state, emit: (type, fields) => frames.push({ type, ...fields }) });
  return { frames, state, wiki, wikiIndex, wikiSearch, wikiRead, decide };
}
const text = (result) => result.content[0].text;

test("index returns index.md and emits a step", async () => {
  const { wikiIndex, frames } = setup();
  assert.match(text(await wikiIndex.execute("c1", {})), /concepts\/alpha/);
  assert.deepEqual(frames[0].step, { step_index: 1, kind: "wiki_index", label: "Wiki 목록 확인", doc_path: null });
});

test("search is literal, case-insensitive, canonical first, excludes private folders", async () => {
  const { wikiSearch, frames } = setup();
  const hits = JSON.parse(text(await wikiSearch.execute("c1", { query: "ALPHAWORD" })));
  const paths = hits.map((hit) => hit.path);
  assert.ok(paths.length > 0 && paths.length <= 20);
  assert.ok(paths.includes("concepts/alpha.md"));
  for (const path of paths) assert.ok(!/^(inbox|docs|\.obsidian)\//.test(path) && path.endsWith(".md"));
  const firstRaw = paths.findIndex((path) => path.startsWith("raw/"));
  if (firstRaw >= 0) assert.ok(paths.slice(firstRaw).every((path) => path.startsWith("raw/")));
  for (const hit of hits) assert.ok(hit.snippet.length <= 200 && Number.isInteger(hit.line));
  assert.equal(frames[0].step.label, "Wiki 검색 중");
  assert.equal(JSON.stringify(frames).includes("ALPHAWORD"), false);
});

test("read returns frontmatter, records a source, emits the title step", async () => {
  const { wikiRead, state, frames } = setup();
  const result = JSON.parse(text(await wikiRead.execute("c1", { path: "concepts/alpha.md" })));
  assert.equal(result.title, "Alpha 개념");
  assert.equal(result.confidence, "low");
  assert.equal(result.contested, true);
  assert.deepEqual([...state.reads.keys()], ["concepts/alpha.md"]);
  assert.equal(frames[0].step.label, "문서 읽는 중: Alpha 개념");
  assert.equal(frames[0].step.doc_path, "concepts/alpha.md");
});

test("read windows long documents by offset and token budget", async () => {
  const { wikiRead } = setup({ ...LIMITS, max_read_tokens: 200 });
  const first = JSON.parse(text(await wikiRead.execute("c1", { path: "comparisons/beta.md" })));
  assert.ok(tokenizer.count(first.text) <= 200);
  assert.ok(first.next_offset > 0);
  const later = JSON.parse(text(await wikiRead.execute("c2", { path: "comparisons/beta.md", offset: first.next_offset })));
  assert.notEqual(later.text, first.text);
  const beyond = JSON.parse(text(await wikiRead.execute("c3", { path: "comparisons/beta.md", offset: 100000 })));
  assert.equal(beyond.text, "");
});

for (const path of ["../outside.md", "inbox/secret.md", "docs/guide.md", ".obsidian/app.md",
  "notes.txt", "/etc/passwd", "concepts/missing.md", "concepts\\alpha.md",
  "INBOX/secret.md", "DOCS/guide.md", ".Obsidian/app.md"]) {
  test(`read refuses ${path}`, async () => {
    const { wikiRead, state } = setup();
    assert.match(text(await wikiRead.execute("c1", { path })), /읽을 수 없는 경로/);
    assert.equal(state.reads.size, 0);
  });
}

test("symlink escaping root is refused", async (t) => {
  const root = mkdtempSync(join(tmpdir(), "wiki-"));
  cpSync(FIXTURE, root, { recursive: true });
  const outside = mkdtempSync(join(tmpdir(), "outside-"));
  writeFileSync(join(outside, "leak.md"), "leakword");
  try { symlinkSync(join(outside, "leak.md"), join(root, "concepts", "leak.md")); }
  catch { t.skip("OS refused to create a symlink"); return; }
  const { wikiRead, wikiSearch } = setup(LIMITS, root);
  assert.match(text(await wikiRead.execute("c1", { path: "concepts/leak.md" })), /읽을 수 없는 경로/);
  assert.equal(JSON.parse(text(await wikiSearch.execute("c2", { query: "leakword" }))).length, 0);
});

test("identity is posix on win32", () => {
  const wiki = new WikiRoot(FIXTURE);
  assert.equal(wiki.resolve("comparisons/beta.md").id, "comparisons/beta.md");
  assert.equal(wiki.resolve("comparisons/../concepts/alpha.md").id, "concepts/alpha.md");
});

test("resolve normalizes on-disk case so differently-cased requests share one id", () => {
  const wiki = new WikiRoot(FIXTURE);
  assert.equal(wiki.resolve("Concepts/Alpha.md").id, "concepts/alpha.md");
});

test("hostile title falls back to stem", () => {
  assert.equal(safeTitle("a\u202Eb<script>", "queries/hostile.md"), "hostile");
  assert.equal(safeTitle("가".repeat(121), "queries/long.md"), "long");
  assert.equal(safeTitle("<b>ok</b>", "x/y.md"), "<b>ok</b>");
  assert.equal(safeTitle(null, "x/y.md"), "y");
});

test("frontmatter parsing", () => {
  assert.deepEqual(parseFrontmatter("---\ntitle: 'Q 문서'\nconfidence: medium\ncontested: false\n---\nbody"),
    { title: "Q 문서", confidence: "medium", contested: false, body: "body" });
  assert.deepEqual(parseFrontmatter("no frontmatter"),
    { title: null, confidence: null, contested: false, body: "no frontmatter" });
});

test("tool call cap marks the limit once and refuses further tools", async () => {
  const { wikiSearch, state, frames } = setup({ ...LIMITS, max_tool_calls: 2 });
  await wikiSearch.execute("c1", { query: "alphaword" });
  await wikiSearch.execute("c2", { query: "alphaword" });
  assert.match(text(await wikiSearch.execute("c3", { query: "alphaword" })), /검색 한도/);
  assert.equal(state.limitHit, true);
  assert.equal(frames.filter((frame) => frame.step?.kind === "limit_reached").length, 1);
});

test("tool output budget truncates and marks the limit", async () => {
  const { wikiRead, state } = setup({ ...LIMITS, max_tool_output_tokens: 150 });
  const result = JSON.parse(text(await wikiRead.execute("c1", { path: "comparisons/beta.md" })));
  assert.ok(tokenizer.count(result.text) <= 150);
  assert.equal(state.limitHit, true);
});

test("decide records once and terminates", async () => {
  const { decide, state, frames } = setup();
  const result = await decide.execute("c1", { outcome: "grounded", used_paths: ["concepts/alpha.md"] });
  assert.equal(result.terminate, true);
  assert.deepEqual(state.decision, { outcome: "grounded", used_paths: ["concepts/alpha.md"] });
  assert.equal(frames[0].step.label, "근거 판단 중");
});

test("module source contains no write API", () => {
  const source = readFileSync(fileURLToPath(new URL("./wiki-tools.mjs", import.meta.url)), "utf8");
  assert.equal(/writeFile|appendFile|rename|unlink|rmSync|mkdir|createWriteStream/.test(source), false);
});

test("an excluded folder is excluded whatever its on-disk case", async () => {
  const root = mkdtempSync(join(tmpdir(), "wiki-case-"));
  writeFileSync(join(root, "index.md"), "# index\n");
  mkdirSync(join(root, "Inbox"));
  writeFileSync(join(root, "Inbox", "secret.md"), "casewordz\n");
  mkdirSync(join(root, "concepts"));
  writeFileSync(join(root, "concepts", "open.md"), "casewordz\n");
  const { wiki, wikiSearch } = setup(LIMITS, root);
  assert.equal(wiki.resolve("Inbox/secret.md"), null);
  assert.equal(wiki.resolve("inbox/secret.md"), null);
  assert.deepEqual([...wiki.files()].map((file) => file.id).sort(), ["concepts/open.md", "index.md"]);
  const paths = JSON.parse(text(await wikiSearch.execute("c1", { query: "casewordz" }))).map((hit) => hit.path);
  assert.deepEqual(paths, ["concepts/open.md"]);
});
