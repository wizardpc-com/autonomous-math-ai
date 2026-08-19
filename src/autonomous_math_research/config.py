from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .project import ProjectManifest, discover_workspace_root


DEFAULT_PROTECTED = ["claims", "proofs", "state", "artifacts", "experiments"]


def default_max_audit(max_research_workers: int) -> int:
    """Return the audit default and ceiling for a research-worker cap."""
    research = int(max_research_workers)
    if research < 1:
        raise ValueError("max_research_workers must be positive")
    return research


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass(slots=True)
class HarnessConfig:
    raw: dict[str, Any]
    project_root: Path
    config_path: Path
    manifest: ProjectManifest | None = None
    workspace_root: Path | None = None

    @property
    def max_director(self) -> int:
        return int(self.raw["scheduler"].get("max_director", 1))

    @property
    def max_research_workers(self) -> int:
        scheduler = self.raw["scheduler"]
        return int(scheduler.get("max_research_workers", scheduler.get("max_research")))

    @property
    def max_research(self) -> int:
        """Backward-compatible alias for pre-v3 callers."""
        return self.max_research_workers

    @property
    def max_audit(self) -> int:
        return int(self.raw["scheduler"]["max_audit"])

    @property
    def max_mechanical_subworkers(self) -> int:
        # Legacy/disabled configurations have no mechanical pool. Schema v6
        # requires an explicit positive value before the broker can be enabled.
        return int(self.raw["scheduler"].get("max_mechanical_subworkers", 0))

    @property
    def project_name(self) -> str:
        if self.manifest is not None:
            return self.manifest.project_id
        project = self.raw.get("project", {})
        return str(project.get("name") or self.project_root.name)

    @property
    def final_conjecture_claim_id(self) -> str | None:
        if self.manifest is not None:
            return self.manifest.final_claim_id
        value = self.raw.get("project", {}).get("final_conjecture_claim_id")
        return str(value) if value else None

    @property
    def protected_paths(self) -> list[str]:
        if self.manifest is not None:
            return list(self.manifest.protected_paths)
        return list(self.raw.get("workspace", {}).get("protected_paths", DEFAULT_PROTECTED))

    @property
    def per_thread_limit_action(self) -> str:
        return str(self.raw["budgets"].get("per_thread_limit_action", "observe"))

    def model_for(self, role: str) -> tuple[str, str]:
        route = self.raw["models"][role]
        return str(route["model"]), str(route["effort"])


def load_config(
    project_root: Path,
    config_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
    require_manifest: bool = False,
) -> HarnessConfig:
    project_root = project_root.resolve()
    manifest_path = project_root / "autonomous" / "project.json"
    manifest = ProjectManifest.load(project_root) if manifest_path.is_file() else None
    if require_manifest and manifest is None:
        raise ValueError("project is missing autonomous/project.json")
    path = (
        config_path
        or (manifest.resolve(manifest.config) if manifest is not None else None)
        or project_root / "autonomous" / "config.yaml"
    ).resolve()
    if not path.is_relative_to(project_root):
        raise ValueError("config must be inside the target project")
    # JSON is a strict YAML subset and avoids another long-running dependency.
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_config(raw)
    configured_name = str(raw.get("project", {}).get("name") or project_root.name)
    expected_name = manifest.project_id if manifest is not None else project_root.name
    if configured_name != expected_name:
        raise ValueError(
            f"config project.name {configured_name!r} does not match project directory "
            f"or manifest project_id {expected_name!r}"
        )
    if manifest is not None:
        configured_final = str(raw.get("project", {}).get("final_conjecture_claim_id") or "")
        if configured_final != manifest.final_claim_id:
            raise ValueError("config final claim does not match project manifest")
    return HarnessConfig(
        raw=raw,
        project_root=project_root,
        config_path=path,
        manifest=manifest,
        workspace_root=discover_workspace_root(project_root, workspace_root),
    )


def _validate_config(raw: dict[str, Any]) -> None:
    for section in ("scheduler", "budgets", "models", "audit", "stagnation", "workspace", "policy"):
        if section not in raw:
            raise ValueError(f"config missing section: {section}")
    scheduler = raw["scheduler"]
    if int(raw.get("schema_version", 0)) >= 4:
        project = raw.get("project")
        if not isinstance(project, dict) or set(project) != {
            "name", "final_conjecture_claim_id",
        }:
            raise ValueError(
                "schema v4 project must contain exactly name and final_conjecture_claim_id"
            )
        if not str(project.get("name", "")).strip():
            raise ValueError("project.name must be non-empty")
        if not str(project.get("final_conjecture_claim_id", "")).strip():
            raise ValueError("project.final_conjecture_claim_id must be non-empty")
    schema_version = int(raw.get("schema_version", 0))
    if schema_version >= 3:
        required_concurrency = {
            "max_director", "max_research_workers", "max_audit",
        }
        missing_concurrency = required_concurrency - set(scheduler)
        if missing_concurrency:
            raise ValueError(
                f"scheduler missing concurrency limits: {sorted(missing_concurrency)}"
            )
        if "max_research" in scheduler:
            raise ValueError("schema v3 config must use max_research_workers")
        if "pause_low_priority_exploration_for_critical_audit" in scheduler:
            raise ValueError("schema v3 forbids cross-role audit slot borrowing")
    if schema_version >= 6 and "max_mechanical_subworkers" not in scheduler:
        raise ValueError(
            "schema v6 scheduler requires an explicit max_mechanical_subworkers; "
            "the harness has no implicit default"
        )
    if schema_version >= 6 and "economy_models" in raw:
        raise ValueError(
            "schema v6 forbids economy_models; top-level roles remain on pinned strong routes"
        )
    if schema_version >= 5 and "max_total_model_concurrency" in scheduler:
        raise ValueError(
            "schema v5 forbids max_total_model_concurrency; role caps are independent"
        )
    max_director = int(scheduler.get("max_director", 1))
    max_research_workers = int(
        scheduler.get("max_research_workers", scheduler.get("max_research", 0))
    )
    max_audit = int(scheduler["max_audit"])
    max_mechanical = scheduler.get("max_mechanical_subworkers")
    if min(max_director, max_research_workers, max_audit) < 1:
        raise ValueError("concurrency slots must be positive")
    if max_director != 1:
        raise ValueError("max_director must be 1; the controller has one authoritative Director")
    audit_ceiling = default_max_audit(max_research_workers)
    if max_audit > audit_ceiling:
        raise ValueError(
            "max_audit must not exceed max_research_workers: "
            f"got max_audit={max_audit}, ceiling={audit_ceiling}"
        )
    if max_mechanical is not None and int(max_mechanical) < 1:
        raise ValueError("max_mechanical_subworkers must be positive when configured")
    for key in (
        "max_retries", "transient_protocol_max_retries",
        "model_protocol_max_retries", "director_max_retries",
    ):
        if int(raw["engine"].get(key, 1)) < 0:
            raise ValueError(f"engine {key} must be non-negative")
    if float(raw["engine"].get("director_debounce_seconds", 0.2)) < 0:
        raise ValueError("engine director_debounce_seconds must be non-negative")
    fraction = float(raw["scheduler"]["independent_exploration_fraction"])
    if not 0 <= fraction <= 1:
        raise ValueError("independent_exploration_fraction must be between 0 and 1")
    soft = float(raw["budgets"].get("soft_fraction", 0.75))
    hard = float(raw["budgets"].get("hard_fraction", 0.95))
    if not 0 < soft < hard <= 1:
        raise ValueError("token budget fractions must satisfy 0 < soft < hard <= 1")
    global_tokens = raw["budgets"].get("global_tokens")
    if global_tokens is not None and int(global_tokens) <= 0:
        raise ValueError("global token budget must be positive or null")
    per_thread_limit_action = str(
        raw["budgets"].get("per_thread_limit_action", "observe")
    )
    if per_thread_limit_action not in {"observe", "interrupt"}:
        raise ValueError("per_thread_limit_action must be observe or interrupt")
    rate = raw.get("rate_limits", {})
    reduce_at = float(rate.get("reduce_exploration_percent", 75))
    drain_at = float(rate.get("drain_percent", 90))
    stop_at = float(rate.get("stop_percent", 98))
    if not 0 <= reduce_at < drain_at < stop_at <= 100:
        raise ValueError("rate thresholds must be increasing percentages")
    observability = raw.get("observability", {})
    if float(observability.get("flush_interval_seconds", 0.5)) <= 0:
        raise ValueError("observability flush_interval_seconds must be positive")
    for key, default in (
        ("max_text_chunk_chars", 2000),
        ("max_channel_chars_per_turn", 24000),
        ("max_command_output_chars_per_item", 4000),
    ):
        if int(observability.get(key, default)) <= 0:
            raise ValueError(f"observability {key} must be positive")
    if raw["audit"].get("immediate_threshold", "HIGH") not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        raise ValueError("invalid audit immediate threshold")
    model_tables = ("models",) if schema_version >= 6 else ("models", "economy_models")
    for table_name in model_tables:
        for role, route in raw.get(table_name, {}).items():
            if not route.get("model") or not route.get("effort"):
                raise ValueError(f"model route incomplete for {table_name}.{role}")
            if route.get("service_tier") not in {None, ""}:
                raise ValueError(
                    "all explicit fast or priority service tiers are forbidden by project policy"
                )
    required_routes = {"director", "prover", "falsifier", "explorer", "auditor", "evaluator_auditor", "smoke"}
    missing_routes = required_routes - set(raw["models"])
    if missing_routes:
        raise ValueError(f"missing model routes: {sorted(missing_routes)}")
    policy = raw["policy"]
    if policy.get("stable_core") != "persistent_filesystem_controller":
        raise ValueError("policy stable_core must be persistent_filesystem_controller")
    if schema_version >= 7:
        if policy.get("pack") != "math-research":
            raise ValueError("schema v7 requires the bundled math-research policy pack")
        forbidden_policy_paths = {
            "skill_path", "role_references", "runner_path", "task_schema_path",
            "result_schema_path", "broker_client_path", "schema_validator_path",
            "contract_definitions_path",
        }
        present = forbidden_policy_paths & set(policy)
        if present:
            raise ValueError(
                f"schema v7 policy cannot override packaged resources: {sorted(present)}"
            )
    else:
        if not policy.get("skill_path"):
            raise ValueError("policy skill_path is required")
        role_references = policy.get("role_references")
        if not isinstance(role_references, dict):
            raise ValueError("policy role_references must be an object")
        missing_policy_roles = required_routes - set(role_references)
        if missing_policy_roles:
            raise ValueError(f"missing policy references for roles: {sorted(missing_policy_roles)}")
        if any(not isinstance(items, list) or not items for items in role_references.values()):
            raise ValueError("each policy role must list at least one reference")
    worker = policy.get("one_shot_compute_worker", {})
    if not isinstance(worker, dict):
        raise ValueError("policy.one_shot_compute_worker must be an object")
    if schema_version >= 7:
        forbidden_worker_paths = {
            "runner_path", "task_schema_path", "result_schema_path",
            "broker_client_path", "schema_validator_path",
            "contract_definitions_path", "model_probe_timeout_seconds",
        }
        present = forbidden_worker_paths & set(worker)
        if present:
            raise ValueError(
                "schema v7 mechanical policy cannot override packaged resources: "
                f"{sorted(present)}"
            )
    if type(worker.get("enabled")) is not bool:
        raise ValueError("one_shot_compute_worker.enabled must be boolean")
    enabled = bool(worker.get("enabled", False))
    if enabled and max_mechanical is None:
        raise ValueError(
            "enabled one-shot compute worker requires max_mechanical_subworkers"
        )
    expected_primary = {
        "model": "gpt-5.3-codex-spark", "effort": "high", "service_tier": None,
    }
    expected_fallback = {
        "model": "gpt-5.6-luna", "effort": "medium", "service_tier": None,
    }
    if worker.get("primary_route") != expected_primary:
        raise ValueError("mechanical primary route must be Spark/high/null")
    if worker.get("fallback_route") != expected_fallback:
        raise ValueError("mechanical fallback route must be Luna/medium/null")
    if worker.get("service_tier") is not None:
        raise ValueError("mechanical workers must explicitly clear service tier")
    if worker.get("fallback_condition") != "permanent_unavailable_or_access_denied":
        raise ValueError("mechanical fallback is allowed only for permanent unavailable/access denied")
    if worker.get("recursive_spawn_allowed") is not False:
        raise ValueError("mechanical workers must prohibit recursive spawn")
    for key in ("transient_max_retries", "model_protocol_max_retries"):
        if type(worker.get(key)) is not int or int(worker[key]) < 0:
            raise ValueError(f"one_shot_compute_worker.{key} must be non-negative")
    positive_worker_keys = (
        ("estimated_tokens",)
        if schema_version >= 7 else ("estimated_tokens", "model_probe_timeout_seconds")
    )
    for key in positive_worker_keys:
        if type(worker.get(key)) is not int or int(worker[key]) <= 0:
            raise ValueError(f"one_shot_compute_worker.{key} must be positive")
    if schema_version < 7:
        for key in (
            "runner_path", "task_schema_path", "result_schema_path",
            "broker_client_path", "schema_validator_path",
            "contract_definitions_path",
        ):
            if not str(worker.get(key) or "").strip():
                raise ValueError(f"one_shot_compute_worker.{key} is required")
