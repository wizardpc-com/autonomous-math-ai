# Project manifest

Every target project declares `autonomous/project.json` with schema version 1.
All paths are normalized POSIX paths relative to the project root.

```json
{
  "schema_version": 1,
  "project_id": "example-project",
  "final_claim_id": "C_ROOT",
  "config": "autonomous/config.json",
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

`amr init <directory> [--project-id ID] [--final-claim-id ID]` creates a neutral
complete skeleton. `amr validate --strict` additionally requires every scaffold
directory and checklist, exact claim-ID agreement, nonempty canonical inputs,
consistent protected paths, and removal of mathematical placeholders. It also
validates provider/model configuration and secret references without starting a
model turn.
