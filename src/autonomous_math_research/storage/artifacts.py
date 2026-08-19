from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import shutil
from typing import Iterable

from ..models import CandidateEvent
from . import atomic_write_json, file_digest


PORTABLE_SCHEMES = ("project://", "campaign://", "epoch://")


def portable_project_uri(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("artifact is outside the target project")
    return "project://" + resolved.relative_to(root).as_posix()


def resolve_portable_uri(
    project_root: Path,
    runtime_root: Path,
    value: str,
) -> Path:
    """Resolve a durable URI without depending on the harness source layout."""
    scheme, tail = _safe_uri_tail(value)
    project = project_root.resolve()
    runtime = runtime_root.resolve()
    if scheme == "project://":
        root = project
        target = (root / Path(*tail.parts)).resolve()
    elif scheme == "epoch://":
        if len(tail.parts) < 2:
            raise ValueError(f"invalid durable artifact URI: {value}")
        root = (runtime / "runs" / tail.parts[0]).resolve()
        target = (root / Path(*tail.parts[1:])).resolve()
    else:
        if len(tail.parts) < 2:
            raise ValueError(f"invalid durable artifact URI: {value}")
        root = (runtime / "campaigns" / tail.parts[0]).resolve()
        target = (root / Path(*tail.parts[1:])).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"durable artifact URI is unavailable: {value}")
    return target


def _safe_uri_tail(value: str) -> tuple[str, PurePosixPath]:
    scheme = next((item for item in PORTABLE_SCHEMES if value.startswith(item)), None)
    if scheme is None:
        raise ValueError(f"unsupported durable artifact URI: {value}")
    tail = PurePosixPath(value[len(scheme):])
    if tail.is_absolute() or ".." in tail.parts or not tail.parts:
        raise ValueError(f"invalid durable artifact URI: {value}")
    return scheme, tail


@dataclass(slots=True)
class ArtifactStore:
    project_root: Path
    campaign_id: str
    epoch_id: str
    epoch_root: Path

    @property
    def bundle_root(self) -> Path:
        return self.epoch_root / "candidate_bundles"

    def resolve_uri(self, value: str) -> Path:
        return resolve_portable_uri(
            self.project_root,
            self.epoch_root.parent.parent,
            value,
        )

    def _source_path(self, value: str) -> tuple[Path, str]:
        if value.startswith(PORTABLE_SCHEMES):
            path = self.resolve_uri(value)
            return path, value
        raw = Path(value)
        path = (
            (self.project_root / raw).resolve()
            if not raw.is_absolute() else raw.resolve()
        )
        if not path.is_relative_to(self.project_root.resolve()) or not path.is_file():
            raise ValueError(f"candidate artifact is unavailable or outside project: {value}")
        if path.is_symlink():
            raise ValueError(f"candidate artifact cannot be a symbolic link: {value}")
        return path, portable_project_uri(self.project_root, path)

    def seal_candidate(self, event: CandidateEvent) -> dict[str, str]:
        entries: list[dict[str, object]] = []
        sealed_paths: list[str] = []
        hashes: dict[str, str] = {}
        for raw in event.artifact_paths:
            source, source_ref = self._source_path(raw)
            digest = file_digest(source)
            safe_name = source.name or "artifact"
            relative = Path("candidate_bundles") / event.fingerprint / digest / safe_name
            target = (self.epoch_root / relative).resolve()
            if not target.is_relative_to(self.epoch_root.resolve()):
                raise ValueError("candidate bundle target escapes epoch")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not target.is_file() or file_digest(target) != digest:
                    raise ValueError("content-addressed candidate bundle collision")
            else:
                shutil.copy2(source, target)
            uri = f"epoch://{self.epoch_id}/{relative.as_posix()}"
            sealed_paths.append(uri)
            hashes[uri] = digest
            entries.append({
                "uri": uri,
                "sha256": digest,
                "size": target.stat().st_size,
                "source_ref": source_ref,
            })
        event.artifact_paths = sealed_paths
        manifest = self.bundle_root / event.fingerprint / "MANIFEST.json"
        atomic_write_json(manifest, {
            "schema_version": 1,
            "candidate_fingerprint": event.fingerprint,
            "representation_id": event.representation_id,
            "artifacts": entries,
        })
        return hashes

    def verify(self, hashes: dict[str, str]) -> tuple[bool, dict[str, str]]:
        observed: dict[str, str] = {}
        for uri in hashes:
            try:
                observed[uri] = file_digest(self.resolve_uri(uri))
            except ValueError:
                continue
        return observed == hashes, observed

    def materialize(self, uris: Iterable[str], target_root: Path) -> list[Path]:
        target = target_root.resolve()
        copied: list[Path] = []
        for uri in uris:
            source = self.resolve_uri(uri)
            digest = file_digest(source)
            destination = target / "candidate_bundle" / digest / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
            copied.append(destination)
        return copied
