# Quickstart

This guide starts with zero-model validation. It does not require a live model
turn, an API key, or access to a research repository.

## 1. Install

Autonomous Math AI requires Python 3.11 or later:

```console
python -m pip install autonomous-math-ai
amr --help
```

From a source checkout, use `python -m pip install .` instead.

### Optional Windows double-click entry

A source checkout includes [`../amr-launcher.cmd`](../amr-launcher.cmd). It is a
generic bootstrap: do not add project paths or research settings to it. On first
use, enter a workspace root. The launcher remembers only this root in the user
LOCALAPPDATA directory and scans Git-visible `autonomous/project.json` files on
every start. You can also use `amr launcher` from an installed package.

The selected project's manifest points to its single persistent
`autonomous/config.yaml`. Before dry-run, mock, or real execution, the launcher
shows a redacted summary. Common numbered edits and allowed
`dotted.path=JSON-value` inputs create a temporary profile; they never rewrite
the project. A real run requires the exact phrase `RUN <project_id>`. Each run
action also opens a separate monitor window pinned to the newly allocated run ID.

## 2. Create a neutral research target

```console
amr init ./research-target --project-id research-target --final-claim-id C_ROOT
```

The generated directory contains:

```text
research-target/
├── README.md
├── AGENTS.md
├── INITIALIZATION_CHECKLIST.md
├── autonomous/
│   ├── project.json
│   ├── config.yaml
│   ├── prompts/
│   └── state/
├── claims/
├── state/
├── proofs/
├── tasks/
├── experiments/
├── certificates/
├── audit/
├── sources/
├── conversations/
└── artifacts/
```

Replace the neutral claim and prompts with your exact mathematical statement,
domain, quantifiers, assumptions, and evidence boundaries. Keep generated wire
schemas and controller policy in the installed package; a target may narrow
policy but must not relax the trust protocol.

## 3. Validate without a model

```console
amr validate --project ./research-target
amr config validate --project ./research-target
amr config explain --project ./research-target
amr config summary --project ./research-target
amr run --project ./research-target --dry-run
```

Validation checks the project manifest, paths, configuration, claim graph,
canonical guard, provider/model routes, effort capabilities, secret references,
no-fast/no-priority policy, bundled schemas, and protocol
compatibility. The result includes `model_turns_started: 0`.

The new scaffold intentionally fails strict validation until the exact
mathematical placeholders are replaced consistently in `claims/CLAIMS.md` and
the claim graph:

```console
amr validate --project ./research-target --strict
```

## 4. Run the deterministic mock lifecycle

```console
amr run --project ./research-target --mock --hours 0.01
```

The mock exercises Director, research, candidate, audit, reporting, and
finalization paths with synthetic responses and synthetic telemetry. A mock
verdict is never mathematical evidence and does not authorize changes to a
shared canonical state outside its isolated run.

Inspect the result:

```console
amr status --project ./research-target --run latest
amr catalog --project ./research-target
```

## 5. Configure a live campaign

Before any live run:

1. pin the exact final claim and protected canonical inputs;
2. review model names, reasoning effort, and null service tiers;
3. set campaign duration, epoch duration, global token budget, and independent
   Director/research/audit/mechanical concurrency caps;
4. verify worker tool allowlists, filesystem permissions, and network policy;
5. run `amr validate` again;
6. obtain explicit operator approval for model usage and cost.

Codex App Server is the default provider and reuses the operator's existing
local login. An API is optional; it is used only when a role/profile explicitly
selects an OpenAI-compatible or plugin provider. Do not copy credentials into
configuration, prompts, events, or artifacts—store only an environment variable
name, system-credential name, or provider-profile name.

A live campaign is started by omitting `--mock` and `--dry-run`:

```console
amr run --project ./research-target --hours 12 --epoch-hours 2
```

To run fresh sealed epochs unattended until the same 12-hour campaign budget is
exhausted:

```console
amr run --project ./research-target --hours 12 --epoch-hours 2 --auto-epochs
```

Automatic continuation occurs only after a clean epoch-time boundary. Quota
pause, unsafe canonical state, internal failure, operator stop, or mathematical
completion returns control instead of starting another epoch.

This command is intentionally shown but should not be run until the preceding
checks are complete.

## Recovery and continuation

- `--resume` is only for the same crashed epoch.
- `amr campaign continue` creates a new epoch from a sealed checkpoint.
- `--auto-epochs` repeatedly performs that fresh-epoch boundary during one
  invocation without changing the checkpoint/seal model.
- A paused, sealed epoch must be continued, not resumed; validate the updated
  harness with a separate new dry run before continuing the real campaign.
- Budget or epoch drain stops new dispatch and waits for healthy in-flight work.
- Failed runs remain immutable evidence; recovery imports into a new append-only
  context rather than rewriting the old run.

## Human steering and assets

Use `amr steer` for bounded append-only notes, priorities, route pauses/resumes,
audit requests, or stop-after-epoch instructions. Use `amr ingest` to copy one
explicit local file into content-addressed campaign storage.

Neither command can set claim trust, declare representation compatibility, or
bypass independent audit.
