from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from . import __version__
from .capabilities import inspect_generated_schema


@lru_cache(maxsize=1)
def _source_identity() -> dict[str, Any]:
    package_root = Path(__file__).resolve().parent
    files = sorted(
        path for path in package_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.casefold() in {".py", ".json", ".md"}
    )
    source_hash = sha256()
    for path in files:
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        source_hash.update(len(relative).to_bytes(4, "big"))
        source_hash.update(relative)
        data = path.read_bytes()
        source_hash.update(len(data).to_bytes(8, "big"))
        source_hash.update(data)

    repository = next(
        (parent for parent in (package_root, *package_root.parents) if (parent / ".git").exists()),
        None,
    )
    revision: str | None = None
    dirty: bool | None = None
    if repository is not None:
        try:
            revision = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=True,
            ).stdout.strip() or None
            dirty = bool(subprocess.run(
                ["git", "-C", str(repository), "status", "--short", "--untracked-files=no"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                check=True,
            ).stdout.strip())
        except (OSError, subprocess.CalledProcessError):
            revision = None
            dirty = None
    return {
        "amr_version": __version__,
        "python_version": sys.version.split()[0],
        "source_root": str(package_root),
        "source_git_revision": revision,
        "source_tree_dirty": dirty,
        "source_sha256": source_hash.hexdigest(),
    }


def _codex_capability() -> dict[str, Any]:
    return inspect_generated_schema(work_root=Path(tempfile.gettempdir()))


def _codex_identity(capability: dict[str, Any]) -> dict[str, Any]:
    required_view = {
        key: capability[key]
        for key in (
            "methods", "notifications", "thread_token_usage_fields",
            "thread_goal_fields", "thread_start_fields", "turn_start_fields",
            "service_tier", "sandbox_policy_variants",
        )
    }
    encoded = json.dumps(
        required_view, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "codex_cli_version": capability["codex_version"],
        "app_server_schema_sha256": capability["schema_sha256"],
        "app_server_required_protocol_sha256": sha256(encoded).hexdigest(),
    }


def capture_runtime_provenance(
    *,
    include_codex: bool,
    require_fast_service_tier: bool = False,
) -> dict[str, Any]:
    capability = _codex_capability() if include_codex else None
    if require_fast_service_tier:
        service_tier = (
            capability.get("service_tier") if isinstance(capability, dict) else None
        )
        required = (
            "thread_start_supports_clear",
            "turn_start_supports_clear",
            "thread_start_reports_tier",
        )
        missing = [
            name for name in required
            if not isinstance(service_tier, dict) or service_tier.get(name) is not True
        ]
        if missing:
            raise ValueError(
                "Codex App Server schema cannot safely enable fast service tier; "
                f"missing capabilities: {', '.join(missing)}"
            )
    codex = _codex_identity(capability) if capability is not None else {
        "codex_cli_version": None,
        "app_server_schema_sha256": None,
        "app_server_required_protocol_sha256": None,
    }
    return {**_source_identity(), **codex}
