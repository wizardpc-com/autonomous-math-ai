# Architecture

Autonomous Math Research is a conjecture-neutral controller. A target project
owns mathematical prompts and state; the installed package owns protocols,
policy, lifecycle, scheduling, and durable storage.

The release-facing package boundaries are explicit:

- `engine/` owns dynamic admission;
- `protocol/` exposes wire errors, protocol-v2 contracts, and schema preflight;
- `lifecycle/` owns monotone phases, campaigns, leases, and cognitive views;
- `storage/` owns atomic persistence, portable artifact references, steering,
  and asset ingest;
- `mechanical/` owns the isolated one-shot broker;
- `cli/` owns the `amr` command surface.

The legacy extraction-time `storage_layer` name remains a thin import-only
compatibility wrapper. It contains no second storage implementation.

The trust path is deliberately narrow:

1. a research job may emit a candidate;
2. the controller validates identity, representation, and evidence metadata;
3. referenced artifacts are copied into a content-addressed epoch bundle;
4. a fresh auditor reads that immutable bundle through a controller-owned lease;
5. only deterministic verification plus the audit/canonical gate may change
   trusted claim state.

Campaigns contain append-only epochs, and epochs contain jobs. An epoch is the
unit of sealing and crash recovery. Continuing a campaign creates a new epoch;
resuming is reserved for the same crashed epoch.

The lifecycle is monotone:

`BOOTSTRAP -> RUNNING -> DRAINING_* -> SEALED`

or

`RUNNING -> FINALIZING -> COMPLETED`.

Role output failures are isolated and retried within configured bounds.
Controller, policy, canonical-guard, and local schema failures drain the epoch
as internal failures. A transition out of `RUNNING` never dispatches new work.

Top-level model caps are independent: one Director, up to eight research jobs,
and up to eight audits by default. Mechanical workers have their own cap of
eight, remain one-shot and non-recursive, and still share the global token
budget. Dynamic admission reduces new research dispatch when the audit backlog
grows; it never cancels healthy work.
