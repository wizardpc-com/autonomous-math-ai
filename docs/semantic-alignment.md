# Semantic alignment and representation bridges

The semantic alignment layer is an optional project-level contract for keeping
long-running research attached to the same goal, vocabulary, objects, and
validation meaning. It is independent of domain status: `PROVED`, `OPEN`,
`CERTIFIED`, and other policy-pack statuses are unchanged.

When present, the file is `<runtime_root>/semantics.json` (normally
`autonomous/semantics.json`). Add it to every role's `canonical_inputs` so the
normal canonical-state snapshot also freezes it. The controller additionally
loads, hashes, and supplies it directly to Director, research, and audit task
packets.

## Research contract

`research_contract.versions` is a consecutive history beginning at version 1.
Each record contains the final claim id, exact canonical goal text, its UTF-8
SHA256, and the previous version's SHA256. `active_version` must select the last
record.

Do not edit an existing record. To change the goal, append a new version, set
`supersedes_sha256` to the old head, and advance `active_version`. On first
opt-in the controller records the complete contract history and head in trusted
state. Every fresh run, resume, and new epoch requires that trusted history to
remain an exact prefix of the declaration. Rewriting an old version and
recomputing the chain therefore fails closed. The active goal text must exactly
equal the final ClaimGraph statement.

## Semantic registry

Each registry entry declares:

- a stable `term:` or `object:` id and `TERM` or `OBJECT` kind;
- canonical name and definition;
- canonical source;
- aliases;
- forbidden confusions (nearby concepts that are not equivalent);
- allowed `representation:` ids.

Claim bindings list their structured `core_terms`. Each listed value must
resolve to exactly one registry binding or the claim receives
`TERM_AMBIGUOUS`. This deterministic check does not discover terms in arbitrary
proofs or free text and does not decide whether prose commits a listed forbidden
confusion. Those remain Auditor/LLM review responsibilities.

## Bridge graph

Every bridge declaration has `source`, `target`, `justification`, and
project-relative `evidence`. `semantics.json` has no trusted status field. It is
only a declarative registry, contract, claim binding, and bridge path.

A terminal claim path must be ordered and continuous:

```text
object:... -> representation:... -> evidence:... -> validator:... -> claim:...
```

Additional representation-to-representation steps are allowed. The first
representation must be allowed by the canonical object and the final target
must be the exact claim id. Semantic Alignment v1 accepts only a linear path;
relationships that cannot be expressed this way fail closed.

`VERIFIED` is derived only from controller-owned append-only receipts in the
canonical trusted-state journal. A receipt binds one sealed `CandidateEvent` to
its exact claim and statement, content-addressed `RepresentationContract`,
artifact and bridge-evidence hashes, validator identity/version/config, exact
PASS scope, independent audit result receipts, ordered bridge ids, and the
contract and semantic heads current at verification time. Candidate A's receipt
cannot verify candidate B.

The enforced invariant is **No unverified bridge into trusted final claims.**
A validator PASS or collection of agreeing agents cannot replace a matching
candidate-bound semantic receipt.

## Enforcement boundary

The controller checks deterministically:

- exact JSON fields and portable ids;
- goal text SHA256 and the version chain;
- final goal/ClaimGraph identity;
- declared `core_terms` and registry bindings only;
- bridge existence, ordering, layer types, endpoints, and allowed
  representations;
- sealed-candidate identity, representation, evidence and audit receipt hashes,
  validator configuration and PASS scope;
- persistent opt-in and append-only contract history;
- semantic/contract/candidate preconditions at canonical commit and crash
  recovery;
- the same final semantic postcondition at startup, resume, checkpoint/import,
  direct ClaimGraph terminal transition, canonical mutation, and finalization.

Human or LLM review is still needed to decide whether definitions and canonical
sources are mathematically adequate, whether arbitrary proof/free text
introduces a new term or forbidden confusion, whether bridge evidence proves
the asserted equivalence, and whether a validator's real semantics entail the
claim. Auditor prompts make these checks explicit; the deterministic gate
verifies structured declarations, sealed content, receipts, and hashes, not the
truth of natural-language evidence.

## Dynamic subclaims

Runtime-generated lemmas and subclaims need not be pre-registered. They may
reach a domain-level terminal status while their semantic status remains
`UNREVIEWED`, so ordinary internal research is not frozen. Before such a claim
enters the transitive dependency closure of a trusted final claim, it must gain
its own declaration binding and candidate-bound semantic audit receipt. The
final acceptance gate traverses that complete dependency closure.

## Legacy projects

If the file has never been supplied, validation and campaigns continue with
semantic status `UNREVIEWED`. This is a compatibility state, not a claim
failure. Once the controller persists opt-in, a missing or malformed file is a
trust-boundary failure and cannot silently revert the project to legacy mode.

## Semantic Migration v1

For an existing project:

1. Copy `templates/minimal-project/SEMANTICS.md` and
   `templates/minimal-project/autonomous/semantics.json` as a starting point.
2. Set version 1 `canonical_text` to the exact final ClaimGraph statement and
   compute its UTF-8 SHA256.
3. Register the final canonical object and every structured `core_terms`
   binding; record aliases and explicit non-equivalences conservatively.
4. Inventory each derived representation, computation or certificate,
   validator, and target claim. Create one ordered bridge per layer boundary.
5. Cite durable audit/evidence paths. Do not write bridge status in the
   declaration; the controller creates `VERIFIED` receipts only after its normal
   independent audit boundary.
6. Bind each claim to its canonical object, structured core terms, exact
   `RepresentationContract` content id, and ordered bridge ids.
7. Add `autonomous/semantics.json` to Director, research, and audit
   `canonical_inputs` in `autonomous/project.json`.
8. Run `amr validate --project <project>`, inspect the returned
   `semantic_alignment.claims`, then run strict validation before live work.

Do not combine this migration with claim-status promotion. First establish and
review the semantic declaration, then submit a sealed candidate through the
normal audit and canonical gates. Opt-in and all later bridge-verification
transitions remain in trusted append-only history.
