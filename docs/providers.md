# Provider adapters

Codex App Server is the default provider. It uses the operator's existing Codex
login and requires no API key in project configuration. The bundled
`openai_compatible` adapter is optional and supports Responses-style and Chat
Completions-style HTTP APIs when explicitly selected.

Provider transport is isolated from the mathematical role protocol. Every
adapter still passes through controller schema preflight, normalized error
classification, telemetry/cost normalization, configured retry, canonical
guards, and audit gates. A provider response is never proof.

## Capability declaration

Each provider declares:

- adapter id, endpoint/profile, and a credential reference;
- native JSON Schema, JSON-text, or no structured-output capability;
- canonical reasoning efforts plus an explicit provider-specific mapping;
- safe service tiers and the provider parameter path;
- total/cached/uncached/cache-write input, output, and reasoning-output token
  field paths plus an optional cost path;
- whether a controller-managed mechanical one-shot runner is supported.

Unsupported effort fails preflight unless the route says
`unsupported_effort: "map"` and the capability supplies that exact mapping.
There is no silent downgrade. Providers without native Structured Outputs must
explicitly select `json_text`; the shared local schema gate remains mandatory.

Credentials contain only `{kind, reference}`. Kinds are `environment`,
`system_credential`, `provider_profile`, or `none`. The environment kind stores
only a variable name such as `OPENAI_API_KEY`; validation never resolves or
prints it. Actual resolution happens only when a selected real adapter starts a
request.

Adapters normalize provider quota terminals separately from ordinary rate
limits. A usage-limit/quota-exhausted response is non-retryable within the
current epoch, preserves any provider reset timestamp, pauses the campaign, and
retains pending work. It never becomes mathematical failure or stagnation.

## OpenAI-compatible routes

The built-in optional provider is named `openai-compatible`. Override its
endpoint, API style/capabilities if needed, then point individual roles at it.
Safe `default` or `flex` tiers can be declared for compatible APIs; fast,
priority, and auto remain forbidden by core policy. See the profile example in
[`examples/per-role-api-profile.json`](examples/per-role-api-profile.json).
For a separately named OpenAI-compatible gateway with explicit capability
mapping, see [`examples/custom-provider-profile.json`](examples/custom-provider-profile.json).

## Third-party adapters

An installed distribution can expose an adapter factory through the Python
entry-point group `autonomous_math_research.providers`. The entry-point name is
the provider's `adapter` value. A factory receives `config`, `provider_name`, and
`trace_notification`, and implements the common backend protocol. Referenced
but uninstalled adapters fail `amr config validate` before any turn.

The built-in mechanical runner is registered for `codex_app_server`. An
external adapter that declares `mechanical_one_shot: true` must also register a
factory in `autonomous_math_research.mechanical_runners`, using the same adapter
id. The factory receives `config`, `repository_root`, `primary_route`, and
`fallback_route`, and must implement the one-shot runner protocol. Primary and
fallback routes share one runner adapter; mixed adapters fail preflight. The
controller still owns packet validation, budgets, backpressure, artifact gates,
retry/recovery records, and the recursion prohibition.
