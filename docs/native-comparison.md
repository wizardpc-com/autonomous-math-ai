# Controlled native / AMR comparison

Status: proposed; no live experiments or measured benefits are recorded here.

Select two or three small independent tasks with frozen exact statements,
dependencies, representation contracts, source hashes and useful stopping
conditions. Include one proof obligation and one deterministic computation or
bounded falsification task. Use neutral development fixtures before real research.

For each task, retain the input manifest digest and compare:

| Arm | Execution | Acceptance |
| --- | --- | --- |
| AMR | Explicit Astra profile, `--purpose EVALUATION`, bounded task/theme | Existing independent Result Audit and canonical gates |
| Native | Same model/effort, frozen input, time/token allowance and tools; seal and import | The same independent audit standard and canonical gates |

Keep the task identity, model route, budget, original definitions, audit
requirements and tool versions fixed. Record actual route observations or UNKNOWN.
Separate audit workspaces and alternate arm order. Do not reuse the other arm's
candidate or notes while producing a result. If a native session has broader
tools or permissions, record that difference; do not claim an isolated causal
comparison of execution modes. Budget enforcement differences also remain explicit.

Use the existing unified external result classification and run records:

```console
amr record inspect --project ./target --run-id <evaluation-epoch>
amr record replay-context --project ./target --run-id <evaluation-epoch>
amr record metrics --project ./target --run-id <evaluation-epoch>
```

Native sessions do not have AMR run telemetry. Preserve their exported events,
observed usage and timestamps as source evidence in the sealed result. Join the
two arms manually by frozen task digest; use null for unavailable native costs
or timings. Do not invent an AMR epoch or alter historical records to fill gaps.

Review independently verified obligation progress, reusable exact negative
results, duplicated work, operator interventions, invalid candidates, audit time,
elapsed time and observed token/cost components. Count a candidate only after
its applicable audit; replay PASS and model agreement do not establish a theorem.
Document rejected and incomplete outcomes as well as successes.

After this small comparison, keep the simpler workflow for each task category.
Expand the harness only for a repeated evidenced defect that a small patch can
remove. Agent count, generated files and self-reported PROVED counts are not
success metrics.
