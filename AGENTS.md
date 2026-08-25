# Repository instructions

## Scope

- This repository contains only the generic Autonomous Math AI harness, its
  schemas, policy packs, documentation, and neutral tests.
- Never add conjecture-specific statements, claims, prompts, task packets,
  runs, outcomes, audits, proofs, or research artifacts.
- Integration with a research project must use the public `amr` CLI and that
  project's `autonomous/project.json`; do not assume a monorepo or `projects/`
  directory.

## Trust boundary

- Preserve falsification-first scheduling, independent audit, append-only
  evidence, canonical guards, crash recovery, schema preflight, and mechanical
  worker isolation.
- Model output is never proof by itself. Trusted state changes require the
  deterministic controller and audit/canonical gates.
- When a project supplies `autonomous/semantics.json`, treat its research
  contract and registry as declarative canonical inputs, never as trusted
  verification state. Enforce `No unverified bridge into trusted final claims`
  from controller-owned candidate/audit receipts; validator success or
  agreement among agents cannot replace bridge evidence.
- Projects without semantic metadata remain legacy-compatible and report the
  independent semantic status `UNREVIEWED`; do not convert that status into a
  claim, trust, evidence, or execution failure.
- Do not read, print, persist, or request authentication secrets.
- Fast is permitted only when `execution.fast_mode=true` is explicitly pinned
  for the run. Request only `fast`; accept `priority` solely as its observed
  server alias. Otherwise reject fast/priority/ultrafast, and keep mechanical
  tiers null.

## Validation

- Keep fixtures mathematically neutral and portable.
- Run unit tests, release-content scanning, build/install acceptance, and
  `git diff --check` before publishing.
