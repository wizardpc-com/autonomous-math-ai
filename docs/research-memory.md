# Audited Frontier and shared research memory

Autonomous Math AI separates current routing truth from mathematical authority.

- **Audited Frontier** decides what may be researched now. An exact external
  result with an independent PASS audit can become `DO_NOT_ROUTE` immediately.
- **Research Asset Registry** records reusable theorems, lemmas, representation
  bridges, tools, verifiers, exact negative results, kill gates, and explicitly
  `UNPROVED` hypotheses.
- **ClaimGraph** remains the stable, controller-owned theorem/dependency and
  authority graph. Frontier reconciliation never promotes a claim.

This permits an audited external result to stop duplicate work while its
autonomous terminal binding remains pending.

## Project layout

Project-authored inputs live under:

```text
autonomous/research_memory/
├── external_results/*.json
├── assets/*.json
└── themes/*.json
```

The deterministic controller rebuilds derived coordination state under:

```text
autonomous/coordination/
├── frontier/CURRENT.json
├── frontier/HISTORY.jsonl
├── frontier/<content-hash>.json
├── objects/<content-hash>.json
├── audit_receipts/<audit-key>.json
├── ASSET_REGISTRY.json
├── REPRESENTATION_GRAPH.json
├── METHOD_LEDGER.json
└── AUDIT_INDEX.json
```

These files are machine-readable routing memory, not canonical mathematical
state. `CLAIMS.md`, `PROGRESS.md`, proof prose, or a model's assertion of
`PROVED` never creates a routing PASS by itself. The result manifest must bind
the exact statement, scope, representation, dependencies, proof/certificate/
source hashes, independent audit report hashes, policy version, and provenance.

## External result classes and maturity

External result manifests classify evidence as one of:

- `AUDITED_EXTERNAL_RESULT`: exact evidence identity plus an independent PASS;
- `UNAUDITED_EXTERNAL_RESULT`: potentially useful proof material awaiting a
  Result Audit;
- `COMPUTATION_ONLY`: reproducible evidence that is not a proof;
- `CONFLICTING_RESULT`: an explicit conflict that blocks routing until resolved.

They also declare maturity `RESULT`, `THEME`, or `FINAL`, bound respectively to
an audit level of `RESULT`, `THEME_INTEGRATION`, or `GLOBAL`. A `RESULT` closes only
its exact `scope_ids`. It does not close every task whose parent ClaimGraph claim
appears in `claim_ids`. Theme- or final-level suppression requires the stronger
maturity declaration and remains routing-only until canonical promotion.

The audit key is deterministic over the normalized exact statement,
representation, dependencies, proof hashes, certificate hashes, source hashes,
and audit policy version. An existing routing PASS can be reused only when this
key is identical. These routing receipts do not satisfy the controller's
candidate-to-authority transition by themselves.

## Campaign Themes

Start a real campaign with a project-local theme:

```powershell
amr run --project <project> --theme autonomous/research_memory/themes/type3.json
```

The theme is pinned in the campaign directory and cannot be changed or added
after the campaign starts. It declares included/excluded claims and exact
scopes, objective, allowed/forbidden methods, dependency boundary, combination
scope, and exact obligations. A scope-specific Director task binds its scope via
`input_closure.canonical_object_id`.

Admission order is deterministic:

```text
Global Audited Frontier
→ Campaign Theme filter
→ dependency closure
→ remove closed and duplicate scopes
→ apply exact kill gates
→ route remaining tasks
```

The controller checks this at Director-plan acceptance and again immediately
before model dispatch. A task outside the Theme, closed by an audited external
result, dependency-blocked, or covered by an exact method kill gate is rejected
before a research model starts.

Schema-v2 completion policies may also list `terminal_research_outcomes` from
`BLOCKED`, `FALSIFIED`, and `OBLIGATION_EXHAUSTED`. These values stop the
campaign operationally only when explicitly configured. They do not update a
ClaimGraph status, create a candidate or audit receipt, or authorize a trust,
evidence, representation, parent-claim, or canonical transition.

## Assets and progressive disclosure

Every asset card records a stable id, kind, exact capability, scope,
preconditions, do-not-use conditions, input/output contract, evidence paths and
hashes, audit status, failure modes, dependencies, and provenance.

Representation bridge cards additionally declare source/target representation
ids plus conditions, localization, saturation, content, and exceptional
factors. No registered edge means incompatible. An externally audited edge may
support routing, but it does not authorize a final candidate or bypass semantic
terminal binding.

Negative-result and kill-gate cards require a method id, exact failed statement,
`do_not_repeat`, and `reopen_if`. Their scope is never generalized by the
controller: failure of one invariant does not kill a broader family of methods.

At Director and worker dispatch, the controller selects only assets intersecting
the campaign/task claim, exact scope, representation, method, or dependency
boundary. The task packet tells the worker to reuse an equivalent audited asset
by default. If replacement is necessary, the worker must identify the existing
asset, the violated applicability condition, and the exact difference.
`research_context.loaded_asset_ids` is the exact current-turn reporting scope;
asset rows in an older continuation checkpoint are historical only.

## Commands

Rebuild after adding or auditing external manifests:

```powershell
amr frontier rebuild --project <project> --theme <optional-theme.json>
```

Inspect current routing state or retrieve a small shared context bundle:

```powershell
amr frontier inspect --project <project>
amr frontier context --project <project> --claim <claim-id> --scope <scope-id>
```

Every real campaign also rebuilds before model startup and again during final
artifact sealing. The end delta records new entries, newly closed obligations,
new exact falsified routes, superseded objects, authority drift, and items still
pending audit, dependency, human, or theme integration.

`amr validate --strict` recomputes the Audited Frontier in memory and rejects a
missing, malformed, or stale `CURRENT.json` when research-memory manifests are
present. For a Frontier with a Campaign Theme, validation reloads the
project-local `campaign_theme.source_path` and verifies its normalized digest;
a missing, changed, or out-of-project source fails closed. The check is
read-only and does not update the campaign's pinned `THEME.json`. Missing or
changed active evidence therefore cannot retain an older routing closure;
rebuilding records the corresponding `BLOCKED / EVIDENCE_IDENTITY_MISMATCH`
route without changing ClaimGraph or mathematical authority.

## Audit hierarchy

The shared memory supports the intended three stages without flattening them:

1. A Result Audit makes an exact result or asset reusable in the Frontier.
2. A Theme Integration Audit checks completeness, compatible representation,
   exceptions, union/dedup, dependencies, and statement scope before a stable
   theme claim is promoted through the existing ClaimGraph pipeline.
3. A Global Audit checks the full final dependency and representation closure
   before the final conjecture claim and later formal verification.

Frontier ingestion does not perform stages 2 or 3 and cannot write canonical
authority files.
