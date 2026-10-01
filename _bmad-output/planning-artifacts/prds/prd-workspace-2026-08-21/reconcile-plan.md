# Input Reconciliation — `plan.md`

## Reconciliation basis

- **Source:** `C:/Users/KwonSeokWon/Desktop/OSS_AI/workspace/plan.md`
- **Compared with:** `prd.md`, `addendum.md`
- **Authority rule:** explicit PRD decisions are preserved: chatbot-only MVP; no 14-week delivery constraint; no authentication, reviewer identity, or formal approval proof; Knowledge Snapshot release creation/evaluation/promotion/rollback remains outside the chatbot.
- **Filter:** this report includes only source ideas that are missing, materially distorted, silently weakened, or misplaced. Already preserved ideas and intentionally excluded long-term factory capabilities are not restated as gaps.

## Findings

### GAP-P1 — “Tailoring” is weakened from selective method composition to a generic recommendation canvas

**Source intent**

`plan.md` defines tailoring as explicitly *not* recommending one AIDD methodology wholesale. The agent should decompose and recombine method elements according to the context: retain, change, automate, collaborate, exclude, and add controls. AIDD method comparison is evidence for that composition, not the product outcome itself (§1.3 lines 73–76; §1.3.2 lines 195–198). The intended output also includes a recommended baseline assembled from elements such as design approval, TDD, and review contracts (lines 217–218).

**Current treatment**

The PRD says the Canvas contains “방법론 요소” (FR-7) and applicable/non-applicable conditions (FR-9), but it never makes element-level composition or the prohibition against whole-method recommendation a product invariant. A conforming implementation could therefore return “use methodology X” with citations and still satisfy the written FRs.

**Why material**

This changes the product from a context-specific tailoring aid into a cited methodology recommender, losing the central qualitative distinction established in the source.

**Reconciliation needed**

Strengthen the product principle or FR-7 consequences so every material method recommendation is expressed as selected method elements with disposition (`retain/change/automate/collaborate/exclude/add-control` or an equivalent controlled model), and prohibit treating an entire methodology as the default answer without element-level justification. This remains fully inside chatbot scope.

### GAP-P2 — ITO is named as a context, but end-to-end SR lifecycle completeness is not required

**Source intent**

For ITO, the source treats the complete SR lifecycle as the tailoring object, from registration/intake through completeness checking, impact analysis, change design and planning, development, testing/review/security, required artifact updates, human approval and deployment/handoff, to evidence and retrospective recording. Its key qualitative requirement is that responsibility and deliverables must not disappear between intake and closure (§1.3.4 lines 242–274).

**Current treatment**

The PRD allows the user to select ITO and supplies the same generic Context Card, five Delta axes, Canvas fields, and adoption levels used for AM. It does not require an ITO Canvas to cover the SR lifecycle stages, identify missing stages, or preserve stage-level owner, output, gate, evidence, and failure-route information.

**Why material**

An implementation can satisfy FR-1, FR-3, and FR-7 while analyzing only “development” or another narrow slice of an SR. That silently weakens the source’s end-to-end completeness objective and risks precisely the responsibility/artifact omissions the plan sought to prevent.

**Reconciliation needed**

Add an ITO-specific completeness consequence to Context validation or Canvas generation: lifecycle stages must be covered or explicitly marked not applicable/unknown, with stage-level responsibility, required output, Human Gate, completion evidence, and unresolved failure route. This is tailoring analysis, not SR-system integration or action execution.

### GAP-P3 — The organizational accountability boundary is described, but not preserved in generated artifacts

**Source intent**

The source is explicit that the AI enablement organization is not a delivery substitute. It provides the analysis frame, evidence, tailoring, education, coaching, and shared controls; the field organization provides domain context, validates applicability, performs code/test/artifact work, and retains business, technical, security, deployment, and outcome responsibility (§1.3.8 lines 416–441). Secondary users are not merely passive consumers: they apply the tailored workflow and provide execution feedback (§1.2.1 lines 57–60).

**Current treatment**

The PRD acknowledges follow-on implementers in §2.1 and lists final decision/result responsibility as a Non-Goal, while FR-7 only requires generic “사람·AI 역할.” Nothing requires the Canvas or export to distinguish enablement support from field ownership or to avoid assigning operational ownership to the chatbot/enablement team. Reviewer identity and formal approval are correctly excluded, but that separate decision does not eliminate the need to state role accountability.

**Why material**

Without artifact-level ownership semantics, a well-grounded Canvas can still misplace who supplies context, who executes, who formally approves outside the product, and who owns outcomes. This distorts the source’s operating model and may make the exported artifact appear more authoritative than intended.

**Reconciliation needed**

Require Canvas/export role fields to separate at least: enablement support, field/domain owner, execution owner, and external formal decision responsibility, allowing role labels rather than identities. State that `accepted` remains only Session confirmation and does not transfer field accountability. This respects the no-auth/no-reviewer-identity decision.

### GAP-P4 — “Raise the organizational capability floor” is a principle without a verifiable chatbot outcome

**Source intent**

The source’s qualitative north star is not maximizing expert productivity but enabling less-experienced users to follow validated sequences, decision criteria, guardrails, minimum-quality templates, and actionable failure guidance. It explicitly warns that averages can hide failures in the lower-skill group and calls for lower-tail quality/completeness/independence measures (§1.3 lines 78–81; §1.3.5 lines 305–334).

**Current treatment**

PRD principle 6 repeats “Minimum-quality uplift,” and several requirements provide partial mechanisms (field-level missing-input feedback, completeness checks, Korean blocking reasons). However, no FR or success metric verifies that a less-experienced target user can understand the next action, complete a valid Canvas/Pilot without expert rescue, or avoid omissions. Current success metrics are artifact/evidence system metrics, not evidence that the stated user outcome occurred.

**Why material**

The principle can be declared satisfied by schema validation alone even if novice users cannot interpret the feedback or finish the workflow. That silently weakens the source’s primary human outcome. The long-term skill-factory measures remain out of scope, but the chatbot’s claimed contribution should still be testable.

**Reconciliation needed**

Either narrow principle 6 to the concrete chatbot contribution (guided completeness and guardrails) or add a small usability outcome for representative non-expert AI enablement users, such as successful completion of a valid Context Card and review/export journey with no critical omission and bounded facilitator intervention. Do not import long-term skill adoption, portfolio, training, or organization-wide productivity metrics into this MVP.

## Deliberately not raised as gaps

- The 14-week curriculum plan is not a PRD constraint.
- Skill generation/distribution, enablement portfolio operations, and field execution remain long-term vision, not chatbot FRs.
- Authentication, RBAC, reviewer identity, and proof of formal organizational approval remain out of scope.
- Snapshot creation, evaluation, promotion, release orchestration, and rollback belong to the separate Knowledge Supply Pipeline; the chatbot only consumes and validates the active immutable Snapshot contract.
- Graph retrieval is correctly subject to comparison rather than assumed superior; its implementation direction is preserved in the technical addendum.

## Reconciliation verdict

The PRD and addendum preserve the source’s evidence discipline, abstention behavior, five-axis Delta model, five AI adoption levels, read-only knowledge boundary, Human Review, Pilot planning, evaluation, deployment direction, and explicit MVP exclusions. The four findings above are the remaining material qualitative gaps; none requires broadening the MVP beyond the chatbot.
