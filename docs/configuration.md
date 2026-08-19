# Configuration and profiles

Configuration schema v8 is merged as: built-in `codex-app-server-default`, the
project's `autonomous/config.json`, then an explicit `--profile` override. Core
trust validation runs last, so neither a project nor a user profile can enable
networked worker tools, remove core protected paths, disable independent final
audit, change the persistent controller, or enable fast/priority/auto tiers.

Inspect the exact redacted result without starting a model:

```console
amr config validate --project ./research-target
amr config explain --project ./research-target --profile ./my-profile.json
```

`explain` reports merge precedence, applied migrations, provider/role routes,
and `model_turns_started: 0`. Plaintext-looking secrets and URL user information
fail validation; explanation output redacts defense-in-depth matches.

## Role routes

Each entry under `models` independently declares:

- `provider`, `model`, optional endpoint/profile override;
- canonical `effort` and `unsupported_effort` (`error` or explicit `map`);
- `service_tier` (provider-declared safe tiers only; fast/priority/auto fail);
- output normalization mode;
- timeout, transport/model-protocol retries, and concurrency;
- per-thread token limit, per-role cost limit, and estimated cost reservation.

Director, prover, falsifier, explorer, auditor, evaluator auditor, and smoke can
therefore use different providers and models. The default routes still all use
Codex App Server.

## Budgets and mechanical scheduling

New projects default to 500,000,000 main-role tokens and a separate
1,500,000,000 mechanical-token budget. Cost budgets may remain `null` when a
provider cannot report cost. Mechanical `max_mechanical_subworkers: null` means
no static seat count, not unlimited dispatch: the broker still applies its own
budget, CPU/resource cap, rate limits, queue depth, dispatch batch, timeout, and
operator stop.

Selection modes are `preferred`, `balanced`, `conservative`, `disabled`, or
`custom` with explicit thresholds. Every worker remains one-shot, nonrecursive,
mechanical-only, and unable to update canonical state.

## User profile shape

See [`examples/per-role-api-profile.json`](examples/per-role-api-profile.json).
A profile contains exactly `profile_schema_version`, `name`, `extends`, and
`overrides`. It cannot override project identity. Schema-v7 project configs are
migrated to v8 in memory; the source file is not rewritten.
