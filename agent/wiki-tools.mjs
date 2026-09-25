// AD-28 read-only Wiki tools. Every path goes through WikiRoot.resolve: realpath,
// must stay under the real root, `.md` only, never an excluded folder. Files are
// read at call time; nothing in this module writes.
import { readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { basename, isAbsolute, join, relative, sep } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import { UNSAFE_TEXT } from "./protocol.mjs";

export const EXCLUDED = ["inbox/", "docs/", ".obsidian/", ".git/", ".ua/"];
// Lower-cased on both sides: an on-disk `Inbox/` is still the excluded `inbox/`.
const excluded = (rel) => EXCLUDED.some((dir) => rel.toLowerCase().startsWith(dir));
const CANONICAL = ["entities/", "concepts/", "comparisons/", "queries/"];
const MAX_HITS = 20;
const MAX_SNIPPET = 200;
const MAX_TITLE = 120;
const REFUSED = "읽을 수 없는 경로예요. Wiki 목록이나 검색 결과의 경로를 사용하세요.";
const LIMIT_TEXT = "검색 한도에 도달했어요. 지금까지 읽은 문서로 decide를 호출하세요.";
export const LABELS = {
  wiki_index: "Wiki 목록 확인", wiki_search: "Wiki 검색 중", wiki_read: "문서 읽는 중: ",
  decide: "근거 판단 중", compose: "답변 작성 중", limit_reached: "검색 한도 도달",
};

export class WikiRoot {
  constructor(root) {
    // .native: realpathSync keeps the caller's case on Windows; .native returns
    // the on-disk case so EXCLUDED / identity comparisons can't be bypassed by
    // requesting a differently-cased path (e.g. "INBOX/secret.md").
    this.root = realpathSync.native(root);
  }

  identity(real) {
    const rel = relative(this.root, real);
    if (!rel || rel.startsWith("..") || isAbsolute(rel)) return null;
    const id = rel.split(sep).join("/").normalize("NFC");
    if (!id.endsWith(".md") || excluded(id)) return null;
    return id;
  }

  resolve(path) {
    if (typeof path !== "string" || !path || path.includes("\0") || path.includes("\\") || isAbsolute(path)) return null;
    let real;
    try { real = realpathSync.native(join(this.root, path)); } catch { return null; }
    const id = this.identity(real);
    if (!id) return null;
    try { if (!statSync(real).isFile()) return null; } catch { return null; }
    return { real, id };
  }

  indexReadable() {
    try { readFileSync(join(this.root, "index.md")); return true; } catch { return false; }
  }

  *files(dir = this.root) {
    let entries;
    try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      const full = join(dir, entry.name);
      const rel = relative(this.root, full).split(sep).join("/");
      if (excluded(`${rel}/`)) continue;
      if (entry.isDirectory()) { yield* this.files(full); continue; }
      if (!entry.name.endsWith(".md")) continue;
      const resolved = this.resolve(rel);
      if (resolved) yield resolved;
    }
  }
}

export function parseFrontmatter(text) {
  const empty = { title: null, confidence: null, contested: false, body: text };
  const lines = text.split(/\r?\n/);
  if (lines[0] !== "---") return empty;
  const end = lines.indexOf("---", 1);
  if (end < 0) return empty;
  const fields = {};
  for (const line of lines.slice(1, end)) {
    const match = /^([A-Za-z_]+):\s*(.*)$/.exec(line);
    if (match) fields[match[1]] = match[2].trim().replace(/^(['"])(.*)\1$/, "$2");
  }
  return {
    title: fields.title || null,
    confidence: ["high", "medium", "low"].includes(fields.confidence) ? fields.confidence : null,
    contested: fields.contested === "true",
    body: lines.slice(end + 1).join("\n"),
  };
}

function validTitle(value) {
  return typeof value === "string" && value.length > 0 && value.trim() === value
    && value.length <= MAX_TITLE && !UNSAFE_TEXT.test(value);
}

export function safeTitle(title, id) {
  if (validTitle(title)) return title.normalize("NFC");
  const stem = basename(id, ".md").normalize("NFC");
  return validTitle(stem) ? stem : "문서";
}

export function newRunState() {
  return { stepIndex: 0, toolCalls: 0, toolOutputTokens: 0, limitHit: false, limitAnnounced: false,
    reads: new Map(), decision: null, modelCalls: 0 };
}

export function emitStep(state, emit, kind, label, docPath) {
  state.stepIndex += 1;
  emit("step", { step: { step_index: state.stepIndex, kind, label, doc_path: docPath } });
}

export function limitReached(state, limits) {
  return state.limitHit || state.toolCalls >= limits.max_tool_calls
    || state.toolOutputTokens >= limits.max_tool_output_tokens;
}

export function markLimit(state, emit) {
  state.limitHit = true;
  if (state.limitAnnounced) return;
  state.limitAnnounced = true;
  emitStep(state, emit, "limit_reached", LABELS.limit_reached, null);
}

function fitTokens(tokenizer, text, max) {
  if (max <= 0) return "";
  if (tokenizer.count(text) <= max) return text;
  let low = 0;
  let high = text.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    if (tokenizer.count(text.slice(0, mid)) <= max) low = mid; else high = mid - 1;
  }
  return text.slice(0, low);
}

const reply = (text) => ({ content: [{ type: "text", text }], details: {} });

export function createTools({ wiki, tokenizer, limits, state, emit }) {
  function admit() {
    if (limitReached(state, limits)) { markLimit(state, emit); return false; }
    state.toolCalls += 1;
    return true;
  }

  // Charges a tool result against the Run's budget, cutting it to what is left.
  function charge(text, cap) {
    const remaining = limits.max_tool_output_tokens - state.toolOutputTokens;
    const fitted = fitTokens(tokenizer, text, Math.min(cap, remaining));
    state.toolOutputTokens += tokenizer.count(fitted);
    const cutByRunBudget = fitted.length < text.length && remaining <= cap;
    if (cutByRunBudget || limitReached(state, limits)) markLimit(state, emit);
    return fitted;
  }

  const wikiIndex = {
    name: "wiki_index", label: "Wiki index", description: "Return the Wiki's index.md.",
    parameters: Type.Object({}),
    execute: async () => {
      if (!admit()) return reply(LIMIT_TEXT);
      emitStep(state, emit, "wiki_index", LABELS.wiki_index, null);
      const resolved = wiki.resolve("index.md");
      if (!resolved) return reply(REFUSED);
      return reply(charge(readFileSync(resolved.real, "utf8"), limits.max_read_tokens));
    },
  };

  const wikiSearch = {
    name: "wiki_search", label: "Wiki search",
    description: "Case-insensitive literal search. Returns up to 20 {path, line, snippet} hits, canonical folders first.",
    parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 200 }) }),
    execute: async (_id, { query }) => {
      if (!admit()) return reply(LIMIT_TEXT);
      emitStep(state, emit, "wiki_search", LABELS.wiki_search, null);
      const needle = String(query).toLocaleLowerCase();
      const canonical = [];
      const rest = [];
      for (const { real, id } of wiki.files()) {
        const lines = readFileSync(real, "utf8").split(/\r?\n/);
        // ponytail: one hit per file (first match), not one per matching line --
        // a document with hundreds of matching lines would otherwise crowd out
        // every other document from the top-20 result window. Upgrade to a
        // per-file hit cap > 1 if a real query needs more than the first hit.
        for (let index = 0; index < lines.length; index += 1) {
          const at = lines[index].toLocaleLowerCase().indexOf(needle);
          if (at < 0) continue;
          const start = Math.max(0, at - 80);
          const hit = { path: id, line: index + 1, snippet: lines[index].slice(start, start + MAX_SNIPPET) };
          (CANONICAL.some((dir) => id.startsWith(dir)) ? canonical : rest).push(hit);
          break;
        }
      }
      return reply(charge(JSON.stringify([...canonical, ...rest].slice(0, MAX_HITS)), limits.max_read_tokens));
    },
  };

  const wikiRead = {
    name: "wiki_read", label: "Wiki read",
    description: "Read one Wiki document by path. offset is a 0-based line number; limit is a line count.",
    parameters: Type.Object({
      path: Type.String({ minLength: 1, maxLength: 512 }),
      offset: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 2000 })),
    }),
    execute: async (_id, { path, offset = 0, limit }) => {
      if (!admit()) return reply(LIMIT_TEXT);
      const resolved = wiki.resolve(path);
      if (!resolved) return reply(REFUSED);
      const parsed = parseFrontmatter(readFileSync(resolved.real, "utf8"));
      const title = safeTitle(parsed.title, resolved.id);
      emitStep(state, emit, "wiki_read", LABELS.wiki_read + title, resolved.id);
      if (!state.reads.has(resolved.id)) {
        state.reads.set(resolved.id, { title, confidence: parsed.confidence, contested: parsed.contested });
      }
      const lines = parsed.body.split("\n");
      const window = lines.slice(offset, limit === undefined ? undefined : offset + limit).join("\n");
      const text = charge(window, limits.max_read_tokens);
      const consumed = text === "" ? 0 : text.split("\n").length;
      const nextOffset = text !== "" && offset + consumed < lines.length ? offset + consumed : null;
      return reply(JSON.stringify({ path: resolved.id, title, confidence: parsed.confidence,
        contested: parsed.contested, text, next_offset: nextOffset }));
    },
  };

  const decide = {
    name: "decide", label: "Decide", description: "Finish research. Call exactly once.",
    parameters: Type.Object({
      outcome: Type.Union(["grounded", "partial", "wiki_gap", "out_of_scope", "meta"].map((value) => Type.Literal(value))),
      used_paths: Type.Array(Type.String({ maxLength: 512 }), { maxItems: 20 }),
      uncovered: Type.Optional(Type.String({ maxLength: 300 })),
    }),
    execute: async (_id, params) => {
      if (state.decision) return reply("이미 결정했어요.");
      emitStep(state, emit, "decide", LABELS.decide, null);
      state.decision = params;
      return { ...reply("ok"), terminate: true };
    },
  };

  return [wikiIndex, wikiSearch, wikiRead, decide];
}
