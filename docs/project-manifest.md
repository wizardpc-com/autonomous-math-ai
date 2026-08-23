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
files are embedded in the startup-generated dynamic snapshot as contextual
material. They cannot override the dynamic ClaimGraph frontier even when they
contain stale status prose.

`claim_graph` is the single machine-readable mathematical-status and proof-
frontier authority. `trusted_state` records audit provenance and, after the
first controller transition, binds to the exact ClaimGraph SHA-256. Legacy
unbound trusted files remain readable for compatibility. A canonical Markdown
input can opt into strict consistency checking by containing exactly one
`<!-- AMR-CANONICAL-STATE-BEGIN -->` / `<!-- AMR-CANONICAL-STATE-END -->`
generated JSON block. Unmarked Markdown is prose context, not a second state
store. A malformed or conflicting marked view fails before any model turn.

Startup refresh never edits the graph, trusted metadata, `CLAIMS.md`, or
`PROGRESS.md`. Audit-gated controller transitions atomically update only the
graph and trusted metadata, with append-only authorization and before/after
snapshots under `runtime_root/state/canonical_transitions`. AMR never promotes
candidate output on its own and never rewrites canonical Markdown.

`prompt_root/director.md` is optional. When present it is a stable
project-specific constraints overlay only. Generic falsification, representation,
audit, kill-gate, novelty, and bounded-routing behavior belongs to AMR. Existing
project manifests and Director files continue to load, but any stale frontier or
progress prose in the overlay is subordinate to the dynamic canonical snapshot.

No project-manifest migration is required for crash recovery. The separate
run-local `RUN_MANIFEST` is schema v13 for new epochs and pins campaign/epoch
identity, absolute timing, AMR source provenance, and Codex App Server protocol
provenance. Resume validates that record before any campaign write. Existing
schema-v12 run manifests remain readable; they are marked as legacy-unpinned in
the append-only event log rather than rewritten.

The derived `INTERMEDIATE_INDEX.json` format is schema v2. It hashes finalized
immutable artifacts and records a lifecycle event watermark separately;
`EVENTS.jsonl` and `LIVE_EVENTS.jsonl` remain append-only evidence and are not
misrepresented as immutable before their terminal records are committed. This
does not change `autonomous/project.json` or any canonical mathematical file.

`amr init <directory> [--project-id ID] [--final-claim-id ID]` creates a neutral
complete skeleton. `amr validate --strict` additionally requires every scaffold
directory and checklist, exact claim-ID agreement, nonempty canonical inputs,
consistent protected paths, and removal of mathematical placeholders. It also
validates provider/model configuration and secret references without starting a
model turn.
