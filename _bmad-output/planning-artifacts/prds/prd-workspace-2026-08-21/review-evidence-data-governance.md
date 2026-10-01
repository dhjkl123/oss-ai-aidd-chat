# Evidence & Data Governance Re-review

## Verdict

**CLEAN at critical/high severity.** Both previously remaining high findings are resolved in the current PRD. Four previously identified medium findings remain outside this narrow re-check.

## High-finding re-check

### Schema-valid legacy Evidence classification — resolved

The PRD now:

- defines `hash_coverage_gap` as Schema-valid rather than `invalid`;
- explicitly accepts Validator-approved `raw/notebooklm/`, `raw/web/`, `raw/youtube/`, and Trusted Legacy Copy sources;
- preserves actual metadata and requires the Schema-defined log hash comparison;
- exposes the gap in Confidence Basis and Export; and
- reserves `invalid` for actual contract failure.

This aligns with SCHEMA.md “Directory roles,” “Raw source integrity,” and “Provenance.”

### `stale` revalidation semantics — resolved

The PRD now uses the authoritative event-based triggers:

- referenced Tag change;
- a relevant Stable Release changing Workflow Inventory or Contract; or
- cited-file movement or material modification.

It also distinguishes missing version-sensitive metadata as Unverified Version, prevents it from becoming `supported`, and changes to `stale` only when a Schema revalidation trigger occurs. This aligns with SCHEMA.md “Version and freshness policy.”

## Severity counts

| Severity | Count |
| --- | ---: |
| critical | 0 |
| high | 0 |
| medium | 4 |
| low | 0 |

## Scope note

Only the two previously remaining high findings were re-checked. The prior four medium findings were retained without re-review.
