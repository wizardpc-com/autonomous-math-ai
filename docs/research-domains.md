# Research domains and policy packs

Phase 1 keeps one stable controller and makes the research semantics selectable.
The distribution name remains `autonomous-math-ai`, the Python import namespace
remains `autonomous_math_research`, and the CLI remains `amr`.

## Select a bundled pack

Three bundled packs are supported:

| `policy.pack` | Claim statuses | Positive terminal | Negative terminal | Domain evidence gate |
| --- | --- | --- | --- | --- |
| `math-research` | `OPEN`, `PLAUSIBLE`, `REDUCED_TO`, `COMPUTATION_ONLY`, `FAILED`, `PROVED` | `PROVED` | `FAILED` | Existing proof and falsification policy |
| `certified-computational-research` | `OPEN`, `SUPPORTED`, `INCONCLUSIVE`, `REFUTED`, `CERTIFIED` | `CERTIFIED` | `REFUTED` | Deterministic checker and independent evaluator |
| `empirical-research` | `OPEN`, `SUPPORTED`, `INCONCLUSIVE`, `NOT_SUPPORTED`, `CONFIRMED`, `REPLICATED` | `CONFIRMED` or `REPLICATED` | `NOT_SUPPORTED` | Frozen deterministic protocol and independent evaluator |

`amr init` selects `math-research` when `--domain` is omitted:

```console
amr init ./research-target --domain certified-computational-research
amr init ./study-target --domain empirical-research
```

An existing schema-v11 project selects the same value in this excerpt from its
manifest-selected configuration:

```json
{
  "policy": {
    "pack": "empirical-research"
  }
}
```

This is a bundled-pack selector, not an extension hook. Unknown names and
unbundled directories fail configuration or pack validation.

## Compatibility

`math-research` remains the default and preserves the established ClaimGraph
wire format: the graph omits a top-level `domain`, claims use `math_status`, and
the compact view uses `proof_frontier`. Existing schema-v5 pinned math-policy
manifests resume with math semantics.

Non-math graphs name their domain at the top level, use `research_status`, and
expose `research_frontier`. They do not synthesize mathematical proof
obligations. A graph and a selected or pinned policy whose domains disagree are
rejected before dispatch.

## Discovery, validation, and pinning

Pack discovery scans packaged directories that contain
`resources/policy_packs/<name>/pack.json`. CLI choice construction and run
policy selection use this discovered set. Each descriptor is validated with an
exact field set and must bind:

- its normalized pack name and bundled `POLICY.md`;
- every required core role to one prompt and at least one reference;
- the exact built-in domain contract and audit requirements for that name; and
- the fixed mechanical resources used by the stable controller.

Every referenced package resource must exist. Path traversal, missing files,
unknown contract fields, modified semantics, cross-pack rebinding, and unknown
packs fail closed.

Before a new run starts a model, AMR writes a schema-v6 policy manifest and
copies the descriptor, skill, role prompts, role references, and mechanical
resources into the run-local `policy/` directory. Each entry carries its package
URI, snapshot path, and SHA-256; the manifest also pins the domain contract,
audit requirements, and a fingerprint of the complete policy view.

Resume verifies the manifest, every snapshot digest, descriptor-to-pack
bindings, and the pinned domain contract. It continues from those verified
snapshots even if the installed source pack has drifted, while
`POLICY_STATUS.json` records the drift or source error. A missing or modified
snapshot prevents resume.

The selected skill snapshot is passed to each model role. The controller also
places the role-specific prompt, references, domain contract, and audit
requirements in the role's pinned policy view; worker and auditor envelopes
embed the required role material. Project prompts remain overlays and cannot
replace the pinned domain contract or canonical state.

## Status and audit semantics

The domain contract maps event types to statuses and minimum evidence levels.
It does not let a model set canonical state. The normal candidate identity,
artifact sealing, independent audit, evidence, representation, and canonical
gates still apply.

The math pack retains the existing configured high/critical audit policy rather
than adding a blanket pack minimum to every low- or medium-impact event.

For certified computation:

- `CHECKER_SUPPORT`, `CHECKER_REFUTATION`, and `INCONCLUSIVE` require at least
  `E2_EXACT_TESTED`;
- `CERTIFICATE` requires `E4_CERTIFIED` and a deterministic checker reproduction
  command; and
- every status-changing event has a pack minimum of one independent evaluator
  audit. A critical event requires two when critical double audit is enabled.

`SUPPORTED` is an open frontier state, not a synonym for `CERTIFIED`.

For empirical research:

- `EXPERIMENT_SUPPORT`, `EXPERIMENT_NOT_SUPPORTED`, and `INCONCLUSIVE` require
  exact tested evidence under a frozen protocol;
- `CONFIRMATION` and `REPLICATION` require redundant exact evidence, which may
  be supplied by the independent evaluator replay; and
- every status-changing event has a pack minimum of one independent evaluator
  audit. A critical event requires two when critical double audit is enabled.

The empirical domain has no `PROVED` status. `CONFIRMED` and `REPLICATED` are
empirical outcomes only and must never be rendered, persisted, or reported as a
mathematical proof.

The effective audit count is the stricter of the configured audit threshold and
the pack minimum. `REPRESENTATION_BRIDGE` is a status/trust no-op in every pack;
it only authorizes the separately audited compatibility relation.
