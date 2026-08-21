# Changelog

All notable changes to Autonomous Math AI are documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
