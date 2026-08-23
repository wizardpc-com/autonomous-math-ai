# Architecture

Autonomous Math AI is a research-topic-neutral orchestration layer. A target owns
its research statement, prompts, claim graph, canonical inputs, and local
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
- `domain_semantics.py` owns the fixed status, transition, dependency, and audit
  adapters for bundled research domains; `policy.py` owns pack discovery and
  run-local pinning.
- `experiment.py` owns deterministic non-LLM batch execution and raw evidence;
  `amr experiment validate|run` exposes the same boundary through the CLI.
- `resources/` contains immutable wire schemas and the bundled policy packs.
- `canonical_state.py` freezes manifest-declared startup inputs and builds the
  provenance supplied to derived planning views.
- `canonical_transition.py` owns digest-bound, crash-replayable ClaimGraph and
  trusted-metadata transactions.

`storage_layer` is a compatibility import wrapper, not a second storage
implementation.

## Policy-pack boundary

The stable controller discovers and strictly validates the three bundled packs,
then pins the selected descriptor, domain contract, audit requirements, skill,
role prompts/references, and mechanical resources into each new run. Model
roles receive only the verified run-local policy view. Resume re-verifies every
snapshot and uses the pinned bytes; missing, modified, or cross-pack bindings
fail closed. Pack selection changes domain semantics, not lifecycle, storage,
audit-lease, or canonical-gate ownership. See
[Research domains and policy packs](research-domains.md).

## Startup canonical-state refresh

Every new `amr run` performs a zero-model-turn refresh before backend startup or
the first Director:

```text
autonomous/project.json
    │ resolves canonical_inputs + ClaimGraph + trusted metadata + optional overlay
    ▼
ClaimGraph/trusted/marked-Markdown consistency gate
    ▼
byte-exact run-local snapshots
    │ path + SHA-256 + available Git HEAD
    ▼
canonical_state.json
    │ drift/integrity gate
    ▼
compact_snapshot + CORE_CAPSULE + RESEARCH_MAP
    ▼
fresh Director
```

The run-local `canonical_state.json` freezes each unique canonical input once
and records its role membership. Director inputs are embedded as UTF-8 content
in the dynamic snapshot, so the Director does not need to reopen project files.
The ClaimGraph is loaded into the dynamic snapshot as the sole status and proof-
frontier authority. Trusted metadata must match its recorded graph digest when
one is present. Ordinary Markdown remains contextual. A canonical input may
opt into a strict machine state view with one
`AMR-CANONICAL-STATE-BEGIN`/`END` block; a malformed or nonmatching block stops
before backend startup. The refresh does not rewrite any of these files or
infer mathematical/trust transitions from prose.

A crash resume must match the original frozen canonical inputs. Across fresh
epochs, a changed canonical or planning-context fingerprint invalidates pending
research planning and forces a rebuild. If an unresolved audit frontier cannot
be safely rebound to the changed state, startup fails closed before any model
turn. Canonical inputs are rechecked before every Director snapshot, closing the
read-to-launch race.

An epoch checkpoint binds its planning fingerprint to the ClaimGraph and trusted
state produced by the last committed canonical transition at that checkpoint's
event watermark. Consequently, controller-authorized transitions made during an
epoch remain usable by its successor. A different later state still counts as
drift. Legacy v1 checkpoints are accepted only when their embedded ClaimGraph,
live trusted-state binding, transition ledger, run event, and watermark all name
the same final committed transition; the reconciliation is append-only.

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
atomic ClaimGraph + trusted-metadata transition
    │ append-only authorization + before/after snapshots
    ▼
committed canonical state
```

Producer transcripts are not the trust source. The auditor receives the exact
statement, immutable bundle, representation contract, and requested audit kind.

## Campaign, epoch, and job

A campaign is a long-horizon research effort. It contains append-only epochs;
each epoch contains top-level jobs and isolated mechanical subtasks. Epochs are
the units of sealing, recovery, policy pinning, and handoff.

A controller process is an attempt within an epoch. `ATTEMPT_STARTED`,
`RECOVERY_COMPLETED`, `ATTEMPT_FAILED`, and `ATTEMPT_INTERRUPTED` record that
process lifecycle without
pretending that the epoch ended. Resume validates the original `RUN_MANIFEST`,
including campaign identity and absolute deadline, before constructing a
controller or writing a campaign record. A failure before recovery leaves the
epoch unsealed and does not rewrite `compact_snapshot`, `CORE_CAPSULE`, or
`RESEARCH_MAP`.

The lifecycle is monotone:

```text
BOOTSTRAP → RUNNING → DRAINING_* → SEALED
                    ↘ FINALIZING → COMPLETED
```

Once dispatch leaves `RUNNING`, a continuation cannot reopen the same epoch.
New information becomes a constraint for a later epoch. A crash resume remains
within the same epoch; campaign continuation creates a new one.

Epoch sealing and run artifact finalization are separate commits. After the
checkpoint is sealed, `RUN_ARTIFACT_FINALIZATION_STARTED` covers report,
immutable-file hash index, semantic index, outcome, and run-summary generation.
Only a successful `RUN_ARTIFACT_FINALIZATION_COMPLETED` may be followed by
`ATTEMPT_COMPLETED` and terminal `RUN_STOPPED`. Append-only event logs are
identified by the outcome's event watermark rather than hashed as immutable
files before their terminal records exist. An artifact failure or operator
interrupt records a terminal diagnostic and cannot enter the automatic epoch
loop.

With `amr run --auto-epochs`, an ordinary epoch-time seal immediately launches
a new controller and epoch using the latest usable checkpoint. The campaign
duration remains the outer budget; the last epoch is shortened to the remaining
time. A fresh controller repeats canonical refresh, consistency checks, stale-
planning disposal, and derived-state rebuild every time. Only the exact clean
epoch-time boundary is auto-continuable. Quota pause, canonical/bootstrap
failure, internal failure, stop-after-epoch steering, final-claim completion,
and campaign-budget exhaustion terminate the loop. `amr campaign continue`
remains compatible and accepts optional `--auto-epochs` for unattended
continuation from an already sealed checkpoint.

The same loop may follow a crash recovery with
`amr run --resume EPOCH_ID --auto-epochs`: the resumed epoch must first recover
and seal cleanly before a fresh epoch is admitted.

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
canonical progress, a post-repair controller-actionable execution blocker, or a
configured turn bound. The first `BLOCKED` report therefore receives a repair
turn; blocker verification is only an execution-scheduling decision and never
a mathematical verdict. The built-in per-role bounds are twelve turns. If a
turn or controller token bound is reached without verified progress, the
controller records the result, turn history, canonical current/next proof
obligation, completed evidence bindings, and artifact digests in a noncanonical
checkpoint. The route is paused for the remainder of the epoch and the
digest-bound continuation task is admitted in the next epoch only after
fail-closed integrity and schema checks. This carry-over never changes claim,
trust, evidence, or audit status.

Turn completion is correlated by both turn id and thread because some App
Server versions expose different response and stream ids. A completion buffered
before the `turn/start` response is consumed exactly once across both indexes;
delivered or duplicate notifications are never carried into the next turn.
Repeated `turn/started` for the same owned id is idempotent, while a distinct
start or completion id remains an unmanaged continuation and fails closed.
Cancellation and a failed `turn/start` response attempt to interrupt any remote
turn already observed before releasing controller ownership.

Crash recovery never guesses how far an interrupted proof got or trusts the
latest derived snapshot. It reconstructs the Director watermark and frontier
from append-only events, digest-bound research checkpoints, and the startup
canonical snapshot; stale jobs receive an explicit terminal reconciliation
before their exact tasks are requeued under the existing bounded retry policy.
The rebuilt snapshot records its attempt, generation, event watermark, and
canonical/planning hashes. The original absolute epoch deadline remains
authoritative, so restarting a controller cannot extend an epoch.

### Canonical domain frontier

For `math-research`, proof obligations live inside each canonical `ClaimGraph`
claim (schema v3). They have stable content-derived ids, status, dependencies,
and evidence paths. `proof_frontier` derives `remaining_obligation_ids` and
`next_obligation_id` from that graph; there is no parallel `proof_state`.
Legacy v1/v2 math graphs gain a deterministic root/gap obligation on load.

Non-math graphs use `research_frontier` with the pack's `certificate` or
`empirical_protocol` obligation kind and do not synthesize proof obligations.
Only an audited canonical transition can change either kind of frontier.

ClaimGraph schema v3 is also the single machine-readable claim-status source.
The trusted-state file stores audit provenance and binds to the exact graph
SHA-256; it is not a second frontier. Controller-authorized changes stage both
files, append a `PREPARED` record, atomically install the staged bytes, then
append `COMMITTED`. Recovery accepts only the recorded before or after digest
for every target and otherwise fails closed. The retained snapshots make the
transition reviewable and replayable without promoting a model result directly.
Canonical Markdown is never rewritten by the controller.

Status domains are intentionally separate: the pinned domain contract describes
claim status (`MathStatus` remains the compatibility API for the math wire
format), `TrustStatus` describes review state, `EvidenceLevel` describes what
was checked, and `ExecutionStatus` describes process/transport completion.

## Failure taxonomy

Failures are handled in this order:

1. local schema, bootstrap, canonical, or policy violation;
2. provider quota exhaustion (pause and preserve until the supplied reset);
3. transport, rate-limit, or transient protocol failure;
4. a server-completed turn with failed status;
5. missing or invalid structured model output;
6. role-level semantic validation failure;
7. controller or state-machine failure.

A failed job is never passed into a role parser. Original server errors,
streamed events, identifiers, retry classification, and telemetry are retained.
Role protocol failures use bounded role-local retries. Controller, canonical,
policy, and local-schema failures drain the epoch as internal failures.
Quota exhaustion is not a bounded transport retry or a mathematical failure:
the campaign pauses, official reset metadata is retained, and exact pending
work is carried forward. Token telemetry keeps total, cached input, uncached
input, cache-write input, output, and reasoning output separate; total tokens
remain the budget authority, not a proxy for research depth.

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

Falsification-first ordering, representation bridges, audit gates, route kill
gates, route novelty, task deduplication, and bounded stop conditions are
tool-level Director policy. `prompts/director.md` is only an optional stable
project-constraint overlay. The dynamic canonical snapshot has explicit
precedence over that overlay and every prior planning mirror for current
frontier, open/closed claims, and recent progress.

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

New `RUN_MANIFEST` records use schema v13 and pin the AMR version and source
digest, Python version, Git revision when available, Codex CLI version, and App
Server schema/required-protocol digests. AMR source changes fail resume closed.
A Codex CLI/schema change is accepted only when the required protocol digest is
unchanged and the compatible change is appended to the epoch events. Schema-v12
manifests remain resumable under an explicit legacy-provenance event.

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
graph; startup-frozen inputs are contextual and cannot override it either. Both
derived views carry the canonical state/planning-context fingerprints used to
build them. The capsule's 32 KiB
contract is enforced against the exact compact
UTF-8 bytes written atomically. Oversized nested values and low-priority or old
derived entries are deterministically compacted, with source, dropped, and
truncation counts retained in the capsule; high-priority frontier entries are
discarded last.

During controller shutdown, remote turn containment and locally scheduled
cancellation tasks are completed before provider transports close. Each local
job owner is then cancelled and awaited, so an App Server interrupt request
cannot outlive its controller task or surface as an unobserved Future after
stdio closes.
