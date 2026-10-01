# Plan Reconciliation — Korean Q&A Chat Working Skeleton

## Reconciliation basis

- **Source reviewed in full:** `plan.md` (744 lines)
- **Compared with:** `prd.md` and `addendum.md`, both updated 2026-08-23
- **Latest authority:** the user's explicit override: build a simple ChatGPT-like Korean Q&A working skeleton before Knowledge Graph, Vector, or RAG integration; retain future retrieval seams; do not include Tailoring Canvas, Pilot Plan, Human Review, approval, or Artifact/Case Export in the target product.
- **Purpose:** identify only material gaps, stale-scope leakage, and qualitative source ideas that are intentionally retained or dropped. This report does not restore superseded requirements.

## Overall verdict

The updated PRD and addendum are substantially aligned with the latest override. They correctly replace the old tailoring workflow with an end-to-end chat loop, make retrieval inactive, remove the explicitly rejected features from both current and target scope, and preserve only narrow provider/retrieval extension boundaries.

Four issues remain. Two are wording or source-governance leaks that could recreate old scope, one is a missing minimum product-quality outcome, and one is an unresolved security behavior introduced by the addendum.

## Material findings

### GAP-CS1 — `plan.md` remains a contradictory requirements source

**Source evidence**

The plan still marks the product direction as confirmed as an “Enterprise AIDD Tailoring Evidence & Enablement Factory” and defines the education MVP around Context Card intake, GraphRAG, Tailoring Canvas, Pilot generation, Human Review, and Case Draft export (`plan.md` lines 62–103, 111–184). It later repeats those dependencies in the conceptual architecture and 14-week plan (lines 569–595, 636–657). It also says implementation should not begin before the old tailoring and platform constraints are settled (lines 739–744).

**Current treatment**

The PRD explicitly states that the 2026-08-23 decision supersedes this scope (`prd.md` lines 12–18), and its Non-Goals correctly remove the rejected capabilities (lines 165–185). However, `plan.md` itself has no supersession marker and still describes the stale scope as “확정.” A later architecture or epic pass that reads both files could treat the plan as a second authoritative requirements source.

**Why material**

This is the highest stale-scope leakage risk. It can reintroduce Canvas, Pilot, review state, exports, LangGraph, Neo4j, RAGAS, or AM/ITO workflows even though the PRD deliberately removed them.

**Reconciliation needed**

Treat `prd.md` plus the latest override as authoritative. Mark `plan.md` as historical/superseded for product requirements, or add a prominent scope note pointing to the new PRD. Do not feed the old plan directly into CE, architecture, UX, or implementation requirement extraction without this filter.

### GAP-CS2 — “공개·합성 질문만” is stale demo language and is not an actionable free-form chat boundary

**Source evidence**

The old plan limited the demo to public or synthetic AM/ITO scenarios because the product previously generated tailoring artifacts from structured enterprise context (`plan.md` lines 105–123, 161–171). It separately raised external model data-transfer constraints as an unresolved question (lines 723–729).

**Current treatment**

The new PRD correctly offers anonymous free-form Korean questions, but says the public demo “uses only public/synthetic questions” (`prd.md` lines 147–155, 189–200). The addendum repeats a public/synthetic data policy (`addendum.md` lines 66–72). A public chatbot cannot know that arbitrary user text is synthetic merely from this statement, and the phrase inherits the prior AM/ITO demo framing.

**Why material**

It is unclear whether this is a user notice, an enforced input policy, a curated demo-script rule, or an operator-only restriction. Each interpretation creates different UI, API, logging, and failure requirements.

**Reconciliation needed**

Replace the old scenario phrase with a precise boundary. For the smallest skeleton, state that users must not submit confidential or personal data, show that notice before use, and do not claim enforcement beyond the controls actually implemented. If blocking or redaction is required, define it as a separate product behavior with visible failure semantics.

### GAP-CS3 — Completion metrics verify transport integrity but not a minimally useful Korean answer

**Source evidence**

The plan's former quality measures focused on retrieval, citation, grounding, and abstention (`plan.md` lines 175–184, 553–564). Those measures are correctly inapplicable before retrieval. The source nevertheless intended an actual AI service rather than a UI-only simulation (lines 14–19), and recommended a real model behind an adapter (lines 527–535, 703–710).

**Current treatment**

The PRD verifies that runs reach `completed`, states remain consistent, sessions are isolated, and recovery works (`prd.md` lines 206–230). It does not require the completed answer to be non-empty, rendered correctly in Korean, responsive to the submitted question, or produced by the configured provider in the release smoke path. A deterministic provider can therefore satisfy all listed success metrics while the user-facing chatbot is unusable.

**Why material**

The user's desired outcome is a working ChatGPT-like Q&A program, not merely a correct run-state machine. Deep factual evaluation is out of scope, but a minimal observable answer-quality bar is still necessary.

**Reconciliation needed**

Add a lightweight release acceptance check: representative Korean questions produce non-empty, readable Assistant responses relevant to the prompt; follow-up questions demonstrate session context; the configured real provider path passes one controlled smoke test outside deterministic CI. Explicitly keep RAG, citations, factual-grounding benchmarks, and RAGAS out of this milestone.

### GAP-CS4 — Sensitive-pattern blocking/redaction is an unbounded hidden feature

**Source evidence**

The plan identifies external API key exposure and data-transfer policy as risks, but leaves the allowed provider, transmitted data, retention, and security constraints open (`plan.md` lines 683, 703–710, 723–729). It does not define a deterministic sensitive-content classifier or redaction product.

**Current treatment**

The PRD requires provider disclosures and content-free operational logs (`prd.md` lines 240–245), which is coherent. The addendum additionally requires credentials and “금지된 민감 Pattern” to be blocked or redacted before provider calls (`addendum.md` lines 57–64), but no pattern set, ownership, false-positive behavior, user message, or test contract exists. The PRD simultaneously claims there are no product Open Questions (`prd.md` lines 16–18).

**Why material**

This sentence can silently expand a thin chatbot into a DLP/redaction subsystem, or produce invisible prompt mutation that makes answers confusing. It also creates a product-visible failure mode without an FR.

**Reconciliation needed**

Keep credential handling and `no-store` responses as technical guardrails. For other user-supplied sensitive content, either limit this milestone to a clear “do not submit confidential data” notice, or define a bounded detection/redaction FR with explicit categories, user-visible behavior, and tests. Do not leave a broad hidden filter as an architecture-only mandate.

## Qualitative source ideas retained correctly

- **Thin, real web service:** FastAPI plus a lightweight web UI and containerized local execution remain appropriate (`plan.md` lines 509–525, 553–562; `prd.md` lines 147–155, 187–200).
- **Model isolation:** model choice should remain replaceable behind an adapter rather than leaking provider SDK concerns into Web/API (`plan.md` lines 703–710; `addendum.md` lines 19–27).
- **Incremental retrieval:** a small corpus does not justify Neo4j as the first runtime dependency; lexical, vector, wikilink, and GraphRAG should be introduced and compared later (`plan.md` lines 527–539; `prd.md` lines 178–185).
- **Source-of-truth discipline:** `my-llm-wiki` Markdown and provenance can remain the future knowledge source while graph/vector stores are rebuildable derivatives (`plan.md` lines 593–595, 627–629). None is active in the skeleton.
- **No false grounding:** the plan's warning that the wiki is incomplete and that general model output can overclaim is translated appropriately into an explicit current knowledge-boundary notice (`plan.md` lines 37–44, 678–682; `prd.md` lines 20–26, 156–163).
- **Reversible early choices:** provider, retrieval technology, persistent storage, and hosting-specific dependencies remain deferred or isolated (`plan.md` lines 698–710; `addendum.md` lines 66–81).

## Source ideas intentionally dropped

The following are not postponed backlog items for this product; they are superseded by the user's latest decision and must not become stories or hidden domain models:

- Enterprise AIDD Tailoring Factory positioning and AM/ITO Context Cards (`plan.md` lines 46–123)
- Delta classification, Tailoring Canvas, AI Adoption Level, and methodology-composition outputs (lines 125–159, 186–240)
- Human Review, approval, review state, canonical promotion, Case Draft, and Artifact Export (lines 135–171, 186–220)
- Pilot hypotheses, plans, evidence/case lifecycle, and pilot portfolio operations (lines 148–159, 336–415)
- ITO SR lifecycle modeling and execution contracts (lines 242–275)
- Skill Factory, Agent Skills packaging/distribution, enablement operating model, and organizational approval workflows (lines 276–335, 416–507)
- Their dependent architecture, metrics, risks, and consultation questions (lines 569–657, 686–697, 718–724, 730–744)

These topics may remain in historical strategy material, but they are neither current requirements nor declared future features of the Korean Q&A chatbot.

## Educational-program boundary

The plan's broader course objective still includes later retrieval, GraphRAG, evaluation, and deployment experience (`plan.md` lines 9–21). The current PRD only defines the first working-skeleton milestone. Therefore:

- the skeleton must not claim to satisfy the full educational-program outcome;
- later Knowledge Graph, Vector, RAG, citation, and retrieval evaluation work requires a separate PRD change and architecture decision, as the current PRD already states;
- Tailoring Canvas, Pilot, Human Review, approval, and Artifact Export remain excluded even if later retrieval work proceeds.

## Reconciliation disposition

`prd.md` and `addendum.md` are suitable inputs for a fresh architecture/UX/epic pass after resolving GAP-CS2 through GAP-CS4. GAP-CS1 should be handled as document governance immediately so stale source text cannot contaminate that pass. No rejected tailoring capability is needed to close any finding in this report.
