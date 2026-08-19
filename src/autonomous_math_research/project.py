from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


PROJECT_MANIFEST_SCHEMA_VERSION = 1
PROJECT_MANIFEST_KEYS = frozenset({
    "schema_version", "project_id", "final_claim_id", "config",
    "claim_graph", "trusted_state", "runtime_root", "prompt_root",
    "canonical_inputs", "protected_paths",
})
CANONICAL_ROLES = frozenset({"director", "research", "audit"})
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def _portable_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty project-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError(f"{label} must be a normalized project-relative POSIX path")
    if path.parts and path.parts[0].endswith(":"):
        raise ValueError(f"{label} must not contain a drive prefix")
    return value


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    project_root: Path
    path: Path
    project_id: str
    final_claim_id: str
    config: str
    claim_graph: str
    trusted_state: str
    runtime_root: str
    prompt_root: str
    canonical_inputs: dict[str, tuple[str, ...]]
    protected_paths: tuple[str, ...]

    @classmethod
    def load(cls, project_root: Path, path: Path | None = None) -> "ProjectManifest":
        root = project_root.resolve()
        source = (path or root / "autonomous" / "project.json").resolve()
        if not source.is_relative_to(root):
            raise ValueError("project manifest must be inside the project root")
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != PROJECT_MANIFEST_KEYS:
            actual = set(raw) if isinstance(raw, dict) else set()
            raise ValueError(
                "project manifest fields differ; "
                f"missing={sorted(PROJECT_MANIFEST_KEYS - actual)}, "
                f"extra={sorted(actual - PROJECT_MANIFEST_KEYS)}"
            )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError("unsupported project manifest schema_version")
        project_id = str(raw["project_id"])
        if not _PROJECT_ID_RE.fullmatch(project_id):
            raise ValueError("project_id must be a normalized portable identifier")
        final_claim_id = str(raw["final_claim_id"])
        if not final_claim_id.strip():
            raise ValueError("final_claim_id must be non-empty")
        canonical = raw["canonical_inputs"]
        if not isinstance(canonical, dict) or set(canonical) != CANONICAL_ROLES:
            raise ValueError("canonical_inputs must contain exactly director, research, audit")
        normalized_inputs: dict[str, tuple[str, ...]] = {}
        for role in sorted(CANONICAL_ROLES):
            values = canonical[role]
            if not isinstance(values, list):
                raise ValueError(f"canonical_inputs.{role} must be an array")
            items = tuple(
                _portable_relative(item, f"canonical_inputs.{role}") for item in values
            )
            if len(items) != len(set(items)):
                raise ValueError(f"canonical_inputs.{role} contains duplicates")
            normalized_inputs[role] = items
        protected = raw["protected_paths"]
        if not isinstance(protected, list):
            raise ValueError("protected_paths must be an array")
        normalized_protected = tuple(
            _portable_relative(item, "protected_paths") for item in protected
        )
        if len(normalized_protected) != len(set(normalized_protected)):
            raise ValueError("protected_paths contains duplicates")
        paths = {
            key: _portable_relative(raw[key], key)
            for key in (
                "config", "claim_graph", "trusted_state", "runtime_root", "prompt_root"
            )
        }
        manifest = cls(
            project_root=root,
            path=source,
            project_id=project_id,
            final_claim_id=final_claim_id,
            canonical_inputs=normalized_inputs,
            protected_paths=normalized_protected,
            **paths,
        )
        manifest.validate_paths()
        return manifest

    def resolve(self, relative: str, *, must_exist: bool = False) -> Path:
        normalized = _portable_relative(relative, "manifest path")
        target = (self.project_root / normalized).resolve()
        if not target.is_relative_to(self.project_root):
            raise ValueError(f"manifest path escapes project: {relative}")
        if must_exist and not target.exists():
            raise ValueError(f"manifest path does not exist: {relative}")
        return target

    def validate_paths(self) -> None:
        for relative in (
            self.config, self.claim_graph, self.trusted_state, self.prompt_root,
        ):
            self.resolve(relative, must_exist=True)
        runtime = self.resolve(self.runtime_root, must_exist=True)
        if not runtime.is_dir():
            raise ValueError("runtime_root must name an existing directory")
        for values in self.canonical_inputs.values():
            for relative in values:
                target = self.resolve(relative, must_exist=True)
                if not target.is_file():
                    raise ValueError(f"canonical input is not a file: {relative}")
        for relative in self.protected_paths:
            self.resolve(relative)

    def canonical_for(self, role: str) -> tuple[Path, ...]:
        if role not in CANONICAL_ROLES:
            raise ValueError(f"unknown canonical input role: {role}")
        return tuple(self.resolve(item, must_exist=True) for item in self.canonical_inputs[role])


def discover_workspace_root(project_root: Path, explicit: Path | None = None) -> Path:
    project = project_root.resolve()
    if explicit is not None:
        root = explicit.resolve()
        if not project.is_relative_to(root) and root != project:
            raise ValueError("--workspace-root must contain the target project")
        return root
    for candidate in (project, *project.parents):
        if (candidate / ".git").exists():
            return candidate
    return project
