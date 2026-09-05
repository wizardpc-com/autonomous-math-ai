# Astra/handoff repair acceptance — 2026-09-06

Baseline: `ea7ffa92ac39c34acb55f1f4b3ce356497148c76`, version 0.2.19.
The version and existing tags are unchanged.

## Failure and repair

[Baseline CI](https://github.com/wizardpc-com/autonomous-math-ai/actions/runs/33977419170)
failed in all four Windows/Linux and Python 3.11/3.x jobs. The reconciliation
test supplied `MockCodexBackend` but deliberately retained `mock=False` to
exercise canonical reconciliation. `_pin_run_inputs` therefore requested real
runtime provenance, which generated a Codex schema and raised `FileNotFoundError`
on hosts without the CLI. An installed local CLI masked the missing test fixture.

The test now injects a neutral capability response at
`provenance.inspect_generated_schema`. Canonical reconciliation and the real-mode
flag remain active. A separate regression points executable resolution at an
absent file and requires non-mock input pinning to fail before a run manifest is
published. Production provenance and controller gates are unchanged.

The Astra profile retains the prescribed high/xhigh roles, null tiers, disabled
Fast, legacy defaults, and mechanical routes. Probe regressions now reject
unrelated command events, disallowed observed turn tiers, reused threads, and
cleanup failures. Thread configuration and observed turn tier remain separate.
All transport tests use fakes; they do not call a model.

Handoff sealing checks the retained input-manifest hash during copying and before
publication. Import requires the complete frozen input references and consistent
manifest, binding, and source hashes. Results remain unaudited and cannot create
an audit receipt, enter the candidate queue, or update canonical authority.

## Local acceptance

Both platforms used isolated wheel installations with Codex absent from the
test process PATH. Tests ran against the installed package, not an editable
source installation.

| Check | Windows 11 / Python 3.14.2 | WSL Ubuntu 22.04 / Python 3.11.0rc1 |
| --- | --- | --- |
| Full unit suite | 474 passed, 276.920 s | 473 passed; 1 existing Windows-only skip, 122.277 s |
| Astra/handoff suite | 20 passed | 20 passed |
| `python -m build` | wheel + sdist passed | wheel + sdist passed |
| `python scripts/check_release.py dist/*` | both passed | both passed |
| Isolated wheel install and `pip check` | passed | passed |
| `amr --help` | passed | passed |
| Generic init, validate, dry-run, mock | passed | passed |
| Astra profile, offline probe, handoff round trip | passed | passed |

Each installed-CLI acceptance record contains 16 successful checks. Compileall
and Git whitespace checks also passed. Linux's existing skip is the Windows npm
wrapper behavior test; no test was disabled for this repair. The WSL interpreter
is a prerelease and does not substitute for GitHub's stable Python matrix.

For Linux checkout-equivalent tests, retain the repository's root `AGENTS.md`:
existing mechanical fixtures read that file, while the sdist does not ship it.
An initial sdist-only test setup failed those fixtures; no production or test
logic was changed to accommodate that setup. Windows-created package files also
required fresh byte-identical input copies for WSL readability; ACLs were not
relaxed. These setup failures are separate from the original CI failure.

## Reproduction and integration boundary

Build with `python -m build`, scan with
`python scripts/check_release.py dist/*`, and install the wheel in a fresh venv
using `python -m pip install --no-index --no-deps <wheel>`. In that venv, with
Codex excluded from PATH, run:

```console
python -m pip check
python -m unittest discover -s tests -p "test_*.py" -q
python -m unittest discover -s tests -p "test_astra_handoff.py" -v
amr --help
amr init ./neutral-target
amr validate --project ./neutral-target
amr run --project ./neutral-target --dry-run
amr run --project ./neutral-target --mock --hours 0.01
git diff --check
```

Real local capability inspection remains a separate, zero-model entry point on
hosts that have Codex installed:

```console
python -c "import json; from autonomous_math_research.capabilities import inspect_generated_schema; print(json.dumps(inspect_generated_schema(), indent=2))"
```

That check passed locally with `codex-cli 0.153.3`; generated schema SHA-256:
`e5f798fd1343c539f01fedea0e8a84a43c080fcca4615c80eb04a5edab4f7d0a`.
Schema generation reflects the installed CLI version, as described in the
[App Server documentation](https://learn.chatgpt.com/docs/app-server#message-schema).
The existing `amr probe` integration entry is also retained; it additionally
starts App Server and reads live capabilities and was not used for unit testing.

Live Astra routing, account model availability, actual permission isolation, and
real research campaigns remain unverified. No mathematical project, historical
evidence, ClaimGraph, or trusted state was modified outside neutral fixtures.
