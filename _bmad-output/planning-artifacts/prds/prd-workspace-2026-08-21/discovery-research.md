# Discovery Research: Evidence-Grounded Internal Recommendation Chatbot

**Research date:** 2026-08-21  
**Decision served:** Define the minimum trustworthy product contract for an internal RAG chatbot that returns structured recommendations, cites evidence, abstains when support is insufficient, routes decisions through human approval, and exposes evidence lineage.  
**Scope:** Current official product documentation and primary risk guidance only; product expectations rather than implementation-stack choices.

## Decision digest

### 1. Make evidence a claim-level object, not a bibliography appended to fluent prose

Current grounding interfaces expose more than source URLs: Google represents a grounding chunk as evidence supporting a model claim, exposes claim-to-chunk support metadata, and provides citation spans, source title, URI, and publication date [1][2]. OpenAI's official file-search documentation likewise models citations as annotations linked to a concrete uploaded file and quote [3]. The PRD should therefore require every material recommendation claim to resolve to the exact supporting passage and source record; a list of documents at the bottom of an answer is insufficient.

**Product acceptance consequence:** A user can select any recommendation claim and inspect the supporting passage, document identity, source location, and retrieval context. The UI must distinguish “cited” from “supported”: citations without an attributable supporting passage are visibly incomplete.

### 2. Abstention is a first-class successful outcome, with an inspectable reason

Official grounding APIs now expose both support relationships and groundedness scores/confidence, while retrieval interfaces allow relevance thresholds that can deliberately return fewer results [2][5]. This makes a binary “always answer” chat experience an outdated product contract. The product should produce one of three explicit evidence states: **supported recommendation**, **partial/contested evidence**, or **insufficient evidence—no recommendation**.

**Product acceptance consequence:** The structured response includes evidence state, confidence basis, unsupported claims/gaps, and a next action (refine the question, add a source, or escalate). Thresholds must be validated per decision class; a single global confidence number must not silently convert weak retrieval into advice.

### 3. Human approval must govern consequential action, not merely decorate the workflow

NIST identifies confabulation as confidently presented erroneous content, warns that generated citations and logic can themselves be false, and highlights heightened risk when outputs influence consequential decisions [4]. It also identifies automation bias and over-reliance as human–AI configuration risks [4]. Human approval therefore needs to be an enforced state transition with adequate evidence visible at the point of decision—not a generic disclaimer or optional thumbs-up.

**Product acceptance consequence:** The chatbot may draft but cannot finalize or trigger a consequential recommendation until an authorized reviewer approves it. The decision record captures approver, timestamp, selected recommendation, evidence version, edits/overrides, and rationale. Review screens surface contradictions, missing evidence, and abstention reasons before the approve control.

### 4. Evidence lineage must survive after the source corpus and answer change

Current APIs can return file identifiers, retrieved chunks, claim support, source metadata, and output spans [1][2][3]. A trustworthy internal product should preserve those relationships as an immutable decision snapshot: recommendation claim -> cited passage -> source document/version -> retrieval event -> approval. Without a snapshot, later corpus updates can make an old approval appear supported by evidence the reviewer never saw.

**Product acceptance consequence:** Every answer and approval has a stable ID and evidence snapshot. Users can compare versions and see source additions, removals, permission loss, supersession, and changes to the generated recommendation. Lineage is exportable for audit and incident review.

### 5. Internal-data trust includes retention and access behavior, not only answer quality

OpenAI's current data-control documentation illustrates the product questions enterprise buyers now expect to answer: whether submitted data trains models, what is retained as abuse-monitoring logs or application state, default retention windows, deletion/expiry behavior, and how third-party tools change data residency obligations [5]. These controls vary by endpoint and integration, so “internal” does not automatically mean private or least-privileged.

**Product acceptance consequence:** The PRD must specify source-level authorization at retrieval and citation time, no disclosure through titles/snippets, retention and deletion semantics for conversations and evidence snapshots, third-party data-flow disclosure, and auditable access events. Permission loss should redact inaccessible evidence without erasing the historical fact that it supported a prior approved decision.

## Principal product risks

- **Citation theater:** A real source is cited but does not support the adjacent claim. Mitigate with claim-level support inspection and groundedness evaluation, not citation presence alone [1][2][4].
- **Automation bias:** Fluent structure and an approval button can increase over-trust. Mitigate by presenting evidence state, contradictions, and gaps before recommendations and approval [4].
- **False precision:** An opaque confidence score may imply more certainty than retrieval and source quality justify. Confidence must name its basis and never replace the abstention policy [2].
- **Lineage drift:** Mutable documents or permissions can break reproducibility. Preserve the exact evidence snapshot reviewed, while honoring current access controls.
- **Data-boundary leakage:** Logs, stored application state, or third-party connectors may extend beyond the intended internal boundary. Treat retention, deletion, residency, and connector behavior as launch-gate requirements [5].

## PRD-ready minimum response contract

Each response should contain: normalized question; one or more structured recommendation options; evidence state; claim-level citations; exact supporting passages; source/version metadata; confidence basis; contradictions and missing evidence; abstention/escalation reason when applicable; generated-at and evidence-snapshot IDs; and approval status/history. This is a product-level contract; vendor-specific scores and identifiers should remain provenance inputs rather than become the user-facing truth model.

## Source appendix

[1] Claim-to-source spans and citation metadata — [Google Cloud, GenerateContentResponse API reference](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/GenerateContentResponse), current reference accessed 2026-08-21. **Confidence: high.**

[2] Grounding chunks, retrieved context, support, groundedness score and confidence — [Google Cloud, Vertex AI RPC reference](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/reference/rpc/google.cloud.aiplatform.v1), current reference accessed 2026-08-21. **Confidence: high.**

[3] File-search citation annotations linked to a file and quote — [OpenAI, Assistants API deep dive](https://platform.openai.com/docs/assistants/deep-dive/run-lifecycle%23.webm), current official documentation accessed 2026-08-21. **Confidence: medium** because this is a product-specific precedent, not a universal standard.

[4] Confabulation, false citations/logic, consequential-decision risk, automation bias and over-reliance — [NIST AI 600-1, Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf), published 2024-07-26; publication page updated 2026-04-08; accessed 2026-08-21. **Confidence: high.**

[5] API data use, abuse-monitoring/application-state retention, endpoint controls, file expiry, and third-party-tool caveats — [OpenAI, Data controls in the OpenAI platform](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint), current official documentation accessed 2026-08-21. **Confidence: high for the vendor behavior; medium when generalized into a cross-vendor procurement expectation.**

## Open questions for the PRD

- Which recommendation classes are consequential enough to require approval, dual approval, or prohibition?
- What source types are authoritative, and how are conflicts, supersession, and staleness represented?
- What measured groundedness/retrieval thresholds are acceptable for each decision class and language?
- How long must decision snapshots remain reproducible when underlying documents are deleted or access is revoked?

