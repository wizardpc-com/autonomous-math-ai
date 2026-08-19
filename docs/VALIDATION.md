# Zero-budget validation

Validation on 2026-08-19 used Codex CLI 0.147.0 and started no real model
turn. `codex app-server generate-json-schema --experimental --out <temp>`
generated 361 protocol files. The bundled Director, worker, auditor, candidate,
and mechanical schemas then passed the same local compatibility gate used by
mock and App Server execution.

The repository compatibility suite passed 240 tests. The standalone package
suite passed 10 tests on Windows and the same 10 tests under Ubuntu-22.04 WSL
with the official parallel Python 3.11.0rc1 runtime. The WSL check is useful
cross-platform evidence but does not replace the workflow's stable Python 3.11
job. The suite covers arbitrary project roots, manifest-owned project
identity, a nondefault runtime root, package resource lookup, immutable
campaign input, monotone lifecycle, and conjecture-neutral source scanning.
Forty-nine package sources (59 including repository compatibility/tests)
compiled, and
`git diff --check` reported no whitespace error.

The final wheel and source distribution were built offline. A fresh virtual
environment installed the wheel with `--no-index --no-deps`; installed commands
`amr --help`, `amr init`, and `amr validate` succeeded without repository source
imports. The final installed generic dry run `20260819T083622.627654Z` recorded zero
jobs and six events. The installed generic mock full cycle
`20260819T083623.123589Z` recorded five started/completed/terminal jobs, zero
cancellations, and 41 events. Both ended without internal failure.

The final wheel contains 72 files and has SHA-256
`884026bd6eb78879d6056da64c255e6fcddf58df209c0015fd1d591d318c4b01`.
The source distribution contains 94 files and has SHA-256
`9fa6b2b46059824889a1911a0e0f3e1fc6ef1ddbfb0eae20b91574e128524f17`.

The distribution inspection permits only the neutral template claim graph in
the source distribution. It rejects run/outcome directories, historical
artifacts, authentication material, machine-absolute paths, and
conjecture-specific identifiers. Packaged schemas and the one-shot worker
runner are available through the resource API and have stable SHA-256 digests.

The Windows and Ubuntu workflow is present but was not executed on GitHub in
this local validation. After separate user authorization on 2026-08-19, a
single-turn Director schema-role smoke completed through the live App Server
with the packaged v2 schema, explicit null service tier, observed telemetry,
and no canonical changes. That paid acceptance is distinct from the reproducible
zero-budget suite and does not constitute a mathematical result.
