---
name: empirical-research
description: Frozen-protocol empirical computational research policy.
---

# Empirical research policy

The deterministic filesystem controller is authoritative. Model roles may
design a frozen protocol or interpret its outputs, but do not execute individual
benchmark runs and cannot directly change canonical trust state.

- Freeze hypotheses, datasets, splits, metrics, exclusions, stopping rules, and
  analysis choices before execution.
- Bind raw results to exact inputs, configuration, command, versions, hashes,
  stdout, stderr, exit status, and resource metadata.
- Separate infrastructure failures from empirical outcomes.
- Finite benchmark evidence must never be labeled mathematical `PROVED`.
- Confirmation and replication require their declared independent evidence and
  audit; canonical promotion remains a controller action.
