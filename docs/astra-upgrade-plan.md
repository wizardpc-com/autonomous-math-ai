# Astra incremental upgrade

Baseline: `fadbd95164df711cd7cf358ca2b2d6d57bd2d870` (AMR 0.2.19).

## Scope and acceptance

1. Add an explicit Astra profile while preserving existing defaults and pinned
   runs. Validate paginated model capabilities and retain requested versus
   observed routes. Short-reasoning diagnostics use a zero retry allowance.
2. Add a bounded compatibility probe: offline by default, explicit live opt-in,
   at most two turns, local deadline, no campaign and no automatic retries.
3. Add a thin native handoff over ResearchTask, Audited Frontier, packaged policy,
   and ExternalResult. Freeze selected inputs and computed hashes. Import only
   unaudited material; candidate admission and canonical promotion remain separate.
4. Validate with neutral unit/integration fixtures, mock lifecycle, distribution
   scanning, isolated build/install acceptance, and whitespace checks. Record live
   verification as not run when the operator selects offline-only validation.

Validation is offline-only; live model compatibility remains unverified.
Shared-runtime upgrades, research campaigns, and project authority edits are
outside this upgrade's scope.
Preserve immutable evidence and old snapshots. Native workspaces provide only
supervised process separation until their actual permissions are independently
checked; a worktree alone is not a sandbox.

## Components

- Astra configuration, capability validation, route telemetry, and probe tests.
- Native input export/result sealing/import with trust-boundary regression tests.
- Operator guide and controlled comparison protocol using existing record metrics.

Complete these thin adapters, then freeze platform expansion. Repeated observed
friction, a reproducible case, and a small measurable fix justify later work.
