# Security policy

## Supported versions

Security fixes are provided for the latest `0.2.x` release line and the current
`main` branch while the project remains in alpha.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting or a private contact method listed
on the repository owner's GitHub profile. Do not disclose a vulnerability in a
public issue before a fix is available.

Please include, when safe:

- the affected version and platform;
- a minimal reproduction;
- the expected and observed security boundary;
- whether canonical state, evidence integrity, process isolation, path
  containment, or credential handling is affected;
- suggested mitigations, if known.

Do not send real credentials, access tokens, cookies, private keys, proprietary
research data, or copied campaign directories. Use synthetic fixtures.

## Security boundaries

Autonomous Math AI treats model output and imported assets as untrusted input.
High-impact security concerns include:

- escaping the target project or isolated worker workspace;
- modifying canonical state outside the controller gate;
- forging audit identity, representation compatibility, or artifact hashes;
- recursive or unbrokered subprocess delegation;
- leaking environment credentials into prompts, logs, or artifacts;
- bypassing the explicit main-role Fast opt-in or the mechanical
  no-fast/no-priority route policy;
- rewriting append-only evidence or recovery history.

The project is a research orchestration harness, not a sandbox for hostile
native code. Operators remain responsible for OS-level isolation, filesystem
permissions, and reviewing allowed tools before live execution.
