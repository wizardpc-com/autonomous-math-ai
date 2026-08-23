# Deterministic Experiment Runner

`autonomous_math_research.experiment` provides a Phase 1 runner for frozen,
non-LLM experiment batches. It records raw execution evidence; it does not
interpret stdout, create a candidate event, pass an audit, or change a claim.

The runner is available through both the Python API and the `amr experiment`
CLI. The existing `amr new-experiment` command remains the older math-policy
experiment-record helper; it is not this batch runner.

## Strict manifest

`ExperimentManifest.load(project_root, path)` accepts schema version 1 JSON with
exactly these top-level fields:

```json
{
  "schema_version": 1,
  "experiment_id": "toy-check",
  "protocol_version": "frozen-v1",
  "adapter": {"kind": "subprocess", "config": {}},
  "timeout_seconds": 10,
  "inputs": [
    {"path": "checker.py", "sha256": "<lowercase SHA-256>"}
  ],
  "config": {},
  "versions": {"python": "3.11-or-later"},
  "resource_metadata": {"worker_slots": 1},
  "cost_metadata": {"billing": "none", "llm_budget": 0},
  "cases": [
    {"case_id": "check", "argv": ["python", "checker.py"], "cwd": "."}
  ]
}
```

The loader rejects unknown or missing fields, duplicate JSON keys, non-finite
numbers, duplicate case IDs or input paths, invalid identifiers, and unsupported
adapters. Inputs and working directories use normalized project-relative POSIX
paths. Inputs must be regular, non-symbolic files inside the project and match
their declared SHA-256 before execution. `subprocess` requires an empty adapter
configuration. `versions`, `resource_metadata`, and `cost_metadata` are
caller-declared provenance; the Phase 1 runner records them but does not discover
an environment, reserve resources, or enforce billing from those fields.

[`examples/experiment-runner/manifest.schema.json`](examples/experiment-runner/manifest.schema.json)
is a documentation companion for editor validation. The runtime does not load
that file and additionally enforces filesystem containment, input digests, and
duplicate-key checks; generated and recovered run records must also use their
canonical encoding. JSON Schema alone does not cover those checks. Do not add a
`$schema` field to a run manifest: the runtime's exact field set would reject it.

## Run and output

Validate, execute, or resume a frozen batch from the CLI:

```console
amr experiment validate --project ./experiment-runner-example --manifest certified/manifest.json
amr experiment run --project ./experiment-runner-example --manifest certified/manifest.json
amr experiment run --project ./experiment-runner-example --manifest certified/manifest.json --resume
```

The equivalent Python API is:

```python
from pathlib import Path

from autonomous_math_research.experiment import ExperimentRunner

project = Path("./experiment-runner-example").resolve()
summary = ExperimentRunner(project).run(project / "certified/manifest.json")
print(summary.run_id, summary.root)
```

The deterministic experiment fingerprint covers the normalized manifest,
config hash, declared versions, protocol version, and verified input
provenance. Output is written below the project's manifest-selected runtime
root, or `autonomous/experiments/` when no project manifest exists:

```text
autonomous/experiments/run-<experiment-fingerprint>/
├── RUN_MANIFEST.json
├── RAW_RESULTS.jsonl
├── CHECKPOINT.json
└── cases/
    └── case-<case-fingerprint>/
        ├── RESULT.json
        ├── stdout.bin
        └── stderr.bin
```

`RUN_MANIFEST.json` declares `llm_execution_allowed: false` and freezes the
manifest and provenance. Each case uses `shell: false`; stdout and stderr remain
uninterpreted bytes with recorded size and SHA-256. `RESULT.json` always carries
`research_result: {"status": "UNINTERPRETED"}`. A nonzero process exit is raw
execution output, while launch errors, timeouts, adapter failures, and input
mutation are recorded separately as infrastructure failures.

Case directories and result/artifact files are created exclusively. The JSONL
ledger is append-only, ordered by declared case order, flushed after every
record, and digest-binds each result and raw stream. The checkpoint is derived
state: it records the verified terminal prefix and ledger digest and may be
rebuilt without changing raw evidence.

## Resume and recovery

The run ID is content-addressed, so starting the same batch again without
`resume=True` raises `FileExistsError`:

```python
summary = ExperimentRunner(project).run(
    project / "certified/manifest.json",
    resume=True,
)
```

Resume revalidates the frozen run manifest, ledger encoding and order, case
fingerprints, result records, stdout/stderr digests, and frozen inputs before
continuing. Completed cases are never rerun or overwritten. If a durable case
result exists but only the ledger tail was lost, resume verifies it and appends
the missing ledger entry. A missing or stale checkpoint is rebuilt from the
verified ledger. Modified raw evidence, a partial ledger record, an unexpected
case directory, or changed frozen input fails closed.

## Docker seam

Schema v1 accepts `adapter.kind: "docker"`, but AMR does not ship a Docker
executor in Phase 1. The CLI can validate such a manifest but cannot inject an
executor, so `amr experiment run` fails closed for it. A Python API caller may
inject an adapter whose `kind` is `docker`, whose
`deterministic` attribute is `True`, whose `uses_llm` attribute is `False`, and
whose `execute` method returns an `ExperimentExecution`. Container image digest
pinning and any sandbox policy remain the injected adapter's responsibility.

See the [neutral certified and empirical fixtures](examples/experiment-runner/README.md).
