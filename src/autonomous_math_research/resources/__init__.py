"""Bundled, immutable-by-wheel protocol and policy resources."""

from importlib.resources import as_file, files
from pathlib import Path
from typing import Iterator


def resource(name: str) -> Iterator[Path]:
    """Yield a filesystem path for a bundled resource.

    Callers must use this as a context manager because installed wheels may be
    imported from a zip-capable loader.
    """
    local = Path(__file__).resolve().parent / name
    if local.exists():
        class _LocalContext:
            def __enter__(self) -> Path:
                return local

            def __exit__(self, *_args: object) -> None:
                return None

        return _LocalContext()  # type: ignore[return-value]
    return as_file(files("autonomous_math_research.resources").joinpath(name))


def schema_resource(name: str) -> Iterator[Path]:
    return resource(f"schemas/{name}")


def policy_resource(name: str) -> Iterator[Path]:
    return resource(f"policy_packs/math-research/{name}")
