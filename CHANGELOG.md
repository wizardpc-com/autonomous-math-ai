# Changelog

All notable changes to Autonomous Math AI are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- An explicit Astra research/audit/smoke profile with diagnostic-only short
  reasoning signals, preserving legacy profiles and mechanical routes.
- An offline-by-default model compatibility probe with a separate two-turn live
  opt-in, local deadline, tool-output checks, and explicit missing telemetry.
- Native research input export, evidence sealing and unaudited import using the
  existing task, policy, Frontier and external-result contracts. Imports preserve
  source evidence and cannot create audit receipts or canonical transitions.

### Fixed

- Canonical reconciliation unit coverage injects Codex capabilities, allowing
  the suite to run without the CLI while retaining fail-closed live preflight.
- Compatibility probes bind successful command events to their own thread and
  turn, reject reused sessions and disallowed turn tiers, and report cleanup
  failures without leaving a successful probe receipt.
- Native sealing rechecks the retained input-manifest digest. Import requires
  all frozen input references and checks their binding and source digests.

- Codex model capability checks now follow catalog pagination and reject
  unsupported model-level efforts before model turns. Job records distinguish
  requested routes, thread configuration observations, and turn observations.

- Strict project validation now recomputes the Audited Frontier read-only and
  rejects stale `CURRENT.json` routing when source manifests or hash-bound
  evidence have changed or disappeared, including a Frontier's project-local
  Campaign Theme source.
- Completion policies can stop operationally on explicit `BLOCKED`,
  `FALSIFIED`, or `OBLIGATION_EXHAUSTED` research outcomes without changing
  mathematical, trust, evidence, or canonical status.
- Paused routes now wait for controller-owned retry conditions without forcing
  a Director repair loop, and zero independent-exploration capacity is reflected
  in task metadata and Director context.
- Continuation budgets measure cumulative generated work instead of repeatedly
  charged input context, checkpoint chains are compacted,
  relative job artifacts resolve from the job workspace, Windows JSONL decoding
  preserves Unicode, and asset usage is bound to an explicit current-turn id set.

### Added

- Immutable schema-v14 run records now freeze campaign purpose, target and AMR
  revisions, dirty-state/input roots, Theme, Frontier-before, ClaimGraph,
  reusable-asset/representation/kill-gate snapshots, configuration, runtime,
  models, and budgets. Content-addressed terminal records append Frontier-after,
  schema-v2 deltas, exact result identities, end time, and deterministic metrics.
- Director admission, dispatch suppression, asset retrieval/use, AuditKey
  hit/miss and savings, per-job cost, and orthogonal research-outcome telemetry
  are structured events. Output Protocol v4 requires explicit used/rejected and
  cited asset accounting without changing canonical authority.
- `amr record inspect|replay-context|metrics` verifies frozen bytes and rebuilds
  the external decision context. Older run manifests remain untouched and are
  exposed through an explicitly partial legacy normalized view.

- A content-addressed Audited Frontier now rebuilds at real campaign start and
  end from ClaimGraph, trusted state, project evidence inventory, structured
  external results, exact routing audit receipts, and the pinned Campaign Theme.
  Routing suppression is explicitly separate from canonical authority.
- The shared Research Asset Registry, Representation Graph, Method Ledger, and
  deterministic AuditKey index support progressive theorem/bridge/tool/verifier
  reuse, exact-scope kill gates, audit deduplication, and minimal context bundles
  across autonomous and external research threads.
- `amr frontier rebuild|inspect|context` validates and exposes this routing-only
  coordination state. `amr run --theme` pins include/exclude scopes, method and
  dependency boundaries, combination scope, and exact obligations for the full
  campaign.
- Historical trusted-core reconciliation now stages external proof/audit
  evidence append-only and applies terminal claims, named proof obligations, or
  narrow derived subclaims only through fresh controller audit, semantic receipt,
  terminal binding, and one atomic canonical transition. Applied imports are
  crash-replayable and idempotent.
- Claim-local reconciliation drift and deterministic pre-dispatch input closure
  now reject ordinary affected-claim research before a model starts, reporting
  exact missing canonical object, bridge, source, or localizer ids.
- Validation and startup now attest mechanical sandbox capability. Production
  Windows hosts fail closed when split filesystem isolation cannot be enforced;
  finite fixed-method exact work is routed toward Experiment Runner instead.

- The Windows launcher now detects the newest real or mock campaign that still
  has budget and offers `Continue previous`. It routes unsealed epochs through
  pinned `run --resume` recovery and sealed checkpoints through `campaign
  continue`, preserves the prior execution mode, enables safe cross-epoch
  continuation, and opens the monitor on the exact resumed or reserved run ID.
- Entering `fast` at the launcher parameter prompt now creates a one-run-only
  `execution.fast_mode=true` profile and proceeds to execution. Real launches
  retain their exact project confirmation, and unsupported Fast routes fail
  closed during configuration preflight.

### Fixed

- Campaign completion-policy stops now share the finalization lifecycle: a
  terminal bounded audit reaches `COMPLETED` after in-flight audits drain,
  while fresh continuation deterministically restores already-satisfied
  operational completion without model dispatch, duplicate receipts, or a
  mathematical claim-status change.
- Empty Director plans now hold controller-owned pending or active research and
  audit work without consuming `director_no_runnable_work`; exactly one replan
  is requested after the held wave becomes idle. Truly idle empty plans retain
  their bounded fail-closed retry.
- Campaign Theme schema v2 can stop research after a bounded candidate quota,
  run only the configured independent audit attempts, and mark the campaign
  operationally complete on a terminal verdict without changing theorem status.
- Research dispatch now seals the producer task packet and every declared input
  as a hash-bound evidence closure. Auditors receive those original bytes plus
  validated ZIP member inventories, while traversal, credential-like inputs,
  missing files, and tampering fail closed without exposing producer transcripts.
- Rejected evidence for a newly derived candidate is now reconciled through a
  canonical `CANDIDATE_AUDIT_REJECTED` transition: mathematical status and proof
  obligations remain open while evidence trust becomes `REJECTED`.
- A controller-verified next-epoch continuation frontier now seals cleanly when
  a conforming Director returns no current-epoch work. The transition no longer
  depends on the Director resubmitting a task that its prompt forbids, and
  `--auto-epochs` can import the checkpoint in a fresh epoch without consuming
  Director repair retries.
- Candidate dependencies now use one explicit ClaimGraph-only namespace across
  schema, task packet, worker prompt, emit helper, and final controller
  admission. Unknown source, asset, task, or representation ids fail before the
  inbox write and are never silently removed or translated.
- Research jobs now distinguish persistent execution blockers from mathematical
  claim state, stop after one blocker repair, and cap ordinary Prover,
  Falsifier, and Explorer turns at 4/3/3 by default. Candidate validation
  rejections carry controller-bound producer identity, fingerprint, and exact
  sanitized feedback into at most one targeted repair turn.
- Per-thread limits now support `stop_after_turn`: the active model turn may
  finish, but no successor turn is started. Turn/token boundaries create a
  noncanonical next-epoch checkpoint only with a new persisted artifact and an
  explicit next question; otherwise the route pauses without copying work.
- Isolation diagnostics now distinguish a blocked collaboration tool call with
  no child thread from actual child-thread activity. Monitor status reports
  research turn bounds, continuation/candidate disposition, token-limit policy,
  next-turn suppression, and observable telemetry age without inferring hidden
  model progress.
- Cross-domain candidate event types now fail closed as ordinary candidate
  rejections instead of escaping the controller as mapping `KeyError`s. Domain
  transition lookup validates before indexing across live processing, audit
  recovery, and trusted-claim conflict checks, while research task packets list
  the active domain's allowed event types for worker submissions.
- Director plans can now explicitly mark genuinely differentiated tasks as
  `metadata.independent_exploration=true`, matching the scheduler's reserved
  independent-slot contract. If bounded Director repair still ends without a
  dispatchable job, the controller seals and checkpoints the epoch instead of
  silently polling a nonempty but non-runnable queue; resume preserves that
  isolation decision and advances the append-only Director-context generation
  rather than colliding with an existing archive filename. Legacy continuation
  tasks retain their original packet identity through checkpoint verification,
  then receive the new scheduler-only metadata in the fresh epoch.
- Director route updates now pass through a controller-owned state machine.
  `RESUME` and `RETRY` require the exact stored retry condition and independent
  controller evidence that it is satisfied; existing route representations are
  preserved across status changes.
- Director context schema v3 separates current-epoch research from checkpointed
  next-epoch continuations. Selecting only deferred work now seals a clean epoch
  boundary that `--auto-epochs` can continue, while legacy combined frontiers
  remain readable without rewriting historical checkpoints.
- Windows App Server roles strictly validate `pyvenv.cfg` and, when its base
  interpreter is external, receive only that interpreter installation root as
  an additional read-only runtime path. Broad, missing, noncanonical,
  out-of-home, and credential-shaped paths fail closed. Candidate-event
  commands continue to invoke the exact controller Python executable instead
  of relying on a `python` PATH alias. The CLI now also supports `amr
  --version` for launcher provenance checks.
- Canonical-transition schema v2 requires explicit preconditions for every new
  record. A read-only compatibility adapter accepts pre-v1.1 schema-v1 records
  only when their exact legacy fields, snapshots, trusted-state binding,
  authorization, and legacy transaction digest agree; it never admits semantic
  trust state. Declarative validation now initializes its transition reader
  before reconciliation, and zero-model dry-runs cannot persist semantic opt-in.
- Canonical startup records the workspace HEAD with a command-scoped exact
  `safe.directory` value, so read-only revision freezing works under an isolated
  Windows sandbox SID without changing global or repository Git configuration.

- Semantic alignment verification is now controller-owned and candidate-bound.
  Persistent opt-in, append-only contract heads, evidence/audit/validator scope
  receipts, controller-owned independent execution identities, exact
  terminal-state/candidate/receipt bindings for each positive promotion,
  globally disjoint ClaimGraph claim/proof-obligation id namespaces,
  ClaimGraph-normalized dependency closure, assignment-time audit authority
  contexts, validation-authority freshness,
  and fully verified `PREPARED`/`COMMITTED` canonical authorizations
  enforce **No unverified bridge into trusted final claims** across live,
  startup, resume, recovery, import, direct transition, and finalization paths.
  Structured `core_terms` checks no longer claim to discover arbitrary terms in
  proof or free text.

- Controller-owned App Server roles no longer inherit ambient `AGENTS.md`,
  goals, hooks, memories, plugins, apps, browser/computer tools, dynamic skill
  discovery, or multi-agent tools. Standalone MCP servers are disabled by id
  and the post-start inventory must expose no MCP tools or resources. Thread
  and turn execution use an attested controller-owned permission profile with
  exact writable roots and no network. Login shells are rejected; the
  allowlisted non-login environment removes auth-like variables and denies the
  Codex executable while retaining the pinned AMR Python/platform runtime and
  command contract. This boundary
  does not claim that the App Server's own `CODEX_HOME` configuration files are
  unreadable on current Windows Codex runtimes.
- Controller-owned thread and turn requests explicitly disable multi-agent
  delegation. Current and legacy collaboration events are intercepted in the
  App Server client, the parent turn is interrupted, and the run fails closed.
- Mechanical workers classify an unenforceable Windows split-filesystem sandbox
  as a deterministic, non-retryable policy failure, without provider fallback
  or repeated attempts.
- The monitor distinguishes a recoverable nonzero local-command exit from an
  actual App Server tool-call failure instead of reporting both as the same
  red tool error.
- Role jobs now receive only controller-materialized, digest-verified inputs
  instead of a static project-root read grant; credential-shaped requested
  paths are rejected, and unreadable `PATH` entries fail closed.
- App Server permission-profile and MCP-inventory pagination rejects repeated
  cursors and excessive page counts instead of risking an unbounded startup.
- Controller-authorized ClaimGraph transitions update an explicitly marked
  Markdown machine-state block in the same crash-replayable transaction while
  preserving surrounding prose.

### Added

- Optional domain-independent semantic alignment contracts with versioned goal
  hashes, canonical term/object registries, typed representation bridge graphs,
  orthogonal semantic status, and legacy `UNREVIEWED` compatibility.
- Selectable, strictly validated bundled policy packs for `math-research`,
  `certified-computational-research`, and `empirical-research`, with pinned
  domain contracts, role resources, audit requirements, and fail-closed resume.
- A deterministic, non-LLM Experiment Runner API and `amr experiment` CLI with
  strict frozen-input manifests, content-addressed raw outputs, append-only
  per-case evidence, verified resume/checkpoint recovery, and an injected
  Docker adapter seam.
- Read-only deterministic evidence receipts, v2 evidence-attempt candidate
  identities, executable/environment provenance, scratch-tree subprocess
  execution, and domain-aware real-provider smoke lifecycles.
- Neutral certified-checker and frozen empirical-protocol fixtures and their
  documentation-only manifest schema.

- Configuration schema v12 with one explicit `execution.fast_mode` switch for
  all controller-owned main roles. Fast remains off by default; mechanical
  workers remain pinned to a null tier.

- Artifact-finalization progress events, structured operator-interrupt
  terminals, and `campaign continue --auto-epochs`.

- Attempt-scoped crash recovery events, run-manifest runtime provenance, and
  unattended `--resume --auto-epochs` continuation.

- Configuration schema v11 with an explicit provider-execution fallback policy;
  older configs migrate in memory.
- Configuration schema v10 with per-role research turn bounds and normalized
  uncached-input token telemetry; v7/v8/v9 projects migrate in memory.
- Configuration schema v9 with project campaign defaults, built-in Codex
  profile, migrations, and redacted `amr config validate/explain/summary`.
- Provider capability declarations, per-role provider/model routes, an optional
  OpenAI-compatible adapter, and third-party adapter entry points.
- `amr init --project-id/--final-claim-id`, a complete neutral scaffold,
  initialization checklist, and zero-model `amr validate --strict`.
- Separate main/mechanical token and cost governors, unbounded static mechanical
  scheduling with resource/queue/rate backpressure, and provider/cost telemetry.
- Repository-local Codex discovery entry for the bundled `math-research`
  policy pack.
- `amr detect-tools --project-root` for writing a tool inventory into the
  selected research project rather than the harness checkout.
- Conjecture-neutral catalog, evidence, lifecycle, policy, and mechanical
  worker regression suites migrated from the former monorepo integration.
- Controller-owned same-thread multi-turn research jobs, turn ownership
  detection, canonical `ClaimGraph` proof obligations/frontiers, and a
  diagnostic-only reasoning health monitor.

### Changed

- `math-research` remains the default and preserves legacy math ClaimGraph and
  schema-v5 pinned-policy resume behavior; the `autonomous-math-ai`
  distribution, `autonomous_math_research` namespace, and `amr` command names
  are unchanged. Empirical outcomes never become mathematical `PROVED` state.

- Fast requests now pin `serviceTier=fast` on thread and every turn, while
  accepting an observed `priority` alias only for an explicit Fast request.
  Runtime schema preflight rechecks the required App Server fields every epoch,
  resumed auto-epoch campaigns retain their pinned Fast selection, and
  pre-response transport/quota failures preserve their original cause.

- New configurations default campaigns to five hours; explicit project,
  profile, and CLI duration overrides remain authoritative.

- Incremental Director turns now receive a sub-4-KiB routing envelope instead
  of inline snapshots or transcripts. Complete current context is stored in a
  digest-bound external archive, `compact_snapshot.json` is a bounded summary,
  and prompts at or above 10 KiB fail before App Server thread creation.

- Sealed checkpoint provenance now follows the epoch's last audited canonical
  transition instead of retaining its startup ClaimGraph/trusted-state hashes.
  Strictly matched v1 checkpoints are reconciled append-only; unrelated
  canonical drift with an open audit frontier still fails closed.

- `RUN_STOPPED` is now committed only after report, immutable-file index,
  semantic index, outcome, and run summary generation. Automatic epoch
  continuation requires that durable artifact commit; monitors remain in a
  visible finalizing state until it completes and drain buffered Windows SGR
  mouse input before returning to PowerShell.

- Resume now hydrates campaign identity before controller construction,
  rebuilds derived frontier state from append-only evidence, preserves the
  original failure and absolute deadline, and never seals or rewrites planning
  state after a pre-recovery failure. Monitor exits retain actionable failure
  reasons and artifact paths.

- Mechanical Spark execution failures now continue once on the configured Luna
  medium fallback, while policy, schema, protocol, and artifact failures remain
  terminal; unknown mechanical token telemetry is shown as a lower bound.
- Codex App Server remains the default provider; API transports are opt-in.
- A generic Windows launcher and `amr launcher` discover project manifests from
  a remembered workspace, keep persistent settings in each project's config,
  open a run-pinned monitor window, and provide validated disposable overrides
  for dry-run, mock, and explicitly confirmed real runs.
- Director snapshots now expose controller-owned representation compatibility
  and latest route state. Plans whose tasks all fail semantic admission receive
  one bounded repair turn, then pause cleanly instead of reaching an idle
  controller invariant failure; route-only updates do not count as runnable work.
- New-project token defaults are 500 million for main roles and 1.5 billion for
  mechanical workers; existing schema-v7 configs retain their pinned limits.
- Mechanical delegation is preferred for finite, mechanically checkable work
  while remaining one-shot, nonrecursive, and outside canonical trust.
- Top-level delegation enforcement now parses actual shell command positions,
  so read-only process inspection and text searches may mention Codex while
  direct, wrapped, or recursive Codex execution still fails closed.
- The harness now lives in its own source checkout and research repositories
  integrate exclusively through the installed `amr` CLI and project manifest.
- App Server active goals are no longer armed for autonomous jobs; token limits
  remain controller-enforced, unowned native continuations fail closed, and
  only controller-verified canonical progress resets stagnation.
- The built-in research continuation bound is now 12 turns without changing
  scheduler concurrency. Turn-bound tasks are digest-bound as noncanonical
  checkpoints and carried into the next epoch instead of being classified as
  route failures or stagnation progress.
- A first `BLOCKED` research result now receives a same-thread repair turn;
  only a post-repair controller-actionable blocker may pause the route, with no
  mathematical or trust effect. Turn/token boundaries record explicit current,
  completed-evidence, and next-obligation checkpoint fields.
- Provider usage exhaustion is classified as `provider_quota_exhausted`,
  preserves official reset hints, pauses the campaign, and requeues exact work
  without consuming retry, route-failure, or stagnation state.
- Director task admission now resolves portable `project://`, `campaign://`,
  and `epoch://` required-file references, rechecks them before dispatch, and
  gives workers an internal readable-path mapping without weakening durable
  evidence references. Task dependencies are explicitly ClaimGraph claim ids.
- Director task admission rejects attempts to bind one stable task id to
  different task content.
- Top-level job workspaces and mechanical broker control files are isolated by
  controller job id, with a final active-task-id guard against concurrent
  duplicate dispatch.
- `amr campaign continue` now forwards the complete internal run namespace,
  including the fresh-epoch run id default, instead of failing before preflight.
- App Server completion correlation no longer leaves a thread-level copy of a
  delivered turn that can be consumed by the next same-thread continuation.
  Response/stream id aliases, repeated start notifications, request failure,
  cancellation, and unknown completion ids now have explicit tested handling.
- `CORE_CAPSULE` size enforcement now validates and atomically writes the same
  compact UTF-8 bytes, bounds oversized nested values and active-task sets,
  records compaction counts, and preserves the highest-priority frontier.
- Controller shutdown now reaps scheduled cancellation work and cancelled job
  owners before closing provider transports, preventing orphaned App Server
  interrupt futures during internal-failure cleanup.

### Planned

- Additional deterministic verifier integrations and third-party mechanical
  runner adapters.

## [0.2.0] - 2026-08-19

### Added

- Conjecture-neutral `amr` command-line interface and installable
  `autonomous_math_research` namespace.
- Declarative project manifests and a neutral starter template.
- Monotone campaign, epoch, and job lifecycle with crash recovery.
- Output Protocol v2 and a shared Structured Outputs preflight boundary.
- Immutable candidate bundles, audit leases, representation contracts, and a
  fail-closed canonical gate.
- Append-only steering, external asset ingest, route ledger, research map, and
  core capsule support.
- Controller-brokered, one-shot mechanical workers with bounded fallback.
- Windows and Ubuntu zero-model CI.

### Changed

- Public distribution renamed to `autonomous-math-ai`; the Python namespace
  and `amr` CLI remain stable.

[Unreleased]: https://github.com/wizardpc-com/autonomous-math-ai/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/wizardpc-com/autonomous-math-ai/releases/tag/v0.2.0
