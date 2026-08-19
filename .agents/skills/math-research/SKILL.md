---
name: math-research
description: Conduct research-grade mathematics with cheap falsification, exact or symbolic computation, proof or counterexample search, reproducible evidence, independent audit, or selective formalization. Use for nontrivial claims needing rigorous verification; do not use for routine calculations or explanations.
---

# Math Research

`research controller decides -> local backend computes -> deterministic verifier judges -> artifacts persist`

## Research controller

- Preserve the exact statement, domain, quantifiers, assumptions, and requested
  mode. Strategy, lemma choice, interpretation, and escalation stay with the
  capable parent model.
- Read [compute-orchestration.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/compute-orchestration.md)
  before parallel work and
  [falsification-policy.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/falsification-policy.md)
  before expensive proof work.
- Prefer the cheapest exact falsification that can meaningfully test the claim.
  Use [tool-routing.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/tool-routing.md)
  and [local-compute.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/local-compute.md)
  to choose local CPU, CAS, SAT/SMT, GPU, or formal tools.
- Assign evidence only through
  [verification-levels.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/verification-levels.md).
  Failed search and raw CAS output are not proof. Read
  [formalization-policy.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/formalization-policy.md)
  before substantial formalization.
- Refresh a target project's local tool inventory with
  `amr detect-tools --project-root <path>`; the inventory is project state, not
  harness source.

## Controlled one-shot mechanical worker

Delegate only a bounded, repetitive task with an exact objective, fixed inputs,
finite bounds, a stop condition, and mechanically checkable output. Never
delegate proof strategy, invariant choice, research direction, or a final audit
verdict. Roles may request a task, but only the persistent controller may start
the worker.

Read [worker-contract.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/worker-contract.md)
and invoke the controller-owned broker:

```text
amr mechanical-run path/to/task.json --project-root path/to/project --timeout 600
```

The fixed primary route is `gpt-5.3-codex-spark` / `high` /
`service_tier=null`. Only an explicit permanent unavailable/access-denied result
for that exact route may fall back once to `gpt-5.6-luna` / `medium` /
`service_tier=null`. Transient failures receive finite same-route retries. Never
use Sol, Terra, the parent model, fast, priority, or a third fallback for the
mechanical worker.

The child is one-shot, isolated, network-disabled, and unable to spawn another
agent. Its result is mechanical evidence only; it cannot change canonical
claims, proofs, trusted state, or an audit verdict.

## Project evidence

Every target project declares its own portable paths in
`autonomous/project.json`. Keep all statements, prompts, claims, tasks,
experiments, runs, outcomes, audits, and artifacts under that target project,
never in this harness repository.

Create a project-local experiment record with:

```text
amr new-experiment <problem-id> --project-root <path> --statement "<exact statement>"
```

Follow [experiment-protocol.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/experiment-protocol.md)
and [project-state.md](../../../src/autonomous_math_research/resources/policy_packs/math-research/references/project-state.md).
