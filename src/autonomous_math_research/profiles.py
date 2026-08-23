from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any


CONFIG_SCHEMA_VERSION = 12
PROFILE_SCHEMA_VERSION = 1
BUILTIN_PROFILE_NAME = "codex-app-server-default"
DEFAULT_GLOBAL_TOKENS = 500_000_000
DEFAULT_MECHANICAL_TOKENS = DEFAULT_GLOBAL_TOKENS * 3


def _usage_mapping(*, camel: bool) -> dict[str, list[str]]:
    names = {
        "input_tokens": "inputTokens" if camel else "input_tokens",
        "cached_input_tokens": "cachedInputTokens" if camel else "cached_input_tokens",
        "uncached_input_tokens": (
            "uncachedInputTokens" if camel else "uncached_input_tokens"
        ),
        "cache_write_input_tokens": (
            "cacheWriteInputTokens" if camel else "cache_write_input_tokens"
        ),
        "output_tokens": "outputTokens" if camel else "output_tokens",
        "reasoning_output_tokens": (
            "reasoningOutputTokens" if camel else "reasoning_output_tokens"
        ),
        "total_tokens": "totalTokens" if camel else "total_tokens",
    }
    return {key: [value] for key, value in names.items()}


def _provider_defaults() -> dict[str, Any]:
    return {
        "codex": {
            "adapter": "codex_app_server",
            "endpoint": None,
            "profile": "local-login",
            "credential": {"kind": "system_credential", "reference": "codex-login"},
            "capabilities": {
                "version": 1,
                "structured_outputs": "native_json_schema",
                "reasoning": {
                    "parameter": "effort",
                    "supported_efforts": [
                        "none", "minimal", "low", "medium", "high", "xhigh", "max",
                    ],
                    "mapping": {},
                },
                "service_tier_parameter": "serviceTier",
                "service_tiers": [],
                "usage_mapping": _usage_mapping(camel=True),
                "cost_path": None,
                "mechanical_one_shot": True,
            },
        },
        "openai-compatible": {
            "adapter": "openai_compatible",
            "endpoint": "https://api.openai.com/v1",
            "profile": None,
            "credential": {"kind": "environment", "reference": "OPENAI_API_KEY"},
            "capabilities": {
                "version": 1,
                "api_style": "responses",
                "structured_outputs": "native_json_schema",
                "reasoning": {
                    "parameter": "reasoning.effort",
                    "supported_efforts": [
                        "none", "minimal", "low", "medium", "high", "xhigh", "max",
                    ],
                    "mapping": {},
                },
                "service_tier_parameter": "service_tier",
                "service_tiers": ["default", "flex", "fast"],
                "usage_mapping": {
                    "input_tokens": ["input_tokens", "prompt_tokens"],
                    "cached_input_tokens": [
                        "input_tokens_details.cached_tokens",
                        "prompt_tokens_details.cached_tokens",
                    ],
                    "uncached_input_tokens": [
                        "uncached_input_tokens",
                        "input_tokens_details.uncached_tokens",
                        "prompt_tokens_details.uncached_tokens",
                    ],
                    "cache_write_input_tokens": [],
                    "output_tokens": ["output_tokens", "completion_tokens"],
                    "reasoning_output_tokens": [
                        "output_tokens_details.reasoning_tokens",
                        "completion_tokens_details.reasoning_tokens",
                    ],
                    "total_tokens": ["total_tokens"],
                },
                "cost_path": None,
                "mechanical_one_shot": False,
            },
        },
    }


def _role_route(
    model: str,
    effort: str,
    *,
    timeout_seconds: int,
    max_concurrency: int,
    token_limit: int,
    transport_retries: int = 1,
    model_protocol_retries: int = 1,
) -> dict[str, Any]:
    return {
        "provider": "codex",
        "model": model,
        "endpoint": None,
        "profile": None,
        "effort": effort,
        "unsupported_effort": "error",
        "service_tier": None,
        "output_mode": "auto",
        "timeout_seconds": timeout_seconds,
        "retries": {
            "transport": transport_retries,
            "model_protocol": model_protocol_retries,
        },
        "max_concurrency": max_concurrency,
        "token_limit": token_limit,
        "cost_limit_usd": None,
        "estimated_cost_usd": None,
    }


def builtin_profile(project_id: str, final_claim_id: str) -> dict[str, Any]:
    roles = {
        "director": _role_route(
            "gpt-5.6-sol", "high", timeout_seconds=900,
            max_concurrency=1, token_limit=60_000,
        ),
        "prover": _role_route(
            "gpt-5.6-sol", "xhigh", timeout_seconds=3600,
            max_concurrency=8, token_limit=160_000,
        ),
        "falsifier": _role_route(
            "gpt-5.6-sol", "high", timeout_seconds=1800,
            max_concurrency=8, token_limit=120_000,
        ),
        "explorer": _role_route(
            "gpt-5.6-sol", "high", timeout_seconds=2400,
            max_concurrency=8, token_limit=120_000,
        ),
        "auditor": _role_route(
            "gpt-5.6-sol", "xhigh", timeout_seconds=3000,
            max_concurrency=8, token_limit=160_000,
        ),
        "evaluator_auditor": _role_route(
            "gpt-5.6-sol", "xhigh", timeout_seconds=2400,
            max_concurrency=8, token_limit=120_000,
        ),
        "smoke": _role_route(
            "gpt-5.6-terra", "medium", timeout_seconds=900,
            max_concurrency=1, token_limit=20_000,
        ),
    }
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "profile": BUILTIN_PROFILE_NAME,
        "project": {
            "name": project_id,
            "final_conjecture_claim_id": final_claim_id,
        },
        "campaign": {
            "hours": 5.0,
            "epoch_hours": 2.0,
        },
        "execution": {
            "fast_mode": False,
        },
        "engine": {
            "poll_interval_seconds": 0.1,
            "max_consecutive_controller_errors": 5,
            "error_rate_threshold": 0.5,
            "error_rate_min_jobs": 4,
            "max_retries": 1,
            "transient_protocol_max_retries": 1,
            "model_protocol_max_retries": 1,
            "director_max_retries": 1,
            "director_debounce_seconds": 2.0,
            "research_max_turns": {
                "prover": 12,
                "falsifier": 12,
                "explorer": 12,
            },
            "reasoning_health_short_tokens": 600,
            "reasoning_health_repeated_token_tolerance": 2,
            "reasoning_health_retry_limit": 2,
        },
        "scheduler": {
            "max_director": 1,
            "max_research_workers": 8,
            "max_audit": 8,
            "max_mechanical_subworkers": None,
            "independent_exploration_fraction": 0.25,
        },
        "budgets": {
            "global_tokens": DEFAULT_GLOBAL_TOKENS,
            "mechanical_tokens": DEFAULT_MECHANICAL_TOKENS,
            "global_cost_usd": None,
            "mechanical_cost_usd": None,
            "soft_fraction": 0.75,
            "hard_fraction": 0.95,
            "per_thread_limit_action": "observe",
            "per_thread_default": 100_000,
            "per_thread": {
                role: int(route["token_limit"])
                for role, route in roles.items()
                if role != "smoke"
            },
            "per_role": {
                role: DEFAULT_GLOBAL_TOKENS
                for role in roles
                if role != "smoke"
            },
            "per_role_cost_usd": {
                role: None for role in roles if role != "smoke"
            },
            "estimated_tokens": {
                "LOW": 60_000,
                "MEDIUM": 120_000,
                "HIGH": 180_000,
                "director": 60_000,
                "auditor": 160_000,
                "evaluator_auditor": 120_000,
            },
        },
        "providers": _provider_defaults(),
        "models": roles,
        "audit": {
            "immediate_threshold": "HIGH",
            "critical_double_audit": True,
            "low_impact_batch_size": 8,
        },
        "stagnation": {
            "attempt_threshold": 3,
            "priority_penalty": 0.2,
            "force_diversification": True,
        },
        "rate_limits": {
            "reduce_exploration_percent": 75,
            "drain_percent": 90,
            "stop_percent": 98,
            "poll_interval_seconds": 60,
        },
        "observability": {
            "live_agent_feed": True,
            "flush_interval_seconds": 0.5,
            "max_text_chunk_chars": 2000,
            "max_channel_chars_per_turn": 24_000,
            "capture_command_output": True,
            "max_command_output_chars_per_item": 4_000,
        },
        "timeouts": {
            "default_seconds": 3600,
            **{
                role: int(route["timeout_seconds"])
                for role, route in roles.items()
                if role != "smoke"
            },
        },
        "workspace": {
            "protected_paths": [
                "claims", "proofs", "state", "artifacts", "experiments",
                "certificates", "audit",
            ],
            "use_worktree_for_code_modification": True,
            "network_access": False,
        },
        "policy": {
            "pack": "math-research",
            "stable_core": "persistent_filesystem_controller",
            "one_shot_compute_worker": {
                "enabled": True,
                "selection_policy": {
                    "mode": "preferred",
                    "custom_thresholds": {},
                },
                "service_tier": None,
                "primary_route": {
                    "provider": "codex",
                    "model": "gpt-5.3-codex-spark",
                    "endpoint": None,
                    "profile": None,
                    "effort": "high",
                    "unsupported_effort": "error",
                    "service_tier": None,
                },
                "fallback_route": {
                    "provider": "codex",
                    "model": "gpt-5.6-luna",
                    "endpoint": None,
                    "profile": None,
                    "effort": "medium",
                    "unsupported_effort": "error",
                    "service_tier": None,
                },
                "fallback_condition": "provider_execution_failure",
                "recursive_spawn_allowed": False,
                "transient_max_retries": 1,
                "model_protocol_max_retries": 1,
                "estimated_tokens": 60_000,
                "estimated_cost_usd": None,
                "backpressure": {
                    "dispatch_batch_size": 8,
                    "max_queue_depth": 256,
                    "max_active_per_cpu": 1.0,
                    "minimum_dispatch_interval_seconds": 0.0,
                },
            },
        },
    }


def _augment_role_routes(config: dict[str, Any]) -> None:
    defaults = builtin_profile(
        str(config.get("project", {}).get("name") or "math-project"),
        str(config.get("project", {}).get("final_conjecture_claim_id") or "C_ROOT"),
    )
    config.setdefault("providers", deepcopy(defaults["providers"]))
    for provider in config.get("providers", {}).values():
        if not isinstance(provider, dict):
            continue
        capabilities = provider.get("capabilities")
        if not isinstance(capabilities, dict):
            continue
        usage_mapping = capabilities.get("usage_mapping")
        if isinstance(usage_mapping, dict):
            usage_mapping.setdefault("uncached_input_tokens", [])
    engine = config.setdefault("engine", {})
    for key in (
        "research_max_turns",
        "reasoning_health_short_tokens",
        "reasoning_health_repeated_token_tolerance",
        "reasoning_health_retry_limit",
    ):
        engine.setdefault(key, defaults["engine"][key])
    turns = engine.get("research_max_turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        engine["research_max_turns"] = {
            role: max(2, int(turns))
            for role in ("prover", "falsifier", "explorer")
        }
    elif isinstance(turns, dict):
        engine["research_max_turns"] = {
            role: int(turns.get(role, defaults["engine"]["research_max_turns"][role]))
            for role in ("prover", "falsifier", "explorer")
        }
    for role, route in list(config.get("models", {}).items()):
        base = deepcopy(defaults["models"].get(role) or defaults["models"]["explorer"])
        if isinstance(route, dict):
            base.update(route)
        config["models"][role] = base
    budgets = config.setdefault("budgets", {})
    budgets.setdefault("mechanical_tokens", DEFAULT_MECHANICAL_TOKENS)
    budgets.setdefault("global_cost_usd", None)
    budgets.setdefault("mechanical_cost_usd", None)
    budgets.setdefault("per_role_cost_usd", {
        role: None for role in config.get("models", {}) if role != "smoke"
    })
    worker = config.setdefault("policy", {}).setdefault("one_shot_compute_worker", {})
    for key in ("primary_route", "fallback_route"):
        if isinstance(worker.get(key), dict):
            old = dict(worker[key])
            worker[key] = {
                "provider": "codex",
                "endpoint": None,
                "profile": None,
                "unsupported_effort": "error",
                **old,
            }
    worker.setdefault("selection_policy", {"mode": "preferred", "custom_thresholds": {}})
    worker.setdefault("estimated_cost_usd", None)
    worker.setdefault("backpressure", {
        "dispatch_batch_size": 8,
        "max_queue_depth": 256,
        "max_active_per_cpu": 1.0,
        "minimum_dispatch_interval_seconds": 0.0,
    })


def migrate_config(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    value = deepcopy(raw)
    version = int(value.get("schema_version", 0))
    migrations: list[str] = []
    if version == 7:
        value["schema_version"] = 8
        value["profile"] = BUILTIN_PROFILE_NAME
        _augment_role_routes(value)
        migrations.append("7->8")
        version = 8
    if version == 8:
        value["schema_version"] = 9
        value["campaign"] = {
            "hours": 5.0,
            "epoch_hours": 2.0,
        }
        _augment_role_routes(value)
        migrations.append("8->9")
        version = 9
    if version == 9:
        value["schema_version"] = 10
        _augment_role_routes(value)
        migrations.append("9->10")
        version = 10
    if version == 10:
        worker = value.setdefault("policy", {}).setdefault(
            "one_shot_compute_worker", {}
        )
        if worker.get("fallback_condition") == "permanent_unavailable_or_access_denied":
            worker["fallback_condition"] = "provider_execution_failure"
        value["schema_version"] = 11
        _augment_role_routes(value)
        migrations.append("10->11")
        version = 11
    if version == 11:
        value["schema_version"] = CONFIG_SCHEMA_VERSION
        value.setdefault("execution", {"fast_mode": False})
        _augment_role_routes(value)
        migrations.append("11->12")
        version = CONFIG_SCHEMA_VERSION
    if version == CONFIG_SCHEMA_VERSION:
        _augment_role_routes(value)
        return value, migrations
    raise ValueError(f"unsupported configuration schema_version: {version}")


def load_user_profile(path: Path) -> tuple[str, dict[str, Any]]:
    source = path.resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    required = {"profile_schema_version", "name", "extends", "overrides"}
    if not isinstance(raw, dict) or set(raw) != required:
        raise ValueError("user profile fields must be profile_schema_version, name, extends, overrides")
    if raw["profile_schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported user profile schema version")
    if raw["extends"] != BUILTIN_PROFILE_NAME:
        raise ValueError("user profile must extend codex-app-server-default")
    if not isinstance(raw["name"], str) or not raw["name"].strip():
        raise ValueError("user profile name must be non-empty")
    if not isinstance(raw["overrides"], dict):
        raise ValueError("user profile overrides must be an object")
    if "project" in raw["overrides"]:
        raise ValueError("user profiles cannot override project identity")
    return str(raw["name"]), deepcopy(raw["overrides"])
