# Lightweight project state

Use the filesystem as the durable handoff layer. Preserve useful existing layouts; add only missing directories that a project actually needs.

For a new project, declare paths in `autonomous/project.json` and prefer:

```text
<project-root>/
  state/
  claims/
  tasks/
  experiments/
  artifacts/
  proofs/
```

- `state/`: short current-state and decision records.
- `claims/`: one stable record per material claim.
- `tasks/`: controller-approved task packets, not autonomous queues.
- `experiments/`: source, commands, logs, metadata, and reports.
- `artifacts/`: durable derived data and handoff bundles.
- `proofs/`: informal or formal proof sources with explicit status.

When material subagent routing is part of the research, `state/routing-log.jsonl` may hold compact records conforming to [routing-log.schema.json](routing-log.schema.json). Do not create it for routine chatter or duplicate the detailed metadata already preserved by an ordinary one-shot worker run.

Do not move historical material solely to normalize a project. Link existing conversations, audit files, certificates, and experiment roots from the relevant claim or state record.

## Claim metadata

A JSON or YAML claim record should include at least:

```json
{
  "claim_id": "lemma-001",
  "statement": "Exact natural-language mathematical statement.",
  "status": "open",
  "evidence_level": "E0_SPECULATIVE",
  "dependencies": [],
  "artifacts": [],
  "formal_statement": null,
  "limitations": [],
  "updated_at_utc": "2026-01-01T00:00:00Z"
}
```

Use only `open`, `refuted`, `experimentally_supported`, `informally_proved`, or `formally_verified` for claim status. Evidence level and status are separate: an exact finite check may support a claim without proving its general form. When formalizing, preserve both the intended statement and the formal statement so fidelity can be audited.
