# `mvp.md` PRD Source Extract

## Extraction rules

- **Explicit** means directly stated in `mvp.md`.
- **Implication** means a conservative consequence of explicit source content, not a new requirement.
- The source's **14-week plan is not treated as product scope, a deadline, a sequencing constraint, or an acceptance constraint**. Requirements mentioned elsewhere in the source remain captured on their own merits.
- Technical implementation prescriptions are separated into **Technical-how addendum candidates** rather than presented as product requirements.

## Product vision

### Explicit

Build an AI service that lets an internal AI-adoption owner enter a structured AM (Application Modernization) or ITO context, uses GraphRAG evidence from `my-llm-wiki`, and produces:

- an AIDD tailoring draft;
- hypotheses that still require validation;
- a Pilot plan; and
- source citations and evidence levels for every judgment.

The MVP validates the long-term vision's core assumption: whether public evidence can improve tailoring quality and whether evidence gaps can be converted into empirical validation plans, demonstrated through a deployable service and evaluation report.

The longer-term progression described by the source is:

1. evidence retrieval → tailoring draft → Pilot plan → Case Draft;
2. validated patterns → OpenCode/Copilot skills → education/coaching → practitioner execution;
3. cross-department Pilot portfolio → case/skill registry → a higher organization-wide minimum level of AIDD maturity.

### Implication

The MVP is an evidence-backed decision-support and learning-loop product, not the full long-term AIDD execution or portfolio platform.

## Primary user and job

### Explicit

**Primary user:** an AIDD methodology/tailoring owner in the company's AI-adoption organization.

**Primary job:** structure an AM or ITO work context; investigate relevant evidence; analyze the As-Is/To-Be delta; draft an appropriate AIDD tailoring; distinguish supported judgments from unvalidated hypotheses; design a Pilot for evidence gaps; review/edit the result; and export reusable artifacts.

Other named audiences/users:

- **Demo user:** a developer reviewing public or synthetic AM/ITO scenarios.
- **Later user:** practitioner development organizations that apply tailoring results and skills.

### Implication

The primary user's outcome is a reviewable, traceable starting point for a tailoring decision and its empirical validation—not autonomous delivery of the tailored development work.

## Problem

### Explicit

- There are too few concrete internal and external cases available for AIDD tailoring.
- Public methodology material exists, but combining and applying it to a project context requires expertise.
- It is difficult to distinguish evidence-backed judgments from hypotheses that have not yet been validated.
- Empirical results are not recorded in a consistent format, limiting reuse in later tailoring work.

The MVP assists with initial analysis, evidence investigation, tailoring drafts, and empirical-validation design. It does not replace organization-wide skill distribution or hands-on development.

### Implication

The product addresses both a decision-quality problem (contextual tailoring and grounding) and a knowledge-reuse problem (consistent case capture).

## Input contract: AIDD Context Card

### Explicit

The user provides a privacy-scrubbed Context Card containing:

- application context: AM or ITO;
- As-Is systems, work, and development activities;
- To-Be goals and changed technologies;
- work, functionality, and controls that must be preserved;
- work and development procedures that must be changed or improved;
- team roles and AI maturity;
- permitted automation scope and human-approval requirements; and
- security, quality, and schedule constraints.

The MVP does not directly collect internal Word, PDF, PowerPoint, or SR-system content. The user removes sensitive information before entering the Context Card.

## User flow

### Explicit

1. Context intake.
2. Check required information and conflicts.
3. Classify the As-Is/To-Be delta.
4. Search `my-llm-wiki` with GraphRAG.
5. Generate an AIDD tailoring draft.
6. Check citations, evidence levels, and overclaims.
7. Generate a Pilot plan for areas with insufficient evidence.
8. Human review and modification.
9. Export the Tailoring Canvas and Case Draft.

### Implication

The product deliberately inserts human review before reusable artifacts leave the workflow, and it routes low-evidence conclusions toward validation rather than presenting them as established facts.

## Outputs

### Explicit

### AIDD Tailoring Canvas

- application context and As-Is/To-Be delta;
- preserve/change/improve targets;
- AI adoption level by development activity;
- human and AI roles;
- human gates and completion evidence;
- relevant AIDD methodology elements;
- `my-llm-wiki` citations and verified versions;
- evidence level and confidence;
- applicability and non-applicability conditions;
- unresolved items and hypotheses requiring validation; and
- Pilot baseline, evaluation metrics, and stop criteria.

Allowed AI adoption levels are:

- `Human only`
- `AI assist`
- `AI execute with approval`
- `AI execute with verification`
- `Automated`

### Pilot Plan

The Pilot plan includes hypotheses, baseline, comparison metrics, risks, and stop criteria. It is generated in the same flow as the Tailoring Canvas.

### Case Draft

A human-reviewed result is exported as a Markdown case draft. It is produced as a separate draft or download and can be promoted to durable knowledge only after a person verifies the wiki schema and original evidence.

### Supporting deliverables

The source also requires a RAGAS/custom-metric evaluation report and documentation of the PRD, architecture, evaluation results, limitations, and reproduction procedure. It calls for a publicly accessible demo URL.

## Product capabilities

### Explicit

1. **Evidence Q&A:** answer questions about methodology workflows, human gates, and applicability/non-applicability conditions with sources.
2. **GraphRAG retrieval:** search canonical documents, raw sources, tags, versions, and wikilink relationships.
3. **Context analysis:** classify As-Is/To-Be differences across work, function, technology, development activity, and control.
4. **Tailoring draft:** propose human/AI roles, gates, and preserve/change/improve items by activity.
5. **Evidence Guard:** check citation validity, confidence, version, unsupported claims, and overclaims.
6. **Pilot Plan:** generate hypotheses, baselines, comparison metrics, risks, and stop criteria.
7. **Case Draft:** export human-reviewed results as a Markdown case draft.
8. **Input validation:** check required Context Card information and conflicts.
9. **Human review/edit:** allow a person to review and modify generated results before export.

### Implication

Evidence provenance and uncertainty handling are cross-cutting product behaviors, not isolated retrieval features.

## Scope

### Explicitly in scope

- AM and ITO Context Card intake.
- Required-information and conflict checks.
- As-Is/To-Be delta classification.
- Evidence Q&A and retrieval over validated `my-llm-wiki` content.
- Evidence-grounded AIDD tailoring draft generation.
- Citation, provenance, version, confidence, evidence-level, unsupported-claim, and overclaim checks.
- Pilot planning when evidence is insufficient.
- Human review and modification.
- Tailoring Canvas and Case Draft export.
- Reproducible results for Q&A plus AM and ITO Context Cards.
- Evaluation with RAGAS and deterministic custom metrics.
- A deployable API/web UI packaged for reproducible execution, a public demo URL, and supporting documentation/evaluation reporting.

### Explicitly out of scope

- Actual code development, modification, or deployment.
- Automatic generation or deployment of OpenCode or GitHub Copilot skills.
- Integration with SR systems, documents, or internal portals.
- Organization-wide Pilot portfolio, permissions, or user management.
- Automatic collection of internal confidential materials.
- Automatic modification of `my-llm-wiki` raw or canonical content.
- Replacing organization-wide skill distribution or practitioner development work.

### Implication

The product reads/indexes approved knowledge and emits drafts; it does not autonomously mutate the durable knowledge base or operational systems.

## Evaluation and success criteria

### Explicit evaluation set

- Golden questions about methodology and workflows.
- At least one synthetic AM Context Card.
- At least one synthetic ITO Context Card.
- Questions and Context Cards where the correct behavior is to withhold an answer because evidence is missing.

### Explicit evaluation dimensions

- retrieval hit@k;
- context precision and recall;
- RAGAS faithfulness-family metrics;
- 100% citation-path validity;
- completeness of required Tailoring Canvas fields;
- accuracy of evidence-level classification;
- rate of unsupported claims and overgeneralization;
- accuracy of abstention and conversion to a Pilot when evidence is insufficient; and
- comparison of lexical, vector, graph, and hybrid retrieval.

### Explicit MVP completion criteria

- Index only data that passes the wiki validator.
- Ensure citations connect through real paths from canonical content to raw provenance.
- Generate reproducible results for Q&A and AM/ITO Context Cards.
- Do not present an unsupported result as a validated case.
- Generate the Tailoring Canvas and Pilot Plan in one flow.
- Produce a RAGAS/custom-metric evaluation report.
- Run the FastAPI/web UI through Docker.
- Deploy a demo URL on Hugging Face Spaces.
- Document the PRD, architecture, evaluation results, limitations, and reproduction procedure.

### Implication

Success requires both artifact generation and trustworthy behavior under insufficient evidence. The source specifies several metrics and a single exact threshold (citation-path validity at 100%) but does not give pass thresholds for the other quantitative measures.

## Constraints and controls

### Explicit product/domain constraints

- Initial application contexts are AM and ITO.
- Source knowledge is `my-llm-wiki`.
- Only wiki-validator-passing data may be indexed.
- Canonical-to-raw provenance must remain navigable through actual citation paths.
- Users, not the service, remove sensitive information from Context Cards before entry.
- The service does not directly collect internal Word/PDF/PPT/SR-system data.
- Human review precedes export/reuse, and durable promotion of a Case Draft requires human verification of wiki schema and original evidence.
- The service must distinguish evidence-backed claims from unresolved hypotheses and abstain/route to a Pilot when evidence is absent.
- Context Cards can carry security, quality, schedule, automation-boundary, and human-approval constraints that tailoring must account for.

### Explicit delivery constraints

- The service is expected to provide an API and web UI, run reproducibly with Docker, expose a Hugging Face Spaces demo URL, and publish the named documents/reports.

### Explicit override applied

The source includes a 14-week implementation plan, but **14 weeks is not a product constraint** for this extraction. No requirement has been removed merely because it appeared in that plan; conversely, week-by-week sequencing has not been promoted into the PRD.

## Risks and failure modes

### Explicit

- Evidence or concrete cases may be insufficient for a requested tailoring.
- Generated content may contain unsupported claims or overgeneralizations.
- Citations may be invalid, versions may be wrong/unverified, or provenance paths may fail.
- Evidence levels may be classified incorrectly.
- Sensitive information could be exposed if the user does not sanitize the Context Card as required.
- A Case Draft could be treated as durable knowledge before its schema and original evidence are human-verified.
- Inconsistent recording of empirical results can prevent later reuse.
- Retrieval behavior/quality differs across lexical, vector, graph, and hybrid approaches and therefore requires comparison.

### Implication

- Tailoring quality depends on the coverage, validation, versioning, and relationship quality of `my-llm-wiki`.
- The source's public/synthetic demo scenarios may not demonstrate performance on confidential real-world cases; this follows from the no-automatic-internal-collection boundary.
- Model and retrieval evaluation are material product risks because the source explicitly requires benchmarking, faithfulness evaluation, failure cases, and limitations to be reported.

## Open points left unspecified by the source

These are omissions, not proposed requirements:

- pass/fail thresholds for metrics other than 100% citation-path validity;
- the exact definition or taxonomy of evidence levels and confidence;
- the exact required-field schema for the Tailoring Canvas beyond the listed contents;
- the editing interaction and export interface for human review;
- access/authentication expectations for the demo or service;
- latency, throughput, availability, accessibility, localization, retention, and cost targets;
- criteria for selecting the benchmarked public model;
- the exact reproducibility tolerance for generated results.

## Technical-how addendum candidates

The following are explicitly prescribed in the source but describe solution mechanisms or delivery tooling. They belong in a technical/architecture addendum rather than the capability-level PRD narrative, while their intended outcomes remain represented above.

| Technical prescription | Stated use |
| --- | --- |
| Python and FastAPI | API, configuration, input/response contracts, and web service |
| Obsidian / LLM-Wiki | Use `my-llm-wiki` as the durable source of truth |
| Neo4j / GraphRAG | Search a derived graph of documents, sources, tags, versions, and relationships |
| LangChain / LangGraph | Orchestrate the multistep intake, retrieval, tailoring, validation, and export flow |
| Public model behind an adapter | Benchmark candidates and apply one model to the MVP |
| RAGAS plus deterministic custom metrics | Evaluate retrieval and grounding |
| Docker | Local reproducibility and deployment image |
| Hugging Face Spaces | Public demo URL |
| Markdown | Case Draft export format |

The distinction is important: the source explicitly names these technologies, but the enduring product requirements are their outcomes—grounded search, traceable provenance, multistep guarded generation, measurable quality, reproducible execution, export, and a deployable demo.
