# Trust model

Autonomous Math AI assumes model outputs can be incorrect, incomplete,
internally inconsistent, or operationally malformed. The harness therefore
separates generation, evidence, audit, and trust.

## Evidence is not trust

- A research response is an untrusted proposal.
- A computation is evidence within its exact inputs, arithmetic, bounds, and
  software assumptions.
- Failure to find a counterexample is not proof outside the searched scope.
- A candidate event records provenance but is not an audit verdict.
- A fresh audit verdict remains subject to deterministic and canonical gates.
- Only an explicit trusted-state transition changes a canonical claim.
- A role's self-reported `PROOF`, `COUNTEREXAMPLE`, or `COMPLETED` value is not
  controller-verified state progress and does not reset stagnation.

Evidence levels communicate what was actually checked. They do not silently
upgrade when a related or narrower subclaim receives stronger evidence.

Canonical `ClaimGraph` obligations remain open until the selected domain's
required audit and canonical gates close them. The math pack uses proof
obligations; non-math packs use certificate or empirical-protocol frontiers and
never synthesize proof obligations. Finite computation can attach scoped
evidence, but it neither closes a universal obligation nor proves a general
statement.

The pinned domain contract owns the allowed statuses and evidence transitions.
In particular, empirical `CONFIRMED` and `REPLICATED` are not mathematical
`PROVED` states. A model cannot add a status, relax the pack's minimum audit, or
rebind a graph to another domain. See
[Research domains and policy packs](research-domains.md).

## Reasoning health is not correctness

`ReasoningHealthMonitor` observes only operational symptoms such as an
unexpectedly short reasoning-token count or the same count repeated across
turns. A signal may request a bounded diagnostic retry or an explicit
`xhigh -> max` continuation when the provider declares `max` support. Unknown
telemetry stays unknown. These signals never set mathematical status, trust,
evidence level, audit verdict, or canonical progress.

## Falsification-first

The scheduler prefers cheap exact falsification, boundary checks, independent
replay, and invariant testing before expensive proof attempts. Search reports
must retain the exact finite domain, arithmetic, versions, commands, seeds, and
remaining gap.

## Fresh independent audit

The producer cannot approve its own result. Audit leases bind candidate
fingerprint, audit kind, and attempt. The auditor reads a sealed evidence bundle
and controller-supplied identity metadata. A request may reprioritize an
existing lease but cannot create duplicate trust votes.

Critical final claims require the configured independent audit threshold before
normal finalization. Invalid audit output leaves the candidate pending or in
bounded retry state; it does not reject or approve the mathematics by default.

## Representation contract

Each task, candidate, route, and audit records:

- branch;
- localization;
- saturation;
- normalization;
- content convention;
- exceptional factors;
- combination scope.

The controller hashes this contract into a `representation_id`. Equal hashes
may be combined directly. Different hashes fail closed unless a dedicated
`REPRESENTATION_BRIDGE` candidate passes fresh independent audit. Neither a
model nor human steering can declare compatibility directly. Legacy records map
to `LEGACY_UNSPECIFIED` and require the same bridge.

The compact Director snapshot exposes the controller's claim-to-representation
mapping, complete known contracts, and audited bridge pairs. Merely knowing a
contract does not make it compatible. Persisted contract ids are recomputed from
their content on load and a mismatch fails before model dispatch.

## Semantic alignment contract

The representation contract above compares low-level task/candidate encodings.
An optional, domain-independent `semantics.json` adds the end-to-end question:
does this exact canonical object, through this representation and this
certificate/computation, reach a validator whose PASS entails this claim?

Its receipt-derived `SemanticStatus` (`VERIFIED`, `BRIDGE_OPEN`,
`TERM_AMBIGUOUS`, or `UNREVIEWED`) is orthogonal to claim, trust, evidence, and
execution statuses. `semantics.json` is declaration only; controller-owned
append-only receipts bind verification to one sealed candidate, its evidence,
validator configuration and scope, audits, and semantic/contract heads. For an
opted-in project, **No unverified bridge into trusted final claims** is checked
over the complete dependency closure. Multi-agent agreement is not evidence for
a bridge. See [semantic alignment](semantic-alignment.md).

## Structured output boundary

Output Protocol v2 minimizes role-owned fields. Director, worker, auditor, and
candidate schemas are immutable package resources. Mock and live App Server
paths use the same compatibility preflight and parser contracts. Controller-
known identity, timestamps, fingerprints, and report references are injected by
the controller rather than copied from a model response.

## Mechanical worker boundary

Mechanical workers execute one bounded, mechanically checkable task packet.
They cannot select research direction, invent lemmas, issue new research tasks,
modify canonical state, use network access, or spawn another worker.

The primary route is Spark/high/null. Only a structured permanent unavailable
or access-denied result permits one Luna/medium/null fallback. Timeout, network,
rate-limit, or transient App Server failures receive bounded same-route retries
and never poison the availability cache. No fallback uses a parent model, fast,
or priority service.

Mechanical results are evidence for the parent role, never an automatic proof
or audit verdict.

## Service-tier boundary

Main-role Fast execution requires the pinned configuration switch
`execution.fast_mode=true`. AMR requests only `fast`; a returned `priority` is
accepted only as its documented observation alias. Missing or conflicting
thread-start confirmation fails before the first turn. When the switch is off,
any nonempty Fast/priority observation fails closed. This operational option
does not alter evidence, audit, mathematical status, or trust gates, and it
never applies to mechanical workers.

“Unbounded” mechanical concurrency removes only a configured seat count. It
does not bypass the broker's separate token/cost governor, resource capacity,
provider rate limits, queue depth, dispatch batch, timeout, recovery, or
operator stop. Mechanical workers remain one layer deep.

## Human and external inputs

Human steering and ingested local assets are append-only inputs. Steering can
add notes, prioritize claims, pause or resume routes, request audit, or stop
after an epoch. It cannot set trust, inject arbitrary model work, or bypass
representation and audit gates.

Ingest copies one explicit local file into content-addressed campaign storage,
records its digest and media type, rejects path escape, and persists only a
portable URI. An asset is research input, not trusted evidence by itself.

## Operational claims

Reports distinguish dry, mock, active live, failed live, and completed live
runs. Unknown telemetry remains unknown. Internal errors use non-success exit
status and cannot be rewritten as normal queue exhaustion. Failed runs remain
immutable evidence for later append-only recovery.
