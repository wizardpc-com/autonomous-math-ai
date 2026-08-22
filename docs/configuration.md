# Configuration and profiles

Configuration schema v11 is merged as: built-in `codex-app-server-default`, the
project's manifest-selected `autonomous/config.yaml`, an explicit `--profile`,
then an optional launcher one-shot override. Core
trust validation runs last, so neither a project nor a user profile can enable
networked worker tools, remove core protected paths, disable independent final
audit, change the persistent controller, or enable fast/priority/auto tiers.

Inspect the exact redacted result without starting a model:

```console
amr config validate --project ./research-target
amr config explain --project ./research-target --profile ./my-profile.json
amr config summary --project ./research-target
```

`explain` reports merge precedence, applied migrations, provider/role routes,
and `model_turns_started: 0`. Plaintext-looking secrets and URL user information
fail validation; explanation output redacts defense-in-depth matches.

`summary` emits a compact redacted view of project identity, campaign duration,
concurrency, budgets, each role route, and mechanical routing. Schema v9 added
`campaign.hours` (default `12`) and `campaign.epoch_hours` (default `2`). When
the corresponding CLI flags are absent, `amr run` uses these project values;
explicit `--hours` and `--epoch-hours` remain highest priority.

Startup canonical refresh has no configuration switch and cannot be disabled by
a project or user profile. `amr run` pins manifest-declared canonical inputs,
their SHA-256 values, available Git revision, the authoritative ClaimGraph,
digest-bound trusted metadata, and the optional Director overlay before any
model turn. Crash resume requires
the original pinned inputs. A new epoch discards stale pending planning after a
safe canonical change, while unresolved audit state that cannot be rebound stops
fail-closed. The refresh writes only run-local derived state.

Unattended epoch continuation is an explicit CLI mode rather than mutable
project policy:

```console
amr run --project ./research-target --hours 12 --epoch-hours 2 --auto-epochs
```

It preserves the configured epoch seal boundary and repeats full startup
refresh in every new epoch. Only an ordinary epoch-time boundary continues
automatically; quota pause, fail-closed state/bootstrap errors, internal
failure, operator stop, completion, or exhausted campaign time stops the loop.

The `engine` section also controls controller-owned research continuation:

- `research_max_turns.prover`, `.falsifier`, and `.explorer` (each defaulting
  to `12`) independently bound turns in one logical research job;
- `reasoning_health_short_tokens` (default `600`) is a diagnostic threshold;
- `reasoning_health_repeated_token_tolerance` (default `2`) detects repeated
  counts;
- `reasoning_health_retry_limit` (default `2`) bounds diagnostic retry/escalation.

These settings never relax audit or canonical gates. App Server goals are not
armed; per-thread token limits remain controller-enforced from telemetry.
The first model-reported `BLOCKED` result always receives a controller-owned
repair turn. Only a structurally actionable blocker repeated after repair may
end the logical job, and that verification is scheduling-only: it has no
mathematical, trust, or evidence effect. Reaching a turn or controller token
boundary without verified progress creates a controller-owned, noncanonical
checkpoint for the next epoch instead of marking the route failed or resetting
stagnation. The checkpoint records the current canonical obligation, completed
evidence bindings, and next obligation; the checkpoint and full continuation
task are digest-bound and revalidated before fresh-epoch dispatch.

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

Normalized telemetry separately records total input, cached input, uncached
input, cache-write input, output, and reasoning-output tokens. Provider total
tokens remain the budget authority, while uncached/output/reasoning components
are the appropriate view for task depth. A mechanical Spark provider execution
failure (`model_unavailable`, quota, transport, or timeout) continues exactly
once on the configured fallback, which defaults to Luna with `medium`
reasoning. Policy, permission, task eligibility, schema, protocol, and artifact
failures never trigger fallback. If the fallback also reports a usage-quota
terminal such as `You've hit your usage limit`, it is classified as
`provider_quota_exhausted`: the campaign pauses, the exact task or its
noncanonical checkpoint is retained for the next epoch, and an official reset
timestamp is preserved when supplied. It is neither a route failure nor
stagnation.

The monitor displays mechanical tokens as an observed lower bound whenever one
or more attempts lack complete usage telemetry. A displayed zero with unknown
usage is not a measured zero-token execution.

Selection modes are `preferred`, `balanced`, `conservative`, `disabled`, or
`custom` with explicit thresholds. Every worker remains one-shot, nonrecursive,
mechanical-only, and unable to update canonical state.

## User profile shape

See [`examples/per-role-api-profile.json`](examples/per-role-api-profile.json).
A profile contains exactly `profile_schema_version`, `name`, `extends`, and
`overrides`. It cannot override project identity. Schema-v7, v8, v9, and v10
project configs migrate to v11 in memory. Use
`amr config migrate --project PATH --write`
for an atomic persistent migration after reviewing the redacted effective
configuration; the command starts no model.

The launcher permits only simple runtime/route fields as one-shot overrides.
Provider capabilities, credentials, protected paths, audit policy, and other
trust-sensitive settings must be edited in the project configuration and pass
full schema, capability, secret-reference, and trust-boundary validation.
