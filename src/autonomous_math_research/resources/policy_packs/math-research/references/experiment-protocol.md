# Experiment protocol

Create a lightweight record with:

1. problem or conjecture identifier;
2. exact mathematical statement;
3. tool and version;
4. exact command;
5. source commit or hash when available;
6. parameters;
7. random seed when relevant;
8. runtime;
9. exact searched range;
10. raw or exact output;
11. evidence level;
12. interpretation;
13. unresolved limitations.

For a worker run, also preserve the task packet, requested model and reasoning effort, actual model and effort only when explicitly observable, Codex CLI version, schema, worker prompt, stdout/stderr, exit status, timeout outcome, available token usage and latency, primary execution status, interpretation, and observations. The worker runner records these automatically. The first actual Spark attempt is the availability check; there is no separate paid probe turn.

Initialize a record for an explicit project root with:

```text
amr new-experiment <problem-id> --project-root <path> --statement "<exact statement>"
```

The helper creates a timestamped directory below `<project-root>/experiments/<problem-id>/`, writes `metadata.json` and `report.md`, and never overwrites an existing experiment. Fill its placeholders during the run; store bulky raw output beside the metadata and link it from both files.

Use exact serialization for integers, rationals, finite fields, polynomials, graphs, and solver instances. For random work, record the generator as well as the seed. For exhaustive work, specify inclusivity, pruning rules, canonicalization, and skipped cases. For certificates, document a small replay command and place the durable artifact under `certificates/` when appropriate.

An important result report should contain: Statement; Status; Evidence level; Mathematical reduction used; Falsification attempts; Computations; Independent checks; Counterexamples; Remaining gaps; Files/certificates; Recommended next action.
