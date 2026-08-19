from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from uuid import uuid4


REQUIRED_METHODS = [
    "thread/start", "thread/resume", "thread/fork",
    "thread/goal/set", "thread/goal/get", "thread/goal/clear",
    "turn/start", "turn/steer", "turn/interrupt",
    "permissionProfile/list", "account/rateLimits/read", "account/usage/read",
]
REQUIRED_NOTIFICATIONS = ["turn/started", "turn/completed", "thread/tokenUsage/updated"]


def _resolve_codex(codex: str) -> str:
    if os.name == "nt" and codex == "codex":
        npm_shim = shutil.which("codex.cmd")
        if npm_shim:
            native = (
                Path(npm_shim).parent
                / "node_modules" / "@openai" / "codex" / "node_modules"
                / "@openai" / "codex-win32-x64" / "vendor"
                / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
            )
            if native.exists():
                return str(native)
            return npm_shim
        return shutil.which("codex.exe") or codex
    return shutil.which(codex) or codex


def local_codex_version(codex: str = "codex") -> str:
    executable = _resolve_codex(codex)
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()


def inspect_generated_schema(codex: str = "codex", work_root: Path | None = None) -> dict[str, Any]:
    executable = _resolve_codex(codex)
    base = (work_root or Path.cwd() / ".amr-schema-tmp").resolve()
    base.mkdir(parents=True, exist_ok=True)
    directory = base / f"schema-{uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    try:
        subprocess.run(
            [executable, "app-server", "generate-json-schema", "--experimental", "--out", str(directory)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        path = directory / "codex_app_server_protocol.v2.schemas.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        encoded = json.dumps(raw, ensure_ascii=False)
        return {
            "codex_version": local_codex_version(executable),
            "schema_file": path.name,
            "methods": {name: f'"{name}"' in encoded for name in REQUIRED_METHODS},
            "notifications": {name: f'"{name}"' in encoded for name in REQUIRED_NOTIFICATIONS},
            "thread_token_usage_fields": _definition_properties(raw, "TokenUsageBreakdown"),
            "thread_goal_fields": _definition_properties(raw, "ThreadGoalSetParams"),
            "thread_start_fields": _definition_properties(raw, "ThreadStartParams"),
            "turn_start_fields": _definition_properties(raw, "TurnStartParams"),
            "service_tier": {
                "thread_start_supports_clear": "serviceTier" in _definition_properties(raw, "ThreadStartParams"),
                "turn_start_supports_clear": "serviceTier" in _definition_properties(raw, "TurnStartParams"),
                "requested_value": None,
            },
            "sandbox_policy_variants": _sandbox_variants(raw),
        }
    finally:
        if directory.is_relative_to(base):
            shutil.rmtree(directory, ignore_errors=True)


def _definition_properties(raw: dict[str, Any], name: str) -> list[str]:
    return sorted((raw.get("definitions", {}).get(name, {}).get("properties") or {}).keys())


def _sandbox_variants(raw: dict[str, Any]) -> list[str]:
    variants: list[str] = []
    for option in raw.get("definitions", {}).get("SandboxPolicy", {}).get("oneOf", []):
        variants.extend(option.get("properties", {}).get("type", {}).get("enum", []))
    return sorted(variants)
