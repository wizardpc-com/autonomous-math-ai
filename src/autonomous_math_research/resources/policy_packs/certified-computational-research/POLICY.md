---
name: certified-computational-research
description: Deterministic checker and certificate driven computational research policy.
---

# Certified computational research policy

The deterministic filesystem controller is authoritative. Model roles may
design or interpret checks, but cannot manufacture checker outcomes or directly
change canonical trust state.

- Bind every result to exact inputs, configuration, command, checker version,
  hashes, stdout, stderr, exit status, and limitations.
- `SUPPORTED` records replayable checker support; it is not `CERTIFIED`.
- `CERTIFIED` requires certificate evidence accepted by the pinned deterministic
  checker and an independent audit.
- Infrastructure failure is not evidence for or against a research claim.
- Canonical promotion remains a deterministic controller action after the
  domain-required audit gates pass.
