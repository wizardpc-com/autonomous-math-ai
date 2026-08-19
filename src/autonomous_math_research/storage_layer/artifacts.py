"""Compatibility wrapper for :mod:`autonomous_math_research.storage.artifacts`."""

from ..storage.artifacts import (
    PORTABLE_SCHEMES,
    ArtifactStore,
    portable_project_uri,
    resolve_portable_uri,
)

__all__ = [
    "PORTABLE_SCHEMES",
    "ArtifactStore",
    "portable_project_uri",
    "resolve_portable_uri",
]
