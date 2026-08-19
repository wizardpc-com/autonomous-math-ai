# Contributing

Thank you for helping improve Autonomous Math AI. Contributions are welcome in
the form of bug reports, tests, documentation, protocol hardening, and focused
implementation changes.

## Trust boundary first

Changes must preserve these invariants:

- model output is untrusted until deterministic checks and independent audit;
- a failed bounded search is not a proof;
- canonical state changes only through the canonical gate;
- evidence and lifecycle records are append-only;
- incompatible representations fail closed;
- mechanical workers are bounded, one-shot, non-recursive, and strategy-free;
- mock and live paths use the same protocol/schema checks;
- missing telemetry is not reported as a confirmed zero;
- no route uses fast or priority service.

Changes that relax a schema, silently swallow an error, rewrite evidence, or
turn an internal failure into a normal terminal state will not be accepted.

## Development setup

Use Python 3.11 or later:

```console
python -m venv .venv
python -m pip install --upgrade pip build
python -m pip install -e .
python -m unittest discover -s tests -p "test_*.py" -q
```

Build both distributions before submitting packaging changes:

```console
python -m build
```

All validation must be zero-model unless a maintainer explicitly authorizes a
live smoke test. Never include credentials, local absolute paths, campaign run
directories, generated outcomes, or conjecture-specific material in a change.

## Pull requests

Keep pull requests small and auditable. Include:

1. the problem and trust-boundary impact;
2. the exact behavior change;
3. tests and commands run;
4. migration or recovery considerations;
5. remaining risks.

Protocol and lifecycle fixes should include a deterministic replay or failure
fixture whenever practical. Documentation-only changes should still pass
`git diff --check` and the release-content scan.

By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
