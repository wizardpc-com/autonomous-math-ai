from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .app_server import (
    AppServerClient, AppServerError, AppServerRequestError, AppServerTurnFailed,
    AppServerTurnTimeout, ModelRoutePolicyError, ServiceTierPolicyError,
    StructuredOutputProtocolError, attest_model_route, attest_no_service_tier,
    parse_structured_message, redact_auth_material,
)
from .config import HarnessConfig
from .models import CandidateEvent, JobOutcome, ResearchTask, TokenUsage, utc_now
from .schema import (
    OutputSchemaCompatibilityError, SchemaError, validate,
    validate_output_schema_compatibility,
)


CandidateSink = Callable[[CandidateEvent], Awaitable[None]]


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
    if isinstance(exc, ModelRoutePolicyError):
        return "model_route_policy", False, {
            "phase": exc.phase,
            "requested_model": exc.requested_model,
            "observed_model": exc.observed_model,
            "route_event": exc.route_event,
        }
    if isinstance(exc, AppServerTurnTimeout):
        return "transport_transient", True, _server_error_details(exc.server_error)
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
    ) -> JobOutcome: ...
    async def cancel(self, job_id: str) -> bool: ...
    async def rate_limits(self) -> dict[str, Any] | None: ...
    def set_economy_mode(self, enabled: bool) -> None: ...


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
            if token_budget:
                await self.client.set_goal(thread_id, task.exact_objective, token_budget)
            # start_turn returns only after completion; active cancellation becomes visible once
            # turn/start has returned its id through notifications, so timeout is the primary guard.
            completed, raw_output, usage, token_telemetry = await self.client.start_turn(
                thread_id=thread_id,
                prompt=prompt,
                cwd=workspace,
                model=model,
                effort=effort,
                output_schema=output_schema,
                writable_roots=writable_roots,
                timeout=timeout,
                skill_path=skill_path,
                on_started=lambda turn_id: self.active.__setitem__(job_id, (thread_id, turn_id)),
            )
            turn = completed.get("turn") or {}
            turn_id = turn.get("id")
            turn_tier = attest_no_service_tier(turn, "turn/completed")
            if turn_tier != "unobservable":
                observed_tier = turn_tier
            attest_model_route(turn, "turn/completed", model)
            if str(turn.get("status") or "").lower() != "completed":
                raise AppServerTurnFailed(
                    turn,
                    raw_output=raw_output,
                    token_usage=usage,
                    token_telemetry=token_telemetry,
                )
            parsed = parse_structured_message(raw_output)
            validate(parsed, output_schema)
            return JobOutcome(
                job_id=job_id, task_id=task.task_id, role=task.role, claim_id=task.target_claim,
                status=str(turn.get("status", "completed")), result=parsed,
                thread_id=thread_id, turn_id=turn_id, model=model, reasoning_effort=effort,
                provider=self.provider_name,
                provider_profile=route_config.get("profile"),
                requested_service_tier=None, observed_service_tier=observed_tier,
                token_usage=usage, token_telemetry=token_telemetry,
                cost_usd=None, cost_telemetry="unknown",
                artifact_paths=list(parsed.get("artifact_paths", [])),
            )
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
                status="ERROR", result={}, thread_id=thread_id, turn_id=turn_id,
                model=model, reasoning_effort=effort,
                provider=self.provider_name,
                provider_profile=route_config.get("profile"),
                requested_service_tier=None, observed_service_tier=observed_tier,
                token_usage=usage, token_telemetry=token_telemetry,
                cost_usd=None, cost_telemetry="unknown",
                error=str(redact_auth_material(str(exc))),
                failure_kind=failure_kind, retryable=retryable, server_error=server_error,
                terminal_event=terminal_event, raw_output=raw_output or None,
            )
        finally:
            self.active.pop(job_id, None)

    async def cancel(self, job_id: str) -> bool:
        ids = self.active.get(job_id)
        if not ids:
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
    ) -> JobOutcome:
        del prompt, workspace, writable_roots, timeout, token_budget, skill_path
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
