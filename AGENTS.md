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
- Do not read, print, persist, or request authentication secrets.
- Never use fast or priority service tiers.

## Validation

- Keep fixtures mathematically neutral and portable.
- Run unit tests, release-content scanning, build/install acceptance, and
  `git diff --check` before publishing.
