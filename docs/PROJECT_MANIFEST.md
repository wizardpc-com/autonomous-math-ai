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
  "protected_paths": ["claims", "proofs", "state"]
}
```

The CLI accepts any project directory with `--project`. `--workspace-root` may
pin a containing workspace; otherwise the target project's nearest Git root is
used. No package code assumes a monorepo or a fixed project-collection directory.

`amr init <directory>` creates a neutral example. `amr validate --project
<directory>` validates the manifest, paths, configuration, policy, all bundled
wire schemas, no-fast routing, claim graph, and canonical guard without starting
a model turn.
