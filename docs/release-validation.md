# Release validation

All default release validation is zero-model. It must not start a live App
Server turn, read credentials, or depend on an external research target.

## Unit tests

```console
python -m unittest discover -s tests -p "test_*.py" -q
```

The suite covers arbitrary target roots, manifest-owned identity, nondefault
runtime roots, package resource lookup, immutable campaign input, monotone
lifecycle, generic mock execution, and conjecture-neutral release scanning.

## Build

```console
python -m build
```

Inspect both wheel and source distribution. They may contain only package code,
neutral templates, schemas, the bundled policy pack, documentation, tests, and
standard release metadata. Reject campaign runs, generated outcomes, historical
evidence archives, credentials, private keys, machine-specific paths, and
conjecture-specific identifiers.

## Fresh installation

Install the wheel into a new virtual environment without importing source from
the checkout:

```console
python -m venv <temporary-directory>
<temporary-python> -m pip install --no-index --no-deps <wheel>
<temporary-amr> --help
<temporary-amr> init <temporary-target>
<temporary-amr> validate --project <temporary-target>
<temporary-amr> run --project <temporary-target> --dry-run
<temporary-amr> run --project <temporary-target> --mock --hours 0.01
```

The dry run must report zero model turns. Mock telemetry must be labeled
synthetic and must not be presented as mathematical evidence.

## Cross-platform CI

GitHub Actions runs on Windows and Ubuntu with Python 3.11 and the latest stable
Python release. The workflow builds distributions, installs the wheel, executes
the complete standalone suite, validates the CLI entry point, and checks package
metadata without a live model.

## Final repository checks

```console
git diff --check
```

Before a release, also verify the remote target is empty or a fast-forward,
confirm the exact commit being published, and prohibit force, mirror, or all-ref
pushes.
