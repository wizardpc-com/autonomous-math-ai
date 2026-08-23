from __future__ import annotations

from contextlib import contextmanager
import hmac
from importlib.resources import as_file, files
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import TYPE_CHECKING, Any, Iterator

from .domain_semantics import builtin_domain_contract, domain_semantics_from_contract
from .models import stable_hash
from .storage import atomic_write_json, file_digest

if TYPE_CHECKING:
    from .config import HarnessConfig


POLICY_SCHEMA_VERSION = 6
LEGACY_POLICY_SCHEMA_VERSION = 5
PACK_DESCRIPTOR_SCHEMA_VERSION = 1
STABLE_CORE = "persistent_filesystem_controller"
POLICY_NAME = "math-research"
PACKS_ROOT = "resources/policy_packs"
PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
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
WORKER_RESOURCE_KEYS = frozenset(WORKER_RESOURCES)


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


def _portable_resource(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty portable path")
    normalized = PurePosixPath(value)
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or normalized.as_posix() != value
    ):
        raise ValueError(f"{label} must be a normalized portable path")
    return value


def _pack_resource(pack_name: str, value: Any, label: str) -> str:
    relative = _portable_resource(value, label)
    return f"{PACKS_ROOT}/{pack_name}/{relative}"


def _discovered_pack_names() -> tuple[str, ...]:
    local_root = Path(__file__).resolve().parent / PACKS_ROOT
    if local_root.is_dir():
        names = {
            item.name
            for item in local_root.iterdir()
            if item.is_dir() and (item / "pack.json").is_file()
        }
    else:
        root = files("autonomous_math_research").joinpath(PACKS_ROOT)
        names = {
            item.name
            for item in root.iterdir()
            if item.is_dir() and item.joinpath("pack.json").is_file()
        }
    invalid = sorted(name for name in names if PACK_NAME_RE.fullmatch(name) is None)
    if invalid:
        raise ValueError(f"invalid bundled policy pack names: {invalid}")
    if not names:
        raise ValueError("no bundled policy packs were discovered")
    return tuple(sorted(names))


def _validate_pack_descriptor(
    value: Any,
    pack_name: str,
    *,
    verify_packaged_resources: bool,
) -> dict[str, Any]:
    keys = {
        "schema_version", "name", "skill", "roles", "domain_contract",
        "mechanical_resources",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("policy pack descriptor fields are invalid")
    if value["schema_version"] != PACK_DESCRIPTOR_SCHEMA_VERSION:
        raise ValueError("unsupported policy pack descriptor schema_version")
    if value["name"] != pack_name or PACK_NAME_RE.fullmatch(pack_name) is None:
        raise ValueError("policy pack descriptor name is invalid")

    resources: list[str] = [
        _pack_resource(pack_name, value["skill"], "policy pack skill"),
    ]
    roles = value["roles"]
    if not isinstance(roles, dict) or not POLICY_ROLES <= set(roles):
        raise ValueError("policy pack is missing required core roles")
    for role, definition in roles.items():
        if not isinstance(role, str) or PACK_NAME_RE.fullmatch(role.replace("_", "-")) is None:
            raise ValueError(f"invalid policy pack role: {role!r}")
        if not isinstance(definition, dict) or set(definition) != {"prompt", "references"}:
            raise ValueError(f"policy pack role {role} fields are invalid")
        resources.append(_pack_resource(
            pack_name, definition["prompt"], f"policy pack role {role} prompt",
        ))
        references = definition["references"]
        if not isinstance(references, list) or not references:
            raise ValueError(f"policy pack role {role} references are invalid")
        if any(not isinstance(reference, str) for reference in references):
            raise ValueError(f"policy pack role {role} references are invalid")
        if len(set(references)) != len(references):
            raise ValueError(f"policy pack role {role} references contain duplicates")
        resources.extend(
            _pack_resource(
                pack_name, reference, f"policy pack role {role} reference",
            )
            for reference in references
        )

    contract = value["domain_contract"]
    domain_semantics_from_contract(contract)
    if contract != builtin_domain_contract(pack_name):
        raise ValueError("policy pack domain contract does not match its builtin adapter")

    mechanical = value["mechanical_resources"]
    if not isinstance(mechanical, dict) or set(mechanical) != WORKER_RESOURCE_KEYS:
        raise ValueError("policy pack mechanical resource fields are invalid")
    if mechanical != WORKER_RESOURCES:
        raise ValueError(
            "policy pack mechanical resources must match the stable core bindings"
        )
    for key, relative in mechanical.items():
        resources.append(_portable_resource(
            relative, f"policy pack mechanical resource {key}",
        ))

    if verify_packaged_resources:
        for relative in sorted(set(resources)):
            _entry(relative)
    return value


def load_policy_pack_descriptor(pack_name: str) -> dict[str, Any]:
    if not isinstance(pack_name, str) or PACK_NAME_RE.fullmatch(pack_name) is None:
        raise ValueError(f"invalid policy pack name: {pack_name!r}")
    if pack_name not in _discovered_pack_names():
        raise ValueError(f"unknown policy pack: {pack_name}")
    relative = f"{PACKS_ROOT}/{pack_name}/pack.json"
    with _resource_path(relative) as path:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid policy pack descriptor: {pack_name}") from exc
    return _validate_pack_descriptor(
        value, pack_name, verify_packaged_resources=True,
    )


def discover_policy_packs() -> dict[str, dict[str, Any]]:
    """Return every strictly validated bundled policy pack by name."""
    return {
        name: load_policy_pack_descriptor(name)
        for name in _discovered_pack_names()
    }


def domain_contract_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the validated domain contract pinned by a policy manifest."""
    _validate_manifest(manifest)
    return builtin_domain_contract(str(manifest["policy_name"]))


def domain_contract_for_run(
    config: HarnessConfig,
    manifest_path: Path,
    *,
    resume: bool,
) -> dict[str, Any]:
    """Select source semantics for a new run or pinned semantics for resume."""
    if resume:
        if not manifest_path.is_file():
            pack_name = str(config.raw["policy"].get("pack") or POLICY_NAME)
            # A crash before policy pinning still needs the historical math
            # semantics for recovery inspection. Execution remains fail-closed
            # when pin_policy_manifest later requires the missing snapshot.
            if pack_name == POLICY_NAME:
                return builtin_domain_contract(POLICY_NAME)
            raise ValueError("cannot resume a legacy run without a pinned policy")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _verify_snapshots(manifest, manifest_path)
        return domain_contract_from_manifest(manifest)
    if manifest_path.exists():
        raise ValueError("run already has a pinned research policy")
    pack_name = str(config.raw["policy"].get("pack") or POLICY_NAME)
    return builtin_domain_contract(
        load_policy_pack_descriptor(pack_name)["domain_contract"]["domain"]
    )


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


def _validate_worker_policy(
    worker: Any,
    resources: dict[str, str],
) -> dict[str, Any]:
    worker_keys = {
        "enabled", "boundary", "primary_route", "fallback_route",
        "fallback_condition", "recursive_spawn_allowed", "transient_max_retries",
        "model_protocol_max_retries", "selection_policy", "backpressure",
        "estimated_tokens", "estimated_cost_usd", *WORKER_RESOURCE_KEYS,
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
    for key, relative in resources.items():
        validated = _validate_entry(worker[key], f"pinned mechanical {key}")
        if validated["uri"] != _uri(relative):
            raise ValueError(f"pinned mechanical {key} URI is rebound")
    return worker


def _validate_legacy_manifest(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "policy_name", "stable_core", "precedence", "skill",
        "role_references", "one_shot_compute_worker", "manifest_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("pinned policy manifest fields must exactly match the current contract")
    if value["schema_version"] != LEGACY_POLICY_SCHEMA_VERSION:
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
    _validate_worker_policy(value["one_shot_compute_worker"], WORKER_RESOURCES)
    fingerprinted = dict(value)
    reported = str(fingerprinted.pop("manifest_sha256", ""))
    if not hmac.compare_digest(stable_hash(fingerprinted), reported):
        raise ValueError("pinned policy manifest fingerprint is invalid")
    return value


def _validate_current_manifest(
    value: Any,
    descriptor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    keys = {
        "schema_version", "policy_name", "stable_core", "precedence",
        "descriptor", "skill", "role_prompts", "role_references",
        "domain_contract", "audit_requirements", "one_shot_compute_worker",
        "manifest_sha256",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("pinned policy manifest fields must exactly match the current contract")
    if value["schema_version"] != POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported pinned policy schema_version")
    pack_name = value["policy_name"]
    if not isinstance(pack_name, str) or PACK_NAME_RE.fullmatch(pack_name) is None:
        raise ValueError("pinned policy_name is invalid")
    if value["stable_core"] != STABLE_CORE:
        raise ValueError("pinned policy stable_core is invalid")
    if value["precedence"] != PRECEDENCE:
        raise ValueError("pinned policy precedence is invalid")

    descriptor_entry = _validate_entry(value["descriptor"], "pinned policy descriptor")
    expected_descriptor_uri = _uri(f"{PACKS_ROOT}/{pack_name}/pack.json")
    if descriptor_entry["uri"] != expected_descriptor_uri:
        raise ValueError("pinned policy descriptor URI is rebound")
    skill = _validate_entry(value["skill"], "pinned policy skill")

    prompts = value["role_prompts"]
    references = value["role_references"]
    if (
        not isinstance(prompts, dict)
        or not isinstance(references, dict)
        or set(prompts) != set(references)
        or not POLICY_ROLES <= set(prompts)
    ):
        raise ValueError("pinned policy roles are invalid")
    for role, prompt in prompts.items():
        _validate_entry(prompt, f"pinned policy role {role} prompt")
        entries = references[role]
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"pinned policy role {role} references are invalid")
        for index, entry in enumerate(entries):
            _validate_entry(entry, f"pinned policy role {role} reference {index}")

    contract = value["domain_contract"]
    domain_semantics_from_contract(contract)
    if contract != builtin_domain_contract(pack_name):
        raise ValueError("pinned policy domain contract is unsupported")
    if value["audit_requirements"] != contract["audit_requirements"]:
        raise ValueError("pinned policy audit requirements do not match domain contract")

    if descriptor is not None:
        _validate_pack_descriptor(
            descriptor, pack_name, verify_packaged_resources=False,
        )
        if value["domain_contract"] != descriptor["domain_contract"]:
            raise ValueError("pinned policy domain contract is rebound")
        expected_skill = _pack_resource(
            pack_name, descriptor["skill"], "policy pack skill",
        )
        if skill["uri"] != _uri(expected_skill):
            raise ValueError("pinned policy skill URI is rebound")
        expected_roles = descriptor["roles"]
        if set(prompts) != set(expected_roles):
            raise ValueError("pinned policy roles are rebound")
        for role, definition in expected_roles.items():
            expected_prompt = _pack_resource(
                pack_name, definition["prompt"], f"policy pack role {role} prompt",
            )
            if prompts[role]["uri"] != _uri(expected_prompt):
                raise ValueError(f"pinned policy role {role} prompt URI is rebound")
            expected_references = definition["references"]
            if len(references[role]) != len(expected_references):
                raise ValueError(f"pinned policy role {role} references are rebound")
            for index, relative in enumerate(expected_references):
                expected_reference = _pack_resource(
                    pack_name, relative, f"policy pack role {role} reference",
                )
                if references[role][index]["uri"] != _uri(expected_reference):
                    raise ValueError(
                        f"pinned policy role {role} reference {index} URI is rebound"
                    )
        resources = descriptor["mechanical_resources"]
    else:
        pack_prefix = _uri(f"{PACKS_ROOT}/{pack_name}/")
        if not skill["uri"].startswith(pack_prefix):
            raise ValueError("pinned policy skill URI leaves its pack")
        for role, prompt in prompts.items():
            if not prompt["uri"].startswith(pack_prefix):
                raise ValueError(f"pinned policy role {role} prompt URI leaves its pack")
            if any(not item["uri"].startswith(pack_prefix) for item in references[role]):
                raise ValueError(f"pinned policy role {role} reference URI leaves its pack")
        resources = {
            key: _entry_relative(value["one_shot_compute_worker"][key])
            for key in WORKER_RESOURCE_KEYS
        }
    _validate_worker_policy(value["one_shot_compute_worker"], resources)

    fingerprinted = dict(value)
    reported = str(fingerprinted.pop("manifest_sha256", ""))
    if not hmac.compare_digest(stable_hash(fingerprinted), reported):
        raise ValueError("pinned policy manifest fingerprint is invalid")
    return value


def _validate_manifest(
    value: Any,
    descriptor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("pinned policy manifest must be an object")
    version = value.get("schema_version")
    if version == LEGACY_POLICY_SCHEMA_VERSION:
        if descriptor is not None:
            raise ValueError("legacy policy manifest cannot bind a v6 descriptor")
        return _validate_legacy_manifest(value)
    if version == POLICY_SCHEMA_VERSION:
        return _validate_current_manifest(value, descriptor)
    raise ValueError("unsupported pinned policy schema_version")


def build_policy_manifest(config: HarnessConfig) -> dict[str, Any]:
    policy = config.raw["policy"]
    pack_name = str(policy.get("pack") or POLICY_NAME)
    descriptor = load_policy_pack_descriptor(pack_name)
    worker = dict(policy.get("one_shot_compute_worker", {}))
    pack_root = f"{PACKS_ROOT}/{pack_name}"
    payload: dict[str, Any] = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_name": pack_name,
        "stable_core": str(policy["stable_core"]),
        "precedence": PRECEDENCE,
        "descriptor": _entry(f"{pack_root}/pack.json"),
        "skill": _entry(f"{pack_root}/{descriptor['skill']}"),
        "role_prompts": {
            role: _entry(f"{pack_root}/{definition['prompt']}")
            for role, definition in sorted(descriptor["roles"].items())
        },
        "role_references": {
            role: [
                _entry(f"{pack_root}/{name}")
                for name in definition["references"]
            ]
            for role, definition in sorted(descriptor["roles"].items())
        },
        "domain_contract": descriptor["domain_contract"],
        "audit_requirements": descriptor["domain_contract"]["audit_requirements"],
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
            **{
                key: _entry(relative)
                for key, relative in descriptor["mechanical_resources"].items()
            },
        },
    }
    payload["manifest_sha256"] = stable_hash(payload)
    return _validate_manifest(payload, descriptor)


def _pinned_files(manifest: dict[str, Any]) -> list[dict[str, str]]:
    worker = manifest["one_shot_compute_worker"]
    entries = [
        *([manifest["descriptor"]] if manifest["schema_version"] >= 6 else []),
        manifest["skill"],
        *(
            list(manifest["role_prompts"].values())
            if manifest["schema_version"] >= 6 else []
        ),
        *(item for values in manifest["role_references"].values() for item in values),
        *(worker[key] for key in WORKER_RESOURCE_KEYS),
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
    if manifest["schema_version"] >= 6:
        descriptor_path = root / manifest["descriptor"]["snapshot_path"]
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("pinned policy descriptor snapshot is invalid") from exc
        _validate_manifest(manifest, descriptor)


def pin_policy_manifest(
    config: HarnessConfig, path: Path, *, resume: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.exists():
        if not resume:
            raise ValueError("run already has a pinned research policy")
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
    _verify_snapshots(manifest, manifest_path)
    if role not in manifest["role_references"]:
        raise ValueError(f"unknown policy role: {role}")
    root = manifest_path.parent
    legacy = manifest["schema_version"] == LEGACY_POLICY_SCHEMA_VERSION
    contract = (
        builtin_domain_contract(POLICY_NAME)
        if legacy else manifest["domain_contract"]
    )
    return {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": manifest["manifest_sha256"],
        "policy_name": manifest["policy_name"],
        "domain": contract["domain"],
        "stable_core": manifest["stable_core"],
        "precedence": manifest["precedence"],
        "descriptor_snapshot": (
            None if legacy else str(
                (root / manifest["descriptor"]["snapshot_path"]).resolve()
            )
        ),
        "skill_snapshot": str((root / manifest["skill"]["snapshot_path"]).resolve()),
        "role_prompt_snapshot": (
            None if legacy else str(
                (root / manifest["role_prompts"][role]["snapshot_path"]).resolve()
            )
        ),
        "required_reference_snapshots": [
            str((root / item["snapshot_path"]).resolve())
            for item in manifest["role_references"][role]
        ],
        "domain_contract": contract,
        "audit_requirements": contract["audit_requirements"],
        "role": role,
        "one_shot_compute_worker": manifest["one_shot_compute_worker"],
    }
