# Neutral Experiment Runner fixtures

These fixtures use only Python 3.11+ standard-library code and project-relative
POSIX paths:

- `certified/` checks a small JSON certificate deterministically;
- `empirical/` runs a frozen decimal-mean protocol over a fixed CSV input.

The fixture subtree pins LF line endings so its declared input digests remain
the same on Windows and POSIX checkouts.

The manifests pin the scripts and data by SHA-256. Copy this directory to a
scratch location before running it because the runner intentionally creates
`autonomous/experiments/` beneath the selected project root.

From the copied `experiment-runner` directory:

```console
amr experiment validate --project . --manifest certified/manifest.json
amr experiment run --project . --manifest certified/manifest.json
amr experiment validate --project . --manifest empirical/manifest.json
amr experiment run --project . --manifest empirical/manifest.json
```

Or use the Python API:

```python
from pathlib import Path

from autonomous_math_research.experiment import ExperimentRunner

root = Path.cwd().resolve()
certified = ExperimentRunner(root).run(root / "certified/manifest.json")
empirical = ExperimentRunner(root).run(root / "empirical/manifest.json")
print(certified.root)
print(empirical.root)
```

The manifests use `python` as the portable executable name used throughout the
documentation. If the local interpreter has another name, change the first
`argv` item in a copied manifest. That deliberate manifest change produces a
different content-addressed run ID. The portable `3.11-or-later` version value
is likewise illustrative; replace it with the exact interpreter version before
using a copied manifest as real evidence.

The checker and protocol outputs are raw evidence only. An exit code of zero or
a JSON field such as `supported: true` does not create `CERTIFIED`, `CONFIRMED`,
`REPLICATED`, or any other canonical status.
