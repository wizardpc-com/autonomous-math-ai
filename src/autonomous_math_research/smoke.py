from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from .app_server import (
    AppServerClient, AppServerError, AppServerRequestError, AppServerTurnFailed,
    ModelRoutePolicyError, ServiceTierPolicyError, StructuredOutputProtocolError,
    attest_model_route, attest_no_service_tier, parse_structured_message,
)
from .audit_gate import AuditGate
from .capabilities import inspect_generated_schema
from .claim_graph import ClaimGraph
from .config import HarnessConfig
from .contracts import (
    AUDIT_RESULT_KEYS, DIRECTOR_PLAN_KEYS, WORKER_RESULT_KEYS,
    render_contract_keys,
)
from .models import (
    AuditResult, CandidateEvent, Claim, EvidenceLevel, MathStatus, TokenUsage,
    TrustStatus, utc_now,
)
from .policy import pin_policy_manifest, policy_view_for_role
from .reporting import write_report
from .representation import RepresentationContract
from .resources import schema_resource
from .schema import (
    OutputSchemaCompatibilityError, SchemaError, preflight_output_schema_files,
    validate,
)
from .storage import (
    CanonicalGuard, EventStore, ProjectLayout, atomic_write_json, file_digest,
)


SCHEMA_ROLES = ("director", "worker", "audit")
SCHEMA_ROLE_SMOKE_BUDGET = 32_000
FULL_LIFECYCLE_SMOKE_BUDGET = 320_000
SMOKE_BUDGET_SEMANTICS = "soft_dispatch_gate_inflight_turns_finish"
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


class SmokeRunFailed(RuntimeError):
    """A real smoke failed after its diagnostic artifacts were finalized."""

    def __init__(self, run_id: str, report_path: Path, cause: Exception):
        self.run_id = run_id
        self.report_path = report_path
        self.cause = cause
        super().__init__(f"real smoke {run_id} failed: {cause}")


class SmokeBudgetExhausted(RuntimeError):
    pass


def _redact_text(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result[-12_000:]


def _server_error(exc: Exception) -> dict[str, Any] | None:
    if isinstance(exc, (AppServerTurnFailed, AppServerRequestError)):
        return dict(exc.server_error)
    if isinstance(exc, ModelRoutePolicyError):
        return {
            "phase": exc.phase,
            "requested_model": exc.requested_model,
            "observed_model": exc.observed_model,
            "route_event": exc.route_event,
        }
    if isinstance(exc, OutputSchemaCompatibilityError):
        return {
            "issues": [
                {
                    "schema_file": issue.schema_path,
                    "json_path": issue.json_path,
                    "reason": issue.reason,
                }
                for issue in exc.issues
            ]
        }
    return None


def _failure_kind(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, SmokeBudgetExhausted):
        return "budget_exhausted", False
    if isinstance(exc, OutputSchemaCompatibilityError):
        return "invalid_output_schema", False
    if isinstance(exc, ServiceTierPolicyError):
        return "service_tier_policy", False
    if isinstance(exc, ModelRoutePolicyError):
        return "model_route_policy", False
    if isinstance(exc, (AppServerTurnFailed, AppServerRequestError)):
        raw = json.dumps(_server_error(exc), ensure_ascii=False).lower()
        if "invalid_json_schema" in raw:
            return "invalid_output_schema", False
        if "rate_limit" in raw or "rate limit" in raw or '"status": 429' in raw:
            return "rate_limit", True
        retryable = any(item in raw for item in (
            '"status": 500', '"status": 502', '"status": 503', '"status": 504',
            "server_error", "temporarily_unavailable", "service_unavailable",
        ))
        return ("transport_transient" if retryable else "turn_failed"), retryable
    if isinstance(exc, StructuredOutputProtocolError):
        return "model_output_protocol", True
    if isinstance(exc, (AppServerError, asyncio.TimeoutError, TimeoutError)):
        return "transport_transient", True
    if isinstance(exc, SchemaError):
        return "model_output_validation", True
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "role_semantic_validation", True
    return "smoke_internal", False


def _toy_graph(path: Path) -> ClaimGraph:
    graph = ClaimGraph({
        "TOY-SUM-ODD": Claim(
            claim_id="TOY-SUM-ODD",
            statement=(
                "For every integer n >= 0, the sum of the first n positive odd "
                "integers equals n^2."
            ),
            assumptions=["n is an integer", "n >= 0"],
            math_status=MathStatus.OPEN,
            trust_status=TrustStatus.UNTRUSTED_CANDIDATE,
            dependencies=[], downstream_dependents=[], evidence_paths=[],
            known_counterexamples=[], current_gaps=["toy smoke proof and audit"],
            active_tasks=[], last_meaningful_progress=None,
            priority={"score": 1.0}, source_status="SMOKE_ONLY",
        )
    }, path)
    graph.save()
    return graph


def _toy_candidate(
    graph: ClaimGraph, prover: dict[str, Any], prover_record: dict[str, Any],
) -> CandidateEvent:
    claim = graph.claims["TOY-SUM-ODD"]
    return CandidateEvent.from_dict({
        "event_id": f"smoke-event-{uuid4().hex[:10]}",
        "producer_thread_id": prover_record["thread_id"],
        "producer_task_id": "smoke-prover", "claim_id": claim.claim_id,
        "type": "THEOREM_CANDIDATE", "impact": "HIGH",
        "concise_summary": prover["main_finding"],
        "exact_statement": claim.statement,
        "artifact_paths": [], "reproduction_commands": [],
        "dependency_impact": ["smoke lifecycle only"],
        "parent_claim_id": None,
        "representation": RepresentationContract.legacy().to_dict(),
        "bridge_representation_ids": [],
        # Existing-claim candidates must preserve identity-defining fields.
        "assumptions": list(claim.assumptions),
        "dependencies": list(claim.dependencies),
        "proposed_evidence_level": "E0_SPECULATIVE", "timestamp": utc_now(),
    })


def _schema_role_prompt(role: str) -> str:
    if role == "director":
        return (
            "Protocol-only schema acceptance check. Do not perform research. Return a director "
            f"plan with exactly these top-level keys: {render_contract_keys(DIRECTOR_PLAN_KEYS)}. "
            "Use assessment and short_rationale strings and empty spawn, audit_priorities, "
            "and route_updates arrays."
        )
    if role == "worker":
        return (
            "Protocol-only schema acceptance check. Do not perform research. Return a worker "
            f"result with exactly these top-level keys: {render_contract_keys(WORKER_RESULT_KEYS)}. "
            "Use result_type NO_PROGRESS, status COMPLETED, empty artifact_paths, "
            "evidence_level E0_SPECULATIVE, and short string "
            "values for the other fields."
        )
    return (
        "Protocol-only schema acceptance check. Do not assess a real mathematical claim. Return "
        f"exactly these top-level keys: {render_contract_keys(AUDIT_RESULT_KEYS)}. Return "
        "verdict UNRESOLVED, exactly one check object with passed false, one gap, no notes, "
        "and verified_evidence_level E0_SPECULATIVE."
    )


async def run_real_smoke(
    config: HarnessConfig,
    token_budget: int = FULL_LIFECYCLE_SMOKE_BUDGET,
    *,
    schema_role: str | None = None,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
) -> Path:
    """Run an isolated no-fast/no-priority App Server smoke and finalize evidence."""
    if token_budget <= 0:
        raise ValueError("smoke token budget must be positive")
    if schema_role not in {None, *SCHEMA_ROLES}:
        raise ValueError(f"schema_role must be one of {SCHEMA_ROLES}")

    layout = ProjectLayout(config.project_root)
    layout.ensure()
    run_id = datetime.now(timezone.utc).strftime("smoke-%Y%m%dT%H%M%S.%fZ")
    run_dir = layout.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    store = EventStore(run_dir / "EVENTS.jsonl", run_id)
    policy_path = run_dir / "policy" / "MANIFEST.json"
    guard = CanonicalGuard(config.project_root, config.protected_paths)
    before = guard.snapshot()
    atomic_write_json(run_dir / "canonical_guard.before.json", before)
    graph = _toy_graph(run_dir / "state" / "claim_graph.json")
    jobs: list[dict[str, Any]] = []
    policy_manifest: dict[str, Any] | None = None
    policy_status: dict[str, Any] | None = None
    capability: dict[str, Any] = {}
    live: dict[str, Any] | None = None
    client: AppServerClient | None = None
    failure: Exception | None = None
    model, effort = config.model_for("smoke")
    stopped_reason = (
        "minimal real App Server smoke completed"
        if schema_role is None
        else f"real App Server {schema_role} output schema accepted"
    )
    mode_label = "full-lifecycle" if schema_role is None else f"schema-role:{schema_role}"
    store.append("RUN_STARTED", {
        "mode": "smoke", "execution_mode": "smoke", "smoke_scope": mode_label,
        "global_token_budget": token_budget, "requested_service_tier": None,
        "budget_semantics": SMOKE_BUDGET_SEMANTICS, "budget_hard_cap": False,
    })
    store.append("SMOKE_STARTED", {
        "model": model, "effort": effort, "token_budget": token_budget,
        "schema_role": schema_role, "budget_semantics": SMOKE_BUDGET_SEMANTICS,
        "budget_hard_cap": False,
    })

    async def trace(message: dict[str, Any]) -> None:
        method = message.get("method")
        if method in {
            "thread/started", "turn/started", "turn/completed",
            "thread/tokenUsage/updated", "thread/goal/updated",
        }:
            store.append("APP_SERVER_NOTIFICATION", {
                "method": method, "params": message.get("params", {}),
            })

    async def turn(
        role: str,
        prompt: str,
        schema: dict[str, Any],
        budget: int | None,
        *,
        include_policy: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        assert client is not None
        workspace = run_dir / "jobs" / f"{role}-{uuid4().hex[:8]}"
        workspace.mkdir(parents=True, exist_ok=True)
        started_at = utc_now()
        started_clock = time.monotonic()
        job_id = f"smoke-{role}-{uuid4().hex[:8]}"
        thread_id: str | None = None
        turn_id: str | None = None
        observed_tier = "unobservable"
        usage = TokenUsage()
        token_telemetry = "unknown"
        store.append("JOB_STARTED", {
            "job_id": job_id, "role": role, "schema_role": schema_role,
            "requested_service_tier": None,
        })
        try:
            started = await client.start_thread(
                model=model, cwd=workspace, sandbox="workspace-write",
                developer_instructions=(
                    "This is a bounded no-fast/no-priority App Server protocol smoke. Do not spawn "
                    "subagents, access the network, or modify canonical project files. "
                    "Return only the requested JSON."
                ),
            )
            thread_data = started["thread"]
            thread_id = str(thread_data["id"])
            observed_tier = attest_no_service_tier(thread_data, "thread/start")
            attest_model_route(thread_data, "thread/start", model)
            if budget is not None and config.per_thread_limit_action == "interrupt":
                await client.set_goal(thread_id, f"Complete the bounded {role} smoke task", budget)
            final_prompt = prompt
            if include_policy:
                assert policy_manifest is not None
                role_policy = policy_view_for_role(policy_manifest, policy_path, role)
                final_prompt = (
                    f"{prompt}\n\nPINNED RESEARCH POLICY: {role_policy}. "
                    "Read the pinned skill and listed references; the controller/filesystem "
                    "is the stable core."
                )
            completed, text, usage, token_telemetry = await client.start_turn(
                thread_id=thread_id, prompt=final_prompt, cwd=workspace, model=model,
                effort=effort, output_schema=schema, writable_roots=[workspace],
                timeout=180,
            )
            turn_data = completed.get("turn") or {}
            turn_id = str(turn_data.get("id") or "") or None
            store.append("TURN_RETURNED", {
                "role": role, "thread_id": thread_id, "turn_id": turn_id,
            })
            turn_tier = attest_no_service_tier(turn_data, "turn/completed")
            if turn_tier != "unobservable":
                observed_tier = turn_tier
            attest_model_route(turn_data, "turn/completed", model)
            if str(turn_data.get("status") or "").lower() != "completed":
                raise AppServerTurnFailed(
                    turn_data,
                    raw_output=text,
                    token_usage=usage,
                    token_telemetry=token_telemetry,
                )
            parsed = parse_structured_message(text)
            validate(parsed, schema)
            record = {
                "job_id": job_id, "role": role, "thread_id": thread_id,
                "turn_id": turn_id, "model": model, "reasoning_effort": effort,
                "status": "completed", "requested_service_tier": None,
                "observed_service_tier": observed_tier,
                "token_usage": usage.to_dict(), "token_telemetry": token_telemetry,
                "result": parsed, "useful": schema_role is None,
                "cwd": str(workspace), "start_time": started_at,
                "end_time": utc_now(),
                "workspace_metadata": {"kind": "smoke_isolated_output", "path": str(workspace)},
                "elapsed_seconds": max(0.0, time.monotonic() - started_clock),
                "exit_reason": "completed", "artifact_paths": [],
            }
            jobs.append(record)
            store.append("JOB_COMPLETED", record)
            return parsed, record
        except Exception as exc:
            kind, retryable = _failure_kind(exc)
            if isinstance(exc, ServiceTierPolicyError):
                observed_tier = exc.observed_service_tier
            evidence_usage = getattr(exc, "token_usage", None)
            if isinstance(evidence_usage, TokenUsage):
                usage = evidence_usage
            evidence_telemetry = getattr(exc, "token_telemetry", None)
            if isinstance(evidence_telemetry, str):
                token_telemetry = evidence_telemetry
            raw_output = getattr(exc, "raw_output", None)
            terminal_event = getattr(exc, "turn", None)
            record = {
                "job_id": job_id, "role": role, "thread_id": thread_id,
                "turn_id": turn_id, "model": model, "reasoning_effort": effort,
                "status": "ERROR", "requested_service_tier": None,
                "observed_service_tier": observed_tier,
                "token_usage": usage.to_dict(), "token_telemetry": token_telemetry,
                "result": {}, "useful": False, "error": _redact_text(str(exc)),
                "failure_kind": kind, "retryable": retryable,
                "server_error": _server_error(exc), "cwd": str(workspace),
                "terminal_event": (
                    dict(terminal_event)
                    if isinstance(terminal_event, dict) else None
                ),
                "raw_output": (
                    _redact_text(raw_output)
                    if isinstance(raw_output, str) and raw_output else None
                ),
                "start_time": started_at, "end_time": utc_now(),
                "elapsed_seconds": max(0.0, time.monotonic() - started_clock),
                "exit_reason": kind, "artifact_paths": [],
            }
            jobs.append(record)
            store.append("JOB_COMPLETED", record)
            raise

    try:
        policy_manifest, policy_status = pin_policy_manifest(config, policy_path)
        store.append("RUN_POLICY_PINNED", {
            "manifest": str(policy_path),
            "manifest_sha256": policy_manifest["manifest_sha256"],
            "stable_core": policy_manifest["stable_core"],
        })
        snapshot_root = run_dir / "config" / "output_schemas"
        snapshot_paths: list[Path] = []
        schema_manifest: dict[str, Any] = {}
        for name in (
            "director_plan.schema.json", "worker_result.schema.json",
            "audit_result.schema.json",
        ):
            with schema_resource(name) as source:
                loaded = preflight_output_schema_files([source])
                target = snapshot_root / name
                atomic_write_json(target, loaded[name])
                snapshot_paths.append(target)
                schema_manifest[name] = {
                    "source": f"package://autonomous_math_research/resources/schemas/{name}",
                    "snapshot": f"epoch://{run_id}/config/output_schemas/{name}",
                    "source_sha256": file_digest(source),
                    "snapshot_sha256": file_digest(target),
                    "sha256": file_digest(target),
                }
        # Validate the immutable copies that will actually be sent on the wire.
        snapshotted = preflight_output_schema_files(snapshot_paths)
        schemas = {
            "director": snapshotted["director_plan.schema.json"],
            "worker": snapshotted["worker_result.schema.json"],
            "audit": snapshotted["audit_result.schema.json"],
        }
        atomic_write_json(run_dir / "SMOKE_MANIFEST.json", {
            "run_id": run_id, "execution_mode": "smoke", "scope": mode_label,
            "requested_service_tier": None, "model": model, "effort": effort,
            "token_budget": token_budget, "dispatch_token_budget": token_budget,
            "budget_semantics": SMOKE_BUDGET_SEMANTICS, "budget_hard_cap": False,
            "schemas": schema_manifest,
            "policy_manifest_sha256": policy_manifest["manifest_sha256"],
        })
        store.append("SCHEMA_PREFLIGHT_PASSED", {
            "schemas": schema_manifest, "scope": mode_label,
        })
        capability = inspect_generated_schema(work_root=run_dir / "schema-probe")
        client = client_factory(notification_handler=trace)
        await client.start()
        live = await client.probe_capabilities(config.project_root)
        capability["live_probe"] = live
        atomic_write_json(run_dir / "app_server_capabilities.json", capability)

        if schema_role is not None:
            _, record = await turn(
                "auditor" if schema_role == "audit" else schema_role,
                _schema_role_prompt(schema_role), schemas[schema_role], None,
                include_policy=False,
            )
            store.append("SCHEMA_ROLE_ACCEPTED", {
                "schema_role": schema_role, "thread_id": record["thread_id"],
                "turn_id": record["turn_id"], "sha256": schema_manifest[
                    "audit_result.schema.json" if schema_role == "audit"
                    else f"{schema_role}_result.schema.json" if schema_role == "worker"
                    else "director_plan.schema.json"
                ]["sha256"],
            })
        else:
            def require_dispatch_budget(next_stage: str) -> None:
                unknown = [job["role"] for job in jobs if job["token_telemetry"] == "unknown"]
                if unknown:
                    raise SmokeBudgetExhausted(
                        f"cannot dispatch {next_stage}: token telemetry unknown for {unknown}"
                    )
                observed = sum(
                    int((job.get("token_usage") or {}).get("total_tokens", 0))
                    for job in jobs
                )
                if observed >= token_budget:
                    raise SmokeBudgetExhausted(
                        f"cannot dispatch {next_stage}: observed {observed} tokens reached "
                        f"smoke budget {token_budget}"
                    )

            per_turn = max(800, token_budget // 4)
            director, _ = await turn(
                "director",
                f"""You are a fresh toy Research Director. You cannot judge truth. Return JSON only, with exactly these top-level keys: {render_contract_keys(DIRECTOR_PLAN_KEYS)}. Use assessment and short_rationale strings, empty audit_priorities and route_updates, and exactly two spawn objects. Each spawn object must have exactly the schema fields, including metadata with only allow_derived_claims=false and a complete LEGACY_UNSPECIFIED representation with exceptional_factors empty. Create smoke-prover with role prover and smoke-falsifier with role falsifier for TOY-SUM-ODD. Use HIGH information gain and mathematical impact, LOW cost, no dependencies or required files, one bounded stop condition, modifies_code false, and priority between 0 and 1. Set route_family main for the prover and independent for the falsifier. The claim is: for every integer n >= 0, 1+3+...+(2n-1)=n^2. Spawn no other tasks.""",
                schemas["director"], per_turn,
            )
            store.append("DIRECTOR_PLAN_ACCEPTED", {
                "short_rationale": director["short_rationale"],
                "accepted_tasks": len(director["spawn"]),
            })
            require_dispatch_budget("research wave")
            worker_shape = (
                "Return JSON only with exactly these keys: "
                f"{render_contract_keys(WORKER_RESULT_KEYS)}."
            )
            prover_future = asyncio.create_task(turn(
                "prover",
                f"Act as the bounded Prover. Give a rigorous induction or telescoping proof of: for every integer n>=0, 1+3+...+(2n-1)=n^2. {worker_shape} Use result_type PROOF only if complete; evidence_level E0_SPECULATIVE because this is an informal proof; artifact_paths must be empty; claim identity, impact, and trust status are controller-owned and forbidden.",
                schemas["worker"], per_turn,
            ))
            falsifier_future = asyncio.create_task(turn(
                "falsifier",
                f"Act as an adversarial Falsifier. Check n=0 through n=20 exactly and inspect the n=0 convention and quantifiers for: 1+3+...+(2n-1)=n^2. {worker_shape} Do not call a finite no-counterexample search a proof; use result_type NO_PROGRESS unless a genuine counterexample exists; evidence_level E0_SPECULATIVE because this smoke writes no durable exact artifact; artifact_paths must be empty; do not repeat controller-owned claim identity or impact.",
                schemas["worker"], per_turn,
            ))
            research_results = await asyncio.gather(
                prover_future, falsifier_future, return_exceptions=True,
            )
            first_error = next(
                (item for item in research_results if isinstance(item, BaseException)),
                None,
            )
            if first_error is not None:
                raise first_error
            (prover, prover_record), _ = research_results  # type: ignore[misc]
            store.append("CONCURRENT_RESEARCH_CONFIRMED", {
                "roles": ["prover", "falsifier"],
                "prover_thread": prover_record["thread_id"],
            })
            require_dispatch_budget("auditor")
            if prover["result_type"] != "PROOF":
                raise RuntimeError("smoke prover did not produce a proof candidate")
            event = _toy_candidate(graph, prover, prover_record)
            atomic_write_json(
                run_dir / "candidates" / f"{event.fingerprint}.json", event.to_dict(),
            )
            graph.mark_candidate(event)
            graph.save()
            store.append("CANDIDATE_PROCESSED", {
                "fingerprint": event.fingerprint, "claim_id": event.claim_id,
                "impact": event.impact,
            })
            audit, audit_record = await turn(
                "auditor",
                f"You are a fresh independent proof Auditor. You do not have the producer transcript. Audit this toy candidate from the exact statement only: {event.exact_statement} Reconstruct the proof independently; check n=0, quantifiers, induction base and step. Return JSON only with exactly these keys: {render_contract_keys(AUDIT_RESULT_KEYS)}. Use verdict PASS only if complete, verified_evidence_level E0_SPECULATIVE, checks as an array of objects, gaps as blocking strings, and notes as non-blocking strings. Candidate identity and audit metadata are controller-owned and must not be repeated.",
                schemas["audit"], per_turn,
            )
            gate = AuditGate(
                high_threshold=config.raw["audit"].get("immediate_threshold", "HIGH"),
                critical_double_audit=bool(config.raw["audit"].get("critical_double_audit", True)),
            )
            state = gate.register(event)
            audit_result = AuditResult.from_wire_v2(
                audit,
                audit_id=f"smoke-audit-{uuid4().hex[:10]}",
                candidate_fingerprint=event.fingerprint,
                auditor_thread_id=audit_record["thread_id"],
                audit_kind="proof",
                statement_checked=event.exact_statement,
                report_path=None,
            )
            candidate_trust = gate.record(audit_result)
            store.append("AUDIT_RECORDED", {
                **audit_result.to_dict(), "trust_status": candidate_trust,
            })
            if candidate_trust == TrustStatus.AUDITED_NIGHTLY:
                graph.apply_audit_pass(
                    event, gate.pass_count(event.fingerprint), state.required,
                    gate.verified_evidence_level(event.fingerprint),
                )
                store.append("TRUST_STATE_CHANGED", {
                    "claim_id": event.claim_id,
                    "trust_status": "AUDITED_NIGHTLY", "scope": "SMOKE_ONLY",
                })
            elif audit_result.verdict == "REJECT":
                store.append("CANDIDATE_REJECTED", {
                    "claim_id": event.claim_id, "fingerprint": event.fingerprint,
                    "reason": "; ".join(audit_result.gaps) or audit_result.verdict,
                    "scope": "SMOKE_ONLY",
                })
            else:
                store.append("CANDIDATE_AUDIT_UNRESOLVED", {
                    "claim_id": event.claim_id, "fingerprint": event.fingerprint,
                    "gaps": audit_result.gaps, "notes": audit_result.notes,
                    "scope": "SMOKE_ONLY",
                })
            graph.save()
    except Exception as exc:
        failure = exc
        kind, retryable = _failure_kind(exc)
        stopped_reason = f"real smoke failed: {kind}"
        payload = {
            "failure_kind": kind, "retryable": retryable,
            "error": _redact_text(str(exc)), "server_error": _server_error(exc),
            "schema_role": schema_role,
        }
        if isinstance(exc, OutputSchemaCompatibilityError):
            store.append("SCHEMA_PREFLIGHT_FAILED", payload)
        store.append("SMOKE_FAILED", payload)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as close_exc:
                store.append("APP_SERVER_CLOSE_FAILED", {
                    "error": _redact_text(str(close_exc)),
                })
            stderr_lines = getattr(client, "stderr_lines", [])
            if stderr_lines:
                atomic_write_json(run_dir / "app_server.stderr.json", {
                    "redacted": True,
                    "lines": [_redact_text(str(line)) for line in stderr_lines[-200:]],
                })

    changed = guard.verify()
    atomic_write_json(run_dir / "canonical_guard.after.json", {"changed": changed})
    observed_tokens = sum(
        int((job.get("token_usage") or {}).get("total_tokens", 0))
        for job in jobs
    )
    unknown_turns = sum(
        1 for job in jobs if job.get("token_telemetry") == "unknown"
    )
    observed_overshoot = max(0, observed_tokens - token_budget)
    budget_accounting = {
        "dispatch_token_budget": token_budget,
        "observed_tokens": observed_tokens,
        "unknown_turns": unknown_turns,
        "observed_tokens_are_lower_bound": unknown_turns > 0,
        "observed_overshoot_tokens": observed_overshoot,
        "budget_semantics": SMOKE_BUDGET_SEMANTICS,
        "budget_hard_cap": False,
    }
    store.append("SMOKE_BUDGET_ACCOUNTED", budget_accounting)
    if changed and failure is None:
        failure = RuntimeError(f"canonical project files changed during smoke: {changed}")
        stopped_reason = "real smoke failed: canonical_guard"
        store.append("SMOKE_FAILED", {
            "failure_kind": "canonical_guard", "retryable": False,
            "error": str(failure), "schema_role": schema_role,
        })
    if failure is None:
        store.append("SMOKE_COMPLETED", {
            "canonical_changed": changed, "schema_role": schema_role,
        })
    store.append("RUN_STOPPED", {
        "reason": stopped_reason,
        "outcome": "failed real smoke" if failure else "completed real smoke",
        "internal_failure": failure is not None, "mode": "smoke",
        "execution_mode": "smoke",
    })
    report_path = layout.nightly_root / run_id / "NIGHTLY_REPORT.md"
    write_report(
        report_path, run_id=run_id, graph=graph, events=store.replay(), jobs=jobs,
        stopped_reason=stopped_reason, capability_snapshot=live or capability,
        policy_manifest=policy_manifest, policy_status=policy_status,
        promotion_allowed=False, execution_mode="smoke",
        run_outcome="failed real smoke" if failure else "completed real smoke",
        internal_failure=failure is not None,
    )
    atomic_write_json(report_path.parent / "SMOKE_SUMMARY.json", {
        "run_id": run_id, "report": str(report_path), "jobs": jobs,
        "capabilities": str(run_dir / "app_server_capabilities.json"),
        "policy_manifest": str(policy_path),
        "policy_manifest_sha256": (
            policy_manifest.get("manifest_sha256") if policy_manifest else None
        ),
        "canonical_changed": changed, "scope": "TOY_SMOKE_ONLY_DO_NOT_PROMOTE",
        "schema_role": schema_role,
        "outcome": "failed real smoke" if failure else "completed real smoke",
        "internal_failure": failure is not None,
        "stopped_reason": stopped_reason,
        "error": _redact_text(str(failure)) if failure else None,
        "budget_accounting": budget_accounting,
    })
    if failure is not None:
        raise SmokeRunFailed(run_id, report_path, failure) from failure
    return report_path
