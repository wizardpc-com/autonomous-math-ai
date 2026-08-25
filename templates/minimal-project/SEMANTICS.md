# Semantic alignment

`autonomous/semantics.json` is the machine-readable authority for the frozen
research goal, canonical vocabulary, and representation bridges.

- Replace every placeholder and recompute the SHA256 of the exact UTF-8
  `canonical_text`.
- Never edit an existing goal version. Append a consecutive version whose
  `supersedes_sha256` names the previous version, then advance `active_version`.
- Register each structured `core_terms` value for a claim binding. Free-text
  terminology and forbidden-confusion review remain Auditor/LLM work.
- Keep non-equivalent concepts in `forbidden_confusions` rather than aliases.
- Record the complete linear
  object-to-representation-to-evidence-to-validator-to-claim path and the
  claim's exact `RepresentationContract` content id.
- Do not put `VERIFIED` or `BRIDGE_OPEN` in this declaration. `VERIFIED` is
  produced only by a controller-owned, candidate-bound transition after the
  cited evidence and exact validator PASS scope are independently audited.
- Multi-agent agreement and validator success do not replace a matching
  append-only verification receipt.

The invariant is: **No unverified bridge into trusted final claims.** Dynamic
internal subclaims may remain `UNREVIEWED`; they must be bound and audited
before entering a trusted final claim's dependency closure. A project that has
never opted in remains runnable as legacy `UNREVIEWED`; after opt-in, missing or
damaged semantic metadata fails closed.
