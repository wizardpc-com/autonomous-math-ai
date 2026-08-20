# Autonomous Math AI

**Autonomous Math AI — Auditable AI orchestration for mathematical research**

[![CI](https://github.com/wizardpc-com/autonomous-math-ai/actions/workflows/tests.yml/badge.svg)](https://github.com/wizardpc-com/autonomous-math-ai/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Autonomous Math AI is a conjecture-neutral Python harness for long-running,
model-assisted mathematical research. It coordinates research, adversarial
falsification, independent audit, durable evidence, recovery, and controlled
mechanical delegation without treating model output as mathematical truth.

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
resources, and tests. Mathematical statements, project prompts, claim/task
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

Initialization deliberately leaves marked mathematical placeholders. Replace
them and complete the checklist before `amr validate --strict` can pass. These
validation/config commands never start a provider.

Exercise the full controller lifecycle with deterministic mock agents:

```console
amr run --project ./research-target --mock --hours 0.01
amr detect-tools --project-root ./research-target
```

These commands are zero-model checks. Codex App Server is the built-in default
provider and reuses the operator's Codex login. APIs are optional and require an
explicit OpenAI-compatible or plugin provider selection.
See [the quickstart](docs/quickstart.md) before enabling live execution.

### Windows one-file launcher

Double-click [`amr-launcher.cmd`](amr-launcher.cmd), or run `amr launcher` after
installation. The generic launcher never contains a project name, project path,
or research configuration. On first use it asks for a workspace root and stores
only that choice in `%LOCALAPPDATA%\autonomous-math-ai\launcher.json`; every
launch rescans Git-visible `autonomous/project.json` manifests.

After choosing a project, the menu offers validation, strict validation,
redacted configuration, dry-run, mock, and a separately confirmed real run.
Persistent settings belong only in the manifest-selected
`autonomous/config.yaml`. Menu edits are validated temporary overrides and are
removed after the command. Starting dry-run, mock, or real also opens a separate
monitor window pinned to that exact run. Keep credentials out of project files.

## Project contract

Each research target owns a declarative `autonomous/project.json`, mathematical
prompts, a claim graph, trusted-state metadata, and protected canonical inputs.
The installed package owns wire schemas, lifecycle rules, scheduling, storage,
recovery, audit leases, and policy enforcement.

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

The Director receives a controller-owned compatibility view for claim
representations and audited bridges. If every proposed task fails semantic
admission, route bookkeeping is retained and the controller requests one bounded
repair plan. A second plan with no runnable research or audit work pauses the
campaign cleanly; route updates alone never keep an empty execution queue alive.

## Controlled mechanical delegation

Research and audit roles may request simple, finite, mechanically checkable
work through the controller-managed broker. The default route is
`gpt-5.3-codex-spark` with high reasoning effort and a null service tier. Only
an explicit permanent unavailable/access-denied result permits one fallback to
`gpt-5.6-luna` with medium effort and a null service tier. Transient failures do
not poison model availability, and no fallback uses a parent model, fast, or
priority service.

Mechanical output is execution evidence only. The parent research role must
interpret it, and a strong independent auditor remains responsible for any
verdict.

The default static mechanical seat cap is unbounded. This removes only a fixed
seat count: dispatch still obeys a separate 1.5-billion-token default budget,
cost limits, CPU/resource capacity, provider rate limits, a bounded queue,
dispatch batches, timeouts, and operator stop. The main-role default is 500
million tokens; the monitor displays both budgets separately.

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
