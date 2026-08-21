from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import entry_points
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from typing import Any, Protocol

from ..models import TokenUsage, stable_hash
from ..storage import atomic_write_json


MECHANICAL_SCHEMA_VERSION = 1
MECHANICAL_ROLE = "mechanical_subworker"
MECHANICAL_PARENT_ROLES = frozenset({
    "director", "prover", "falsifier", "explorer",
    "auditor", "evaluator_auditor",
})
MECHANICAL_TASK_KINDS = frozenset({
    "finite_enumeration",
    "finite_exact_computation",
    "formula_expansion",
    "code_preparation",
    "code_modification",
    "data_extraction",
    "data_normalization",
    "deterministic_reproduction",
    "artifact_verification",
    "specified_formal_check",
    "mechanical_analysis",
})
MECHANICAL_TASK_FIELDS = frozenset({
    "schema_version",
    "task_id",
    "task_kind",
    "objective",
    "mathematical_statement",
    "input_files",
    "allowed_tools",
    "bounds",
    "timeout_seconds",
    "expected_artifacts",
    "success_condition",
    "falsification_condition",
    "stop_condition",
    "verification_steps",
    "requires_mathematical_judgment",
    "project_id",
    "notes",
})
MECHANICAL_REQUEST_FIELDS = frozenset({
    "schema_version", "parent_job_id", "parent_task_id", "parent_role",
    "submitted_at", "task_packet", "request_sha256",
})
MECHANICAL_RESPONSE_FIELDS = frozenset({
    "schema_version", "parent_job_id", "parent_task_id", "parent_role",
    "subtask_id", "status", "model", "reasoning_effort", "service_tier",
    "token_usage", "token_telemetry", "result", "artifacts",
    "runner_directory", "fallback", "error", "failure_kind", "retryable",
})
_SENSITIVE_INPUT_DIRECTORIES = frozenset({".git", ".codex", ".ssh", ".gnupg"})
_SENSITIVE_INPUT_NAMES = frozenset({
    "auth.json", "credentials", "credentials.json", "credential.json",
    "secrets.json", "secret.json", ".git-credentials", ".netrc", ".npmrc",
    ".pypirc", "id_rsa", "id_ed25519",
})
PRIMARY_MECHANICAL_ROUTE = {
    "model": "gpt-5.3-codex-spark",
    "reasoning_effort": "high",
    "service_tier": None,
}
FALLBACK_MECHANICAL_ROUTE = {
    "model": "gpt-5.6-luna",
    "reasoning_effort": "medium",
    "service_tier": None,
}
MECHANICAL_FALLBACK_FAILURE_KINDS = frozenset({
    "model_unavailable",
    "provider_quota_exhausted",
    "transport_transient",
    "timeout_transient",
})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_FORBIDDEN_TOOL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:codex|subagents?|agents?|workers?|web(?:_search)?|"
    r"network|browsers?|plugins?|apps?|memory|multi[-_ ]?agent)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_JUDGMENT_MARKERS = (
    "choose a proof", "choose proof", "proof strategy", "select a lemma",
    "choose a lemma", "invent a lemma", "find an invariant", "choose an invariant",
    "research direction", "prioritize", "decide the truth", "judge whether",
    "derive a new lemma", "interpret the result", "规划证明", "证明策略",
    "选择引理", "寻找不变量", "选择不变量", "研究方向", "决定下一步",
    "判断命题", "解释数学意义",
)
_PROOF_DIRECTIVE_MARKERS = (
    "prove the", "prove that", "disprove", "construct a proof",
    "证明该", "证明此", "证明命题", "给出证明", "构造证明", "证伪命题",
)


def _contains_proof_directive(text: str, marker: str) -> bool:
    start = 0
    while True:
        index = text.find(marker, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 24):index]
        if not (
            re.search(r"(?:does|do|did|will|would|can|could)\s+not\s+$", prefix)
            or prefix.endswith(("不", "不能", "并非要", "并不"))
        ):
            return True
        start = index + len(marker)


class MechanicalTaskRejected(ValueError):
    """The request is outside the one-shot mechanical trust boundary."""


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MechanicalTaskRejected(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise MechanicalTaskRejected(f"{label} must be a list of non-empty strings")
    if nonempty and not value:
        raise MechanicalTaskRejected(f"{label} must not be empty")
    return value


def _sensitive_repository_input(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    return (
        bool(set(parts) & _SENSITIVE_INPUT_DIRECTORIES)
        or name in _SENSITIVE_INPUT_NAMES
        or name == ".env"
        or name.startswith(".env.")
    )


def validate_mechanical_task_packet(
    value: Any,
    *,
    repository_root: Path,
    maximum_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Validate the controller-owned boundary before any worker can start."""
    if not isinstance(value, dict) or set(value) != MECHANICAL_TASK_FIELDS:
        actual = set(value) if isinstance(value, dict) else set()
        raise MechanicalTaskRejected(
            "mechanical task fields differ; "
            f"missing={sorted(MECHANICAL_TASK_FIELDS - actual)}, "
            f"extra={sorted(actual - MECHANICAL_TASK_FIELDS)}"
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != MECHANICAL_SCHEMA_VERSION:
        raise MechanicalTaskRejected("unsupported mechanical task schema_version")
    task_id = _nonempty_string(value["task_id"], "task_id")
    if not _ID_RE.fullmatch(task_id):
        raise MechanicalTaskRejected("task_id has an unsafe or unsupported form")
    if value["task_kind"] not in MECHANICAL_TASK_KINDS:
        raise MechanicalTaskRejected("task_kind is not an approved mechanical class")
    for field_name in (
        "objective", "mathematical_statement", "success_condition",
        "falsification_condition", "stop_condition",
    ):
        _nonempty_string(value[field_name], field_name)
    if value["requires_mathematical_judgment"] is not False:
        raise MechanicalTaskRejected(
            "mechanical worker cannot accept a task requiring mathematical judgment"
        )
    tools = _string_list(value["allowed_tools"], "allowed_tools", nonempty=True)
    for tool in tools:
        forbidden = _FORBIDDEN_TOOL_RE.search(tool)
        if forbidden is not None:
            raise MechanicalTaskRejected(
                "allowed_tools contains forbidden recursive/network capability "
                f"{forbidden.group(0)!r}: {tool!r}"
            )
    inputs = _string_list(value["input_files"], "input_files", nonempty=False)
    repository = repository_root.resolve()
    for raw in inputs:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise MechanicalTaskRejected(
                f"input file must be parent-workspace-relative: {raw}"
            )
        if _sensitive_repository_input(path):
            raise MechanicalTaskRejected(
                f"input file is authentication/VCS metadata and is forbidden: {raw}"
            )
        resolved = (repository / path).resolve()
        if not resolved.is_relative_to(repository) or not resolved.is_file():
            raise MechanicalTaskRejected(
                f"input file is missing or outside the parent workspace: {raw}"
            )
    bounds = value["bounds"]
    if not isinstance(bounds, dict) or not bounds:
        raise MechanicalTaskRejected("bounds must be a non-empty finite-bound object")
    if bounds.get("finite") is not True:
        raise MechanicalTaskRejected("bounds.finite must be true")
    _nonempty_string(bounds.get("description"), "bounds.description")
    parameters = bounds.get("parameters")
    if not isinstance(parameters, list) or not parameters:
        raise MechanicalTaskRejected("bounds.parameters must be a non-empty array")
    for index, item in enumerate(parameters):
        if not isinstance(item, dict) or set(item) != {"name", "value"}:
            raise MechanicalTaskRejected(
                f"bounds.parameters[{index}] must contain exactly name and value"
            )
        _nonempty_string(item["name"], f"bounds.parameters[{index}].name")
        _nonempty_string(item["value"], f"bounds.parameters[{index}].value")
    timeout = value["timeout_seconds"]
    if type(timeout) is not int or timeout <= 0:
        raise MechanicalTaskRejected("timeout_seconds must be a positive integer")
    if maximum_timeout_seconds is not None and timeout > maximum_timeout_seconds:
        raise MechanicalTaskRejected(
            "mechanical timeout exceeds the remaining parent/controller limit"
        )
    artifacts = _string_list(
        value["expected_artifacts"], "expected_artifacts", nonempty=True,
    )
    if len(set(artifacts)) != len(artifacts):
        raise MechanicalTaskRejected("expected_artifacts contains duplicates")
    for raw in artifacts:
        path = Path(raw)
        if (
            path.is_absolute() or ".." in path.parts or not path.parts
            or path.parts[0] != "artifacts"
        ):
            raise MechanicalTaskRejected(
                f"expected artifact must be a normalized artifacts/... path: {raw}"
            )
    verification_steps = _string_list(
        value["verification_steps"], "verification_steps", nonempty=True,
    )
    directive_parts = [
        value["objective"], value["success_condition"],
        value["falsification_condition"], value["stop_condition"],
        value["notes"], bounds.get("description"),
        *verification_steps,
        *(
            str(part)
            for item in parameters
            for part in (item.get("name"), item.get("value"))
        ),
    ]
    directive_text = " ".join(str(item).casefold() for item in directive_parts)
    all_text = f"{value['mathematical_statement'].casefold()} {directive_text}"
    marker = next((item for item in _JUDGMENT_MARKERS if item in all_text), None)
    if marker is None:
        marker = next(
            (
                item for item in _PROOF_DIRECTIVE_MARKERS
                if _contains_proof_directive(directive_text, item)
            ),
            None,
        )
    if marker:
        raise MechanicalTaskRejected(
            f"task text crosses the strategy/judgment boundary ({marker!r})"
        )
    project_id = value["project_id"]
    if project_id is not None and (
        not isinstance(project_id, str) or not _PROJECT_RE.fullmatch(project_id)
    ):
        raise MechanicalTaskRejected("project_id must be null or a normalized project id")
    if value["notes"] is not None and not isinstance(value["notes"], str):
        raise MechanicalTaskRejected("notes must be a string or null")
    return dict(value)


def validate_mechanical_request(
    value: Any,
    *,
    repository_root: Path,
    expected_parent_job_id: str,
    expected_parent_task_id: str,
    expected_parent_role: str,
    maximum_timeout_seconds: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MECHANICAL_REQUEST_FIELDS:
        raise MechanicalTaskRejected("mechanical request envelope fields are invalid")
    if value["schema_version"] != MECHANICAL_SCHEMA_VERSION:
        raise MechanicalTaskRejected("unsupported mechanical request schema_version")
    expected = (expected_parent_job_id, expected_parent_task_id, expected_parent_role)
    observed = (value["parent_job_id"], value["parent_task_id"], value["parent_role"])
    if observed != expected:
        raise MechanicalTaskRejected("mechanical request is not bound to the active parent job")
    if expected_parent_role not in MECHANICAL_PARENT_ROLES:
        raise MechanicalTaskRejected("parent role is not permitted to delegate mechanically")
    _nonempty_string(value["submitted_at"], "submitted_at")
    packet = validate_mechanical_task_packet(
        value["task_packet"], repository_root=repository_root,
        maximum_timeout_seconds=maximum_timeout_seconds,
    )
    fingerprinted = dict(value)
    reported = str(fingerprinted.pop("request_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", reported) or stable_hash(fingerprinted) != reported:
        raise MechanicalTaskRejected("mechanical request fingerprint is invalid")
    return {**value, "task_packet": packet}


def validate_mechanical_response(
    value: Any,
    *,
    allowed_routes: set[tuple[str | None, str | None, None]] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != MECHANICAL_RESPONSE_FIELDS:
        raise MechanicalTaskRejected("mechanical response fields are invalid")
    if value["schema_version"] != MECHANICAL_SCHEMA_VERSION:
        raise MechanicalTaskRejected("unsupported mechanical response schema_version")
    if value["service_tier"] is not None:
        raise MechanicalTaskRejected("mechanical response reported a forbidden service tier")
    route = (value["model"], value["reasoning_effort"], value["service_tier"])
    allowed = allowed_routes or {
        tuple(PRIMARY_MECHANICAL_ROUTE.values()),
        tuple(FALLBACK_MECHANICAL_ROUTE.values()),
        (None, None, None),
    }
    if route not in allowed:
        raise MechanicalTaskRejected("mechanical response reported an unapproved model route")
    usage = value["token_usage"]
    if not isinstance(usage, dict) or set(usage) != set(TokenUsage().to_dict()):
        raise MechanicalTaskRejected("mechanical response token_usage is invalid")
    if any(type(item) is not int or item < 0 for item in usage.values()):
        raise MechanicalTaskRejected("mechanical response token usage must be non-negative integers")
    if value["token_telemetry"] not in {"observed", "partial", "unknown", "synthetic"}:
        raise MechanicalTaskRejected("mechanical response token_telemetry is invalid")
    if not isinstance(value["result"], dict) or not isinstance(value["artifacts"], list):
        raise MechanicalTaskRejected("mechanical response result/artifacts are invalid")
    if value["status"] not in {"COMPLETED", "FALSIFIED", "NO_COUNTEREXAMPLE_WITHIN_SCOPE", "FORMAL_CHECK_PASSED", "BLOCKED", "TOOL_ERROR", "REJECTED"}:
        raise MechanicalTaskRejected("mechanical response status is invalid")
    return dict(value)


@dataclass(slots=True)
class MechanicalExecution:
    status: str
    result: dict[str, Any]
    model: str | None
    reasoning_effort: str | None
    provider: str | None = None
    provider_profile: str | None = None
    service_tier: None = None
    actual_model: str | None = None
    actual_reasoning_effort: str | None = None
    model_route_attestation: str = "unobservable"
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    token_telemetry: str = "unknown"
    cost_usd: float | None = None
    cost_telemetry: str = "unknown"
    artifacts: list[str] = field(default_factory=list)
    runner_directory: str | None = None
    fallback: dict[str, Any] | None = None
    error: str | None = None
    failure_kind: str | None = None
    retryable: bool = False
    provider_reset_at: str | None = None
    unavailable_routes: list[dict[str, Any]] = field(default_factory=list)


class MechanicalRunner(Protocol):
    async def run(
        self, *, packet_path: Path, output_root: Path, timeout_seconds: int,
        route: str = "primary",
    ) -> MechanicalExecution: ...


def installed_mechanical_runner_adapters() -> set[str]:
    """Return provider adapter ids with a controller-managed one-shot runner."""
    return {
        "codex_app_server",
        *(item.name for item in entry_points(
            group="autonomous_math_research.mechanical_runners"
        )),
    }


def build_mechanical_runner(
    config: Any,
    repository_root: Path,
    *,
    primary_route: dict[str, Any],
    fallback_route: dict[str, Any],
) -> MechanicalRunner:
    """Build the pinned runner only after capability/configuration preflight."""
    route_adapters = {
        str(config.raw["providers"][str(route["provider"])]["adapter"])
        for route in (primary_route, fallback_route)
    }
    if route_adapters == {"codex_app_server"}:
        return SubprocessMechanicalRunner(
            repository_root,
            primary_route=primary_route,
            fallback_route=fallback_route,
        )
    if len(route_adapters) != 1:
        raise ValueError(
            "mechanical primary/fallback routes must share one installed runner adapter"
        )
    adapter = next(iter(route_adapters))
    factories = {
        item.name: item.load()
        for item in entry_points(
            group="autonomous_math_research.mechanical_runners"
        )
    }
    factory = factories.get(adapter)
    if factory is None:
        raise ValueError(
            f"mechanical runner adapter {adapter!r} is not installed"
        )
    runner = factory(
        config=config,
        repository_root=repository_root.resolve(),
        primary_route=dict(primary_route),
        fallback_route=dict(fallback_route),
    )
    if not callable(getattr(runner, "run", None)):
        raise TypeError(
            f"mechanical runner adapter {adapter!r} did not return a runner"
        )
    return runner


def _merge_usage(target: TokenUsage, raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens"),
        "uncached_input_tokens": ("uncached_input_tokens", "uncachedInputTokens"),
        "cache_write_input_tokens": ("cache_write_input_tokens", "cacheWriteInputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "reasoning_output_tokens": ("reasoning_output_tokens", "reasoningOutputTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
    }
    found = False
    for field_name, keys in aliases.items():
        for key in keys:
            if type(raw.get(key)) is int and raw[key] >= 0:
                setattr(target, field_name, getattr(target, field_name) + int(raw[key]))
                found = True
                break
    return found


def _runner_usage(metadata: dict[str, Any]) -> tuple[TokenUsage, str]:
    usage = TokenUsage()
    observed = 0
    unknown = 0
    attempts = metadata.get("route_attempts")
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            usage_keys: list[str] = []
            if (
                metadata.get("worker_started") is True
                and attempt.get("model") == metadata.get("selected_model")
            ):
                usage_keys.append("worker_usage")
            for key in usage_keys:
                if _merge_usage(usage, attempt.get(key)):
                    observed += 1
                else:
                    unknown += 1
    else:
        probe = metadata.get("model_probe")
        if isinstance(probe, dict) and "usage" in probe:
            observed += int(_merge_usage(usage, probe.get("usage")))
            unknown += int(probe.get("usage") is None)
        if "worker_usage" in metadata:
            observed += int(_merge_usage(usage, metadata.get("worker_usage")))
            unknown += int(metadata.get("worker_usage") is None)
    if usage.uncached_input_tokens == 0 and (
        usage.input_tokens or usage.cached_input_tokens
    ):
        usage.uncached_input_tokens = max(
            0, usage.input_tokens - usage.cached_input_tokens,
        )
    if observed and unknown:
        return usage, "partial"
    if observed:
        return usage, "observed"
    return usage, "unknown"


class SubprocessMechanicalRunner:
    """Controller-only launcher for the pinned one-shot skill runner."""

    def __init__(
        self,
        repository_root: Path,
        *,
        primary_route: dict[str, Any] | None = None,
        fallback_route: dict[str, Any] | None = None,
    ):
        self.repository_root = repository_root.resolve()
        self.primary_route = dict(primary_route or {
            "provider": "codex",
            **PRIMARY_MECHANICAL_ROUTE,
            "profile": None,
            "endpoint": None,
        })
        self.fallback_route = dict(fallback_route or {
            "provider": "codex",
            **FALLBACK_MECHANICAL_ROUTE,
            "profile": None,
            "endpoint": None,
        })
        package_root = Path(__file__).resolve().parent
        policy_root = (
            package_root / "resources" / "policy_packs" / "math-research"
        )
        self.script = (
            policy_root / "scripts" / "run_worker.py"
        )
        self.task_schema = (
            policy_root / "references" / "worker-task.schema.json"
        )
        self.result_schema = (
            policy_root / "references" / "worker-result.schema.json"
        )
        self.schema_validator = package_root / "schema.py"
        self.contract_definitions = package_root / "contracts.py"
        self.expected_hashes: dict[Path, str] = {}
        self.supports_explicit_route_selection = True
        self._unavailable_records: dict[tuple[str, str | None, None], dict[str, Any]] = {}
        self._load_read_only_status_seed()

    def _route_key(
        self, model: Any, reasoning_effort: Any, service_tier: Any,
    ) -> tuple[str, str | None, None] | None:
        if service_tier is not None or not isinstance(model, str):
            return None
        effort = str(reasoning_effort) if reasoning_effort is not None else None
        key = (model, effort, None)
        permitted = {
            (
                str(self.primary_route["model"]),
                str(self.primary_route["reasoning_effort"]),
                None,
            ),
            (
                str(self.fallback_route["model"]),
                str(self.fallback_route["reasoning_effort"]),
                None,
            ),
        }
        return key if key in permitted else None

    def remember_unavailable(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        service_tier: None = None,
        error: str = "controller-recovered permanent unavailable attestation",
        run_directory: str = "controller-event-replay",
    ) -> dict[str, Any] | None:
        key = self._route_key(model, reasoning_effort, service_tier)
        if key is None:
            return None
        record = {
            "model": key[0],
            "reasoning_effort": key[1],
            "service_tier": None,
            "failed_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": str(error),
            "run_directory": str(run_directory),
        }
        self._unavailable_records[key] = record
        return dict(record)

    def _load_read_only_status_seed(self) -> None:
        path = self.repository_root / ".tooling" / "math-worker-model-status.json"
        if not path.is_file():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(value, dict) or not isinstance(value.get("unavailable"), list):
            return
        for record in value["unavailable"]:
            if not isinstance(record, dict):
                continue
            key = self._route_key(
                record.get("model"),
                record.get("reasoning_effort"),
                record.get("service_tier"),
            )
            if key is not None and all(
                isinstance(record.get(field), str)
                for field in ("failed_at_utc", "error", "run_directory")
            ):
                self._unavailable_records[key] = dict(record)

    def persist_unavailable(
        self,
        *,
        model: str,
        reasoning_effort: str | None,
        service_tier: None = None,
        error: str = "permanent unavailable attestation",
        run_directory: str = "controller-event",
    ) -> dict[str, Any]:
        """Persist one exact controller-attested route in the global circuit breaker.

        Broker children never call this method. They receive an attempt-local
        seed; only the deterministic controller may update cross-run state.
        """
        normalized = self.remember_unavailable(
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            error=error,
            run_directory=run_directory,
        )
        if normalized is None:
            raise MechanicalTaskRejected(
                "refusing to persist an unapproved mechanical route"
            )
        path = self.repository_root / ".tooling" / "math-worker-model-status.json"
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MechanicalTaskRejected(
                    f"cannot safely update mechanical route circuit breaker: {exc}"
                ) from exc
            if (
                not isinstance(current, dict)
                or current.get("schema_version") != 1
                or not isinstance(current.get("unavailable"), list)
                or any(not isinstance(item, dict) for item in current["unavailable"])
            ):
                raise MechanicalTaskRejected(
                    "cannot safely update invalid mechanical route circuit breaker"
                )
            records = list(current["unavailable"])
        else:
            records = []
        key = self._route_key(model, reasoning_effort, service_tier)
        records = [
            record for record in records
            if self._route_key(
                record.get("model"),
                record.get("reasoning_effort"),
                record.get("service_tier"),
            ) != key
        ]
        records.append(normalized)
        atomic_write_json(path, {"schema_version": 1, "unavailable": records})
        return dict(normalized)

    def _remember_metadata_unavailable(
        self, metadata: dict[str, Any], run_dir: Path,
    ) -> list[dict[str, Any]]:
        remembered: dict[tuple[str, str | None, None], dict[str, Any]] = {}
        attempts = metadata.get("route_attempts")
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                for field in ("newly_cached_unavailable", "cached_unavailable"):
                    record = attempt.get(field)
                    if not isinstance(record, dict):
                        continue
                    normalized = self.remember_unavailable(
                        model=str(record.get("model") or ""),
                        reasoning_effort=(
                            str(record["reasoning_effort"])
                            if record.get("reasoning_effort") is not None else None
                        ),
                        service_tier=record.get("service_tier"),
                        error=str(record.get("error") or "permanent unavailable"),
                        run_directory=str(record.get("run_directory") or run_dir),
                    )
                    if normalized is not None:
                        key = self._route_key(
                            normalized["model"],
                            normalized["reasoning_effort"],
                            normalized["service_tier"],
                        )
                        if key is not None:
                            remembered[key] = normalized
        failure = metadata.get("failure")
        if isinstance(failure, dict) and failure.get("kind") == "model_unavailable":
            model = metadata.get("selected_model") or metadata.get("requested_model")
            effort = (
                metadata.get("selected_reasoning_effort")
                or metadata.get("requested_reasoning_effort")
            )
            if model:
                normalized = self.remember_unavailable(
                    model=str(model),
                    reasoning_effort=str(effort) if effort is not None else None,
                    service_tier=None,
                    error=str(failure.get("message") or "permanent unavailable"),
                    run_directory=str(run_dir),
                )
                if normalized is not None:
                    key = self._route_key(
                        normalized["model"],
                        normalized["reasoning_effort"],
                        normalized["service_tier"],
                    )
                    if key is not None:
                        remembered[key] = normalized
        return list(remembered.values())

    @staticmethod
    def model_status_path(packet_path: Path, output_root: Path) -> Path:
        return (
            output_root.resolve().parent
            / "model-status"
            / f"{packet_path.stem}.json"
        )

    def _write_attempt_status_seed(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 1,
            "unavailable": list(self._unavailable_records.values()),
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)

    def configure_pinned_policy(
        self, worker_manifest: dict[str, Any], manifest_path: Path,
    ) -> None:
        policy_root = manifest_path.parent.resolve()

        def snapshot(entry: dict[str, Any]) -> Path:
            path = (policy_root / str(entry["snapshot_path"])).resolve()
            if not path.is_relative_to(policy_root):
                raise MechanicalTaskRejected(
                    "mechanical policy snapshot escapes the run policy directory"
                )
            return path

        self.script = snapshot(worker_manifest["runner"])
        self.task_schema = snapshot(worker_manifest["task_schema"])
        self.result_schema = snapshot(worker_manifest["result_schema"])
        self.schema_validator = snapshot(worker_manifest["schema_validator"])
        self.contract_definitions = snapshot(worker_manifest["contract_definitions"])
        for attribute, name in (
            ("primary_route", "primary_route"),
            ("fallback_route", "fallback_route"),
        ):
            route = worker_manifest[name]
            setattr(self, attribute, {
                "provider": route["provider"],
                "model": route["model"],
                "reasoning_effort": route["effort"],
                "service_tier": None,
                "profile": route.get("profile"),
                "endpoint": route.get("endpoint"),
            })
        self.expected_hashes = {
            self.script.resolve(): str(worker_manifest["runner"]["sha256"]),
            self.task_schema.resolve(): str(worker_manifest["task_schema"]["sha256"]),
            self.result_schema.resolve(): str(worker_manifest["result_schema"]["sha256"]),
            self.schema_validator.resolve(): str(
                worker_manifest["schema_validator"]["sha256"]
            ),
            self.contract_definitions.resolve(): str(
                worker_manifest["contract_definitions"]["sha256"]
            ),
        }
        self.supports_explicit_route_selection = (
            '"start_route"' in self.script.read_text(encoding="utf-8")
        )

    @staticmethod
    def _digest(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    async def _terminate_recovered_process_tree(pid: int) -> None:
        """Terminate only the PID attested by a matching broker receipt."""
        if pid <= 0:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    @staticmethod
    def _pid_is_alive(pid: int) -> bool | None:
        """Return True/False when safely knowable, otherwise None.

        Recovery must never convert an access-denied/ambiguous PID check into
        permission to launch a duplicate attempt.
        """
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, pid,
            )
            if not handle:
                error = int(kernel32.GetLastError())
                return False if error == 87 else None  # ERROR_INVALID_PARAMETER
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return True

    @staticmethod
    def receipt_path(packet_path: Path, output_root: Path) -> Path:
        return (
            output_root.resolve().parent
            / "receipts"
            / f"{packet_path.stem}.json"
        )

    def _execution_from_run_dir(
        self, run_dir: Path, *, output_root: Path,
    ) -> MechanicalExecution:
        run_dir = run_dir.resolve()
        output_root = output_root.resolve()
        if not run_dir.is_relative_to(output_root) or not run_dir.is_dir():
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                error="mechanical receipt returned a run directory outside its output root",
                failure_kind="runner_receipt_validation", retryable=False,
            )
        try:
            result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
            metadata = json.loads((run_dir / "runner.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                runner_directory=str(run_dir), error=f"invalid runner output: {exc}",
                failure_kind="runner_protocol", retryable=False,
            )
        unavailable_routes = self._remember_metadata_unavailable(metadata, run_dir)
        usage, telemetry = _runner_usage(metadata)
        model = metadata.get("selected_model") or metadata.get("requested_model")
        effort = metadata.get("selected_reasoning_effort") or metadata.get(
            "requested_reasoning_effort"
        )
        provider = metadata.get("selected_provider") or metadata.get("requested_provider")
        provider_profile = metadata.get("selected_provider_profile")
        actual_model = metadata.get("actual_model")
        actual_effort = metadata.get("actual_reasoning_effort")
        model_attestation = str(
            metadata.get("model_route_attestation") or "unobservable"
        )
        tier = metadata.get(
            "selected_service_tier", metadata.get("requested_service_tier")
        )
        observed_tier = metadata.get("observed_service_tier")
        raw_cost = metadata.get("cost_usd")
        cost_usd = (
            float(raw_cost)
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
            and raw_cost >= 0
            else None
        )
        cost_telemetry = "observed" if cost_usd is not None else "unknown"
        observed_tier_violation = (
            observed_tier is not None
            and observed_tier != ""
            and observed_tier != []
            and observed_tier != ()
        )
        if tier not in {None, ""} or observed_tier_violation:
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=str(model) if model else None,
                reasoning_effort=str(effort) if effort else None,
                actual_model=(str(actual_model) if actual_model else None),
                actual_reasoning_effort=(
                    str(actual_effort) if actual_effort else None
                ),
                model_route_attestation=model_attestation,
                token_usage=usage, token_telemetry=telemetry,
                runner_directory=str(run_dir),
                error=(
                    "runner requested or observed forbidden service tier: "
                    f"requested={tier!r}, observed={observed_tier!r}"
                ),
                failure_kind="service_tier_policy", retryable=False,
                unavailable_routes=unavailable_routes,
            )
        model_mismatch = bool(
            model_attestation == "violation"
            or (actual_model and actual_model != model)
            or (actual_effort and actual_effort != effort)
        )
        if model_mismatch:
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=str(model) if model else None,
                reasoning_effort=str(effort) if effort else None,
                actual_model=(str(actual_model) if actual_model else None),
                actual_reasoning_effort=(
                    str(actual_effort) if actual_effort else None
                ),
                model_route_attestation="violation",
                token_usage=usage, token_telemetry=telemetry,
                runner_directory=str(run_dir),
                error=(
                    "runner observed a forbidden model reroute or route mismatch: "
                    f"requested={model!r}/{effort!r}, "
                    f"actual={actual_model!r}/{actual_effort!r}"
                ),
                failure_kind="model_route_policy", retryable=False,
                unavailable_routes=unavailable_routes,
            )
        artifacts: list[str] = []
        for raw in result.get("artifacts", []) if isinstance(result, dict) else []:
            resolved = (run_dir / str(raw)).resolve()
            if resolved.is_relative_to(run_dir) and resolved.is_file():
                artifacts.append(str(resolved))
        status = (
            str(result.get("status") or "TOOL_ERROR")
            if isinstance(result, dict) else "TOOL_ERROR"
        )
        failure = (
            metadata.get("failure")
            if isinstance(metadata.get("failure"), dict) else {}
        )
        return MechanicalExecution(
            status=status,
            result=result if isinstance(result, dict) else {},
            model=str(model) if model else None,
            reasoning_effort=str(effort) if effort else None,
            provider=str(provider) if provider else None,
            provider_profile=(str(provider_profile) if provider_profile else None),
            actual_model=(str(actual_model) if actual_model else None),
            actual_reasoning_effort=(str(actual_effort) if actual_effort else None),
            model_route_attestation=model_attestation,
            token_usage=usage,
            token_telemetry=telemetry,
            cost_usd=cost_usd,
            cost_telemetry=cost_telemetry,
            artifacts=artifacts,
            runner_directory=str(run_dir),
            fallback=(
                metadata.get("fallback")
                if isinstance(metadata.get("fallback"), dict) else None
            ),
            error=(str(failure.get("message")) if failure.get("message") else None),
            failure_kind=(str(failure.get("kind")) if failure.get("kind") else None),
            retryable=bool(failure.get("retryable", False)),
            provider_reset_at=(
                str(failure.get("provider_reset_at"))[:200]
                if failure.get("provider_reset_at") is not None else None
            ),
            unavailable_routes=unavailable_routes,
        )

    async def recover(
        self,
        *,
        packet_path: Path,
        output_root: Path,
        receipt_path: Path,
        timeout_seconds: int,
    ) -> MechanicalExecution:
        """Reattach to one runner that survived a controller crash.

        A fresh matching heartbeat is treated as an in-flight attempt. A retry
        is permitted only after an exited receipt or a safely dead PID. Missing,
        stale-but-live, and PID-ambiguous leases fail closed without dispatching
        a second process for the same attempt.
        """
        packet_path = packet_path.resolve()
        output_root = output_root.resolve()
        receipt_path = receipt_path.resolve()
        allowed_receipts = (output_root.parent / "receipts").resolve()
        if receipt_path.parent != allowed_receipts:
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                error="mechanical recovery receipt escapes the attempt receipts directory",
                failure_kind="runner_receipt_validation", retryable=False,
            )
        expected_packet_hash = self._digest(packet_path)
        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        receipt: dict[str, Any] | None = None
        heartbeat_fresh = False
        pid_state: bool | None = None
        expected_fields = {
            "schema_version", "status", "pid", "packet_path", "packet_sha256",
            "output_root", "run_directory", "started_at", "heartbeat_at",
            "timeout_seconds", "finished_at",
        }
        while True:
            now_monotonic = asyncio.get_running_loop().time()
            try:
                raw = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                if set(raw) != expected_fields:
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error="mechanical recovery receipt fields are invalid",
                        failure_kind="runner_receipt_validation", retryable=False,
                    )
                receipt = raw
                if (
                    raw.get("schema_version") != 1
                    or Path(str(raw.get("packet_path"))).resolve() != packet_path
                    or str(raw.get("packet_sha256")) != expected_packet_hash
                    or Path(str(raw.get("output_root"))).resolve() != output_root
                    or type(raw.get("pid")) is not int
                    or int(raw["pid"]) <= 0
                ):
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error="mechanical recovery receipt identity does not match the attempt",
                        failure_kind="runner_receipt_validation", retryable=False,
                    )
                run_directory = raw.get("run_directory")
                if run_directory:
                    run_dir = Path(str(run_directory)).resolve()
                    if not run_dir.is_relative_to(output_root):
                        return MechanicalExecution(
                            status="TOOL_ERROR", result={}, model=None,
                            reasoning_effort=None,
                            error="mechanical recovery run directory escapes output root",
                            failure_kind="runner_receipt_validation", retryable=False,
                        )
                    if (run_dir / "result.json").is_file() and (
                        run_dir / "runner.json"
                    ).is_file():
                        return self._execution_from_run_dir(
                            run_dir, output_root=output_root,
                        )
                try:
                    heartbeat = datetime.fromisoformat(str(raw["heartbeat_at"]))
                    if heartbeat.tzinfo is None:
                        raise ValueError("heartbeat is not timezone-aware")
                    heartbeat_age = (
                        datetime.now(timezone.utc) - heartbeat.astimezone(timezone.utc)
                    ).total_seconds()
                except (TypeError, ValueError):
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error="mechanical recovery receipt heartbeat is invalid",
                        failure_kind="runner_receipt_validation", retryable=False,
                    )
                if raw.get("status") == "exited":
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error=(
                            "mechanical runner exited without a complete durable result; "
                            "the original attempt is preserved as unknown"
                        ),
                        failure_kind="mechanical_crash_unknown", retryable=True,
                    )
                if raw.get("status") != "running":
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error="mechanical recovery receipt status is invalid",
                        failure_kind="runner_receipt_validation", retryable=False,
                    )
                pid_state = self._pid_is_alive(int(raw["pid"]))
                if pid_state is False:
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None,
                        reasoning_effort=None,
                        error=(
                            "mechanical runner PID is no longer alive after controller restart; "
                            "the original attempt telemetry is unknown"
                        ),
                        failure_kind="mechanical_crash_unknown", retryable=True,
                    )
                heartbeat_fresh = heartbeat_age <= 15.0
            else:
                receipt = None
                heartbeat_fresh = False
                pid_state = None
            if now_monotonic >= deadline:
                if (
                    receipt is not None
                    and receipt.get("status") == "running"
                    and heartbeat_fresh
                    and pid_state is True
                ):
                    await self._terminate_recovered_process_tree(int(receipt["pid"]))
                    return MechanicalExecution(
                        status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                        error=(
                            "recovered mechanical runner exceeded its original controller "
                            "envelope and its fresh attested process was terminated"
                        ),
                        failure_kind="timeout_transient", retryable=True,
                    )
                reason = (
                    "mechanical runner receipt never appeared after controller restart"
                    if receipt is None
                    else "mechanical runner lease is stale or its PID identity is ambiguous"
                )
                return MechanicalExecution(
                    status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                    error=(
                        f"{reason}; duplicate dispatch is forbidden and the original "
                        "attempt remains immutable unknown evidence"
                    ),
                    failure_kind="mechanical_lease_uncertain", retryable=False,
                )
            try:
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                if (
                    receipt is not None
                    and receipt.get("status") == "running"
                    and heartbeat_fresh
                    and pid_state is True
                ):
                    await self._terminate_recovered_process_tree(int(receipt["pid"]))
                raise

    async def run(
        self, *, packet_path: Path, output_root: Path, timeout_seconds: int,
        route: str = "primary",
    ) -> MechanicalExecution:
        if route not in {"primary", "fallback"}:
            raise MechanicalTaskRejected("mechanical route must be primary or fallback")
        packet_path = packet_path.resolve()
        output_root = output_root.resolve()
        attempt_root = output_root.parent
        workspace_root = attempt_root.parent
        if (
            output_root != (workspace_root / "mechanical_subtasks" / "runs").resolve()
            or packet_path.parent
            != (workspace_root / "mechanical_subtasks" / "packets").resolve()
            or not workspace_root.is_relative_to(self.repository_root)
        ):
            raise MechanicalTaskRejected(
                "mechanical packet/output paths do not share a valid parent attempt workspace"
            )
        output_root.mkdir(parents=True, exist_ok=True)
        if not output_root.is_relative_to(self.repository_root):
            raise MechanicalTaskRejected("mechanical output root escapes repository")
        if not self.expected_hashes:
            raise MechanicalTaskRejected("mechanical runner has no pinned policy hashes")
        for path, expected in self.expected_hashes.items():
            if not path.is_file() or self._digest(path) != expected:
                raise MechanicalTaskRejected(
                    f"mechanical runner policy source drifted after pinning: {path}"
                )
        command = [
            sys.executable,
            str(self.script),
            str(packet_path),
            "--output-root", str(output_root),
            "--timeout", str(int(timeout_seconds)),
            "--broker-managed",
        ]
        route_config_path = (
            output_root.parent / "route-configs" / f"{packet_path.stem}.json"
        )
        selected_primary = self.primary_route
        if route == "fallback" and not self.supports_explicit_route_selection:
            selected_primary = self.fallback_route
        route_config = {
            "schema_version": 2 if self.supports_explicit_route_selection else 1,
            "primary_route": {
                "provider": selected_primary.get("provider"),
                "model": selected_primary["model"],
                "reasoning_effort": selected_primary["reasoning_effort"],
                "service_tier": None,
                "profile": selected_primary.get("profile"),
            },
            "fallback_route": {
                "provider": self.fallback_route.get("provider"),
                "model": self.fallback_route["model"],
                "reasoning_effort": self.fallback_route["reasoning_effort"],
                "service_tier": None,
                "profile": self.fallback_route.get("profile"),
            },
        }
        if self.supports_explicit_route_selection:
            route_config["start_route"] = route
        atomic_write_json(route_config_path, route_config)
        command.extend(["--route-config", str(route_config_path)])
        receipt_path = self.receipt_path(packet_path, output_root)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--broker-receipt", str(receipt_path)])
        model_status_path = self.model_status_path(packet_path, output_root)
        self._write_attempt_status_seed(model_status_path)
        command.extend(["--model-status-path", str(model_status_path)])
        environment = dict(os.environ)
        for key in (
            "MATH_WORKER_MODEL", "MATH_WORKER_REASONING_EFFORT",
            "MATH_WORKER_SERVICE_TIER", "MATH_WORKER_REPOSITORY_ROOT",
            "MATH_WORKER_SCHEMA_VALIDATOR_PATH",
            "MATH_WORKER_CONTRACT_DEFINITIONS_PATH",
        ):
            environment.pop(key, None)
        environment["MATH_WORKER_REPOSITORY_ROOT"] = str(workspace_root)
        environment["MATH_WORKER_SCHEMA_VALIDATOR_PATH"] = str(
            self.schema_validator.resolve()
        )
        environment["MATH_WORKER_CONTRACT_DEFINITIONS_PATH"] = str(
            self.contract_definitions.resolve()
        )
        subprocess_options: dict[str, Any] = {}
        if os.name == "nt":
            subprocess_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            subprocess_options["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workspace_root),
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_options,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, int(timeout_seconds)) + 30,
            )
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            raise
        except asyncio.TimeoutError:
            await self._terminate_process_tree(process)
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                error="mechanical runner exceeded the controller envelope timeout",
                failure_kind="timeout_transient", retryable=True,
            )
        rendered_stdout = stdout.decode("utf-8", errors="replace")
        rendered_stderr = stderr.decode("utf-8", errors="replace")
        run_dir: Path | None = None
        for line in rendered_stdout.splitlines():
            candidate = Path(line.strip())
            if candidate.is_dir():
                resolved = candidate.resolve()
                if resolved.is_relative_to(output_root):
                    run_dir = resolved
                    break
        if run_dir is None:
            return MechanicalExecution(
                status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                error=(
                    "mechanical runner did not return an isolated run directory; "
                    + (rendered_stderr.strip() or rendered_stdout.strip())[:1000]
                ),
                failure_kind="runner_protocol", retryable=False,
            )
        return self._execution_from_run_dir(run_dir, output_root=output_root)
