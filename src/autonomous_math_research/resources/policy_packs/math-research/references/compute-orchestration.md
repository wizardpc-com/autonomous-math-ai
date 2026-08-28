# Stable-core compute orchestration

Keep the problem, notation, known results, failed routes, artifacts, and strategy in one main thread. Allocate peripheral compute by research state, not easy/medium/hard labels; do not change the main effort for a local task.

## Roles and phases

| Role | Requested configuration | Use and boundary |
| --- | --- | --- |
| Main | `gpt-5.6-sol` / `xhigh` | Stable long-term state, strategy, synthesis, proof |
| Explorer | `gpt-5.6-sol` / `high` | One differentiated route, reformulation, special case, or counterexample direction |
| Breakthrough | `gpt-5.6-sol` / `max` | One gated, narrow bottleneck; one attempt before re-evaluation |
| Auditor | fresh `gpt-5.6-sol` / `xhigh`; separately gated `max` only for an audit gap | Adversarial reconstruction without confidence priming |
| Local compute | installed CAS/compiler/solver on the user's machine | First route for a known deterministic command; batch or shard, verify, persist |
| Compute agent | primary `gpt-5.3-codex-spark` / `high` / null; permanent-unavailable fallback `gpt-5.6-luna` / `medium` / null | Prepare/adapt one bounded mechanical packet through the controller broker; verify, return, stop; never recurse |

All settings are requested and capability-preflighted. Main roles use Fast only
when the run pins `execution.fast_mode=true`; `priority` is observation-only
and `auto` is forbidden. Every bundled mechanical route explicitly uses
`service_tier=null` in either mode. The broker never
launches a separate paid probe turn: its first real primary task is the
availability check.

```text
unknown route     -> differentiated High breadth + falsification/computation
promising route   -> XHigh main + only complementary breadth
mature route      -> XHigh main preserves the proof chain
narrow bottleneck -> one gated local Max attempt
candidate proof   -> fresh independent audit
```

## Spawn gate

Before any nontrivial spawn, record short answers:

1. Local exact target and stop condition?
2. Mathematically complete packet without full history?
3. Information gain exceeds routing, relearning, duplication, retry, and coordination cost?
4. Direct tools or the controlled mechanical broker are insufficient?
5. No equivalent attempted or assigned workstream?
6. Separation adds diversity, independence, or execution isolation?

If localization, completeness, novelty, or net value fails, do not spawn a
research role. Keep unified-context strategy in the parent. Prefer the
controller broker for finite enumeration, data normalization, formula
expansion, code preparation, deterministic reproduction, and artifact checks
whenever the packet and acceptance conditions are mechanical.
If controller capability attestation marks the mechanical sandbox unavailable,
the broker is prohibited rather than downgraded. For a finite exact task with a
fixed algorithm, prefer a frozen Experiment Runner manifest; otherwise retain the
work in a research role or repair the missing execution preconditions.

## Explorer

Use High when routes are unknown or broad, several method families are plausible, counterexamples/equivalent forms are needed, a route needs a quick viability test, or main shows path dependence. Assign distinct method families, assumptions, targets, search regions, or failure criteria. If no distinction can be named, do not parallelize. Stop fan-out when reports duplicate, a route matures, or marginal work is no longer distinct; redirect to depth, falsification, computation, or audit.

## Max gate and budget

Hard prerequisite: one self-contained narrow lemma/construction with one success target. Also require at least 2 of 4:

1. two independent high-quality attempts identify the same bottleneck;
2. exact computation or strong special cases support it;
3. a near-complete proof lacks only this step;
4. failure plausibly needs long-chain depth, case analysis, or precise construction—not information, statement, truth, or route repair.

```text
one bottleneck -> one Max attempt -> main re-evaluates
```

Failure certificate: target, attempts, strongest partial result, new objects/lemmas, exact gap, new-information flag, and retry conditions. Retry the same bottleneck only after new mathematics (new lemma/object, boundary, exact evidence, assumptions, or formulation); otherwise return to breadth, falsification, computation, assumption audit, or reframing.

## Stability, audit, and multi-agent

- **Hysteresis:** keep main stable; one route failure does not trigger Max and one easy success does not downgrade main. Change peripheral allocation after two consistent signals, except hard evidence such as a verified counterexample or complete candidate proof. Integrate every specialist result before the next spawn; never retry failed Max without new mathematics.
- **Audit:** send a candidate proof to a fresh auditor with statement, definitions, assumptions, cited lemmas, and proof, but no confidence priming. It reconstructs steps, tests boundaries, challenges assumptions/lemmas, and seeks counterexamples; return `SURVIVES_AUDIT`, `LOCAL_FLAW`, `STRUCTURAL_FLAW`, or `UNRESOLVED`. Surviving proofs proceed to useful exact/formal verification.
- **Ultra:** treat as multi-agent coordination, not stronger Max. Require at least three independent workstreams, each with distinct target/method/search region, stop condition, and compact output. Never use for one continuous lemma, delicate chain, inseparable context, or likely duplicate routes.

## Handoff and observability

Send no full history by default; use no forked turns or the smallest recent slice containing a complete packet:

- input: statement; definitions/notation; relevant lemmas and status; allowed assumptions; exact target/stop; constraining failures; permitted tools/artifacts; output format;
- return: conclusion/evidence; proof or counterexample skeleton; new lemmas/objects; failed approaches; unresolved gap; confidence basis; one next test.

Use [local-compute.md](local-compute.md) for direct CPU/GPU execution and
[worker-contract.md](worker-contract.md) for the controlled Spark→Luna mechanical route. Link durable artifacts
instead of raw logs. Record requested model/effort; record actual only when
runtime reports it, otherwise `null` with observation source. Probe success is
acceptance, not observed resolution. For material routing only, append one
schema-valid record to `<project-root>/state/routing-log.jsonl` using
[routing-log.schema.json](routing-log.schema.json); do not duplicate chatter or
worker metadata, and leave unavailable usage/cost/latency/actual fields `null`.

## Acceptance cases

| Case | Required route |
| --- | --- |
| A: open conjecture, no route | Stable XHigh main; differentiated High breadth plus falsification/computation as useful; no direct Max |
| B: mature proof framework | XHigh main deepens it; no ceremonial explorer fan-out or main-thread gear change |
| C: one critical lemma | Check localization and the Max signals; send one self-contained Max task if the gate passes |
| D: Max fails | Integrate a failure certificate; without new mathematics, prohibit immediate Max retry |
| E: complete candidate proof | Fresh independent auditor, then exact/formal verification where valuable |
| F: simple enumeration or script | Prefer the controlled one-shot mechanical broker for a finite packet; verify the returned artifacts, interpret in the parent, and stop |
| G: four independent directions | Multi-agent or Ultra may be justified; assign distinct methods, targets, and stop conditions |
| H: large inseparable lemma | No Ultra; preserve one context and use local Max only if its evidence gate passes |
