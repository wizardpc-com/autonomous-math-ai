from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .mechanical import (
    MECHANICAL_TASK_KINDS, installed_mechanical_runner_adapters,
)
from .project import ProjectManifest, discover_workspace_root
from .profiles import (
    BUILTIN_PROFILE_NAME, CONFIG_SCHEMA_VERSION, builtin_profile,
    load_user_profile, migrate_config,
)
from .provider_config import (
    mapped_reasoning_effort, redact_config, secret_reference_issues,
    validate_provider_and_routes,
)
from .resources import schema_resource
from .schema import load_schema, validate


DEFAULT_PROTECTED = [
    "claims", "proofs", "state", "artifacts", "experiments",
]
SUPPORTED_POLICY_PACKS = {
    "math-research",
    "certified-computational-research",
    "empirical-research",
}


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
    profile_name: str = BUILTIN_PROFILE_NAME
    user_profile_path: Path | None = None
    migrations_applied: tuple[str, ...] = ()

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
    def max_mechanical_subworkers(self) -> int | None:
        value = self.raw["scheduler"].get("max_mechanical_subworkers")
        return int(value) if value is not None else None

    @property
    def campaign_hours(self) -> float:
        return float(self.raw["campaign"]["hours"])

    @property
    def epoch_hours(self) -> float:
        return float(self.raw["campaign"]["epoch_hours"])

    @property
    def fast_mode(self) -> bool:
        return bool(self.raw["execution"]["fast_mode"])

    @property
    def requested_service_tier(self) -> str | None:
        return "fast" if self.fast_mode else None

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

    def route_for(self, role: str) -> dict[str, Any]:
        route = dict(self.raw["models"][role])
        provider = dict(self.raw["providers"][str(route["provider"])])
        route["mapped_effort"] = mapped_reasoning_effort(provider, route)
        route["endpoint"] = route.get("endpoint") or provider.get("endpoint")
        route["profile"] = route.get("profile") or provider.get("profile")
        return route

    def provider_for(self, role: str) -> dict[str, Any]:
        route = self.raw["models"][role]
        return dict(self.raw["providers"][str(route["provider"])])

    def role_timeout(self, role: str) -> float:
        route = self.raw["models"].get(role, {})
        value = route.get("timeout_seconds")
        if value is None:
            value = self.raw["timeouts"].get(
                role, self.raw["timeouts"]["default_seconds"],
            )
        return float(value)

    def role_concurrency(self, role: str) -> int:
        route = self.raw["models"][role]
        return int(route["max_concurrency"])

    def role_token_limit(self, role: str) -> int | None:
        value = self.raw["models"].get(role, {}).get("token_limit")
        if value is None:
            value = self.raw["budgets"].get("per_thread", {}).get(role)
        if value is None:
            value = self.raw["budgets"].get("per_thread_default")
        return int(value) if value is not None else None

    def retry_limit(self, role: str, retry_class: str) -> int:
        route = self.raw["models"][role]
        return int(route.get("retries", {}).get(retry_class, 0))

    def research_max_turns(self, role: str) -> int:
        limits = self.raw["engine"]["research_max_turns"]
        if role not in limits:
            raise ValueError(f"role {role!r} has no research continuation limit")
        return int(limits[role])

    def explained(self) -> dict[str, Any]:
        return {
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "profile": self.profile_name,
            "project_config": str(self.config_path),
            "user_profile": (
                str(self.user_profile_path) if self.user_profile_path is not None else None
            ),
            "migrations_applied": list(self.migrations_applied),
            "precedence": [
                "builtin profile", "project config", "explicit user profile",
                "core trust-boundary validation",
            ],
            "effective_config": redact_config(self.raw),
            "model_turns_started": 0,
        }

    def summarized(self) -> dict[str, Any]:
        budgets = self.raw["budgets"]
        worker = self.raw["policy"]["one_shot_compute_worker"]
        role_fields = (
            "provider", "model", "effort", "service_tier", "timeout_seconds",
            "max_concurrency", "token_limit", "cost_limit_usd",
        )
        route_fields = (
            "provider", "model", "effort", "service_tier", "endpoint", "profile",
        )
        return {
            "valid": True,
            "config_schema_version": CONFIG_SCHEMA_VERSION,
            "profile": self.profile_name,
            "project": self.project_name,
            "final_claim_id": self.final_conjecture_claim_id,
            "project_config": str(self.config_path),
            "user_profile": (
                str(self.user_profile_path) if self.user_profile_path is not None else None
            ),
            "migrations_applied": list(self.migrations_applied),
            "campaign": dict(self.raw["campaign"]),
            "execution": dict(self.raw["execution"]),
            "research_continuation": {
                key: self.raw["engine"][key]
                for key in (
                    "research_max_turns",
                    "reasoning_health_short_tokens",
                    "reasoning_health_repeated_token_tolerance",
                    "reasoning_health_retry_limit",
                )
            },
            "scheduler": {
                key: self.raw["scheduler"][key]
                for key in (
                    "max_director", "max_research_workers", "max_audit",
                    "max_mechanical_subworkers", "independent_exploration_fraction",
                )
            },
            "budgets": {
                key: budgets.get(key)
                for key in (
                    "global_tokens", "mechanical_tokens", "global_cost_usd",
                    "mechanical_cost_usd", "soft_fraction", "hard_fraction",
                )
            },
            "roles": {
                role: {key: route.get(key) for key in role_fields}
                for role, route in sorted(self.raw["models"].items())
            },
            "mechanical": {
                "enabled": worker["enabled"],
                "selection_policy": worker["selection_policy"],
                "fallback_condition": worker["fallback_condition"],
                "primary_route": {
                    key: worker["primary_route"].get(key) for key in route_fields
                },
                "fallback_route": {
                    key: worker["fallback_route"].get(key) for key in route_fields
                },
                "backpressure": worker["backpressure"],
            },
            "model_turns_started": 0,
        }


def load_config(
    project_root: Path,
    config_path: Path | None = None,
    *,
    workspace_root: Path | None = None,
    require_manifest: bool = False,
    profile_path: Path | None = None,
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
    project_document = json.loads(path.read_text(encoding="utf-8"))
    migrated, migrations = migrate_config(project_document)
    declared_project = migrated.get("project", {})
    declared_name = str(declared_project.get("name") or project_root.name)
    declared_final = str(
        declared_project.get("final_conjecture_claim_id")
        or (manifest.final_claim_id if manifest is not None else "C_ROOT")
    )
    base = builtin_profile(declared_name, declared_final)
    raw = deep_merge(base, migrated)
    selected_profile = str(raw.get("profile") or BUILTIN_PROFILE_NAME)
    if selected_profile != BUILTIN_PROFILE_NAME:
        raise ValueError(
            "project config profile must be codex-app-server-default; use --profile "
            "for an explicit user profile"
        )
    resolved_profile: Path | None = None
    if profile_path is not None:
        resolved_profile = profile_path.resolve()
        profile_name, overrides = load_user_profile(resolved_profile)
        raw = deep_merge(raw, overrides)
        selected_profile = profile_name
    _apply_fast_mode(raw)
    secret_issues = secret_reference_issues(raw)
    if secret_issues:
        raise ValueError(
            "configuration contains forbidden secret material: "
            + "; ".join(secret_issues)
        )
    with schema_resource("config.schema.json") as config_schema_path:
        validate(raw, load_schema(config_schema_path))
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
        profile_name=selected_profile,
        user_profile_path=resolved_profile,
        migrations_applied=tuple(migrations),
    )


def _validate_config(raw: dict[str, Any]) -> None:
    for section in (
        "campaign", "execution", "scheduler", "budgets", "providers", "models", "audit",
        "stagnation", "workspace", "policy",
    ):
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
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"effective configuration must use schema v{CONFIG_SCHEMA_VERSION}"
        )
    execution = raw["execution"]
    if not isinstance(execution, dict) or set(execution) != {"fast_mode"}:
        raise ValueError("execution must contain exactly fast_mode")
    if type(execution["fast_mode"]) is not bool:
        raise ValueError("execution.fast_mode must be boolean")
    campaign = raw["campaign"]
    campaign_hours = float(campaign["hours"])
    epoch_hours = float(campaign["epoch_hours"])
    if campaign_hours <= 0 or epoch_hours <= 0:
        raise ValueError("campaign hours and epoch_hours must be positive")
    if epoch_hours > campaign_hours:
        raise ValueError("campaign epoch_hours must not exceed campaign hours")
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
    turn_limits = raw["engine"].get("research_max_turns")
    expected_turn_roles = {"prover", "falsifier", "explorer"}
    if not isinstance(turn_limits, dict) or set(turn_limits) != expected_turn_roles:
        raise ValueError(
            "engine research_max_turns must contain exactly prover, falsifier, explorer"
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 2
        for value in turn_limits.values()
    ):
        raise ValueError(
            "engine research_max_turns values must be integers of at least 2"
        )
    if int(raw["engine"].get("reasoning_health_short_tokens", 0)) < 0:
        raise ValueError("engine reasoning_health_short_tokens must be non-negative")
    if int(raw["engine"].get("reasoning_health_repeated_token_tolerance", 2)) < 2:
        raise ValueError(
            "engine reasoning_health_repeated_token_tolerance must be at least 2"
        )
    if int(raw["engine"].get("reasoning_health_retry_limit", 0)) < 0:
        raise ValueError("engine reasoning_health_retry_limit must be non-negative")
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
    mechanical_tokens = raw["budgets"].get("mechanical_tokens")
    if mechanical_tokens is not None and int(mechanical_tokens) <= 0:
        raise ValueError("mechanical token budget must be positive or null")
    for key in ("global_cost_usd", "mechanical_cost_usd"):
        value = raw["budgets"].get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"{key} must be positive or null")
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
    required_routes = {"director", "prover", "falsifier", "explorer", "auditor", "evaluator_auditor", "smoke"}
    missing_routes = required_routes - set(raw["models"])
    if missing_routes:
        raise ValueError(f"missing model routes: {sorted(missing_routes)}")
    validate_provider_and_routes(raw)
    policy = raw["policy"]
    if policy.get("stable_core") != "persistent_filesystem_controller":
        raise ValueError("policy stable_core must be persistent_filesystem_controller")
    if schema_version >= 7:
        if policy.get("pack") not in SUPPORTED_POLICY_PACKS:
            raise ValueError("policy.pack must select a bundled research policy pack")
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
    mechanical_adapters: set[str] = set()
    installed_runners = installed_mechanical_runner_adapters()
    for route_name in ("primary_route", "fallback_route"):
        route = worker.get(route_name)
        if not isinstance(route, dict):
            raise ValueError(f"one_shot_compute_worker.{route_name} must be an object")
        provider_name = route.get("provider")
        if provider_name not in raw["providers"]:
            raise ValueError(f"mechanical {route_name} names an unknown provider")
        provider = raw["providers"][provider_name]
        if provider["capabilities"].get("mechanical_one_shot") is not True:
            raise ValueError(
                f"mechanical {route_name} provider lacks mechanical_one_shot capability"
            )
        adapter = str(provider.get("adapter"))
        mechanical_adapters.add(adapter)
        if adapter not in installed_runners:
            raise ValueError(
                f"mechanical {route_name} requires an installed controller-managed "
                f"runner for provider adapter {adapter!r}"
            )
        if not route.get("model") or not route.get("effort"):
            raise ValueError(f"mechanical {route_name} is incomplete")
        mapped_reasoning_effort(provider, route)
        if route.get("service_tier") not in {None, ""}:
            raise ValueError("mechanical workers must explicitly clear service tier")
    if len(mechanical_adapters) != 1:
        raise ValueError(
            "mechanical primary/fallback routes must share one installed runner adapter"
        )
    if worker.get("service_tier") is not None:
        raise ValueError("mechanical workers must explicitly clear service tier")
    if worker.get("fallback_condition") != "provider_execution_failure":
        raise ValueError(
            "mechanical fallback must use the explicit provider execution failure policy"
        )
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
    selection = worker.get("selection_policy")
    if not isinstance(selection, dict) or set(selection) != {"mode", "custom_thresholds"}:
        raise ValueError("one_shot_compute_worker.selection_policy fields are invalid")
    if selection["mode"] not in {
        "preferred", "balanced", "conservative", "disabled", "custom",
    }:
        raise ValueError("invalid mechanical selection policy")
    if not isinstance(selection["custom_thresholds"], dict):
        raise ValueError("mechanical custom_thresholds must be an object")
    if selection["mode"] == "custom" and not selection["custom_thresholds"]:
        raise ValueError("custom mechanical selection policy requires thresholds")
    custom_thresholds = selection["custom_thresholds"]
    allowed_custom_thresholds = {
        "allowed_task_kinds", "max_timeout_seconds",
        "max_expected_artifacts", "max_input_files",
    }
    unknown_thresholds = set(custom_thresholds) - allowed_custom_thresholds
    if unknown_thresholds:
        raise ValueError(
            f"unknown mechanical custom thresholds: {sorted(unknown_thresholds)}"
        )
    if "allowed_task_kinds" in custom_thresholds and (
        not isinstance(custom_thresholds["allowed_task_kinds"], list)
        or not custom_thresholds["allowed_task_kinds"]
        or any(
            not isinstance(item, str) or not item
            for item in custom_thresholds["allowed_task_kinds"]
        )
    ):
        raise ValueError("mechanical allowed_task_kinds must be a non-empty string list")
    if "allowed_task_kinds" in custom_thresholds and not set(
        custom_thresholds["allowed_task_kinds"]
    ) <= set(MECHANICAL_TASK_KINDS):
        raise ValueError("mechanical allowed_task_kinds contains an unknown task kind")
    for key in (
        "max_timeout_seconds", "max_expected_artifacts", "max_input_files",
    ):
        if key in custom_thresholds and (
            type(custom_thresholds[key]) is not int or custom_thresholds[key] < 1
        ):
            raise ValueError(f"mechanical {key} must be a positive integer")
    estimated_cost = worker.get("estimated_cost_usd")
    if estimated_cost is not None and (
        not isinstance(estimated_cost, (int, float))
        or isinstance(estimated_cost, bool) or estimated_cost <= 0
    ):
        raise ValueError("mechanical estimated_cost_usd must be positive or null")
    backpressure = worker.get("backpressure")
    required_backpressure = {
        "dispatch_batch_size", "max_queue_depth", "max_active_per_cpu",
        "minimum_dispatch_interval_seconds",
    }
    if not isinstance(backpressure, dict) or set(backpressure) != required_backpressure:
        raise ValueError("mechanical backpressure fields are invalid")
    if type(backpressure["dispatch_batch_size"]) is not int or backpressure["dispatch_batch_size"] < 1:
        raise ValueError("mechanical dispatch_batch_size must be positive")
    if type(backpressure["max_queue_depth"]) is not int or backpressure["max_queue_depth"] < 1:
        raise ValueError("mechanical max_queue_depth must be positive")
    if (
        not isinstance(backpressure["max_active_per_cpu"], (int, float))
        or isinstance(backpressure["max_active_per_cpu"], bool)
        or backpressure["max_active_per_cpu"] <= 0
    ):
        raise ValueError("mechanical max_active_per_cpu must be positive")
    if float(backpressure["minimum_dispatch_interval_seconds"]) < 0:
        raise ValueError("mechanical minimum dispatch interval must be non-negative")
    core_protected = set(DEFAULT_PROTECTED)
    configured_protected = set(raw["workspace"].get("protected_paths", []))
    if not core_protected <= configured_protected:
        raise ValueError(
            "project config cannot remove core protected paths: "
            f"{sorted(core_protected - configured_protected)}"
        )
    if raw["workspace"].get("network_access") is not False:
        raise ValueError("core workspace policy requires network_access=false")
    if raw["audit"].get("critical_double_audit") is not True:
        raise ValueError("core trust policy requires critical_double_audit=true")
    if schema_version < 7:
        for key in (
            "runner_path", "task_schema_path", "result_schema_path",
            "broker_client_path", "schema_validator_path",
            "contract_definitions_path",
        ):
            if not str(worker.get(key) or "").strip():
                raise ValueError(f"one_shot_compute_worker.{key} is required")


def _apply_fast_mode(raw: dict[str, Any]) -> None:
    """Derive main-role tiers from the single explicit fast opt-in."""
    execution = raw.get("execution")
    if not isinstance(execution, dict) or type(execution.get("fast_mode")) is not bool:
        return
    fast_mode = bool(execution["fast_mode"])
    routes = raw.get("models")
    providers = raw.get("providers")
    if not isinstance(routes, dict) or not isinstance(providers, dict):
        return
    if not fast_mode:
        for role, route in routes.items():
            tier = route.get("service_tier") if isinstance(route, dict) else None
            if isinstance(tier, str) and tier.casefold() in {"fast", "priority", "auto"}:
                raise ValueError(
                    f"models.{role}.service_tier requires execution.fast_mode=true"
                )
        return
    for role, route in routes.items():
        if not isinstance(route, dict):
            continue
        tier = route.get("service_tier")
        if tier not in {None, "", "fast"}:
            raise ValueError(
                "execution.fast_mode=true conflicts with "
                f"models.{role}.service_tier={tier!r}"
            )
        route["service_tier"] = "fast"
        provider = providers.get(str(route.get("provider")))
        if not isinstance(provider, dict):
            continue
        capabilities = provider.get("capabilities")
        if provider.get("adapter") == "codex_app_server" and isinstance(
            capabilities, dict
        ):
            tiers = capabilities.get("service_tiers")
            if isinstance(tiers, list) and "fast" not in tiers:
                tiers.append("fast")
