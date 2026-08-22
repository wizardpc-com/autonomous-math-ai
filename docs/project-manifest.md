# Project manifest

Every target project declares `autonomous/project.json` with schema version 1.
All paths are normalized POSIX paths relative to the project root.

```json
{
  "schema_version": 1,
  "project_id": "example-project",
  "final_claim_id": "C_ROOT",
  "config": "autonomous/config.yaml",
  "claim_graph": "autonomous/state/claim_graph.json",
  "trusted_state": "autonomous/state/nightly_trusted.json",
  "runtime_root": "autonomous",
  "prompt_root": "autonomous/prompts",
  "canonical_inputs": {
    "director": ["claims/CLAIMS.md"],
    "research": ["claims/CLAIMS.md"],
    "audit": ["claims/CLAIMS.md"]
  },
  "protected_paths": [
    "claims", "proofs", "state", "artifacts", "experiments",
    "certificates", "audit"
  ]
}
```

The CLI accepts any target directory with `--project`. `--workspace-root` may
pin a containing workspace; otherwise the target's nearest Git root is used.
No package code assumes a source checkout or fixed collection layout.

`canonical_inputs` keeps the existing three role arrays. No manifest migration
is required. At each new run, AMR resolves every listed file, reads it once,
records its project-relative path and SHA-256, copies the exact bytes into the
run, and records the containing Git `HEAD` when available. The Director-role
files are embedded in the startup-generated dynamic snapshot. They are the
current source for frontier and progress descriptions even when a project
prompt or an older derived planning file says otherwise.

`claim_graph` and `trusted_state` remain explicit structured controller mirrors.
They are frozen as startup provenance and loaded into the dynamic view, but the
refresh never edits them, `CLAIMS.md`, `PROGRESS.md`, mathematical status, or
trust status. AMR does not attempt a conjecture-specific Markdown-to-claim-graph
migration. A state change that cannot be represented safely therefore fails
closed instead of guessing.

`prompt_root/director.md` is optional. When present it is a stable
project-specific constraints overlay only. Generic falsification, representation,
audit, kill-gate, novelty, and bounded-routing behavior belongs to AMR. Existing
project manifests and Director files continue to load, but any stale frontier or
progress prose in the overlay is subordinate to the dynamic canonical snapshot.

`amr init <directory> [--project-id ID] [--final-claim-id ID]` creates a neutral
complete skeleton. `amr validate --strict` additionally requires every scaffold
directory and checklist, exact claim-ID agreement, nonempty canonical inputs,
consistent protected paths, and removal of mathematical placeholders. It also
validates provider/model configuration and secret references without starting a
model turn.
