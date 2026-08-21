# One-shot worker contract

Use a worker only when every field below is concrete and a mechanical executor can recognize completion without choosing a new mathematical strategy.

## Task packet

Use UTF-8 JSON conforming exactly to [worker-task.schema.json](worker-task.schema.json). Every field is required (nullable fields use JSON `null`):

- `schema_version`: integer `1`.
- `task_id`: stable identifier using letters, digits, `.`, `_`, or `-`.
- `task_kind`: one approved mechanical class from the schema; strategy/judgment is not a class.
- `objective`: one exact execution objective.
- `mathematical_statement`: the claim or computational question without silent reformulation.
- `input_files`: parent-job-workspace-relative sealed files to copy into the isolated run; use `[]` when none. Project-root fallback and `..` traversal are forbidden.
- `allowed_tools`: exact executables or tool families the worker may invoke.
- `bounds`: the closed object `{finite:true, description, parameters:[{name,value}, ...]}`.
- `timeout_seconds`: positive finite controller-enforced timeout.
- `expected_artifacts`: normalized `artifacts/...` files the worker should create.
- `success_condition`: mechanically recognizable completion.
- `falsification_condition`: exact event that refutes the tested statement; use a clear non-applicable explanation when appropriate.
- `stop_condition`: when to stop even without success or falsification.
- `verification_steps`: deterministic checks by which the parent/controller can accept the artifacts.
- `requires_mathematical_judgment`: must be `false`.
- `project_id` and `notes`: string or `null`.

Input paths and artifact paths must remain inside the repository and isolated child directory respectively. Network, Codex, agents, subagents, workers, memory, apps, plugins, browsers, and multi-agent tools cannot appear in `allowed_tools`.

Example:

```json
{
  "schema_version": 1,
  "task_id": "demo-odd-square-mod8",
  "task_kind": "finite_enumeration",
  "objective": "Enumerate the stated finite range using exact integer arithmetic.",
  "mathematical_statement": "For every odd integer n with 1 <= n <= 999, n^2 is congruent to 1 modulo 8.",
  "input_files": [],
  "allowed_tools": ["Python standard library"],
  "bounds": {
    "finite": true,
    "description": "Odd n from 1 through 999 inclusive, exact integers",
    "parameters": [
      {"name": "n_min", "value": "1"},
      {"name": "n_max", "value": "999"},
      {"name": "n_parity", "value": "odd"}
    ]
  },
  "timeout_seconds": 600,
  "expected_artifacts": ["artifacts/check.py", "artifacts/results.json"],
  "success_condition": "Every enumerated odd n has n^2 % 8 == 1 and the full count is recorded.",
  "falsification_condition": "An odd n in the inclusive range has n^2 % 8 != 1; record the least such n.",
  "stop_condition": "Stop immediately after a counterexample or after n = 999 is checked.",
  "verification_steps": ["Run artifacts/check.py and compare the recorded count to 500."],
  "requires_mathematical_judgment": false,
  "project_id": null,
  "notes": "Mechanical example only."
}
```

## Worker behavior

The worker receives one copied packet and one writable run directory. It must:

1. preserve the statement and bounds exactly;
2. use only the listed tools and copied inputs;
3. record source, commands, parameters, bounds, seeds, tool versions, outputs, interpretation, and limitations;
4. stop immediately at the success, falsification, or stop condition;
5. return one final JSON object conforming to [worker-result.schema.json](worker-result.schema.json);
6. terminate without resuming, enqueuing, launching another research task, or invoking any child process that is itself an agent.

The worker must not expand the objective, invent a research program, choose the next lemma, spawn recursive workers, change model, or silently change the statement. It may record unexpected facts in `observations` but must not investigate them.

## Primary execution statuses

Return exactly one:

- `COMPLETED`: a bounded calculation or reproduction task completed; do not use this for a counterexample search that exhausted its scope.
- `FALSIFIED`: an independently checkable counterexample met the falsification condition.
- `NO_COUNTEREXAMPLE_WITHIN_SCOPE`: a counterexample search checked its complete stated scope with none found. This is never proof outside that scope.
- `FORMAL_CHECK_PASSED`: the specified formal check completed successfully; evidence level still depends on statement fidelity and the absence of admitted gaps.
- `BLOCKED`: mechanical work cannot continue without a new mathematical judgment.
- `TOOL_ERROR`: a required tool or mechanical invocation failed.

For `BLOCKED`, preserve all evidence obtained and set `blocked_on` to the smallest controller decision required. For `FALSIFIED`, put the exact witness in `counterexample`. The runner may synthesize `TOOL_ERROR` metadata when the child fails before producing valid JSON.

## Broker and stop boundary

Director, prover, falsifier, explorer, auditor, and evaluator_auditor may request this packet. They cannot launch the worker: the controller broker validates eligibility, binds `parent_job_id/subtask_id`, assigns an isolated directory, applies the configured static cap (or resource-derived cap when unbounded), separate token/cost governor, queue and rate backpressure, and records REQUESTED/STARTED/FALLBACK/COMPLETED/FAILED. Success, falsification, scope exhaustion, formal success, blockage, tool error, timeout, or the packet stop condition ends the worker process. Observations return to the parent role; they never trigger automatic follow-up work or trust changes.

The runner checks each observed command executable and every returned replay command against `allowed_tools`; parent-directory/repository-scope escapes and authentication/environment inspection fail non-retryably. Input copying also rejects VCS metadata and secret-bearing paths such as `.git/`, `.codex/`, `.ssh/`, `.env*`, credential files, and private keys. Copied inputs plus runner-owned task/schema/prompt files are hashed before the turn and must remain byte-identical afterward. Writes stay inside the isolated worker run directory.

## Configured routes and model-unavailable circuit breaker

The built-in default primary route is `gpt-5.3-codex-spark` / `high` /
`service_tier=null`; the default fallback is `gpt-5.6-luna` / `medium` / null.
The controller pins configurable provider/model/effort routes into the run-local
policy bundle. The exact primary route may continue once on the fallback after
`model_unavailable`, `provider_quota_exhausted`, `transport_transient`, or
`timeout_transient`. Policy, permission, eligibility, schema, protocol, and
artifact failures never trigger fallback.
Environment route overrides are rejected. The child uses `approval_policy=never`, disables network access, memories, plugins, apps, multi-agent tools, and web search. The Codex process retains `CODEX_HOME` only to reuse the existing login, while a native permission profile denies model-started commands access to the filesystem root, grants only minimal runtime reads and the isolated workspace, and disables network. The command also uses `--strict-config`, so unsupported permission or isolation keys fail before a turn rather than silently weakening the boundary. `shell_environment_policy` removes `CODEX_HOME`, home/profile locators, API keys, tokens, passwords, credential helpers, and similar secret-bearing variables from model-started commands.

Persist the exact primary configuration as unavailable only after an explicit
permanent unavailable/access-denied rejection. A local parse/startup error,
timeout, rate limit, network failure, or ambiguous transport failure is not
evidence that the model is unavailable. After the one permitted fallback,
transient retry budgets apply only to the selected fallback route. A provider
usage-limit/quota-exhausted terminal is never cached as model unavailability;
if it also occurs on the fallback, the controller drains and pauses while
preserving unfinished parent research. When the provider supplies a reset time,
the append-only event records that value for the operator. It is not
mathematical failure or stagnation.
The first actual primary execution is also its availability check; no extra paid
probe turn exists. Once primary is cached unavailable, later tasks use only the
pinned fallback, which has no further fallback. Never rotate beyond the two
pinned routes or to the parent/controller route. `--retry-unavailable-route`
remains an explicit operator override and is never passed by the autonomous broker.

In broker-managed mode, model availability state is seeded from controller-owned append-only events into an attempt-local file beneath the parent job; the worker never writes the repository-global status cache. The deterministic controller alone may atomically persist an exact permanent route rejection in the cross-run circuit breaker. Each attempt atomically maintains a controller receipt containing the exact packet hash, PID, output root, run directory and heartbeat. Crash recovery may reattach only to that matching lease. A retry is allowed only after an exited receipt or a safely dead PID; a missing, stale-but-live, or PID-ambiguous lease fails non-retryably instead of launching a duplicate or terminating an uncertain process. The runner, task/result schemas, queue-only broker client, compatibility validator and contract constants are all executed from one run-local pinned policy bundle.

Mechanical-route output is execution evidence. The strong parent research role
interprets it; the strong parent Auditor alone decides PASS/REJECT/UNRESOLVED.
Candidate, independent-audit, and canonical gates remain unchanged.
