# Autonomous Math Research

`autonomous-math-research` is an installable Python 3.11+ harness for long-lived,
auditable mathematical research campaigns. It keeps mathematical state behind
independent audit and deterministic canonical gates while preserving append-only
events and artifacts.

The package is conjecture-neutral. A project supplies a declarative
`autonomous/project.json`, mathematical prompts, configuration, and claim state;
the package owns wire protocols, validation, lifecycle, storage, and policy
enforcement.

```console
python -m pip install .
amr init ./my-project
amr validate --project ./my-project
amr run --project ./my-project --dry-run
```

After separate authorization for a paid acceptance check, one wire schema can
be tested with exactly one model turn:

```console
amr smoke --project ./my-project --schema-role director
```

The smoke budget is a soft dispatch gate, not a hard cutoff for a turn already
in flight. Schema-role smoke defaults to 32,000 tokens and full-lifecycle smoke
to 320,000 tokens; observed use, unknown telemetry, and any overshoot are
recorded explicitly. Reaching the gate prevents another stage from starting
and never interrupts a healthy turn merely because of its token count.

No command uses a fast or priority service tier. Mechanical delegation is a
single, isolated, one-shot path using the packaged policy: Spark/high/null, with
Luna/medium/null allowed only after an explicit permanent-unavailable result.

See `docs/ARCHITECTURE.md`, `docs/PROJECT_MANIFEST.md`, `docs/TRUST_MODEL.md`,
and `docs/VALIDATION.md`.
