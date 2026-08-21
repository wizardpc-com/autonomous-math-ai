from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .app_server import (
    AppServerClient, AppServerError, AppServerRequestError,
    AppServerTransportClosed, AppServerTurnFailed,
    AppServerTurnTimeout, ModelRoutePolicyError, ServiceTierPolicyError,
    StructuredOutputProtocolError, UnmanagedContinuationError,
    attest_model_route, attest_no_service_tier,
    parse_structured_message, redact_auth_material,
)
from .config import HarnessConfig
from .models import CandidateEvent, JobOutcome, ResearchTask, TokenUsage, utc_now
from .schema import (
    OutputSchemaCompatibilityError, SchemaError, validate,
    validate_output_schema_compatibility,
)


CandidateSink = Callable[[CandidateEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnDirective:
    continue_same_thread: bool
    reason: str
    next_prompt: str | None = None
    effort_override: str | None = None

    @classmethod
    def stop(cls, reason: str) -> "TurnDirective":
        return cls(False, reason)

    @classmethod
    def continue_with(
        cls,
        prompt: str,
        *,
        reason: str = "controller requested same-thread continuation",
        effort_override: str | None = None,
    ) -> "TurnDirective":
        return cls(True, reason, prompt, effort_override)


TurnController = Callable[[JobOutcome, int], Awaitable[TurnDirective]]


_PROVIDER_QUOTA_CODES = frozenset({
    "usage_limit_reached", "usage_limit_exceeded", "insufficient_quota",
    "quota_exceeded", "billing_hard_limit_reached", "spend_limit_reached",
})
_PROVIDER_QUOTA_PHRASES = (
    "you've hit your usage limit",
    "you have hit your usage limit",
    "exceeded your current quota",
    "insufficient quota",
    "billing hard limit",
    "usage quota exhausted",
)
_PROVIDER_RESET_KEYS = (
    "provider_reset_at", "reset_at", "resets_at", "resetAt", "resetsAt",
    "reset_time", "resetTime", "next_reset_at", "nextResetAt",
)


def _error_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    pending: list[Any] = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            nodes.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return nodes


def _provider_quota_details(details: dict[str, Any]) -> dict[str, Any] | None:
    nodes = _error_nodes(details)
    codes = {
        str(node.get(key) or "").strip().casefold()
        for node in nodes for key in ("code", "type", "error_code", "errorCode")
    }
    serialized = json.dumps(details, ensure_ascii=False).casefold()
    if not (
        codes & _PROVIDER_QUOTA_CODES
        or any(phrase in serialized for phrase in _PROVIDER_QUOTA_PHRASES)
    ):
        return None
    normalized = dict(details)
    reset_at: Any = None
    for node in nodes:
        for key in _PROVIDER_RESET_KEYS:
            candidate = node.get(key)
            if (
                isinstance(candidate, (str, int, float))
                and not isinstance(candidate, bool)
                and str(candidate).strip()
            ):
                reset_at = candidate
                break
        if reset_at is not None:
            break
    normalized["provider_reset_at"] = reset_at
    normalized["quota_codes"] = sorted(code for code in codes if code)
    return normalized


def _server_error_details(raw: dict[str, Any]) -> dict[str, Any]:
    details = dict(raw)
    data = details.get("data")
    if isinstance(data, dict):
        nested_data_error = data.get("error")
        details = {
            **details,
            **data,
            **(nested_data_error if isinstance(nested_data_error, dict) else {}),
        }
    raw_message = details.get("message")
    if isinstance(raw_message, str):
        try:
            decoded = json.loads(raw_message)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            nested = decoded.get("error")
            if isinstance(nested, dict):
                details = {**details, **nested}
            if decoded.get("status") is not None:
                details["http_status"] = decoded["status"]
    return details


def _turn_error_details(exc: AppServerTurnFailed) -> dict[str, Any]:
    return _server_error_details(exc.server_error)


def _classify_failure(exc: Exception) -> tuple[str, bool, dict[str, Any] | None]:
    if isinstance(exc, OutputSchemaCompatibilityError):
        return "invalid_output_schema", False, {
            "issues": [
                {
                    "schema_path": item.schema_path,
                    "json_path": item.json_path,
                    "reason": item.reason,
                }
                for item in exc.issues
            ]
        }
    if isinstance(exc, ServiceTierPolicyError):
        return "service_tier_policy", False, None
    if isinstance(exc, UnmanagedContinuationError):
        return "unmanaged_continuation", False, None
    if isinstance(exc, ModelRoutePolicyError):
        return "model_route_policy", False, {
            "phase": exc.phase,
            "requested_model": exc.requested_model,
            "observed_model": exc.observed_model,
            "route_event": exc.route_event,
        }
    if isinstance(exc, AppServerTurnTimeout):
        details = _server_error_details(exc.server_error)
        if details.get("did_not_stop_after_interrupt"):
            return "provider_transport_lost", False, details
        return "transport_transient", True, details
    if isinstance(exc, (AppServerTurnFailed, AppServerRequestError)):
        details = (
            _turn_error_details(exc) if isinstance(exc, AppServerTurnFailed)
            else _server_error_details(exc.server_error)
        )
        code = str(details.get("code") or "").lower()
        error_type = str(details.get("type") or "").lower()
        serialized = json.dumps(details, ensure_ascii=False).lower()
        try:
            status = int(details.get("http_status") or 0)
        except (TypeError, ValueError):
            status = 0
        if code == "invalid_json_schema" or "invalid_json_schema" in serialized:
            return "invalid_output_schema", False, details
        quota_details = _provider_quota_details(details)
        if quota_details is not None:
            return "provider_quota_exhausted", False, quota_details
        if (
            status == 429 or "rate_limit" in code or "rate_limit" in error_type
            or "rate limit" in serialized
        ):
            return "rate_limit", True, details
        retryable = status >= 500 or code in {
            "server_error", "internal_error", "temporarily_unavailable", "service_unavailable",
        }
        return "transport_transient" if retryable else "turn_failed", retryable, details
    if isinstance(exc, StructuredOutputProtocolError):
        return "model_output_protocol", True, None
    if isinstance(exc, AppServerTransportClosed):
        details = getattr(exc, "server_error", None)
        if not isinstance(details, dict):
            details = {
                "code": "app_server_transport_closed",
                "message": str(redact_auth_material(str(exc))),
            }
        return "provider_transport_lost", False, details
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, AppServerError)):
        return "transport_transient", True, None
    if isinstance(exc, SchemaError):
        return "model_output_validation", True, None
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "role_semantic_validation", True, None
    return "backend_internal", False, None


class CodexBackend(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def run_job(
        self,
        *,
        job_id: str,
        task: ResearchTask,
        prompt: str,
        output_schema: dict[str, Any],
        workspace: Path,
        writable_roots: list[Path],
        timeout: float,
        token_budget: int | None,
        candidate_sink: CandidateSink,
        skill_path: Path | None = None,
        turn_controller: TurnController | None = None,
    ) -> JobOutcome: ...
    async def cancel(self, job_id: str) -> bool: ...
    async def rate_limits(self) -> dict[str, Any] | None: ...
    def set_economy_mode(self, enabled: bool) -> None: ...
    def supports_same_thread_continuation(self, role: str) -> bool: ...


class AppServerBackend:
    def __init__(
        self,
        config: HarnessConfig,
        trace_notification: Callable[[dict[str, Any]], Any] | None = None,
        *,
        provider_name: str = "codex",
    ):
        self.config = config
        self.provider_name = provider_name
        self.client = AppServerClient(notification_handler=trace_notification)
        self.active: dict[str, tuple[str, str]] = {}

    def set_economy_mode(self, enabled: bool) -> None:
        # Budget pressure may reduce concurrency or stop exploratory dispatch,
        # but top-level mathematical roles remain on their pinned strong routes.
        # Mechanical Spark/Luna routing is owned exclusively by the broker.
        del enabled

    def _model_for(self, role: str) -> tuple[str, str]:
        route = self.config.route_for(role)
        return str(route["model"]), str(route["mapped_effort"])

    def supports_same_thread_continuation(self, role: str) -> bool:
        return role in {"prover", "falsifier", "explorer"}

    async def start(self) -> None:
        await self.client.start()

    async def close(self) -> None:
        await self.client.close()

    async def run_job(
        self,
        *,
        job_id: str,
        task: ResearchTask,
        prompt: str,
        output_schema: dict[str, Any],
        workspace: Path,
        writable_roots: list[Path],
        timeout: float,
        token_budget: int | None,
        candidate_sink: CandidateSink,
        skill_path: Path | None = None,
        turn_controller: TurnController | None = None,
    ) -> JobOutcome:
        del candidate_sink  # Real workers submit early candidates through the filesystem helper.
        route_config = self.config.route_for(task.role)
        if route_config["provider"] != self.provider_name:
            raise ValueError(
                f"AppServerBackend cannot execute provider route {route_config['provider']!r}"
            )
        model, effort = self._model_for(task.role)
        developer = (
            "You are one bounded autonomous math-research role. Do not spawn research, strategy, "
            "or recursive subagents. The sole delegation exception is the controller-installed "
            "delegate_mechanical_task command in the job workspace; never invoke codex exec or "
            "another worker directly. "
            "Do not use fast or priority service tier. Do not modify canonical claims, proofs, state, artifacts, "
            "or historical experiments. Write only inside the supplied job workspace."
        )
        thread_id: str | None = None
        turn_id: str | None = None
        observed_tier = "unobservable"
        usage = TokenUsage()
        token_telemetry = "unknown"
        raw_output = ""
        turn_history: list[dict[str, Any]] = []
        last_completed_result: dict[str, Any] = {}
        try:
            validate_output_schema_compatibility(
                output_schema, schema_path=f"{task.output_contract} ({task.role})",
            )
            started = await self.client.start_thread(
                model=model, cwd=workspace, sandbox="workspace-write", developer_instructions=developer
            )
            thread = started["thread"]
            thread_id = thread["id"]
            # This check deliberately precedes goal/turn creation so a server-side
            # tier override cannot consume research tokens under a forbidden mode.
            observed_tier = attest_no_service_tier(thread, "thread/start")
            attest_model_route(thread, "thread/start", model)
            # Never arm an App Server goal for autonomous work.  An active
            # server goal may create native continuations outside controller
            # ownership.  Per-thread budgets are enforced from observed token
            # notifications by the deterministic controller instead.
            current_prompt = prompt
            current_effort = effort
            turn_index = 0
            final_outcome: JobOutcome | None = None
            while True:
                turn_index += 1
                completed, raw_output, turn_usage, turn_telemetry = await self.client.start_turn(
                    thread_id=thread_id,
                    prompt=current_prompt,
                    cwd=workspace,
                    model=model,
                    effort=current_effort,
                    output_schema=output_schema,
                    writable_roots=writable_roots,
                    timeout=timeout,
                    skill_path=skill_path,
                    on_started=lambda active_turn_id: self.active.__setitem__(
                        job_id, (thread_id, active_turn_id)
                    ),
                )
                turn = completed.get("turn") or {}
                turn_id = str(turn.get("id") or "") or None
                turn_tier = attest_no_service_tier(turn, "turn/completed")
                if turn_tier != "unobservable":
                    observed_tier = turn_tier
                attest_model_route(turn, "turn/completed", model)
                if str(turn.get("status") or "").lower() != "completed":
                    raise AppServerTurnFailed(
                        turn,
                        raw_output=raw_output,
                        token_usage=turn_usage,
                        token_telemetry=turn_telemetry,
                    )
                parsed = parse_structured_message(raw_output)
                validate(parsed, output_schema)
                last_completed_result = dict(parsed)
                # App Server thread usage is cumulative. Keep the largest
                # observed component rather than double-counting later turns.
                for field_name in TokenUsage.__dataclass_fields__:
                    setattr(
                        usage,
                        field_name,
                        max(getattr(usage, field_name), getattr(turn_usage, field_name)),
                    )
                if turn_telemetry == "observed":
                    token_telemetry = "observed"
                partial = JobOutcome(
                    job_id=job_id, task_id=task.task_id, role=task.role,
                    claim_id=task.target_claim, status="completed", result=parsed,
                    thread_id=thread_id, turn_id=turn_id, model=model,
                    reasoning_effort=current_effort, provider=self.provider_name,
                    provider_profile=route_config.get("profile"),
                    requested_service_tier=None,
                    observed_service_tier=observed_tier,
                    token_usage=turn_usage, token_telemetry=turn_telemetry,
                    cost_usd=None, cost_telemetry="unknown",
                    artifact_paths=list(parsed.get("artifact_paths", [])),
                )
                if token_budget is not None:
                    if turn_telemetry != "observed":
                        partial.continuation_budget_stop_reason = (
                            "token telemetry unavailable; bounded continuation stopped fail-closed"
                        )
                    elif turn_usage.total_tokens >= token_budget:
                        partial.continuation_budget_stop_reason = (
                            "controller token budget reached"
                        )
                history_row = {
                    "turn_index": turn_index,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "effort": current_effort,
                    "result_type": parsed.get("result_type"),
                    "role_reported_status": parsed.get("status"),
                    "reasoning_output_tokens": turn_usage.reasoning_output_tokens,
                    "total_tokens": turn_usage.total_tokens,
                    "token_telemetry": turn_telemetry,
                    "token_usage": turn_usage.to_dict(),
                }
                turn_history.append(history_row)
                directive = (
                    await turn_controller(partial, turn_index)
                    if turn_controller is not None
                    else TurnDirective.stop("single-turn backend compatibility mode")
                )
                if (
                    directive.continue_same_thread
                    and partial.continuation_budget_stop_reason
                ):
                    directive = TurnDirective.stop(
                        partial.continuation_budget_stop_reason
                    )
                history_row["controller_directive"] = (
                    "CONTINUE" if directive.continue_same_thread else "STOP"
                )
                history_row["controller_reason"] = directive.reason
                history_row["next_effort"] = directive.effort_override
                final_outcome = partial
                if not directive.continue_same_thread:
                    break
                if not directive.next_prompt:
                    raise ValueError("same-thread continuation requires a non-empty next prompt")
                current_prompt = directive.next_prompt
                current_effort = directive.effort_override or current_effort
            assert final_outcome is not None
            final_outcome.token_usage = usage
            final_outcome.token_telemetry = token_telemetry
            final_outcome.turn_history = turn_history
            final_outcome.reasoning_effort = effort
            final_outcome.logical_stop_reason = turn_history[-1]["controller_reason"]
            return final_outcome
        except Exception as exc:
            if isinstance(exc, ServiceTierPolicyError):
                observed_tier = exc.observed_service_tier
            evidence_usage = getattr(exc, "token_usage", None)
            if isinstance(evidence_usage, TokenUsage):
                usage = evidence_usage
            evidence_telemetry = getattr(exc, "token_telemetry", None)
            if isinstance(evidence_telemetry, str):
                token_telemetry = evidence_telemetry
            evidence_output = getattr(exc, "raw_output", None)
            if isinstance(evidence_output, str):
                raw_output = evidence_output
            raw_output = str(redact_auth_material(raw_output))
            evidence_turn_id = getattr(exc, "turn_id", None)
            if evidence_turn_id:
                turn_id = str(evidence_turn_id)
            failure_kind, retryable, server_error = _classify_failure(exc)
            evidence_turn = getattr(exc, "turn", None)
            terminal_event = (
                dict(evidence_turn)
                if isinstance(evidence_turn, dict) and evidence_turn
                else None
            )
            return JobOutcome(
                job_id=job_id, task_id=task.task_id, role=task.role, claim_id=task.target_claim,
                status="ERROR", result=last_completed_result,
                thread_id=thread_id, turn_id=turn_id,
                model=model, reasoning_effort=effort,
                provider=self.provider_name,
                provider_profile=route_config.get("profile"),
                requested_service_tier=None, observed_service_tier=observed_tier,
                token_usage=usage, token_telemetry=token_telemetry,
                cost_usd=None, cost_telemetry="unknown",
                error=str(redact_auth_material(str(exc))),
                failure_kind=failure_kind, retryable=retryable, server_error=server_error,
                terminal_event=terminal_event, raw_output=raw_output or None,
                turn_history=turn_history,
            )
        finally:
            self.active.pop(job_id, None)

    async def cancel(self, job_id: str) -> bool:
        ids = self.active.get(job_id)
        if not ids:
            return False
        if not self.client.transport_available:
            return False
        await self.client.interrupt(*ids)
        return True

    async def rate_limits(self) -> dict[str, Any] | None:
        try:
            return await self.client.request("account/rateLimits/read", None, timeout=20)
        except Exception:
            return None


class MockCodexBackend:
    """Deterministic asynchronous backend used by lifecycle and recovery tests."""

    def __init__(self, scripts: dict[str, list[dict[str, Any]]] | None = None):
        self.scripts = scripts or {}
        self.cancelled: list[str] = []
        self.started = False
        self.calls: list[ResearchTask] = []
        self.rate_limit_payload: dict[str, Any] | None = None

    def set_economy_mode(self, enabled: bool) -> None:
        del enabled

    def supports_same_thread_continuation(self, role: str) -> bool:
        del role
        return False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def run_job(
        self,
        *,
        job_id: str,
        task: ResearchTask,
        prompt: str,
        output_schema: dict[str, Any],
        workspace: Path,
        writable_roots: list[Path],
        timeout: float,
        token_budget: int | None,
        candidate_sink: CandidateSink,
        skill_path: Path | None = None,
        turn_controller: TurnController | None = None,
    ) -> JobOutcome:
        del prompt, workspace, writable_roots, timeout, token_budget, skill_path, turn_controller
        usage = TokenUsage()
        thread_id: str | None = None
        turn_id: str | None = None
        raw_output = ""
        try:
            validate_output_schema_compatibility(
                output_schema, schema_path=f"{task.output_contract} ({task.role}, mock)",
            )
            self.calls.append(task)
            queue = self.scripts.setdefault(task.role, [])
            script = queue.pop(0) if queue else self._default_script(task)
            await asyncio.sleep(float(script.get("delay", 0)))
            if script.get("candidate"):
                await candidate_sink(CandidateEvent.from_dict(script["candidate"]))
                await asyncio.sleep(float(script.get("post_candidate_delay", 0)))
            if script.get("raise"):
                raise script["raise"]
            result = dict(script.get("result", {}))
            raw_output = json.dumps(result, ensure_ascii=False, sort_keys=True)
            validate(result, output_schema)
            usage = TokenUsage(
                total_tokens=int(script.get("tokens", 100)), input_tokens=60, output_tokens=40,
            )
            thread_id = f"mock-thread-{uuid4().hex[:8]}"
            turn_id = f"mock-turn-{uuid4().hex[:8]}"
            return JobOutcome(
                job_id=job_id,
                task_id=task.task_id, role=task.role, claim_id=task.target_claim,
                status=script.get("status", "completed"), result=result,
                thread_id=thread_id, turn_id=turn_id,
                model="mock-no-fast", reasoning_effort="mock", token_usage=usage,
                provider="mock", provider_profile=None,
                token_telemetry="synthetic",
                cost_usd=0.0, cost_telemetry="synthetic",
                artifact_paths=list(result.get("artifact_paths", [])),
            )
        except Exception as exc:
            evidence_usage = getattr(exc, "token_usage", None)
            if isinstance(evidence_usage, TokenUsage):
                usage = evidence_usage
            evidence_output = getattr(exc, "raw_output", None)
            if isinstance(evidence_output, str):
                raw_output = evidence_output
            evidence_turn_id = getattr(exc, "turn_id", None)
            if evidence_turn_id:
                turn_id = str(evidence_turn_id)
            evidence_telemetry = getattr(exc, "token_telemetry", None)
            token_telemetry = (
                evidence_telemetry if isinstance(evidence_telemetry, str) else "synthetic"
            )
            failure_kind, retryable, server_error = _classify_failure(exc)
            evidence_turn = getattr(exc, "turn", None)
            return JobOutcome(
                job_id=job_id,
                task_id=task.task_id, role=task.role, claim_id=task.target_claim,
                status="ERROR", result={}, thread_id=thread_id, turn_id=turn_id,
                model="mock-no-fast", reasoning_effort="mock", token_usage=usage,
                provider="mock", provider_profile=None,
                token_telemetry=token_telemetry,
                cost_usd=0.0, cost_telemetry="synthetic",
                error=str(redact_auth_material(str(exc))),
                failure_kind=failure_kind, retryable=retryable, server_error=server_error,
                terminal_event=(
                    dict(evidence_turn)
                    if isinstance(evidence_turn, dict) and evidence_turn
                    else None
                ),
                raw_output=raw_output or None,
            )

    async def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True

    async def rate_limits(self) -> dict[str, Any] | None:
        return self.rate_limit_payload

    def _default_script(self, task: ResearchTask) -> dict[str, Any]:
        if task.role == "director":
            result = {
                "assessment": "No further mock tasks.", "spawn": [],
                "audit_priorities": [],
                "route_updates": [{
                    "route_id": "mock-route", "action": "PAUSE",
                    "reason": "mock backend exhausted", "retry_condition": None,
                }],
                "short_rationale": "mock backend exhausted",
            }
        elif task.role in {"auditor", "evaluator_auditor"}:
            result = {
                "verdict": "PASS",
                "checks": [{"name": "mock independent reconstruction", "passed": True, "detail": "deterministic"}],
                "gaps": [], "notes": [],
                "verified_evidence_level": "E0_SPECULATIVE",
            }
        else:
            result = {
                "result_type": "NO_PROGRESS",
                "main_finding": "mock no progress", "status": "COMPLETED",
                "artifact_paths": [], "next_suggested_question": "replan",
                "evidence_level": "E0_SPECULATIVE",
            }
        return {"result": result, "tokens": 100}
