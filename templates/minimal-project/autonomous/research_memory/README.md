# Shared research memory

This directory contains project-authored, structured coordination inputs. It is
not mathematical authority and never writes ClaimGraph or trusted state.

- `external_results/*.json` registers exact externally produced results.
- `assets/*.json` registers theorem, bridge, tool, verifier, kill-gate, or
  explicitly unproved hypothesis cards.
- `themes/*.json` defines campaign-local routing boundaries.

Each evidence reference binds a project-relative path to its SHA-256 digest.
`AUDITED_EXTERNAL_RESULT` requires an independent PASS audit block; a statement
in `CLAIMS.md` or `PROGRESS.md` is not enough. Run `amr frontier rebuild` to
validate inputs and regenerate `autonomous/coordination/`.

The `*.json.example` files are inert examples. Rename and complete one only
after every field and digest is known.
