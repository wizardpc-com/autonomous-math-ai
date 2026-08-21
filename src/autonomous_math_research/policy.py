from __future__ import annotations

from contextlib import contextmanager
import hmac
from importlib.resources import as_file, files
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Iterator

from .config import HarnessConfig
from .models import stable_hash
from .storage import atomic_write_json, file_digest


POLICY_SCHEMA_VERSION = 5
STABLE_CORE = "persistent_filesystem_controller"
POLICY_NAME = "math-research"
POLICY_ROLES = {
    "director", "prover", "falsifier", "explorer",
    "auditor", "evaluator_auditor", "smoke",
}
PRECEDENCE = (
    "The persistent deterministic controller and filesystem state are the stable core. "
    "Fresh Directors, research workers, and Auditors are ephemeral and have no hidden-state authority."
)
WORKER_BOUNDARY = "bounded mechanical execution only; never research strategy or recursive scheduling"
PACK_ROOT = "resources/policy_packs/math-research"
ROLE_REFERENCES = {
    "director": (
        "references/compute-orchestration.md", "references/falsification-policy.md",
        "references/project-state.md", "references/verification-levels.md",
        "references/worker-contract.md",
    ),
    "prover": (
        "references/falsification-policy.md", "references/verification-levels.md",
        "references/worker-contract.md",
    ),
    "falsifier": (
        "references/falsification-policy.md", "references/tool-routing.md",
        "references/experiment-protocol.md", "references/verification-levels.md",
        "references/worker-contract.md",
    ),
    "explorer": (
        "references/compute-orchestration.md", "references/falsification-policy.md",
        "references/tool-routing.md", "references/experiment-protocol.md",
        "references/verification-levels.md", "references/worker-contract.md",
    ),
    "auditor": (
        "references/verification-levels.md", "references/formalization-policy.md",
        "references/worker-contract.md",
    ),
    "evaluator_auditor": (
        "references/verification-levels.md", "references/tool-routing.md",
        "references/experiment-protocol.md", "references/worker-contract.md",
    ),
    "smoke": ("references/verification-levels.md",),
}
WORKER_RESOURCES = {
    "runner": f"{PACK_ROOT}/scripts/run_worker.py",
    "task_schema": f"{PACK_ROOT}/references/worker-task.schema.json",
    "result_schema": f"{PACK_ROOT}/references/worker-result.schema.json",
    "broker_client": "delegate_mechanical_task.py",
    "schema_validator": "schema.py",
    "contract_definitions": "contracts.py",
}


def _uri(relative: str) -> str:
    return f"package://autonomous_math_research/{relative}"


@contextmanager
def _resource_path(relative: str) -> Iterator[Path]:
    local = Path(__file__).resolve().parent / relative
    if local.is_file():
        yield local
        return
    target = files("autonomous_math_research").joinpath(relative)
    with as_file(target) as path:
        resolved = Path(path)
        if not resolved.is_file():
            raise ValueError(f"packaged policy resource is missing: {relative}")
        yield resolved


def _entry(relative: str) -> dict[str, str]:
    normalized = PurePosixPath(relative)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"invalid packaged policy resource: {relative}")
    with _resource_path(relative) as path:
        digest = file_digest(path)
    return {
        "uri": _uri(normalized.as_posix()),
        "snapshot_path": f"files/{normalized.as_posix()}",
        "sha256": digest,
    }


def _validate_entry(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"uri", "snapshot_path", "sha256"}:
        raise ValueError(f"{label} fields are invalid")
    uri = str(value["uri"])
    prefix = "package://autonomous_math_research/"
    if not uri.startswith(prefix):
        raise ValueError(f"{label} must use a package URI")
    relative = PurePosixPath(uri[len(prefix):])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} URI is not portable")
    if value["snapshot_path"] != f"files/{relative.as_posix()}":
        raise ValueError(f"{label} snapshot path is invalid")
    digest = str(value["sha256"])
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError(f"{label} digest is invalid")
    return value


def _validate_manifest(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "policy_name", "stable_core", "precedence", "skill",
        "role_references", "one_shot_compute_worker", "manifest_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("pinned policy manifest fields must exactly match the current contract")
    if value["schema_version"] != POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported pinned policy schema_version")
    if value["policy_name"] != POLICY_NAME:
        raise ValueError("pinned policy_name is invalid")
    if value["stable_core"] != STABLE_CORE:
        raise ValueError("pinned policy stable_core is invalid")
    if value["precedence"] != PRECEDENCE:
        raise ValueError("pinned policy precedence is invalid")
    skill = _validate_entry(value["skill"], "pinned policy")
    if skill["uri"] != _uri(f"{PACK_ROOT}/POLICY.md"):
        raise ValueError("pinned policy skill URI is rebound")
    references = value["role_references"]
    if not isinstance(references, dict) or set(references) != POLICY_ROLES:
        raise ValueError("pinned policy roles are invalid")
    for role, entries in references.items():
        if not isinstance(entries, list) or len(entries) != len(ROLE_REFERENCES[role]):
            raise ValueError(f"pinned policy role {role} references are invalid")
        for index, entry in enumerate(entries):
            validated = _validate_entry(
                entry, f"pinned policy role {role} reference {index}"
            )
            expected_uri = _uri(f"{PACK_ROOT}/{ROLE_REFERENCES[role][index]}")
            if validated["uri"] != expected_uri:
                raise ValueError(
                    f"pinned policy role {role} reference {index} URI is rebound"
                )
    worker = value["one_shot_compute_worker"]
    worker_keys = {
        "enabled", "boundary", "primary_route", "fallback_route",
        "fallback_condition", "recursive_spawn_allowed", "transient_max_retries",
        "model_protocol_max_retries", "selection_policy", "backpressure",
        "estimated_tokens", "estimated_cost_usd", *WORKER_RESOURCES,
    }
    if not isinstance(worker, dict) or set(worker) != worker_keys:
        raise ValueError("pinned mechanical worker policy fields are invalid")
    if type(worker["enabled"]) is not bool or worker["boundary"] != WORKER_BOUNDARY:
        raise ValueError("pinned mechanical worker enabled/boundary fields are invalid")
    route_keys = {
        "provider", "model", "endpoint", "profile", "effort",
        "unsupported_effort", "service_tier",
    }
    for route_name in ("primary_route", "fallback_route"):
        route = worker[route_name]
        if not isinstance(route, dict) or set(route) != route_keys:
            raise ValueError(f"pinned mechanical {route_name} fields are invalid")
        if not route["provider"] or not route["model"] or not route["effort"]:
            raise ValueError(f"pinned mechanical {route_name} is incomplete")
        if route["service_tier"] is not None:
            raise ValueError(f"pinned mechanical {route_name} has a forbidden service tier")
    if worker["fallback_condition"] not in {
        "provider_execution_failure",
        "permanent_unavailable_or_access_denied",
    }:
        raise ValueError("pinned mechanical fallback condition is invalid")
    if worker["recursive_spawn_allowed"] is not False:
        raise ValueError("pinned mechanical worker must prohibit recursive spawn")
    for key in ("transient_max_retries", "model_protocol_max_retries"):
        if type(worker[key]) is not int or worker[key] < 0:
            raise ValueError(f"pinned mechanical {key} is invalid")
    if (
        not isinstance(worker["selection_policy"], dict)
        or set(worker["selection_policy"]) != {"mode", "custom_thresholds"}
    ):
        raise ValueError("pinned mechanical selection policy is invalid")
    if not isinstance(worker["backpressure"], dict):
        raise ValueError("pinned mechanical backpressure is invalid")
    if type(worker["estimated_tokens"]) is not int or worker["estimated_tokens"] <= 0:
        raise ValueError("pinned mechanical estimated_tokens is invalid")
    if worker["estimated_cost_usd"] is not None and (
        not isinstance(worker["estimated_cost_usd"], (int, float))
        or isinstance(worker["estimated_cost_usd"], bool)
        or worker["estimated_cost_usd"] <= 0
    ):
        raise ValueError("pinned mechanical estimated_cost_usd is invalid")
    for key in WORKER_RESOURCES:
        validated = _validate_entry(worker[key], f"pinned mechanical {key}")
        if validated["uri"] != _uri(WORKER_RESOURCES[key]):
            raise ValueError(f"pinned mechanical {key} URI is rebound")
    fingerprinted = dict(value)
    reported = str(fingerprinted.pop("manifest_sha256", ""))
    if not hmac.compare_digest(stable_hash(fingerprinted), reported):
        raise ValueError("pinned policy manifest fingerprint is invalid")
    return value


def build_policy_manifest(config: HarnessConfig) -> dict[str, Any]:
    policy = config.raw["policy"]
    if str(policy.get("pack") or POLICY_NAME) != POLICY_NAME:
        raise ValueError("only the bundled math-research policy pack is supported")
    worker = dict(policy.get("one_shot_compute_worker", {}))
    payload: dict[str, Any] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_name": POLICY_NAME,
        "stable_core": str(policy["stable_core"]),
        "precedence": PRECEDENCE,
        "skill": _entry(f"{PACK_ROOT}/POLICY.md"),
        "role_references": {
            role: [_entry(f"{PACK_ROOT}/{name}") for name in names]
            for role, names in sorted(ROLE_REFERENCES.items())
        },
        "one_shot_compute_worker": {
            "enabled": bool(worker.get("enabled", False)),
            "boundary": WORKER_BOUNDARY,
            "primary_route": worker["primary_route"],
            "fallback_route": worker["fallback_route"],
            "fallback_condition": worker["fallback_condition"],
            "recursive_spawn_allowed": worker["recursive_spawn_allowed"],
            "transient_max_retries": int(worker["transient_max_retries"]),
            "model_protocol_max_retries": int(worker["model_protocol_max_retries"]),
            "selection_policy": worker["selection_policy"],
            "backpressure": worker["backpressure"],
            "estimated_tokens": int(worker["estimated_tokens"]),
            "estimated_cost_usd": worker.get("estimated_cost_usd"),
            **{key: _entry(relative) for key, relative in WORKER_RESOURCES.items()},
        },
    }
    payload["manifest_sha256"] = stable_hash(payload)
    return _validate_manifest(payload)


def _pinned_files(manifest: dict[str, Any]) -> list[dict[str, str]]:
    worker = manifest["one_shot_compute_worker"]
    entries = [
        manifest["skill"],
        *(item for values in manifest["role_references"].values() for item in values),
        *(worker[key] for key in WORKER_RESOURCES),
    ]
    unique = {str(entry["uri"]): entry for entry in entries}
    return [unique[key] for key in sorted(unique)]


def _entry_relative(entry: dict[str, str]) -> str:
    return str(entry["uri"]).removeprefix("package://autonomous_math_research/")


def _verify_snapshots(manifest: dict[str, Any], manifest_path: Path) -> None:
    _validate_manifest(manifest)
    root = manifest_path.parent.resolve()
    for entry in _pinned_files(manifest):
        snapshot = (root / entry["snapshot_path"]).resolve()
        if not snapshot.is_relative_to(root):
            raise ValueError("pinned policy snapshot escapes run")
        if not snapshot.is_file() or file_digest(snapshot) != entry["sha256"]:
            raise ValueError(f"pinned policy snapshot is missing or modified: {entry['uri']}")


def pin_policy_manifest(
    config: HarnessConfig, path: Path, *, resume: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.exists():
        if not resume:
            raise ValueError("run already has a pinned math-research policy")
        existing = json.loads(path.read_text(encoding="utf-8"))
        _verify_snapshots(existing, path)
        try:
            current_sha: str | None = build_policy_manifest(config)["manifest_sha256"]
            current_error: str | None = None
        except Exception as exc:
            current_sha, current_error = None, str(exc)
        status = {
            "pinned_manifest_sha256": existing["manifest_sha256"],
            "current_source_manifest_sha256": current_sha,
            "source_drift": existing["manifest_sha256"] != current_sha,
            "source_error": current_error,
            "resume_uses_pinned_snapshot": True,
        }
        atomic_write_json(path.parent / "POLICY_STATUS.json", status)
        return existing, status
    if resume:
        raise ValueError("cannot resume a legacy run without a pinned policy")
    current = build_policy_manifest(config)
    for entry in _pinned_files(current):
        target = path.parent / entry["snapshot_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with _resource_path(_entry_relative(entry)) as source:
            shutil.copy2(source, target)
    atomic_write_json(path, current)
    _verify_snapshots(current, path)
    status = {
        "pinned_manifest_sha256": current["manifest_sha256"],
        "current_source_manifest_sha256": current["manifest_sha256"],
        "source_drift": False,
        "source_error": None,
        "resume_uses_pinned_snapshot": False,
    }
    atomic_write_json(path.parent / "POLICY_STATUS.json", status)
    return current, status


def policy_view_for_role(
    manifest: dict[str, Any], manifest_path: Path, role: str,
) -> dict[str, Any]:
    _validate_manifest(manifest)
    if role not in POLICY_ROLES:
        raise ValueError(f"unknown policy role: {role}")
    root = manifest_path.parent
    return {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest["manifest_sha256"],
        "stable_core": manifest["stable_core"],
        "precedence": manifest["precedence"],
        "skill_snapshot": str((root / manifest["skill"]["snapshot_path"]).resolve()),
        "required_reference_snapshots": [
            str((root / item["snapshot_path"]).resolve())
            for item in manifest["role_references"][role]
        ],
        "role": role,
        "one_shot_compute_worker": manifest["one_shot_compute_worker"],
    }
