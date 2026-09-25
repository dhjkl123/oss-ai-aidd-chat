// Minimal Markdown → block AST for model answers. No HTML is ever produced here:
// the page builds DOM from this AST with textContent only, so model output cannot inject markup.
//
// Fenced code follows CommonMark: an opening fence is 3+ backticks or tildes (indent ≤ 3);
// it closes only on a line of the SAME character, at least as long, with nothing after it.
// So ````md … ```bash … ``` … ```` keeps the inner fences as code. An unclosed fence is
// still returned (open: true) so a streaming answer shows a code block while it is typed.
// ponytail: headings, lists, paragraphs, inline code and **bold** only; add tables/links if answers need them.

// Classic script (no build step, CSP 'self'): loaded before app.js, it exposes one
// global, `Markdown`, and the same object to Node's require for tests/markdown.test.mjs.
const Markdown = (() => {
  const FENCE_OPEN = /^( {0,3})(`{3,}|~{3,})(.*)$/;
  const HEADING = /^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$/;
  const LIST = /^ {0,3}([-*+]|\d{1,9}[.)])[ \t]+(.*)$/;
  const fenceOf = line => { const f = FENCE_OPEN.exec(line); return f && !(f[2][0] === "`" && f[3].includes("`")) ? f : null; };
  const startsBlock = line => !line.trim() || fenceOf(line) || HEADING.test(line) || LIST.test(line);

  function parseBlocks(src) {
    const lines = src.replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = fenceOf(line);
      if (fence) {
        const [, indent, marks, info] = fence;
        const close = new RegExp(`^ {0,3}${marks[0] === "`" ? "`" : "~"}{${marks.length},}[ \\t]*$`);
        const body = [];
        let j = i + 1, closed = false;
        for (; j < lines.length; j++) {
          if (close.test(lines[j])) { closed = true; break; }
          body.push(lines[j].replace(new RegExp(`^ {0,${indent.length}}`), ""));
        }
        blocks.push({ type: "code", lang: info.trim().split(/\s+/)[0] || "", text: body.join("\n"), open: !closed });
        i = closed ? j + 1 : j;
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const h = HEADING.exec(line);
      if (h) { blocks.push({ type: "heading", level: h[1].length, inline: parseInline(h[2]) }); i++; continue; }
      const li = LIST.exec(line);
      if (li) {
        const ordered = /\d/.test(li[1]);
        const items = [];
        while (i < lines.length) {
          const m = LIST.exec(lines[i]);
          if (!m || /\d/.test(m[1]) !== ordered) break;
          items.push(parseInline(m[2]));
          i++;
        }
        blocks.push({ type: ordered ? "ol" : "ul", items });
        continue;
      }
      const para = [lines[i++].trim()];   // always consume at least one line
      while (i < lines.length && !startsBlock(lines[i])) para.push(lines[i++].trim());
      blocks.push({ type: "p", inline: parseInline(para.join(" ")) });
    }
    return blocks;
  }

  // Inline: code spans (a run of N backticks closes only on another run of exactly N) and **bold**.
  function parseInline(text) {
    const out = [];
    let buf = "", i = 0;
    const flush = () => { if (buf) { out.push(...parseBold(buf)); buf = ""; } };
    while (i < text.length) {
      if (text[i] === "`") {
        let n = 0; while (text[i + n] === "`") n++;
        let k = i + n, end = -1;
        while (k < text.length) {
          if (text[k] === "`") { let m = 0; while (text[k + m] === "`") m++; if (m === n) { end = k; break; } k += m; }
          else k++;
        }
        if (end !== -1) {
          flush();
          let code = text.slice(i + n, end);
          if (code.length > 2 && code.startsWith(" ") && code.endsWith(" ") && code.trim()) code = code.slice(1, -1);
          out.push({ type: "code", text: code });
          i = end + n;
          continue;
        }
        buf += text.slice(i, i + n); i += n; continue;
      }
      buf += text[i++];
    }
    flush();
    return out;
  }

  function parseBold(text) {
    const out = [];
    const re = /\*\*(?=\S)(.+?)(?<=\S)\*\*/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push({ type: "text", text: text.slice(last, m.index) });
      out.push({ type: "strong", text: m[1] });
      last = re.lastIndex;
    }
    if (last < text.length) out.push({ type: "text", text: text.slice(last) });
    return out;
  }

  return { parseBlocks, parseInline };
})();
if (typeof module === "object" && module.exports) module.exports = Markdown;
