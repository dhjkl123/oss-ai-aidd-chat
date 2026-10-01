# PRD Quality Review — 한국어 LLM-Wiki Q&A 에이전트 (2026-09-23 update)

- PRD: `_bmad-output/planning-artifacts/prds/prd-workspace-2026-08-21/prd.md`
- Addendum: `addendum.md` (same folder)
- Rubric: `.claude/skills/bmad-prd/assets/prd-validation-checklist.md`
- Stakes calibration: personal/hobby, single user (Wiki owner), local-only. Rigor bar is light; substance and internal consistency still apply because this PRD feeds Architecture and stories.

## Overall verdict

`prd.md` works well for a hobby update. It has a clear thesis (answer only from the Wiki, read-only, cite what was read). The new FR-8/9/10/11 have testable consequences, and the prd.md body has no leftover public-demo or general-knowledge-fallback wording. The main risk is in `addendum.md`, which was only appended to. Its older sections still describe a disabled Retrieval adapter, a "지식 미연결" notice, Citation/Evidence as excluded scope and a public HF deployment. All of these now contradict the PRD. Architecture reads both documents, so it would get conflicting instructions. Two smaller product gaps remain: what counts as "no evidence" (partial coverage, chit-chat, follow-ups), and whether the inherited 8,192-token context limit can hold multi-document Wiki reads.

## Decision-readiness — adequate

The decisions are stated plainly in §0. The team replaces the single Model call with an agent and uses pi agent instead of building its own loop. FR-8 is redefined, and no new design system is introduced. The biggest trade-off is Node-only pi versus the existing Python backend. The risk table (§7) names it and routes it to Architecture, backed by NFR-10 and NFR-12. The addendum gives three integration options and says which one is not recommended. That is enough for a hobby PRD. OQ-1 (filing answers into `queries/`) is a real open item, not a rhetorical one.

The PRD does not surface one tension. The "Wiki-only" principle (§3 principle 3) is strict, and the §0 bullets do not say what the user gives up with it: greetings, meta questions and questions only partly covered by the Wiki all get refused. The owner is presumably fine with this, but it should be stated as an explicit choice.

### Findings
- **medium** Wiki-only refusal cost not stated as a trade-off (§0, §3 principle 3, FR-8) — The PRD says what it gains (no fabricated grounding) but not what it gives up: no answers to chit-chat or meta questions such as "뭘 할 수 있어?", and no partial answers. *Fix:* add one line to §0 naming the accepted cost, or carve out meta/greeting handling explicitly.

## Substance over theater — strong

There is one persona (the Wiki owner), and it drives real decisions: read-only access, `contested`/`confidence` display, and the UJ-2 insight that "this is a Wiki gap". The NFRs are product-specific: path traversal via `..` and symlinks (NFR-11), no tool args or Wiki bodies in logs (NFR-5), and `git status`-based read-only proof (SM-9). The counter-metrics (§5.2) are relevant: more sources or more steps is not better. Nothing reads like furniture.

### Findings
- (none)

## Strategic coherence — strong

The thesis is to turn the curated Wiki into a conversation, with honest grounding. The features follow from it. FR-9 is search/read, FR-10 is sources, FR-8 is honest misses, FR-5 is progress (needed because agents are multi-step) and FR-11 is design consistency. The SMs test the thesis rather than activity: SM-7 grounding, SM-8 honest miss, SM-9 read-only. SM-10 is design conformance and is tangential to the thesis but was explicitly requested.

One gap: SM-7 checks that the answer does not *contradict* the source. Principle 3 promises something stronger, that the answer is built *only* from Wiki content. A small local model (the addendum names `qwen3.5:9b`) will tend to pad answers with general knowledge that is not contradictory. SM-7 would pass that, and it violates the core thesis.

### Findings
- **medium** SM-7 checks non-contradiction, not Wiki-only content (§5.1 SM-7 vs §3 principle 3, FR-8 C-8.2) — An answer can cite the right document and still add unsupported general-knowledge claims. *Fix:* add to SM-7's human review "답변의 주요 주장이 근거 문서에서 확인된다(근거 없는 추가 주장 0건)" or similar.

## Done-ness clarity — adequate

Most consequences are verifiable. Good examples are C-9.2 (explicit excluded dirs), C-10.2, C-10.3 (concrete frontmatter keys), C-11.1 (no color literals outside `:root`, which is lintable) and C-5.5. The weak spots are the judgment calls at the core of FR-8, plus a few adjectives.

- FR-8 gives no rule for when evidence is "관련 근거를 찾지 못하면". There are three cases. (a) The Wiki covers part of the question: answer that part, or refuse? (b) A follow-up can be answered from Wiki content read in an earlier turn (C-4.3 keeps it in context): does that count as evidence? Does it appear in FR-10 sources? (c) C-3.4 lets the agent answer with "그때까지 읽은 근거" when the step limit hits, and nothing says how that answer is marked as possibly incomplete.
- NFR-8 says "과도하게 읽히지 않게", which is an adjective, not a bound.
- C-9.1 (search order: Canonical before `raw/`) has no stated verification. It is fine as guidance, but it cannot be tested as written.

### Findings
- **medium** Partial-evidence and prior-turn-evidence behavior undefined (FR-8, C-3.4, C-4.3, FR-10) — The load-bearing refusal rule covers only "nothing found". SM-8 fixtures test only fully missing topics. *Fix:* add C-8.4 for partial coverage (answer only the covered part and state what is uncovered, or refuse), and state whether documents read in earlier turns may be cited in later answers and shown in the source list.
- **low** Step-limit answer not marked (C-3.4) — An answer produced because the step limit was hit looks identical to a normal answer. *Fix:* require a short notice when the limit ended the search.
- **low** NFR-8 "과도하게 읽히지 않게" is unbounded — *Fix:* e.g. announce only step changes, at most one per N seconds, and not per token.
- **low** C-9.1 search order not verifiable — *Fix:* accept it as agent guidance, or have the fixture assert that `index.md` is read before any `raw/` file.

## Scope honesty — adequate

In/Out Scope (§1.1) is explicit, including useful exclusions: Wiki writes/filing, other tools, sub-agents, vector index. Step-limit values, integration method and Runtime choice are explicitly deferred to Architecture. The PRD has no `[ASSUMPTION]` tags even though some values are inferred. The relaxed NFR-1 targets (p50 15s, p95 45s) for a multi-step agent on a local 9B model are the clearest case. Open-items density is low, which fits hobby stakes.

The larger scope-honesty problem is in the addendum. It still asserts excluded scope that this update brought back in (see Downstream usability).

### Findings
- **low** Inferred thresholds untagged (§6.1 NFR-1) — The 15s/45s targets and the 120s timeout (NFR-2) are unverified for multi-step runs on the local Ollama model. *Fix:* tag `[ASSUMPTION]` or add a note that Architecture confirms them after a spike.

## Downstream usability — thin

This PRD feeds Architecture and stories, and its companion addendum contradicts it in several places. An architect who source-extracts both gets two answers.

- The addendum's "현재 Retrieval 경계" section (lines 38–45) says the Retrieval adapter is `disabled`/`no-op` and is not called on the normal path. It also says "UI는 지식 미연결 경계를 고지" and "Retrieval은 현재 Readiness 조건이 아니다". All three are reversed by FR-9, FR-8 and C-9.5. The last line, "Retrieval 도입에는 별도 PRD 변경…이 필요하다", describes exactly the change this PRD makes, but the section was never updated.
- "제외된 기술 범위" (lines 96–105) lists "Citation, Evidence State, Claim Support와 Evidence Guard" as excluded. FR-10 (근거 표시) and C-10.3 (contested/confidence) now are that Citation/Evidence feature.
- Public-demo leftovers: "결정 상태" still lists "Public Demo의 Provider 비용·Rate Limit·Abuse Protection" (line 14). "Session과 데이터" says "공개 Session TTL" (line 62). "실행·배포 방향" plans "Hugging Face Docker Space 공개 배포" (line 92). The PRD says no public demo (§0, §1.1, C-7.3).
- The addendum's title and purpose still say "Working Skeleton" and the 2026-08-23 scope.
- The observability list (line 65) omits Step 수 and 도구 이름, which NFR-9 now allows. `/ready` (line 93) checks only the Model Provider, so there is no Wiki path readiness to pair with C-9.5.
- The module seed says "disabled future retrieval adapter" (line 79).
- The addendum keeps the 8,192-token input limit (line 17) and C-4.3 adds Wiki document content to that budget. Canonical documents are Korean prose and several are read per run. That budget is likely tight, and neither document acknowledges the tension.

Glossary (§2.2) is present and mostly used consistently. Two terms are defined only by pointing to "이전 정의를 유지한다" in a document the reader does not have: Conversation/Session/Message, and UJ-3's reference to "이전 UJ-2·UJ-3".

### Findings
- **high** Addendum Retrieval section contradicts FR-8/FR-9 (addendum §"현재 Retrieval 경계", lines 38–45) — It still says disabled/no-op retrieval, a "지식 미연결" UI notice and that retrieval is not a readiness condition. Architecture will get conflicting instructions. *Fix:* rewrite or delete this section. State that Wiki tools are active, that the Wiki path is part of readiness (C-9.5), and that FR-8 replaces the "미연결 고지".
- **medium** Addendum excludes Citation/Evidence that FR-10 now requires (addendum §"제외된 기술 범위", line 101) — *Fix:* remove "Citation, Evidence State" from the exclusions, or narrow the wording to "Claim-level support scoring / Evidence Guard".
- **medium** Public-demo leftovers in the addendum (addendum lines 14, 62, 92) — Public Demo cost/abuse, "공개 Session TTL" and the HF Space deployment plan conflict with C-7.3. *Fix:* delete them, or mark them as out of scope for this release.
- **medium** 8,192-token context limit vs multi-document Wiki reads (addendum line 17, C-4.3, C-3.4) — Reading even two or three Canonical documents plus history may exceed the limit. C-4.2 would then drop earlier turns quickly, or the Run would fail. *Fix:* add an `[ASSUMPTION]` or `[NOTE FOR PM]` asking Architecture to size the context and per-document read budget for the agent path.
- **low** Addendum observability and `/ready` not updated (addendum lines 65, 93) — *Fix:* add Step count and tool name to match NFR-9, and add a Wiki-root readability check to `/ready`.
- **low** Addendum title and module seed are stale ("Working Skeleton", "disabled future retrieval adapter") — *Fix:* retitle and update the seed comment.
- **low** Glossary and UJ-3 point to an absent prior document (§2.2 "이전 정의를 유지한다"; UJ-3 "이전 UJ-2·UJ-3") — The prior UJ-2 ID now collides with the current UJ-2 (Wiki miss). *Fix:* inline the one-line definitions and restate UJ-3's two flows directly without the old IDs.

## Shape fit — strong

This is a hobby, single-operator, brownfield update. The PRD is appropriately light: one persona, three short UJs, a capability-spec shape. It correctly separates new UJs from inherited ones, and it references existing code accurately in the addendum. `docs/DESIGN.md`, `docs/EXPERIENCE.md`, `docs/sse-contract.md` and `src/aidd_chat/adapters/direct.py` all exist, and `../my-llm-wiki/.git` exists, so SM-9's `git status` check is viable. It is not over-formalized.

### Findings
- (none)

## Mechanical notes

- **FR order:** §4.3 lists FR-9, FR-10, then FR-8; §4.4 lists FR-5 and FR-6; §4.5 lists FR-7 and FR-11. The IDs are unique and contiguous (1–11) but are presented out of order. This is acceptable because keeping FR-8's ID preserves traceability. Optionally, move FR-8 ahead of FR-9 within §4.3 so that at least the section reads in numeric order.
- **NFR order:** NFR-11 sits in §6.2 between NFR-6 and NFR-7, and NFR-12 comes after NFR-10. IDs are unique and contiguous (1–12).
- **Consequence IDs:** every consequence bullet has a `C-N.n` ID and the IDs match their parent FR. Checked: C-1.1–1.2, C-2.1–2.3, C-3.1–3.4, C-4.1–4.3, C-9.1–9.5, C-10.1–10.4, C-8.1–8.3, C-5.1–5.5, C-6.1–6.3, C-7.1–7.3, C-11.1–11.4. No gaps.
- **SM IDs:** SM-1–4 and SM-7–10. SM-5 and SM-6 appear only in the Appendix A changelog ("SM-5·SM-6 삭제"), with no dangling references in the body or risk table. The gap is intentional and documented.
- **Removed FR-8 meaning:** the §0 bullet calls the old wording "지식 미연결 고지" and the FR-8 heading calls it "지식 경계 고지". It is the same concept under two names. Pick one.
- **General-knowledge fallback:** prd.md has no leftovers. C-9.5, C-8.2 and C-6.3 all forbid fallback. The only leftover is in the addendum (Retrieval section, above).
- **Public demo:** prd.md is consistent (§0, §1.1, C-7.3). The leftovers are all in the addendum (lines 14, 62, 92).
- **Appendix A changelog incomplete:** it omits FR-4 (C-4.3 was added) and NFR-7 and NFR-8, which were modified to add the source list, contested and Live Region.
- **Assumptions Index:** there are no `[ASSUMPTION]` tags and no index. This is acceptable for hobby stakes, but see the NFR-1 and context-budget findings.
- **Cross-refs:** the §7 risk table references (C-10.2, FR-8, SM-8, NFR-10, NFR-12, C-9.3, NFR-11, SM-9, FR-5, C-3.4, C-11.3, SM-10) all resolve.

## Severity counts

- Critical: 0
- High: 1
- Medium: 6
- Low: 7
