# Autonomous Math AI

**Autonomous Math AI — Auditable AI orchestration for mathematical research**

[![CI](https://github.com/wizardpc-com/autonomous-math-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/wizardpc-com/autonomous-math-ai/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Autonomous Math AI is a research-topic-neutral Python harness for long-running,
model-assisted mathematical, certified-computational, and empirical research.
It coordinates research, adversarial falsification, independent audit, durable
evidence, recovery, and controlled mechanical delegation without treating model
output as trusted truth.

> Autonomous Math AI is not a proof oracle. A model response, a successful
> computation, or a failed counterexample search does not automatically become
> a proof. Trusted state changes only through explicit deterministic checks,
> fresh audit, and the canonical gate.

[简体中文](README.zh-CN.md) · [Quickstart](docs/quickstart.md) ·
[Configuration](docs/configuration.md) · [Providers](docs/providers.md) ·
[Architecture](docs/architecture.md) · [Trust model](docs/trust-model.md)

## Core guarantees

- **Falsification-first:** bounded exact counterexample searches and cheap
  consistency checks are preferred before expensive proof attempts.
- **Independent audit:** candidates are audited from immutable task packets and
  evidence bundles, not accepted from a producer's self-assessment.
- **Append-only evidence:** events, artifacts, candidate records, route history,
  and steering inputs remain inspectable after failures or restarts.
- **Crash recovery:** campaigns are divided into independently sealable epochs;
  pending work and audit leases can be reconstructed without rewriting history.
- **Mechanical worker isolation:** bounded one-shot workers receive minimal task
  packets, cannot recurse, cannot choose research strategy, and cannot modify
  canonical state.
- **Canonical gate:** unreviewed model output never updates trusted claims,
  proofs, or project state.
- **Representation safety:** branch, localization, saturation, normalization,
  content, exceptional factors, and combination scope are recorded so that
  incompatible mathematical representations fail closed.
- **Protocol preflight:** mock and live App Server execution use the same
  Structured Outputs compatibility checks.
- **Controller-owned continuation:** a hard research task can use bounded,
  explicit same-thread turns; a completed turn or self-reported proof does not
  by itself complete the logical task. A first `BLOCKED` report receives a
  repair turn; turn/token boundaries checkpoint the task for the next epoch.
  Provider quota exhaustion pauses and preserves work without counting as
  mathematical failure or stagnation.
- **Separated routing and authority truth:** the rebuildable Audited Frontier
  decides what may be researched now, while `ClaimGraph` and controller receipts
  remain the only authority truth. An exact independently audited external result
  may suppress duplicate routing without promoting a theorem.
- **Shared research memory:** content-addressed Asset, Representation, Method,
  and Audit registries provide progressive reuse across autonomous, web, manual,
  and Codex research threads without treating chat memory as state.
- **Pinned domain semantics:** a run selects one strictly validated bundled
  policy pack, snapshots its role policy and domain contract, and fails closed
  on missing, modified, or cross-domain state.
- **Semantic alignment:** an optional project contract freezes the final goal,
  registers canonical terms and objects, and requires a controller-owned,
  candidate-bound verification receipt for every bridge in a trusted final
  claim's dependency closure. Never-opted-in projects remain runnable as
  `UNREVIEWED`.

## Installation

Autonomous Math AI requires Python 3.11 or later.

```console
python -m pip install autonomous-math-ai
```

For a source checkout:

```console
python -m pip install .
```

The distribution name is `autonomous-math-ai`, the import namespace remains
`autonomous_math_research`, and the command-line entry point is `amr`.

## Repository separation

This repository contains only the generic harness, neutral templates, policy
resources, and tests. Research statements, project prompts, claim/task
graphs, experiments, audits, runs, outcomes, and artifacts belong in a separate
research repository selected with `--project`.

The optional `.agents/skills/math-research/` entry is only a Codex discovery
adapter. Its links resolve to the same packaged policy resources; it does not
carry a second engine or any research-project state.

## Quick start

Create and validate a neutral project without starting a model:

```console
amr init ./research-target --project-id research-target --final-claim-id C_ROOT
amr validate --project ./research-target
amr config validate --project ./research-target
amr config explain --project ./research-target
amr run --project ./research-target --dry-run
```

`math-research` is the compatible default. New projects may instead select one
of the two Phase 1 non-math packs:

```console
amr init ./checker-target --domain certified-computational-research
amr init ./study-target --domain empirical-research
```

See [research domains and policy packs](docs/research-domains.md) for the exact
statuses and audit gates. Empirical `CONFIRMED` or `REPLICATED` state is never a
mathematical `PROVED` state.

Initialization deliberately leaves marked research placeholders. Replace
them and complete the checklist before `amr validate --strict` can pass. These
validation/config commands never start a provider.

Fast mode is off by default. Set `execution.fast_mode=true` in the project
configuration or an explicit profile to pin Fast for every controller-owned
main role. AMR requests `fast` and accepts a returned `priority` only as that
request's observed alias; mechanical workers always keep a null tier.

Exercise the full controller lifecycle with deterministic mock agents:

```console
amr run --project ./research-target --mock --hours 0.01
amr detect-tools --project-root ./research-target
```

These commands are zero-model checks. Codex App Server is the built-in default
provider and reuses the operator's Codex login. APIs are optional and require an
explicit OpenAI-compatible or plugin provider selection.
See [the quickstart](docs/quickstart.md) before enabling live execution.

Reviewed historical proof and audit artifacts can be staged without changing
ClaimGraph, inspected, and then sent through one fresh controller audit:

```console
amr reconcile stage --project ./research-target --bundle reconciliation.json
amr reconcile inspect --project ./research-target
amr reconcile apply --project ./research-target --id reconciliation-...
```

The second successful `apply` is a zero-model no-op. See
[Semantic Alignment](docs/semantic-alignment.md) for the bundle and trust gates.

External Result Audit artifacts and reusable research assets use the separate
noncanonical coordination path:

```console
amr frontier rebuild --project ./research-target --theme autonomous/research_memory/themes/example.json
amr frontier inspect --project ./research-target
amr frontier context --project ./research-target --claim C_ROOT --scope C_ROOT::EXAMPLE-SCOPE
```

Every real campaign performs the same reconciliation before model startup and
again while sealing its final delta. See [Audited Frontier and shared research
memory](docs/research-memory.md). New epochs also produce [immutable research
records and evaluation telemetry](docs/research-records.md); use `--purpose` to
separate development, natural research, and frozen evaluation data.

### Windows one-file launcher

Double-click [`amr-launcher.cmd`](amr-launcher.cmd), or run `amr launcher` after
installation. The generic launcher never contains a project name, project path,
or research configuration. On first use it asks for a workspace root and stores
only that choice in `%LOCALAPPDATA%\autonomous-math-ai\launcher.json`; every
launch rescans Git-visible `autonomous/project.json` manifests. The bootstrap
reuses the installed harness by default. Set `AMR_REFRESH_HARNESS=1` only after
every process using the shared virtual environment has stopped; refresh fails
closed while that environment is in use.

After choosing a project, the menu offers validation, strict validation,
redacted configuration, dry-run, mock, and a separately confirmed real run.
When the selected project has an unfinished real or mock campaign with remaining
budget, the menu also identifies the newest one and offers `Continue previous`.
The launcher resumes an unsealed epoch in place or creates a new epoch from a
sealed checkpoint as required; real continuation requires an exact confirmation.
Persistent settings belong only in the manifest-selected
`autonomous/config.yaml`. Menu edits are validated temporary overrides and are
removed after the command. Starting dry-run, mock, or real also opens a separate
monitor window pinned to that exact run. Keep credentials out of project files.

## Project contract

Each research target owns a declarative `autonomous/project.json`, research
prompts, a claim graph, trusted-state metadata, and protected canonical inputs.
The installed package owns wire schemas, lifecycle rules, scheduling, storage,
recovery, audit leases, and policy enforcement.

Before every new run, AMR freezes the manifest-declared canonical inputs with
path, SHA-256, and available Git revision, then rebuilds the run-local Director
snapshot, `CORE_CAPSULE`, and `RESEARCH_MAP`. The live `ClaimGraph` is the sole
domain-status and research-frontier authority; frozen Markdown is contextual
input and cannot override it. Optional machine-readable Markdown state blocks
must match the graph byte-for-byte or startup fails closed. Unsafe drift stops
before a model turn; startup refresh never rewrites canonical project files.

Audit-gated claim changes atomically commit the ClaimGraph and its digest-bound
trusted metadata. Each transition keeps append-only authorization plus before
and after snapshots for audit and crash replay. When a canonical Markdown file
contains the explicit AMR machine-state markers, the same authorized transition
replaces only that generated block; bytes outside the markers are preserved.
Unmarked `CLAIMS.md` and `PROGRESS.md` remain untouched.

`amr init` generates a neutral example that can live in any directory. The
engine does not require this source repository, a particular parent directory,
or a conjecture-specific policy file.

## Runtime model

Long-running work is organized as:

```text
campaign
└── epoch
    ├── director job
    ├── research jobs
    ├── audit jobs
    └── isolated mechanical subtasks
```

Epoch transitions are monotone. On budget exhaustion, operator stop, or
internal failure, the controller stops dispatching and drains healthy in-flight
work before sealing. It does not turn an internal failure into queue exhaustion
or silently discard pending candidates.

`amr run --auto-epochs` starts a fresh sealed epoch after each ordinary epoch
time boundary or controller-attested next-epoch continuation frontier until the
campaign duration is exhausted. Every epoch repeats canonical refresh and
planning reconstruction. Quota pause, unsafe drift, internal failure, operator
stop, or a resolved final claim stops the automatic loop. `amr campaign
continue` remains the manual continuation path and accepts `--auto-epochs` when
later clean boundaries should continue unattended.

After the epoch checkpoint is sealed, the monitor remains attached while the
report, immutable-file index, semantic index, outcome, and run summary are
written. `RUN_STOPPED` is committed only after that derived artifact set is
durable. Ctrl+C records a structured interruption and cannot enter the next
automatic epoch.

The Director's falsification, representation-bridge, audit, kill-gate, Theme,
asset-reuse, and route novelty rules live in AMR. A project `director.md` is an
optional stable constraints overlay, not a frontier source. The Director receives
the current Audited Frontier, pinned Campaign Theme, minimal relevant Asset
bundle, and controller-owned compatibility view. Routing-only external bridges
never authorize canonical promotion. If every proposed task fails semantic
admission, route bookkeeping is retained and the controller requests one bounded
repair plan. A second plan with no runnable research or audit work pauses the
campaign cleanly; route updates alone never keep an empty execution queue alive.

Projects that opt into `autonomous/semantics.json` receive the additional
domain-independent **No unverified bridge into trusted final claims** gate.
Opt-in and bridge verification are persisted through the canonical trusted
journal, and the same frozen declaration is materialized into Director,
research, and audit workspaces. See
[semantic alignment and Semantic Migration v1](docs/semantic-alignment.md).

## Controlled mechanical delegation

Research and audit roles may request simple, finite, mechanically checkable
work through the controller-managed broker. The default route is
`gpt-5.3-codex-spark` with high reasoning effort and a null service tier. Only
a provider execution failure—model unavailable, quota, transport, or timeout—
permits one fallback to `gpt-5.6-luna` with medium effort and a null service
tier. Policy, permission, eligibility, schema, protocol, and artifact failures
remain terminal. Transient failures do not poison model availability, and no
fallback uses a parent model, fast, or priority service.

Mechanical output is execution evidence only. The parent research role must
interpret it, and a strong independent auditor remains responsible for any
verdict.

The default static mechanical seat cap is unbounded. This removes only a fixed
seat count: dispatch still obeys a separate 1.5-billion-token default budget,
cost limits, CPU/resource capacity, provider rate limits, a bounded queue,
dispatch batches, timeouts, and operator stop. The main-role default is 500
million tokens; the monitor displays both budgets separately and marks token
usage as a lower bound when any mechanical attempt lacks complete telemetry.

## Safety and scope

- No command reads or stores authentication secrets as research artifacts.
- Local asset ingest rejects escaping symbolic links and records portable,
  content-addressed references.
- Human steering is append-only and cannot directly set trust, inject arbitrary
  model work, or declare representations compatible.
- Unknown token telemetry remains unknown; it is never reported as a confirmed
  zero.
- Dry runs and mock runs remain distinct from active, failed, or completed live
  campaigns.

## Documentation

- [Quickstart](docs/quickstart.md)
- [Configuration and profiles](docs/configuration.md)
- [Provider adapters](docs/providers.md)
- [Architecture](docs/architecture.md)
- [Trust model](docs/trust-model.md)
- [Immutable research records and evaluation telemetry](docs/research-records.md)
- [Research domains and policy packs](docs/research-domains.md)
- [Semantic alignment and representation bridges](docs/semantic-alignment.md)
- [Deterministic Experiment Runner](docs/experiment-runner.md)
- [Project manifest](docs/project-manifest.md)
- [Validation guide](docs/release-validation.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Development status

The project is alpha software. Its core design is conservative: protocol,
policy, canonical-state, and local-schema violations fail closed. Users should
still review project prompts, budgets, permissions, and mathematical evidence
before any live campaign.

## License

Autonomous Math AI is released under the [MIT License](LICENSE).
