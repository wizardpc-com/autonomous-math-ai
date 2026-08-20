# Architecture

Autonomous Math AI is a conjecture-neutral orchestration layer. A target owns
its mathematical statement, prompts, claim graph, canonical inputs, and local
runtime. The installed distribution owns protocols, policy enforcement,
lifecycle, scheduling, audit leases, and durable storage semantics.

## Package boundaries

- `engine/` controls dynamic admission and scheduling pressure.
- `protocol/` exposes Output Protocol v2 contracts, error classes, and schema
  compatibility preflight.
- `lifecycle/` owns monotone phases, campaign/epoch state, audit leases, and
  derived cognitive views.
- `storage/` owns atomic persistence, portable artifact references, steering,
  and local asset ingest.
- `mechanical/` owns the controller-brokered one-shot worker boundary.
- `research_job.py` owns strict logical-job termination and same-thread turn
  continuation policy; `reasoning_health.py` supplies diagnostic-only signals.
- `provider_backend.py` routes roles to transport adapters; provider transport
  never owns mathematical role semantics or canonical-state authority.
- `provider_config.py` normalizes capability, effort, tier, usage, cost, and
  credential-reference declarations before any model turn.
- `cli/` owns the `amr` command surface.
- `resources/` contains immutable wire schemas and the bundled policy pack.

`storage_layer` is a compatibility import wrapper, not a second storage
implementation.

## Trust path

```text
research job
    │ emits an untrusted candidate
    ▼
identity + representation + schema checks
    │
    ▼
content-addressed immutable evidence bundle
    │
    ▼
fresh audit through a controller-owned lease
    │
    ▼
deterministic verification + canonical gate
    │
    ▼
trusted claim-state transition
```

Producer transcripts are not the trust source. The auditor receives the exact
statement, immutable bundle, representation contract, and requested audit kind.

## Campaign, epoch, and job

A campaign is a long-horizon research effort. It contains append-only epochs;
each epoch contains top-level jobs and isolated mechanical subtasks. Epochs are
the units of sealing, recovery, policy pinning, and handoff.

The lifecycle is monotone:

```text
BOOTSTRAP → RUNNING → DRAINING_* → SEALED
                    ↘ FINALIZING → COMPLETED
```

Once dispatch leaves `RUNNING`, a continuation cannot reopen the same epoch.
New information becomes a constraint for a later epoch. A crash resume remains
within the same epoch; campaign continuation creates a new one.

### Controller-owned research turns

A research job is a logical proof task, not one model turn. Prover, falsifier,
and explorer jobs may run several explicitly requested turns in one Codex
thread. Each turn is bound to the job and appended as
`RESEARCH_TURN_COMPLETED`; a further turn requires a controller
`CONTINUE` directive. Director and audit jobs remain single-turn.

The harness does not arm an App Server `thread/goal` for autonomous jobs.
Per-thread limits are enforced from token telemetry, avoiding a race in which
an active native goal could start a continuation outside controller ownership.
Missing token telemetry or an exhausted controller-owned thread budget blocks
the next turn fail-closed.
Any unowned `turn/started` is interrupted and stops the run fail-closed. A
model's `PROOF` or `COUNTEREXAMPLE` label does not end a logical job: termination
requires a validated candidate entering the audit frontier, controller-verified
canonical progress, a concrete execution blocker, or a configured turn bound.

Turn completion is correlated by both turn id and thread because some App
Server versions expose different response and stream ids. A completion buffered
before the `turn/start` response is consumed exactly once across both indexes;
delivered or duplicate notifications are never carried into the next turn.
Repeated `turn/started` for the same owned id is idempotent, while a distinct
start or completion id remains an unmanaged continuation and fails closed.
Cancellation and a failed `turn/start` response attempt to interrupt any remote
turn already observed before releasing controller ownership.

Crash recovery never guesses how far an interrupted proof got. It preserves
completed-turn events, interrupts the stale remote turn, and requeues the exact
task under the existing bounded retry policy.

### Canonical proof frontier

Proof obligations live inside each canonical `ClaimGraph` claim (schema v3).
They have stable content-derived ids, status, dependencies, and evidence paths.
`proof_frontier` derives `remaining_obligation_ids` and `next_obligation_id`
from that graph; there is no parallel `proof_state`. Legacy v1/v2 graphs gain a
deterministic root/gap obligation on load. Only an audited canonical transition
can discharge or refute an obligation.

Status domains are intentionally separate: `MathStatus` describes the claim
(`REFUTED` is the code name for the legacy wire value `FAILED`), `TrustStatus`
describes review state, `EvidenceLevel` describes what was checked, and
`ExecutionStatus` describes process/transport completion.

## Failure taxonomy

Failures are handled in this order:

1. local schema, bootstrap, canonical, or policy violation;
2. transport, rate-limit, or transient protocol failure;
3. a server-completed turn with failed status;
4. missing or invalid structured model output;
5. role-level semantic validation failure;
6. controller or state-machine failure.

A failed job is never passed into a role parser. Original server errors,
streamed events, identifiers, retry classification, and telemetry are retained.
Role protocol failures use bounded role-local retries. Controller, canonical,
policy, and local-schema failures drain the epoch as internal failures.

## Scheduling

Default hard caps are independent: one Director, eight research jobs, and eight
audits. Mechanical scheduling has no static seat cap by default, but the broker
derives a resource cap and enforces token/cost budget, queue, batch, rate-limit,
timeout, and stop backpressure. Main roles and mechanical workers use separate
governors (500 million and 1.5 billion default tokens respectively).
Dynamic admission adjusts only new dispatch based on information gain, route
novelty, estimated cost, and audit backlog. It never cancels healthy work merely
because the target concurrency changed.

Incremental Director work is coalesced and debounced behind a version watermark.
The Director does not wait for all research and audit jobs to drain and cannot
block their normal dispatch.

A task id is a stable binding to one task fingerprint within an epoch. The
controller rejects changed task content that reuses an accepted id and has a
final dispatch-time guard against concurrently active duplicate ids. Each job
attempt receives a job-id-qualified workspace, so sequential retries and even
defense-in-depth test bypasses cannot overwrite another attempt's sealed
mechanical broker configuration.

Every Director snapshot includes a controller-owned representation compatibility
view (claims grouped by representation id, known complete contracts, missing
contract ids, and independently audited bridge pairs) plus latest route state.
Route updates are durable bookkeeping, not runnable queue work. If semantic
admission rejects every proposed task and no audit priority is applicable, the
controller supplies the rejection reasons to one bounded repair turn; retry
exhaustion pauses the epoch instead of falling through to idle queue failure.

## Durable storage

Events and route records are append-only. Candidate artifacts are copied into
content-addressed bundles before audit. Durable references use `project://`,
`campaign://`, or `epoch://` URIs, so evidence does not depend on a machine's
absolute path.

Director `required_files` accepts those durable URIs as well as legacy
project-relative or project-contained absolute paths. The controller resolves
and rechecks each reference before dispatch, then supplies the research worker
with an internal read-path mapping while preserving the portable reference in
the task and event history. `ResearchTask.dependencies` names existing
`ClaimGraph` claims only; task-to-task sequencing is expressed by a later
Director wave rather than by placing task ids in that field.

`CORE_CAPSULE` is a bounded rebuildable snapshot, `RESEARCH_MAP` is a derived
human-readable view, and `ROUTE_LEDGER` records failed approaches and explicit
retry conditions. None of these derived views can override the canonical claim
graph.
