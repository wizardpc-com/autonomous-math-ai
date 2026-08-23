from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from ..models import utc_now
from ..project import ProjectManifest


_STORAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def validate_storage_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _STORAGE_ID_RE.fullmatch(value):
        raise ValueError(
            f"{label} must be a portable identifier without path separators"
        )
    return value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}: {exc}") from exc
    return records


@dataclass(frozen=True, slots=True)
class ProjectLayout:
    project_root: Path

    @property
    def manifest(self) -> ProjectManifest | None:
        path = self.project_root / "autonomous" / "project.json"
        return ProjectManifest.load(self.project_root) if path.is_file() else None

    @property
    def autonomous_root(self) -> Path:
        manifest = self.manifest
        return (
            manifest.resolve(manifest.runtime_root)
            if manifest is not None else self.project_root / "autonomous"
        )

    @property
    def runs_root(self) -> Path:
        return self.autonomous_root / "runs"

    @property
    def nightly_root(self) -> Path:
        return self.autonomous_root / "nightly"

    @property
    def outcomes_root(self) -> Path:
        return self.autonomous_root / "outcomes"

    @property
    def experiments_root(self) -> Path:
        return self.autonomous_root / "experiments"

    @property
    def catalog_root(self) -> Path:
        return self.autonomous_root / "catalog"

    @property
    def campaigns_root(self) -> Path:
        return self.autonomous_root / "campaigns"

    @property
    def candidates_root(self) -> Path:
        return self.autonomous_root / "candidates"

    @property
    def audits_root(self) -> Path:
        return self.autonomous_root / "audits"

    @property
    def state_root(self) -> Path:
        return self.autonomous_root / "state"

    @property
    def inbox_root(self) -> Path:
        return self.autonomous_root / "events" / "inbox"

    @property
    def event_log(self) -> Path:
        return self.autonomous_root / "events" / "EVENTS.jsonl"

    @property
    def claim_graph_path(self) -> Path:
        manifest = self.manifest
        return (
            manifest.resolve(manifest.claim_graph)
            if manifest is not None else self.autonomous_root / "state" / "claim_graph.json"
        )

    @property
    def trusted_state_path(self) -> Path:
        manifest = self.manifest
        return (
            manifest.resolve(manifest.trusted_state)
            if manifest is not None else self.autonomous_root / "state" / "nightly_trusted.json"
        )

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / validate_storage_id(run_id, "run_id")

    def ensure(self) -> None:
        for path in (
            self.runs_root, self.nightly_root, self.outcomes_root, self.inbox_root,
            self.autonomous_root / "events" / "processed",
            self.candidates_root, self.audits_root, self.state_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


class EventStore:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self._sequence = 0
        existing = read_jsonl(path)
        if existing:
            self._sequence = max(int(item.get("sequence", 0)) for item in existing)

    def append(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self._sequence += 1
        record = {
            "sequence": self._sequence,
            "run_id": self.run_id,
            "timestamp": utc_now(),
            "kind": kind,
            "payload": payload or {},
        }
        append_jsonl(self.path, record)
        return record

    def replay(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class CanonicalGuard:
    """Detect any mutation to protected canonical project material."""

    def __init__(self, project_root: Path, protected: Iterable[str]):
        self.project_root = project_root.resolve()
        self.protected = tuple(protected)
        self.baseline: dict[str, str] = {}

    def snapshot(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in self.protected:
            target = (self.project_root / relative).resolve()
            if not target.is_relative_to(self.project_root):
                raise ValueError(f"protected path escapes project: {relative}")
            if target.is_file():
                result[target.relative_to(self.project_root).as_posix()] = file_digest(target)
            elif target.is_dir():
                for file in sorted(p for p in target.rglob("*") if p.is_file()):
                    result[file.relative_to(self.project_root).as_posix()] = file_digest(file)
        self.baseline = result
        return result

    def verify(self) -> list[str]:
        current = CanonicalGuard(self.project_root, self.protected).snapshot()
        changed = sorted(
            key for key in set(self.baseline) | set(current)
            if self.baseline.get(key) != current.get(key)
        )
        return changed

    def accept(self, paths: Iterable[Path]) -> None:
        """Advance the baseline only for controller-authorized target files."""
        protected_roots = [
            (self.project_root / relative).resolve() for relative in self.protected
        ]
        for raw_path in paths:
            path = raw_path.resolve()
            if not path.is_relative_to(self.project_root):
                raise ValueError("authorized canonical target escapes the project")
            if not any(
                path == root or path.is_relative_to(root)
                for root in protected_roots
            ):
                continue
            key = path.relative_to(self.project_root).as_posix()
            if path.is_file():
                self.baseline[key] = file_digest(path)
            else:
                self.baseline.pop(key, None)


def claim_graph_digest(path: Path) -> str:
    return file_digest(path)


# Import the higher-level stores only after the atomic primitives above exist;
# their modules depend on these functions during package initialization.
from .artifacts import ArtifactStore, portable_project_uri, resolve_portable_uri
from .steering import STEERING_KINDS, append_steering, ingest_asset
