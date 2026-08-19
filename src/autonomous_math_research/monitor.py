from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, BinaryIO, Callable, TextIO
import unicodedata

from .contracts import job_lifecycle_metrics, mechanical_lifecycle_metrics
from .storage import ProjectLayout


def resolve_run(project: Path, selector: str = "latest") -> Path:
    """Resolve a run without mutating it.

    ``latest`` means the most recently started run, whether it is active or
    complete.  An explicit run id is restricted to the project's runs directory
    so this read-only helper cannot be used as an arbitrary file reader.
    """
    root = ProjectLayout(project.resolve()).runs_root.resolve()
    if not root.is_dir():
        raise ValueError(f"runs directory does not exist: {root}")
    if selector != "latest":
        run_dir = (root / selector).resolve()
        if not run_dir.is_relative_to(root) or not run_dir.is_dir():
            raise ValueError(f"run does not exist: {selector}")
        if not (run_dir / "EVENTS.jsonl").is_file():
            raise ValueError(f"run has no EVENTS.jsonl: {selector}")
        return run_dir

    candidates = [
        path for path in root.iterdir()
        if path.is_dir() and (path / "EVENTS.jsonl").is_file()
    ]
    if not candidates:
        raise ValueError("no run with EVENTS.jsonl found")
    # "latest" means the most recently *started* run, whether or not it has
    # already stopped.  Preferring every incomplete directory can select an
    # abandoned old smoke run forever.
    candidates.sort(key=_run_order_key)
    return candidates[-1]


def _run_order_key(run_dir: Path) -> tuple[str, int, str]:
    records = load_events(run_dir / "EVENTS.jsonl")
    started = next((event for event in records if event.get("kind") == "RUN_STARTED"), None)
    timestamp = str((started or {}).get("timestamp") or "")
    return timestamp, (run_dir / "EVENTS.jsonl").stat().st_mtime_ns, run_dir.name


def load_events(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    data = path.read_bytes()
    parts = data.split(b"\n")
    if parts and parts[-1]:
        # EventStore always terminates records with a newline.  A non-terminated
        # tail can only be an append observed between write completion points;
        # ignore it until the next read rather than treating a live run as corrupt.
        parts.pop()
    for number, raw in enumerate(parts, 1):
        if not raw.strip():
            continue
        try:
            records.append(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}: {exc}") from exc
    return records


def build_status(
    run_dir: Path,
    events: list[dict[str, Any]] | None = None,
    *,
    include_live: bool = True,
) -> dict[str, Any]:
    records = events if events is not None else load_events(run_dir / "EVENTS.jsonl")
    lifecycle = job_lifecycle_metrics(records)
    mechanical_lifecycle = mechanical_lifecycle_metrics(records)
    started: dict[str, dict[str, Any]] = {}
    terminal: set[str] = set()
    thread_to_job: dict[str, str] = {}
    token_by_thread: dict[str, dict[str, Any]] = {}
    completed_usage_by_job: dict[str, dict[str, Any]] = {}
    completed_telemetry_by_job: dict[str, str] = {}
    mechanical_usage: list[dict[str, Any]] = []
    mechanical_telemetry: list[str] = []
    stop_reason: str | None = None
    execution_mode: str | None = None
    run_outcome: str | None = None
    internal_failure: bool | None = None
    campaign_id: str | None = None
    epoch_id: str | None = None
    campaign_status: str | None = None
    last_rates: dict[str, Any] | None = None
    problems: list[dict[str, Any]] = []
    for event in records:
        kind = str(event.get("kind", ""))
        payload = event.get("payload") or {}
        if kind == "RUN_STARTED":
            execution_mode = str(
                payload.get("execution_mode")
                or payload.get("mode")
                or ("dry-run" if payload.get("dry_run") is True else "")
            ) or execution_mode
            campaign_id = str(payload.get("campaign_id") or "") or campaign_id
            epoch_id = str(payload.get("epoch_id") or "") or epoch_id
        if kind == "JOB_STARTED" and payload.get("job_id"):
            started[str(payload["job_id"])] = dict(payload)
        elif kind in {"JOB_COMPLETED", "JOB_CANCELLED"} and payload.get("job_id"):
            terminal.add(str(payload["job_id"]))
            completed_usage_by_job[str(payload["job_id"])] = {
                "thread_id": payload.get("thread_id"),
                **dict(payload.get("token_usage") or {}),
            }
            completed_telemetry_by_job[str(payload["job_id"])] = str(
                payload.get("token_telemetry") or "unknown"
            )
        elif kind == "JOB_BOUND" and payload.get("thread_id") and payload.get("job_id"):
            thread_to_job[str(payload["thread_id"])] = str(payload["job_id"])
        elif kind in {"MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED"}:
            if not payload.get("cache_reused"):
                mechanical_usage.append(dict(payload.get("token_usage") or {}))
                mechanical_telemetry.append(str(payload.get("token_telemetry") or "unknown"))
        elif kind == "RUN_STOPPED":
            stop_reason = str(payload.get("reason") or "stopped")
            execution_mode = str(
                payload.get("execution_mode") or payload.get("mode") or execution_mode or ""
            ) or None
            run_outcome = str(
                payload.get("run_outcome") or payload.get("outcome") or ""
            ) or None
            if "internal_failure" in payload:
                internal_failure = bool(payload["internal_failure"])
            campaign_id = str(payload.get("campaign_id") or "") or campaign_id
            epoch_id = str(payload.get("epoch_id") or "") or epoch_id
            campaign_status = str(payload.get("campaign_status") or "") or campaign_status
        if kind in {
            "DIRECTOR_REJECTED", "TASK_REJECTED", "AUDIT_ERROR",
            "CANDIDATE_QUARANTINED", "CANONICAL_GUARD_FAILED",
            "SERVICE_TIER_POLICY_VIOLATION", "CLAIM_CONFLICT_DETECTED",
            "SCHEMA_PREFLIGHT_FAILED", "BOOTSTRAP_FAILED",
            "RESEARCH_JOB_FAILED", "TASK_RETAINED_AFTER_ERROR",
            "AUDIT_RETAINED_AFTER_ERROR", "CONTROLLER_INVARIANT_FAILED",
            "DIRECTOR_AUDIT_REQUEST_REJECTED", "CANDIDATE_REJECTED",
            "MECHANICAL_SUBTASK_FAILED", "MECHANICAL_LIFECYCLE_INVARIANT_FAILED",
            "UNAUTHORIZED_DELEGATION_ATTEMPT", "MECHANICAL_BROKER_INTEGRITY_FAILURE",
            "MECHANICAL_ROUTE_CACHE_PERSIST_FAILED",
        }:
            problems.append({
                "sequence": event.get("sequence"), "kind": kind,
                "task_id": payload.get("task_id"),
                "claim_id": payload.get("claim_id"),
                "detail": payload.get("error") or payload.get("reason") or payload.get("changed"),
            })
        elif kind == "JOB_COMPLETED" and str(payload.get("status", "")).upper() in {"ERROR", "TOOL_ERROR", "FAILED"}:
            problems.append({
                "sequence": event.get("sequence"), "kind": "JOB_ERROR",
                "task_id": payload.get("task_id"), "claim_id": payload.get("claim_id"),
                "detail": payload.get("error") or payload.get("exit_reason"),
            })
        elif kind == "APP_SERVER_NOTIFICATION":
            method = payload.get("method")
            params = payload.get("params") or {}
            if method == "thread/tokenUsage/updated" and params.get("threadId"):
                total = (params.get("tokenUsage") or {}).get("total") or {}
                token_by_thread[str(params["threadId"])] = dict(total)
            elif method == "account/rateLimits/updated":
                last_rates = params.get("rateLimits") or params

    active_jobs = []
    for job_id, payload in started.items():
        if job_id in terminal:
            continue
        active_jobs.append({
            "job_id": job_id,
            "task_id": payload.get("task_id"),
            "role": payload.get("role"),
            "claim_id": payload.get("claim_id"),
            "start_time": payload.get("start_time"),
            "thread_id": next(
                (thread for thread, bound_job in thread_to_job.items() if bound_job == job_id), None
            ),
        })
    active_jobs.sort(key=lambda item: str(item.get("start_time") or ""))
    token_keys = {
        "inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens", "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens", "totalTokens": "total_tokens",
    }
    token_totals = {
        camel: sum(int(value.get(camel, 0) or 0) for value in token_by_thread.values())
        for camel in token_keys
    }
    # Completed JobOutcome telemetry is the source for mock backends and a
    # fallback for App Server versions that omit token notifications.  Avoid
    # double-counting a real job when thread telemetry is already available.
    for job_id, usage in completed_usage_by_job.items():
        thread_id = str(usage.get("thread_id") or "")
        if not thread_id:
            thread_id = next(
                (thread for thread, bound_job in thread_to_job.items() if bound_job == job_id), ""
            )
        if thread_id and thread_id in token_by_thread:
            continue
        for camel, snake in token_keys.items():
            token_totals[camel] += int(usage.get(snake, 0) or 0)
    for usage in mechanical_usage:
        for camel, snake in token_keys.items():
            token_totals[camel] += int(usage.get(snake, 0) or 0)
    last = records[-1] if records else {}
    live_path = run_dir / "LIVE_EVENTS.jsonl"
    live_records = (
        load_events(live_path) if include_live and live_path.is_file() else []
    )
    last_live = live_records[-1] if live_records else {}
    return {
        "run_id": run_dir.name,
        "campaign_id": campaign_id or run_dir.name,
        "epoch_id": epoch_id or run_dir.name,
        "campaign_status": campaign_status,
        "state": "STOPPED" if stop_reason is not None else "RUNNING",
        "stop_reason": stop_reason,
        "execution_mode": execution_mode,
        "run_outcome": run_outcome,
        "internal_failure": internal_failure,
        "event_count": len(records),
        "last_sequence": last.get("sequence"),
        "last_event": last.get("kind"),
        "last_timestamp": last.get("timestamp"),
        "jobs_started": lifecycle.jobs_started,
        "jobs_completed": lifecycle.jobs_completed,
        "jobs_cancelled": lifecycle.jobs_cancelled,
        "jobs_terminal": lifecycle.jobs_terminal,
        "mechanical_subtasks": mechanical_lifecycle.to_dict(),
        "job_lifecycle_anomalies": {
            "active_job_ids": list(lifecycle.active_job_ids),
            "duplicate_terminal_job_ids": list(lifecycle.duplicate_terminal_job_ids),
            "orphan_terminal_job_ids": list(lifecycle.orphan_terminal_job_ids),
        },
        "active_jobs": active_jobs,
        "token_usage": token_totals,
        "token_telemetry": {
            "observed": sum(value == "observed" for value in completed_telemetry_by_job.values()),
            "synthetic": sum(value == "synthetic" for value in completed_telemetry_by_job.values()),
            "unknown": sum(value == "unknown" for value in completed_telemetry_by_job.values()),
            "mechanical_observed": sum(value in {"observed", "synthetic"} for value in mechanical_telemetry),
            "mechanical_partial": sum(value == "partial" for value in mechanical_telemetry),
            "mechanical_unknown": sum(value in {"unknown", "partial"} for value in mechanical_telemetry),
        },
        "token_usage_is_lower_bound": any(
            value == "unknown" for value in completed_telemetry_by_job.values()
        ) or any(value in {"unknown", "partial"} for value in mechanical_telemetry),
        "rate_limits": last_rates,
        "problems": problems[-10:],
        "live_event_count": len(live_records),
        "last_live_event": last_live.get("kind"),
        "last_live_timestamp": last_live.get("timestamp"),
    }


def _local_clock(raw: Any) -> str:
    if not raw:
        return "--:--:--"
    try:
        value = str(raw)
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        return parsed.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return str(raw)[:8]


def _compact(value: Any, limit: int = 100) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, dict)):
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        rendered = str(value)
    rendered = " ".join(rendered.split())
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def format_event(event: dict[str, Any]) -> str:
    """Format one bounded lifecycle line; never dump full model output."""
    kind = str(event.get("kind") or "UNKNOWN")
    payload = event.get("payload") or {}
    sequence = event.get("sequence", "?")
    fields: list[tuple[str, Any]] = []
    display_kind = kind

    if kind in {"JOB_STARTED", "JOB_COMPLETED", "JOB_CANCELLED"}:
        fields = [
            ("role", payload.get("role")), ("task", payload.get("task_id")),
            ("claim", payload.get("claim_id")), ("status", payload.get("status")),
        ]
        usage = payload.get("token_usage") or {}
        if usage.get("total_tokens") is not None:
            fields.append(("tokens", usage.get("total_tokens")))
        if kind == "JOB_CANCELLED":
            fields.append(("reason", payload.get("reason")))
    elif kind.startswith("MECHANICAL_SUBTASK_"):
        fields = [
            ("parent", payload.get("parent_job_id")),
            ("role", payload.get("parent_role")),
            ("subtask", payload.get("subtask_id")),
            ("attempt", payload.get("attempt")),
            ("status", payload.get("status")),
            ("requested_model", payload.get("model") or payload.get("to_model")),
            ("actual_model", payload.get("actual_model")),
            ("effort", payload.get("reasoning_effort") or payload.get("to_reasoning_effort")),
            ("route_attestation", payload.get("model_route_attestation")),
            ("tier", payload.get("service_tier")),
            ("error", payload.get("error")),
        ]
    elif kind == "MECHANICAL_ROUTE_UNAVAILABLE":
        fields = [
            ("parent", payload.get("parent_job_id")),
            ("subtask", payload.get("subtask_id")),
            ("model", payload.get("model")),
            ("effort", payload.get("reasoning_effort")),
            ("tier", payload.get("service_tier")),
            ("error", payload.get("error")),
        ]
    elif kind == "MECHANICAL_ROUTE_CACHE_PERSIST_FAILED":
        fields = [
            ("parent", payload.get("parent_job_id")),
            ("subtask", payload.get("subtask_id")),
            ("model", payload.get("model")),
            ("effort", payload.get("reasoning_effort")),
            ("error", payload.get("error")),
        ]
    elif kind == "MECHANICAL_BROKER_INTEGRITY_FAILURE":
        fields = [
            ("parent", payload.get("parent_job_id")),
            ("role", payload.get("parent_role")),
            ("task", payload.get("parent_task_id")),
            ("error", payload.get("error")),
        ]
    elif kind == "JOB_BOUND":
        fields = [("job", payload.get("job_id")), ("thread", payload.get("thread_id"))]
    elif kind == "APP_SERVER_NOTIFICATION":
        method = str(payload.get("method") or "notification")
        params = payload.get("params") or {}
        display_kind = method
        if method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            fields = [
                ("thread", params.get("threadId")), ("total", total.get("totalTokens")),
                ("in", total.get("inputTokens")), ("cached", total.get("cachedInputTokens")),
                ("out", total.get("outputTokens")),
            ]
        elif method == "thread/goal/updated":
            goal = params.get("goal") or {}
            fields = [
                ("thread", params.get("threadId")), ("goal", goal.get("status")),
                ("used", goal.get("tokensUsed")), ("budget", goal.get("tokenBudget")),
            ]
        elif method == "account/rateLimits/updated":
            rates = params.get("rateLimits") or {}
            primary = rates.get("primary") or {}
            fields = [
                ("used", f"{primary.get('usedPercent')}%" if primary.get("usedPercent") is not None else None),
                ("reached", rates.get("rateLimitReachedType")),
            ]
        elif method == "thread/started":
            thread = params.get("thread") or {}
            fields = [("thread", thread.get("id")), ("status", (thread.get("status") or {}).get("type"))]
        else:
            turn = params.get("turn") or {}
            fields = [
                ("thread", params.get("threadId")), ("turn", turn.get("id") or params.get("turnId")),
                ("status", turn.get("status")),
            ]
    elif kind == "RUN_STARTED":
        fields = [
            ("hours", payload.get("hours")), ("budget", payload.get("global_budget")),
            ("director", payload.get("max_director", 1)),
            ("research", payload.get("max_research_workers", payload.get("max_research"))),
            ("audit", payload.get("max_audit")),
            ("mechanical", payload.get("max_mechanical_subworkers")),
        ]
    elif kind == "RUN_STOPPED":
        fields = [("reason", payload.get("reason"))]
    else:
        for key in (
            "role", "task_id", "claim_id", "event_id", "verdict", "trust_status",
            "math_status", "reason", "error", "action",
        ):
            if payload.get(key) is not None:
                fields.append((key.removesuffix("_id"), payload[key]))

    suffix = " ".join(f"{key}={_compact(value)}" for key, value in fields if value is not None)
    prefix = f"{_local_clock(event.get('timestamp'))} #{sequence} {display_kind}"
    return f"{prefix} {suffix}".rstrip()


def format_status(status: dict[str, Any]) -> str:
    usage = status["token_usage"]
    lines = [
        f"run: {status['run_id']}",
        (
            f"campaign: {status.get('campaign_id') or '-'} | "
            f"epoch: {status.get('epoch_id') or '-'} | "
            f"campaign status: {status.get('campaign_status') or '-'}"
        ),
        f"state: {status['state']}",
        (
            f"mode: {status.get('execution_mode') or '-'} | "
            f"outcome: {status.get('run_outcome') or '-'} | "
            f"internal failure: {status.get('internal_failure')}"
        ),
        f"events: {status['event_count']} (last #{status['last_sequence']} {status['last_event']})",
        f"live feed: {status['live_event_count']} events (last {status['last_live_event'] or '-'})",
        (
            ("tokens (observed lower bound): " if status.get("token_usage_is_lower_bound") else "tokens: ")
            + f"total={usage['totalTokens']} input={usage['inputTokens']} "
            f"cached={usage['cachedInputTokens']} output={usage['outputTokens']}"
        ),
        (
            "token telemetry: "
            f"observed={status.get('token_telemetry', {}).get('observed', 0)} "
            f"synthetic={status.get('token_telemetry', {}).get('synthetic', 0)} "
            f"unknown={status.get('token_telemetry', {}).get('unknown', 0)}"
        ),
        (
            f"jobs: started={status['jobs_started']} terminal={status['jobs_terminal']} "
            f"completed={status['jobs_completed']} cancelled={status['jobs_cancelled']} "
            f"active={len(status['active_jobs'])}"
        ),
        (
            "mechanical subtasks: "
            f"requested={status.get('mechanical_subtasks', {}).get('requested', 0)} "
            f"attempts={status.get('mechanical_subtasks', {}).get('attempts_started', 0)} "
            f"terminal={status.get('mechanical_subtasks', {}).get('terminal', 0)} "
            f"active={len(status.get('mechanical_subtasks', {}).get('active_subtasks', []))}"
        ),
    ]
    for job in status["active_jobs"]:
        lines.append(
            "  - "
            f"{job.get('role') or '-'} task={job.get('task_id') or '-'} "
            f"claim={job.get('claim_id') or '-'} thread={job.get('thread_id') or 'binding-pending'}"
        )
    rates = status.get("rate_limits") or {}
    primary = rates.get("primary") or {}
    if primary.get("usedPercent") is not None:
        lines.append(f"rate limit: used={primary['usedPercent']}% reached={rates.get('rateLimitReachedType') or 'no'}")
    if status.get("stop_reason"):
        lines.append(f"stop reason: {status['stop_reason']}")
    if status.get("problems"):
        lines.append("problems:")
        for problem in status["problems"]:
            lines.append(
                f"  - #{problem.get('sequence')} {problem.get('kind')} "
                f"task={problem.get('task_id') or '-'} claim={problem.get('claim_id') or '-'} "
                f"detail={_compact(problem.get('detail'), 180)}"
            )
    return "\n".join(lines)


def _decode_line(raw: bytes, path: Path) -> dict[str, Any] | None:
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid complete JSONL record in {path}: {exc}") from exc


def _initial_events(handle: BinaryIO, path: Path, tail: int) -> tuple[list[dict[str, Any]], bytes]:
    data = handle.read()
    parts = data.split(b"\n")
    pending = parts.pop()
    records: deque[dict[str, Any]] = deque(maxlen=max(0, tail))
    if tail > 0:
        for raw in parts:
            event = _decode_line(raw, path)
            if event is not None:
                records.append(event)
    return list(records), pending


_ROLE_LABELS = {
    "director": "研究主管", "prover": "证明者", "falsifier": "反例搜索者",
    "explorer": "独立探索者", "auditor": "审计员",
    "evaluator_auditor": "独立验证员",
    "mechanical_subworker": "机械子工",
}
_ROLE_SHORT_LABELS = {
    "director": "主管", "prover": "证明", "falsifier": "反例",
    "explorer": "探索", "auditor": "审计", "evaluator_auditor": "验证",
    "mechanical_subworker": "机械",
}


def _agent_label(payload: dict[str, Any]) -> str:
    role = _ROLE_LABELS.get(str(payload.get("role") or ""), "研究 Agent")
    task = str(payload.get("task_id") or "")
    claim = str(payload.get("claim_id") or "")
    parts = [role]
    if claim:
        parts.append(_compact(claim, 28))
    if task and not task.startswith("director-") and not task.startswith("audit-"):
        parts.append(_compact(task, 30))
    return "｜".join(parts)


def _message_prefix(event: dict[str, Any], label: str, nature: str) -> str:
    return f"{_local_clock(event.get('timestamp'))} [{label}｜{nature}]"


def _message_block(prefix: str, text: Any) -> str:
    """Render one logical message with one prefix and indented body lines."""
    body = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not body:
        return prefix
    return prefix + "\n" + "\n".join(f"  {line}" for line in body.split("\n"))


def _duration_text(value: Any) -> str:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return ""
    if milliseconds < 1000:
        return f"{milliseconds} 毫秒"
    return f"{milliseconds / 1000:.1f} 秒"


def _elapsed_text(value: Any) -> str:
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _token_text(value: Any) -> str:
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return "-"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}k"
    return str(amount)


def _reason_text(value: Any) -> str:
    raw = str(value or "")
    if raw.startswith("bootstrap failed: invalid output schema"):
        return "启动前输出 schema 兼容性检查失败；未启动模型"
    if raw.startswith("bootstrap failed:"):
        return "App Server 启动阶段失败：" + _compact(raw.removeprefix("bootstrap failed:").strip(), 180)
    if raw.startswith("director failed:"):
        return "研究主管失败：" + _compact(raw.removeprefix("director failed:").strip(), 180)
    if raw.startswith("director declared research frontier exhausted:"):
        return "研究主管明确判定当前研究前沿已耗尽：" + _compact(
            raw.removeprefix("director declared research frontier exhausted:").strip(), 180
        )
    if raw.startswith(("prover failed:", "falsifier failed:", "explorer failed:", "auditor failed:")):
        return "Agent 协议失败：" + _compact(raw, 180)
    return {
        "work queue exhausted": "旧版隐式任务队列耗尽（缺少明确 Director 停止决定）",
        "dry-run validation complete": "试运行验证完成",
        "global token budget exhausted": "全局 token 预算已用尽",
        "deadline reached": "已到达运行时间上限",
        "time limit reached": "已到达运行时间上限",
        "fresh Director cannot start within remaining token budget": "剩余预算不足以启动新的研究主管",
        "pending research cannot start within remaining token budget": "剩余预算不足以启动待处理研究任务",
        "pending audit cannot start within remaining token budget": "剩余预算不足以启动待处理审计",
        "diversification required but fresh Director cannot start within remaining token budget": (
            "需要重新规划独立路线，但剩余预算不足以启动新的研究主管"
        ),
    }.get(raw, _compact(raw, 240))


def _describe_command(command: Any) -> str:
    """Translate command-line mechanics into a stable human activity label."""
    text = str(command or "")
    lowered = text.lower()
    if "compact_snapshot" in lowered:
        return "读取精简研究状态"
    if "math-research" in lowered or "skill.md" in lowered or "verification-levels" in lowered:
        return "读取数学研究规范"
    if "task_packet" in lowered or "audit_packet" in lowered:
        return "读取当前任务说明"
    if "get-content" in lowered or "read_text" in lowered:
        return "读取研究资料"
    if "rg " in lowered or "select-string" in lowered or "findstr" in lowered:
        return "检索研究文件"
    if "emit_event" in lowered or "candidate_event" in lowered:
        return "提交候选数学结果"
    if "lean" in lowered or "lake " in lowered:
        return "运行形式化证明检查"
    if "sage" in lowered or "singular" in lowered or "gap " in lowered:
        return "运行精确代数计算"
    if "unittest" in lowered or "pytest" in lowered or "test_" in lowered:
        return "运行可复现验证"
    if "python" in lowered:
        return "运行本地数学计算"
    if "git diff" in lowered or "git status" in lowered:
        return "检查研究工作区"
    return "运行本地研究工具"


def _tool_activity(payload: dict[str, Any], *, completed: bool) -> tuple[str, str] | None:
    item_type = str(payload.get("item_type") or "")
    if item_type in {"userMessage", "agentMessage", "reasoning", "plan", ""}:
        return None
    if item_type == "commandExecution":
        action = _describe_command(payload.get("command"))
        exit_code = payload.get("exit_code")
        failed = exit_code not in {None, 0, "0"} or str(payload.get("status") or "").lower() in {
            "failed", "error",
        }
        if completed and failed:
            return "工具失败", f"{action}失败（退出码 {exit_code}）"
        if completed:
            try:
                duration_ms = float(payload.get("duration_ms") or 0)
            except (TypeError, ValueError):
                duration_ms = 0
            duration = _duration_text(duration_ms) if duration_ms >= 300_000 else ""
            suffix = f"（{duration}）" if duration else ""
            return "工具完成", f"{action}已完成{suffix}"
        return "工具", f"正在{action}"
    if item_type == "fileChange":
        count = len(payload.get("changes") or [])
        return ("文件", f"已更新 {count} 个研究文件") if completed else ("文件", "正在更新研究文件")
    if item_type == "webSearch":
        query = _compact(payload.get("query"), 120)
        return "资料检索", f"正在检索：{query}" if not completed else f"资料检索完成：{query}"
    if item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = _compact(payload.get("tool"), 80)
        return "外部工具", f"正在调用 {tool}" if not completed else f"{tool} 调用完成"
    if item_type == "collabToolCall":
        return "协作", "正在协调其他 Agent" if not completed else "Agent 协作步骤完成"
    if item_type == "imageView":
        return "查看材料", "正在查看图像材料" if not completed else "图像材料查看完成"
    return "工具", f"正在执行 {item_type}" if not completed else f"{item_type} 已完成"


def format_live_event(event: dict[str, Any]) -> str | None:
    kind = str(event.get("kind") or "LIVE")
    payload = event.get("payload") or {}
    clock = _local_clock(event.get("timestamp"))
    label = _agent_label(payload)
    if kind == "AGENT_TEXT_CHUNK":
        channel = {
            "agent_message": "回复", "reasoning_summary": "思路",
            "plan": "计划",
        }.get(str(payload.get("channel")), str(payload.get("channel") or "输出"))
        if payload.get("channel") == "command_output":
            # Raw terminal output remains in LIVE_EVENTS.jsonl/--json for audit and
            # troubleshooting.  The normal chat surface reports the activity and
            # completion instead of dumping technical output at the user.
            return None
        text = str(payload.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        return _message_block(_message_prefix(event, label, channel), text)
    if kind in {"AGENT_ITEM_STARTED", "AGENT_ITEM_COMPLETED"}:
        activity = _tool_activity(payload, completed=kind.endswith("COMPLETED"))
        if activity is None:
            return None
        nature, description = activity
        return f"{_message_prefix(event, label, nature)} {description}"
    if kind == "AGENT_PLAN_UPDATED":
        plan = payload.get("plan") or []
        steps = "; ".join(
            f"{item.get('status')}:{_compact(item.get('step'), 100)}"
            for item in plan if isinstance(item, dict)
        )
        return _message_block(_message_prefix(event, label, "计划更新"), _compact(steps, 500))
    if kind == "MECHANICAL_SUBTASK_STARTED":
        return (
            f"{clock} [机械子工｜{_compact(payload.get('subtask_id'), 40)}｜开始] "
            f"父角色 {payload.get('parent_role')} 已通过 controller broker 启动受控执行"
        )
    if kind in {"MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED"}:
        nature = "完成" if kind.endswith("COMPLETED") else "失败"
        detail = payload.get("error") or payload.get("status")
        return (
            f"{clock} [机械子工｜{_compact(payload.get('subtask_id'), 40)}｜{nature}] "
            f"{_compact(detail, 240)}"
        )
    if kind == "AGENT_JOB_STARTED":
        return f"{_message_prefix(event, label, '开始')} 已开始当前研究任务"
    if kind == "AGENT_JOB_COMPLETED":
        detail = payload.get("summary") or payload.get("error") or ""
        status = str(payload.get("status") or "").lower()
        nature = "完成" if status in {"completed", "success", "passed", ""} else "错误"
        message = "当前任务已完成" if nature == "完成" else "当前任务异常结束"
        if detail:
            message += f"：{_compact(detail, 300)}"
        metrics: list[str] = []
        try:
            elapsed_seconds = float(payload.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            elapsed_seconds = 0
        try:
            total_tokens = int(payload.get("total_tokens") or 0)
        except (TypeError, ValueError):
            total_tokens = 0
        # Keep routine chat lightweight.  Cost details become useful only for
        # a substantial job; full metadata remains in LIVE_EVENTS.jsonl.
        if elapsed_seconds >= 300 or total_tokens >= 10_000:
            if elapsed_seconds > 0:
                metrics.append(f"耗时 {_elapsed_text(elapsed_seconds)}")
            if total_tokens > 0:
                metrics.append(f"{_token_text(total_tokens)} tokens")
        if metrics:
            message += "（" + "；".join(metrics) + "）"
        return f"{_message_prefix(event, label, nature)} {message}"
    if kind == "AGENT_JOB_CANCELLED":
        return (
            f"{_message_prefix(event, label, '取消')} "
            f"任务已停止：{_compact(payload.get('reason'), 300)}"
        )
    if kind == "AGENT_JOB_CANCEL_REQUESTED":
        return (
            f"{_message_prefix(event, label, '预算')} "
            "本任务已达到单任务 token 上限，正在安全停止"
        )
    if kind == "AGENT_JOB_BUDGET_OBSERVED":
        return (
            f"{_message_prefix(event, label, '预算')} "
            "本任务已达到单任务参考额度；继续运行至自然完成"
        )
    if kind == "AGENT_JOB_CANCEL_FAILED":
        return (
            f"{_message_prefix(event, label, '错误')} 无法安全停止当前任务；"
            "controller 将结束运行并保留诊断记录"
        )
    if kind == "AGENT_OUTPUT_TRUNCATED":
        return (
            f"{_message_prefix(event, label, '提示')} 此类实时输出已达到 "
            f"{payload.get('limit_chars')} 字符上限；完整结果请查看研究文件"
        )
    if kind == "AGENT_REASONING_SECTION":
        # Section boundaries are useful in raw logs but visually fragment the
        # same public reasoning-summary message in the human chat view.
        return None
    if kind == "LIVE_MONITOR_READY":
        return f"{clock} [监视器｜就绪] 多 Agent 中文工作流已连接；隐藏思维链不采集"
    if kind in {"LIVE_RUN_STOPPED", "AGENT_TEXT_COMPLETED"}:
        return None
    return None


def format_chat_lifecycle_event(event: dict[str, Any]) -> str | None:
    """Human research milestones only; protocol telemetry stays out of chat."""
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}
    def prefix(nature: str) -> str:
        return f"{_local_clock(event.get('timestamp'))} [系统｜{nature}]"

    if kind == "RUN_STARTED":
        return (
            f"{prefix('运行')} 自主研究已启动：Director {payload.get('max_director', 1)}，"
            f"研究席位 {payload.get('max_research_workers', payload.get('max_research'))}，"
            f"审计席位 {payload.get('max_audit')}，机械子工席位 "
            f"{payload.get('max_mechanical_subworkers', 0)}"
        )
    if kind == "MECHANICAL_SUBTASK_REQUESTED":
        return (
            f"{prefix('机械外包')} {payload.get('parent_role')} 请求受控子工 "
            f"{_compact(payload.get('subtask_id'), 60)}；等待 controller 预检"
        )
    if kind == "MECHANICAL_SUBTASK_STARTED":
        return (
            f"{prefix('机械外包')} {_compact(payload.get('subtask_id'), 60)} 已启动："
            f"Spark/high/null（attempt {payload.get('attempt')}）"
        )
    if kind == "MECHANICAL_SUBTASK_FALLBACK":
        return (
            f"{prefix('机械回退')} Spark 被明确判定永久 unavailable/access denied；"
            "切换到 Luna/medium/null"
        )
    if kind == "MECHANICAL_ROUTE_UNAVAILABLE":
        return (
            f"{prefix('机械路由')} {payload.get('model')}/{payload.get('reasoning_effort')}/null "
            "已由永久 unavailable/access denied 证据精确缓存；后续不重复探测"
        )
    if kind == "MECHANICAL_ROUTE_CACHE_PERSIST_FAILED":
        return (
            f"{prefix('机械路由失败')} 无法持久化精确 unavailable circuit-breaker："
            f"{_compact(payload.get('error'), 180)}"
        )
    if kind == "MECHANICAL_SUBTASK_LEASE_REATTACHED":
        return (
            f"{prefix('机械恢复')} {_compact(payload.get('subtask_id'), 60)} 已重新附着原在途进程；"
            "不会重复派发"
        )
    if kind == "MECHANICAL_BROKER_INTEGRITY_FAILURE":
        return (
            f"{prefix('机械边界失败')} 父任务 {_compact(payload.get('parent_task_id'), 60)} "
            f"的 broker 文件发生漂移：{_compact(payload.get('error'), 160)}"
        )
    if kind == "MECHANICAL_SUBTASK_COMPLETED":
        return (
            f"{prefix('机械完成')} {_compact(payload.get('subtask_id'), 60)} 已返回父任务；"
            "结果仍只是机械证据"
        )
    if kind == "MECHANICAL_SUBTASK_FAILED":
        return (
            f"{prefix('机械失败')} {_compact(payload.get('subtask_id'), 60)}："
            f"{_compact(payload.get('error') or payload.get('failure_kind'), 180)}"
        )
    if kind == "DIRECTOR_PLAN_ACCEPTED":
        return f"{prefix('调度')} 研究主管已提交新的研究任务组合"
    if kind == "DIRECTOR_INCREMENTAL_LAUNCHED":
        return (
            f"{prefix('增量调度')} 新审计/研究状态已合并，研究主管正与在途 Agent 并行规划"
        )
    if kind == "DIRECTOR_INCREMENTAL_SUGGESTIONS_DEFERRED":
        return (
            f"{prefix('增量调度')} 并行快照中的可变建议已延后，等待更新状态后复核"
        )
    if kind == "DIRECTOR_STOP_DECLARED":
        return f"{prefix('调度停止')} 研究主管明确判定当前研究前沿已耗尽：{_compact(payload.get('reason'), 180)}"
    if kind == "DIRECTOR_STOP_DEFERRED":
        return (
            f"{prefix('继续研究')} 当前有界前沿虽已耗尽，但最终猜想仍未解决；"
            "拒绝提前结束并切换到新的独立路线"
        )
    if kind == "DIRECTOR_CONTINUATION_REQUIRED":
        return (
            f"{prefix('继续研究')} 正在请求新的研究主管："
            f"{_compact(payload.get('reason'), 180)}"
        )
    if kind == "REPLAN_REQUESTED":
        return f"{prefix('调度')} 当前研究与审计 wave 已完成，正在请求新的研究主管"
    if kind == "TASK_ACCEPTED":
        task = payload.get("task") or {}
        role = _ROLE_LABELS.get(str(task.get("role") or ""), "研究 Agent")
        claim = _compact(task.get("target_claim") or payload.get("claim_id"), 40)
        objective = _compact(task.get("exact_objective"), 240)
        return (
            f"{prefix('新任务')} {role} → {claim}：{objective}"
        )
    if kind == "EXPLORATION_SLOT_RESERVED":
        return f"{prefix('调度')} 正在为独立探索保留研究席位"
    if kind == "DIRECTOR_PAUSED":
        return f"{prefix('预算')} 剩余预算不足，暂不启动新的研究主管"
    if kind == "TASK_PAUSED":
        return f"{prefix('预算')} 任务 {_compact(payload.get('task_id'), 50)} 因预算不足暂停"
    if kind == "AUDIT_PAUSED":
        return f"{prefix('预算')} 审计 {_compact(payload.get('task_id'), 50)} 因预算不足暂停"
    if kind == "THREAD_TOKEN_BUDGET_REACHED":
        action_text = (
            "继续运行至自然完成"
            if payload.get("action") == "observe"
            else "正在安全停止"
        )
        return (
            f"{prefix('预算')} {_compact(payload.get('task_id'), 50)} 已达到单任务 token 上限 "
            f"({_token_text(payload.get('observed_tokens'))}/{_token_text(payload.get('token_budget'))})，"
            f"{action_text}"
        )
    if kind == "TOKEN_BUDGET_DRAIN_STARTED":
        return (
            f"{prefix('额度排空')} 全局 token 额度已达到；停止派发新任务，等待 "
            f"{payload.get('active_jobs', 0)} 个活动 Agent 与 "
            f"{payload.get('active_mechanical_subtasks', 0)} 个机械子工自然完成"
        )
    if kind == "TOKEN_BUDGET_DRAIN_PROGRESS":
        return (
            f"{prefix('额度排空')} 已有 Agent 自然完成；仍等待 "
            f"{payload.get('active_jobs', 0)} 个活动 Agent 与 "
            f"{payload.get('active_mechanical_subtasks', 0)} 个机械子工"
        )
    if kind == "TOKEN_BUDGET_DRAIN_COMPLETED":
        return f"{prefix('额度排空')} 所有活动 Agent 已结束，正在生成最终报告"
    if kind == "FINAL_CONJECTURE_PROVED":
        return (
            f"{prefix('最终猜想')} {_compact(payload.get('claim_id'), 50)} 已通过独立审计，"
            "开始收尾本次自动证明"
        )
    if kind == "FINAL_CONJECTURE_REFUTED":
        return (
            f"{prefix('最终猜想')} {_compact(payload.get('claim_id'), 50)} 的反例已通过独立审计，"
            "开始收尾本次自动证明"
        )
    if kind == "AUDIT_RESULT_DOWNGRADED":
        return (
            f"{prefix('审计保守降级')} Auditor 的矛盾 PASS 已降为 UNRESOLVED；"
            "候选仍保留，未进入可信状态"
        )
    if kind == "AUDIT_FAILURE_ISOLATED":
        return (
            f"{prefix('审计恢复')} 单个审计失败已隔离，候选继续保留并重新规划："
            f"{_compact(payload.get('failure_kind'), 80)}"
        )
    if kind == "FINALIZATION_STARTED":
        return (
            f"{prefix('有序收尾')} 已停止派发新任务；等待 "
            f"{payload.get('in_flight_jobs', 0)} 个在途 Agent 自然完成，中间成果继续保留"
        )
    if kind == "SCHEDULER_DRAINING_IN_FLIGHT":
        return (
            f"{prefix('有序收尾')} 仍在等待 {payload.get('in_flight_jobs', 0)} 个在途 Agent，"
            "没有派发新任务"
        )
    if kind == "FINALIZATION_COMPLETED":
        return f"{prefix('有序收尾')} 在途 Agent 已结束，正在写入成果归档与最终报告"
    if kind == "SCHEDULER_STOPPED":
        return f"{prefix('调度停止')} {_reason_text(payload.get('reason'))}"
    if kind == "CANDIDATE_PROCESSED":
        impact = {
            "CRITICAL": "关键", "HIGH": "高影响", "MEDIUM": "中等影响", "LOW": "观察性",
        }.get(str(payload.get("impact") or "").upper(), "新的")
        return (
            f"{prefix('候选结果')} 已收到关于 {_compact(payload.get('claim_id'), 40)} 的"
            f"{impact}候选结果，等待独立审计"
        )
    if kind == "CANDIDATE_RESCUED_FROM_RUN":
        return (
            f"{prefix('候选恢复')} 已从旧 run {_compact(payload.get('source_run_id'), 40)} "
            f"恢复派生候选 {_compact(payload.get('claim_id'), 55)}；重新进入独立审计"
        )
    if kind == "CANDIDATE_RECOVERY_PREFLIGHT_PASSED":
        return (
            f"{prefix('候选恢复')} 已在模型启动前验证 "
            f"{payload.get('candidate_count', 0)} 个旧协议候选"
        )
    if kind in {"AUDIT_QUEUED", "AUDIT_RETRY_QUEUED"}:
        return f"{prefix('审计')} 候选结果已进入独立审计队列"
    if kind == "AUDIT_RECORDED":
        verdict = {"PASS": "通过", "REJECT": "拒绝", "UNRESOLVED": "未决"}.get(
            str(payload.get("verdict") or "").upper(), _compact(payload.get("verdict"), 40)
        )
        return f"{prefix('审计结果')} 本轮独立审计：{verdict}"
    if kind == "TRUST_STATE_CHANGED":
        trust = {
            "AUDITED_NIGHTLY": "本夜独立审计通过",
            "AUDIT_1_PASS": "第一次独立审计通过",
            "AUDIT_2_PASS": "第二次独立审计通过",
            "REJECTED": "已拒绝",
            "FORMALLY_VERIFIED": "形式化验证通过",
        }.get(str(payload.get("trust_status") or ""), _compact(payload.get("trust_status"), 60))
        return (
            f"{prefix('可信状态')} {_compact(payload.get('claim_id'), 40)} 已更新为 "
            f"{trust}"
        )
    if kind == "DEPENDENCY_PRUNED":
        return f"{prefix('路线剪枝')} 已停止依赖失败命题的研究路线"
    if kind == "STAGNATION_DIVERSIFY":
        return f"{prefix('转向')} 当前路线停滞，下一轮将强制探索独立方向"
    if kind in {
        "DIRECTOR_REJECTED", "TASK_REJECTED", "AUDIT_ERROR", "CANDIDATE_QUARANTINED",
        "CANONICAL_GUARD_FAILED", "SERVICE_TIER_POLICY_VIOLATION", "CLAIM_CONFLICT_DETECTED",
        "JOB_CANCEL_FAILED", "CONTROLLER_ERROR", "BACKEND_CLOSE_FAILED", "BOOTSTRAP_FAILED",
        "RESEARCH_JOB_FAILED", "TASK_RETAINED_AFTER_ERROR", "AUDIT_RETAINED_AFTER_ERROR",
        "CONTROLLER_INVARIANT_FAILED", "DIRECTOR_AUDIT_REQUEST_REJECTED", "CANDIDATE_REJECTED",
    }:
        detail = payload.get("error") or payload.get("reason") or payload.get("changed") or kind
        return f"{prefix('警告')} {_compact(detail, 240)}"
    if kind == "SCHEMA_PREFLIGHT_FAILED":
        issue = next(iter(payload.get("issues") or []), {})
        detail = (
            f"{issue.get('schema_file', '未知 schema')} {issue.get('json_path', '$')}："
            f"{issue.get('reason', payload.get('error', '不兼容'))}"
        )
        return f"{prefix('错误')} 启动前输出 schema 检查失败；模型未启动。{_compact(detail, 240)}"
    if kind == "RUN_STOPPED":
        return f"{prefix('结束')} 自主研究已停止：{_reason_text(payload.get('reason'))}"
    return None


_ANSI_RESET = "\x1b[0m"
_ANSI_DIM = "\x1b[90m"
_ANSI_PANEL_BG = "\x1b[48;5;17m"
_ANSI_PANEL_DIVIDER_BG = "\x1b[48;5;24m"
_ROLE_COLORS = {
    "研究主管": "\x1b[96m", "证明者": "\x1b[92m", "反例搜索者": "\x1b[95m",
    "独立探索者": "\x1b[94m", "审计员": "\x1b[93m", "独立验证员": "\x1b[33m",
    "主管": "\x1b[96m", "证明": "\x1b[92m", "反例": "\x1b[95m",
    "探索": "\x1b[94m", "审计": "\x1b[93m", "验证": "\x1b[33m",
    "机械子工": "\x1b[36m", "机械": "\x1b[36m",
    "系统": "\x1b[97m", "监视器": "\x1b[96m",
}
_NATURE_COLORS = {
    "错误": "\x1b[91m", "警告": "\x1b[91m", "工具失败": "\x1b[91m",
    "取消": "\x1b[91m", "调度停止": "\x1b[91m", "完成": "\x1b[92m",
    "可信状态": "\x1b[92m", "审计结果": "\x1b[93m", "候选结果": "\x1b[93m",
    "审计": "\x1b[93m", "开始": "\x1b[96m", "新任务": "\x1b[96m",
    "预算": "\x1b[93m",
    "思路": "\x1b[94m", "计划": "\x1b[94m", "计划更新": "\x1b[94m",
    "回复": "\x1b[97m", "工具": "\x1b[90m", "工具完成": "\x1b[90m",
    "在线": "\x1b[90m", "状态": "\x1b[96m", "运行": "\x1b[96m",
    "转向": "\x1b[95m", "路线剪枝": "\x1b[95m",
}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PREFIX_RE = re.compile(r"(?m)^(\d{2}:\d{2}:\d{2})(\s+)\[([^\]\n]+)\]")


def _display_width(text: str) -> int:
    width = 0
    for char in _ANSI_RE.sub("", text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate_display(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if used + char_width > width:
            return "".join(result[:-1] if result and used >= width else result) + "…"
        result.append(char)
        used += char_width
    return "".join(result)


def _colorize_human(text: str) -> str:
    def prefix(match: re.Match[str]) -> str:
        timestamp, spacing, content = match.groups()
        parts = content.split("｜")
        styled: list[str] = []
        for index, part in enumerate(parts):
            color = _ROLE_COLORS.get(part) if index == 0 else _NATURE_COLORS.get(part)
            if color is None:
                color = "\x1b[97m" if index == 1 else "\x1b[90m"
            styled.append(f"{color}{part}{_ANSI_RESET}")
        return f"{_ANSI_DIM}{timestamp}{_ANSI_RESET}{spacing}[" + "｜".join(styled) + "]"

    rendered = _PREFIX_RE.sub(prefix, text)
    rendered = re.sub(
        r"模型 ([A-Za-z0-9_.-]+) / ([A-Za-z0-9_.-]+)",
        lambda match: (
            f"模型 \x1b[95m{match.group(1)}{_ANSI_RESET} / "
            f"\x1b[94m{match.group(2)}{_ANSI_RESET}"
        ),
        rendered,
    )
    rendered = re.sub(
        r"(?<![A-Za-z])([0-9]+(?:\.[0-9]+)?[kM]? tokens)",
        lambda match: f"\x1b[96m{match.group(1)}{_ANSI_RESET}",
        rendered,
    )
    rendered = re.sub(
        r"((?:耗时|最长) [0-9]+(?: 小时 [0-9]{2} 分| 分 [0-9]{2} 秒| 秒)|[0-9]+(?:\.[0-9]+)? 毫秒)",
        lambda match: f"\x1b[36m{match.group(1)}{_ANSI_RESET}",
        rendered,
    )
    rendered = rendered.replace(
        "┏━ 固定状态面板｜AUTONOMOUS MATH AI ━",
        "\x1b[96m┏━ 固定状态面板｜AUTONOMOUS MATH AI ━\x1b[0m",
    )
    rendered = re.sub(
        r"状态 (运行中|额度排空中|生成报告中|已停止)",
        lambda match: (
            "状态 " + (
                "\x1b[92m" if match.group(1) == "运行中"
                else "\x1b[91m" if match.group(1) == "已停止"
                else "\x1b[93m"
            )
            + match.group(1) + _ANSI_RESET
        ),
        rendered,
    )
    rendered = re.sub(
        r"(Token [^｜\n]+|Rate [0-9.]+%|槽位占用|"
        r"(?:主管|研究|审计|机械|总计) [0-9]+/[0-9-]+)",
        lambda match: f"\x1b[96m{match.group(1)}{_ANSI_RESET}",
        rendered,
    )
    rendered = re.sub(
        r"(已折叠 [^｜\n]+)",
        lambda match: f"\x1b[90m{match.group(1)}{_ANSI_RESET}",
        rendered,
    )
    return rendered


def _colorize_dashboard(text: str) -> str:
    rendered = _colorize_human(text)
    rendered = re.sub(
        r"(?<![A-Za-z0-9])([0-9]+min)(?![A-Za-z0-9])",
        lambda match: f"\x1b[94m{match.group(1)}{_ANSI_RESET}",
        rendered,
    )
    for role, color in _ROLE_COLORS.items():
        rendered = rendered.replace(role, f"{color}{role}{_ANSI_RESET}")
    return rendered


def _wrap_display_line(line: str, width: int, continuation: str) -> list[str]:
    if width < 4 or _display_width(line) <= width:
        return [line]
    result: list[str] = []
    current = ""
    current_width = 0
    indent_width = _display_width(continuation)
    continuation_chars = len(continuation)
    last_break = -1
    break_chars = set(" \t，。；：、,.!?;:/）】)]}")
    for char in line:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if current and current_width + char_width > width:
            if last_break >= continuation_chars:
                result.append(current[:last_break + 1].rstrip())
                tail = current[last_break + 1:].lstrip()
                current = continuation + tail + char
                current_width = _display_width(current)
            else:
                result.append(current.rstrip())
                current = continuation + char
                current_width = indent_width + char_width
            last_break = max(
                (index for index, value in enumerate(current) if value in break_chars),
                default=-1,
            )
        else:
            current += char
            current_width += char_width
            if char in break_chars:
                last_break = len(current) - 1
    if current or not result:
        result.append(current.rstrip())
    return result


def _wrap_human_message(text: str, width: int) -> str:
    wrapped: list[str] = []
    for line in text.split("\n"):
        if line.startswith("  "):
            continuation = "  "
        else:
            prefix_end = line.find("] ")
            prefix_width = _display_width(line[:prefix_end + 2]) if prefix_end >= 0 else 4
            continuation = " " * min(24, max(4, prefix_width))
        wrapped.extend(_wrap_display_line(line, max(30, width - 1), continuation))
    return "\n".join(wrapped)


def _enable_windows_vt(stream: TextIO) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint()
        kernel32 = ctypes.windll.kernel32
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


class _SgrMouseParser:
    """Incrementally decode xterm SGR mouse reports (CSI < b ; x ; y M/m)."""

    _pattern = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, text: str) -> list[tuple[int, int]]:
        self.pending = (self.pending + text)[-512:]
        positions = [
            (int(match.group(2)), int(match.group(3)))
            for match in self._pattern.finditer(self.pending)
        ]
        last_escape = self.pending.rfind("\x1b")
        self.pending = self.pending[last_escape:] if last_escape >= 0 else ""
        if len(self.pending) > 64 or self._pattern.fullmatch(self.pending):
            self.pending = ""
        return positions


class _TerminalInputParser:
    """Decode mouse/keyboard navigation without blocking the live follower."""

    _mouse = re.compile(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])")
    _keys = {
        "\x1b[5~": ("page", 1, 0),
        "\x1b[6~": ("page", -1, 0),
        "\x1b[H": ("home", 0, 0),
        "\x1b[1~": ("home", 0, 0),
        "\x1b[F": ("end", 0, 0),
        "\x1b[4~": ("end", 0, 0),
        "\x1b[A": ("line", 1, 0),
        "\x1b[B": ("line", -1, 0),
    }
    _windows_keys = {
        "I": ("page", 1, 0),
        "Q": ("page", -1, 0),
        "G": ("home", 0, 0),
        "O": ("end", 0, 0),
        "H": ("line", 1, 0),
        "P": ("line", -1, 0),
    }

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, text: str) -> list[tuple[str, int, int]]:
        self.pending = (self.pending + text)[-4096:]
        actions: list[tuple[str, int, int]] = []
        while self.pending:
            if self.pending.startswith("\x1b[<"):
                match = self._mouse.match(self.pending)
                if match is None:
                    # Preserve a possibly split SGR mouse report for the next poll.
                    if re.fullmatch(r"\x1b\[<[0-9;]*", self.pending):
                        break
                    self.pending = self.pending[1:]
                    continue
                button, x, y, _ = match.groups()
                normalized = int(button) & 0xC3  # ignore Shift/Alt/Ctrl modifiers
                if normalized == 64:
                    actions.append(("line", 3, 0))
                elif normalized == 65:
                    actions.append(("line", -3, 0))
                else:
                    actions.append(("hover", int(x), int(y)))
                self.pending = self.pending[match.end():]
                continue
            matched_key = next(
                (sequence for sequence in self._keys if self.pending.startswith(sequence)),
                None,
            )
            if matched_key is not None:
                actions.append(self._keys[matched_key])
                self.pending = self.pending[len(matched_key):]
                continue
            if any(sequence.startswith(self.pending) for sequence in self._keys):
                break
            if self.pending[0] in {"\x00", "\xe0"}:
                if len(self.pending) < 2:
                    break
                action = self._windows_keys.get(self.pending[1])
                if action is not None:
                    actions.append(action)
                self.pending = self.pending[2:]
                continue
            # Ordinary text is irrelevant to the read-only monitor.
            self.pending = self.pending[1:]
        return actions


class _TerminalMouseInput:
    """Best-effort non-blocking navigation input for Windows Terminal.

    Failure to enable input is deliberately non-fatal: the monitor keeps its
    compact TUI and the raw run remains untouched.
    """

    def __init__(self, input_stream: TextIO | None = None) -> None:
        self.input_stream = input_stream or sys.stdin
        self.parser = _TerminalInputParser()
        self.enabled = False
        self._handle: Any = None
        self._original_mode: int | None = None

    def start(self, output_stream: TextIO) -> bool:
        if os.name != "nt" or not bool(
            getattr(self.input_stream, "isatty", lambda: False)()
        ):
            return False
        try:
            import ctypes
            import msvcrt

            handle = msvcrt.get_osfhandle(self.input_stream.fileno())
            mode = ctypes.c_uint()
            kernel32 = ctypes.windll.kernel32
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            # ENABLE_VIRTUAL_TERMINAL_INPUT; keep Ctrl+C processing unchanged.
            if not kernel32.SetConsoleMode(handle, mode.value | 0x0200):
                return False
            self._handle = handle
            self._original_mode = int(mode.value)
            # Any-event tracking + SGR coordinates.  Shift still permits normal
            # Windows Terminal selection while an application is in mouse mode.
            output_stream.write("\x1b[?1003h\x1b[?1006h")
            output_stream.flush()
            self.enabled = True
            return True
        except (AttributeError, OSError, ValueError):
            return False

    def poll(self) -> list[tuple[str, int, int]]:
        if not self.enabled:
            return []
        try:
            import msvcrt

            incoming = ""
            while msvcrt.kbhit() and len(incoming) < 4096:
                incoming += msvcrt.getwch()
            return self.parser.feed(incoming) if incoming else []
        except (OSError, ValueError):
            return []

    def close(self, output_stream: TextIO) -> None:
        if not self.enabled:
            return
        output_stream.write("\x1b[?1003l\x1b[?1006l")
        output_stream.flush()
        try:
            import ctypes

            if self._handle is not None and self._original_mode is not None:
                ctypes.windll.kernel32.SetConsoleMode(self._handle, self._original_mode)
        except (AttributeError, OSError, ValueError):
            pass
        self.enabled = False


class _StyledStream:
    def __init__(self, stream: TextIO, *, color: bool):
        self.stream = stream
        self.color = color

    def write(self, text: str) -> int:
        rendered = _colorize_human(text) if self.color else text
        return self.stream.write(rendered)

    def flush(self) -> None:
        self.stream.flush()

    def isatty(self) -> bool:
        method = getattr(self.stream, "isatty", None)
        return bool(method and method())


class _TerminalMonitorUI(_StyledStream):
    """ANSI terminal surface with a fixed panel and an owned scrollback view."""

    def __init__(
        self,
        stream: TextIO,
        *,
        color: bool,
        dashboard_rows: int = 14,
        scrollback_lines: int = 20_000,
    ):
        super().__init__(stream, color=color)
        self.dashboard_rows = dashboard_rows
        self.scrollback_lines = max(100, scrollback_lines)
        self.width, self.height = shutil.get_terminal_size((120, 30))
        self.panel_rows = min(self.dashboard_rows, self.maximum_panel_rows(self.height))
        self.log_top = self.panel_rows + 1
        self.log_bottom = self.height
        self.started = False
        self.mouse = _TerminalMouseInput()
        self.hover_detail = ""
        self._event_context: dict[str, Any] | None = None
        self._line_buffer = ""
        self._line_context: dict[str, Any] | None = None
        self._log_lines: deque[tuple[str, dict[str, Any] | None]] = deque(
            maxlen=self.scrollback_lines
        )
        self._rendered_lines: list[tuple[int, dict[str, Any] | None]] = []
        self.scroll_offset = 0
        self._feed_dirty = False

    def write(self, text: str) -> int:
        self._capture_log_text(text)
        self._feed_dirty = True
        return len(text)

    def flush(self) -> None:
        if self.started and self._feed_dirty:
            self._render_feed()
        self.stream.flush()

    def set_event_context(self, event: dict[str, Any] | None) -> None:
        self._event_context = dict(event) if event is not None else None

    def _capture_log_text(self, text: str) -> None:
        before = self._row_count()
        for char in text.replace("\r\n", "\n").replace("\r", "\n"):
            if not self._line_buffer and char != "\n":
                self._line_context = self._event_context
            if char == "\n":
                self._log_lines.append((self._line_buffer, self._line_context))
                self._line_buffer = ""
                self._line_context = None
            else:
                self._line_buffer += char
        after = self._row_count()
        if self.scroll_offset > 0 and after > before:
            self.scroll_offset += after - before
        self._clamp_scroll_offset()

    def _row_count(self) -> int:
        return len(self._log_lines) + (1 if self._line_buffer else 0)

    def _viewport_rows(self) -> int:
        return max(1, self.log_bottom - self.log_top + 1)

    def _maximum_scroll_offset(self) -> int:
        return max(0, self._row_count() - self._viewport_rows())

    def _clamp_scroll_offset(self) -> None:
        self.scroll_offset = max(0, min(self.scroll_offset, self._maximum_scroll_offset()))

    @property
    def following_bottom(self) -> bool:
        return self.scroll_offset == 0

    def scroll_lines(self, lines: int) -> bool:
        previous = self.scroll_offset
        self.scroll_offset += int(lines)
        self._clamp_scroll_offset()
        if self.scroll_offset != previous:
            self._feed_dirty = True
            self._render_feed()
            self.stream.flush()
            return True
        return False

    def scroll_page(self, pages: int) -> bool:
        return self.scroll_lines(pages * max(1, self._viewport_rows() - 2))

    def scroll_home(self) -> bool:
        return self.scroll_lines(self._maximum_scroll_offset())

    def scroll_end(self) -> bool:
        return self.scroll_lines(-self.scroll_offset)

    def start(self) -> None:
        if self.started:
            return
        _enable_windows_vt(self.stream)
        self.stream.write("\x1b[?1049h\x1b[2J\x1b[?25l")
        self.stream.write("\x1b[r")
        self.stream.flush()
        self.mouse.start(self.stream)
        self.started = True

    def current_width(self) -> int:
        return max(40, shutil.get_terminal_size((self.width, self.height)).columns)

    def maximum_panel_rows(self, height: int | None = None) -> int:
        terminal_height = (
            shutil.get_terminal_size((self.width, self.height)).lines
            if height is None else height
        )
        return max(1, terminal_height * 2 // 3)

    def _desired_panel_rows(self, line_count: int, height: int | None = None) -> int:
        terminal_height = self.height if height is None else height
        desired = max(self.dashboard_rows, line_count + 2)  # hover + feed divider
        return min(desired, self.maximum_panel_rows(terminal_height))

    def refresh(self, lines: list[str]) -> None:
        if not self.started:
            return
        width, height = shutil.get_terminal_size((self.width, self.height))
        desired_panel_rows = self._desired_panel_rows(len(lines), height)
        if (
            (width, height) != (self.width, self.height)
            or desired_panel_rows != self.panel_rows
        ):
            self.width, self.height = width, height
            self.panel_rows = desired_panel_rows
            self.log_top = self.panel_rows + 1
            self.log_bottom = self.height
            self._clamp_scroll_offset()
            self.stream.write("\x1b[2J")
        self.stream.write("\x1b7")
        dashboard = lines[:max(0, self.panel_rows - 2)]
        if self.panel_rows >= 2:
            dashboard.append(
                "悬停详情｜" + (
                    self.hover_detail or "将鼠标移到消息前缀，可查看该任务的用时、token 与模型"
                )
            )
            dashboard.append(self._divider_line())
        while len(dashboard) < self.panel_rows:
            dashboard.append("")
        for offset, line in enumerate(dashboard, 1):
            content_width = max(1, self.width - 1)
            plain = _truncate_display(line, content_width)
            padding = " " * max(0, content_width - _display_width(plain))
            if self.color:
                background = (
                    _ANSI_PANEL_DIVIDER_BG
                    if offset == self.panel_rows else _ANSI_PANEL_BG
                )
                styled = _colorize_dashboard(plain).replace(
                    _ANSI_RESET, _ANSI_RESET + background,
                )
                rendered = f"{background}{styled}{padding}{_ANSI_RESET}"
            else:
                rendered = plain + padding
            self.stream.write(f"\x1b[{offset};1H\x1b[2K{rendered}")
        self.stream.write("\x1b8")
        self._render_feed()
        self.stream.flush()

    def _divider_line(self) -> str:
        if self.following_bottom:
            state = "自动跟随：开"
        else:
            state = f"自动跟随：暂停｜距底部 {self.scroll_offset} 行｜End 返回底部"
        if self.width >= 120:
            state += "｜滚轮/↑↓/PageUp PageDown/Home End"
        return f"┗━ Agent 实时信息流｜{state} " + "━" * 8

    def _all_rows(self) -> list[tuple[str, dict[str, Any] | None]]:
        rows = list(self._log_lines)
        if self._line_buffer:
            rows.append((self._line_buffer, self._line_context))
        return rows

    def _visible_rows(self) -> list[tuple[str, dict[str, Any] | None]]:
        self._clamp_scroll_offset()
        rows = self._all_rows()
        end = max(0, len(rows) - self.scroll_offset)
        start = max(0, end - self._viewport_rows())
        return rows[start:end]

    def _render_feed(self) -> None:
        if not self.started:
            return
        capacity = self._viewport_rows()
        visible = self._visible_rows()
        self._rendered_lines = []
        for index in range(capacity):
            if index < len(visible):
                text, context = visible[index]
            else:
                text, context = "", None
            plain = _truncate_display(text, max(1, self.width - 1))
            rendered = _colorize_human(plain) if self.color else plain
            row = self.log_top + index
            self.stream.write(f"\x1b[{row};1H\x1b[2K{rendered}")
            prefix = re.match(r"^\d{2}:\d{2}:\d{2} \[[^\]]+\]", text)
            self._rendered_lines.append(
                (_display_width(prefix.group(0)) if prefix else 0, context)
            )
        self._feed_dirty = False

    def _hover_at(
        self, x: int, y: int, dashboard_state: "_MonitorDashboardState",
    ) -> str:
        index = y - self.log_top
        if index < 0 or index >= len(self._rendered_lines):
            return ""
        prefix_width, context = self._rendered_lines[index]
        if prefix_width <= 0 or x < 1 or x > prefix_width or not context:
            return ""
        payload = context.get("payload") or {}
        job_id = str(payload.get("job_id") or "")
        return dashboard_state.job_detail(job_id) if job_id else ""

    def poll_input(self, dashboard_state: "_MonitorDashboardState") -> bool:
        actions = self.mouse.poll()
        size = shutil.get_terminal_size((self.width, self.height))
        changed = (size.columns, size.lines) != (self.width, self.height)
        for action, first, second in actions:
            if action == "hover":
                detail = self._hover_at(first, second, dashboard_state)
                if detail != self.hover_detail:
                    self.hover_detail = detail
                    changed = True
            elif action == "line":
                changed = self.scroll_lines(first) or changed
            elif action == "page":
                changed = self.scroll_page(first) or changed
            elif action == "home":
                changed = self.scroll_home() or changed
            elif action == "end":
                changed = self.scroll_end() or changed
        return changed

    def close(self) -> None:
        if not self.started:
            return
        self.mouse.close(self.stream)
        self.stream.write(f"{_ANSI_RESET}\x1b[r\x1b[?25h\x1b[?1049l")
        self.stream.flush()
        self.started = False


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None


class _MonitorDashboardState:
    def __init__(
        self,
        run_id: str,
        lifecycle_events: list[dict[str, Any]],
        live_events: list[dict[str, Any]],
    ):
        self.run_id = run_id
        self.state = "RUNNING"
        self.stop_reason: str | None = None
        self.started_at: datetime | None = None
        self.global_budget: int | None = None
        self.max_director = 0
        self.max_research_workers = 0
        self.max_research = 0
        self.max_audit = 0
        self.max_mechanical_subworkers = 0
        self.event_count = 0
        self.live_event_count = 0
        self.active_jobs: dict[str, dict[str, Any]] = {}
        self.job_records: dict[str, dict[str, Any]] = {}
        self.started_jobs: set[str] = set()
        self.terminal_jobs: set[str] = set()
        self.thread_to_job: dict[str, str] = {}
        self.thread_tokens: dict[str, int] = {}
        self.completed_usage: dict[str, tuple[str, int]] = {}
        self.rate_used: Any = None
        self.active_tools: dict[str, tuple[str, str]] = {}
        self.task_names: dict[str, str] = {}
        self.mechanical_task_names: dict[tuple[str, str], str] = {}
        for event in lifecycle_events:
            self.apply_lifecycle(event)
        for event in live_events:
            self.apply_live(event)

    def apply_lifecycle(self, event: dict[str, Any]) -> None:
        self.event_count += 1
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        if kind == "RUN_STARTED":
            self.started_at = _parse_timestamp(event.get("timestamp"))
            self.global_budget = payload.get("global_budget")
            self.max_director = int(payload.get("max_director") or 1)
            self.max_research_workers = int(
                payload.get("max_research_workers", payload.get("max_research")) or 0
            )
            self.max_research = self.max_research_workers
            self.max_audit = int(payload.get("max_audit") or 0)
            self.max_mechanical_subworkers = int(
                payload.get("max_mechanical_subworkers") or 0
            )
        elif kind == "TASK_ACCEPTED":
            task = payload.get("task") or {}
            task_id = str(task.get("task_id") or payload.get("task_id") or "")
            objective = str(task.get("exact_objective") or "").strip()
            if task_id and objective:
                self.task_names[task_id] = objective
                for record in self.job_records.values():
                    if str(record.get("task_id") or "") == task_id:
                        record["thread_name"] = objective
        elif kind == "JOB_STARTED" and payload.get("job_id"):
            job_id = str(payload["job_id"])
            self.started_jobs.add(job_id)
            record = dict(payload)
            record.setdefault("start_time", event.get("timestamp"))
            task_name = self.task_names.get(str(payload.get("task_id") or ""))
            if task_name:
                record["thread_name"] = task_name
            self.job_records.setdefault(job_id, {}).update(record)
            self.active_jobs[job_id] = self.job_records[job_id]
        elif kind == "JOB_BOUND" and payload.get("thread_id") and payload.get("job_id"):
            self.thread_to_job[str(payload["thread_id"])] = str(payload["job_id"])
            active = self.active_jobs.get(str(payload["job_id"]))
            if active is not None:
                active["thread_id"] = payload["thread_id"]
        elif kind in {"JOB_COMPLETED", "JOB_CANCELLED"} and payload.get("job_id"):
            job_id = str(payload["job_id"])
            self.terminal_jobs.add(job_id)
            self.job_records.setdefault(job_id, {}).update(payload)
            self.job_records[job_id]["end_time"] = event.get("timestamp")
            self.active_jobs.pop(job_id, None)
            if kind == "JOB_COMPLETED":
                usage = payload.get("token_usage") or {}
                self.completed_usage[job_id] = (
                    str(payload.get("thread_id") or ""),
                    int(usage.get("total_tokens") or 0),
                )
        elif kind == "MECHANICAL_SUBTASK_REQUESTED":
            parent = str(payload.get("parent_job_id") or "")
            subtask = str(payload.get("subtask_id") or "")
            packet = payload.get("task_packet") or {}
            objective = str(packet.get("objective") or "").strip()
            if parent and subtask and objective:
                self.mechanical_task_names[(parent, subtask)] = objective
        elif kind == "MECHANICAL_SUBTASK_STARTED" and payload.get("mechanical_job_id"):
            job_id = str(payload["mechanical_job_id"])
            parent = str(payload.get("parent_job_id") or "")
            subtask = str(payload.get("subtask_id") or "")
            record = {
                "job_id": job_id,
                "task_id": subtask,
                "role": "mechanical_subworker",
                "parent_job_id": parent,
                "claim_id": f"parent:{payload.get('parent_role')}",
                "task_kind": payload.get("task_kind"),
                "thread_name": self.mechanical_task_names.get((parent, subtask)),
                "model": payload.get("model"),
                "reasoning_effort": payload.get("reasoning_effort"),
                "start_time": payload.get("started_at") or event.get("timestamp"),
            }
            self.job_records[job_id] = record
            self.active_jobs[job_id] = record
        elif (
            kind == "MECHANICAL_SUBTASK_ATTEMPT_FINISHED"
            and payload.get("mechanical_job_id")
        ):
            job_id = str(payload["mechanical_job_id"])
            self.job_records.setdefault(job_id, {}).update(payload)
            self.job_records[job_id]["end_time"] = event.get("timestamp")
            self.active_jobs.pop(job_id, None)
        elif kind in {"MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED"}:
            parent = str(payload.get("parent_job_id") or "")
            subtask = str(payload.get("subtask_id") or "")
            for job_id, record in list(self.active_jobs.items()):
                if (
                    record.get("role") == "mechanical_subworker"
                    and str(record.get("task_id") or "") == subtask
                    and str(record.get("parent_job_id") or "") == parent
                ):
                    self.active_jobs.pop(job_id, None)
        elif kind == "APP_SERVER_NOTIFICATION":
            method = payload.get("method")
            params = payload.get("params") or {}
            if method == "thread/tokenUsage/updated" and params.get("threadId"):
                total = (params.get("tokenUsage") or {}).get("total") or {}
                self.thread_tokens[str(params["threadId"])] = int(total.get("totalTokens") or 0)
            elif method == "account/rateLimits/updated":
                rates = params.get("rateLimits") or {}
                self.rate_used = (rates.get("primary") or {}).get("usedPercent")
        elif kind == "TOKEN_BUDGET_DRAIN_STARTED":
            self.state = "DRAINING"
        elif kind == "TOKEN_BUDGET_DRAIN_COMPLETED":
            self.state = "FINALIZING"
        elif kind == "RUN_STOPPED":
            self.state = "STOPPED"
            self.stop_reason = str(payload.get("reason") or "stopped")

    def apply_live(self, event: dict[str, Any]) -> None:
        self.live_event_count += 1
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        job_id = str(payload.get("job_id") or "")
        if kind == "AGENT_JOB_STARTED" and job_id:
            record = self.job_records.setdefault(job_id, {})
            for key in (
                "job_id", "task_id", "role", "claim_id", "model",
                "reasoning_effort", "start_time", "timeout_seconds",
            ):
                if payload.get(key) is not None:
                    record[key] = payload[key]
            task_name = self.task_names.get(str(payload.get("task_id") or ""))
            if task_name:
                record["thread_name"] = task_name
            self.active_jobs[job_id] = record
        elif kind == "AGENT_ITEM_STARTED":
            activity = _tool_activity(payload, completed=False)
            if activity is not None:
                key = str(payload.get("item_id") or f"{job_id}:{payload.get('item_type')}")
                self.active_tools[key] = (job_id, activity[1])
        elif kind == "AGENT_ITEM_COMPLETED":
            key = str(payload.get("item_id") or f"{job_id}:{payload.get('item_type')}")
            self.active_tools.pop(key, None)
        elif kind in {"AGENT_JOB_COMPLETED", "AGENT_JOB_CANCELLED"} and job_id:
            self.job_records.setdefault(job_id, {}).update(payload)
            self.job_records[job_id].setdefault("end_time", event.get("timestamp"))
            self.active_jobs.pop(job_id, None)

    @property
    def total_tokens(self) -> int:
        total = sum(self.thread_tokens.values())
        for job_id, (thread_id, usage) in self.completed_usage.items():
            resolved_thread = thread_id or next(
                (thread for thread, bound in self.thread_to_job.items() if bound == job_id), ""
            )
            if not resolved_thread or resolved_thread not in self.thread_tokens:
                total += usage
        return total

    @staticmethod
    def _thread_group(role: str) -> str:
        if role == "director":
            return "director"
        if role in {"prover", "falsifier", "explorer"}:
            return "research"
        if role in {"auditor", "evaluator_auditor"}:
            return "audit"
        if role == "mechanical_subworker":
            return "mechanical"
        return "other"

    @staticmethod
    def _fallback_thread_name(payload: dict[str, Any]) -> str:
        role = str(payload.get("role") or "")
        task_id = str(payload.get("task_id") or "")
        claim = str(payload.get("claim_id") or "")
        if role == "director":
            return "增量重规划研究路线" if "incremental" in task_id else "规划研究路线"
        if role == "auditor":
            return f"审计 {claim or task_id or '候选结果'}"
        if role == "evaluator_auditor":
            return f"独立复核 {claim or task_id or '候选结果'}"
        if role == "mechanical_subworker":
            kind = str(payload.get("task_kind") or "机械任务").replace("_", " ")
            return f"{kind}：{task_id}" if task_id else kind
        if task_id:
            return task_id.replace("_", " ").replace("-", " ")
        return claim or "未命名任务"

    def _active_thread_cards(
        self, now: datetime, *, display_width: int,
    ) -> dict[str, list[str]]:
        grouped: dict[str, list[tuple[str, str]]] = {
            "director": [], "research": [], "audit": [], "mechanical": [], "other": [],
        }
        for payload in self.active_jobs.values():
            role = str(payload.get("role") or "")
            name = str(payload.get("thread_name") or "").strip()
            if not name:
                name = self._fallback_thread_name(payload)
            name = " ".join(name.split())
            subtype = _ROLE_SHORT_LABELS.get(role, "Agent")
            duplicate_prefixes = sorted(
                {
                    subtype,
                    _ROLE_LABELS.get(role, ""),
                } - {""},
                key=len,
                reverse=True,
            )
            for duplicate_prefix in duplicate_prefixes:
                if name != duplicate_prefix and name.startswith(duplicate_prefix):
                    remainder = name[len(duplicate_prefix):].lstrip(" ：:·-—_|/")
                    if remainder:
                        name = remainder
                        break
            started = _parse_timestamp(payload.get("start_time"))
            if started is None:
                elapsed = "计时中"
            else:
                current = now.astimezone(started.tzinfo) if started.tzinfo else now.replace(tzinfo=None)
                elapsed_minutes = max(0, int((current - started).total_seconds()) // 60)
                elapsed = f"{elapsed_minutes}min"
            card = f"{subtype}·{name}｜{elapsed}"
            grouped[self._thread_group(role)].append((name.casefold(), card))
        return {
            group: [card for _, card in sorted(items, key=lambda item: (item[0], item[1]))]
            for group, items in grouped.items()
        }

    @staticmethod
    def _pack_thread_cards(
        label: str,
        cards: list[str],
        *,
        display_width: int,
        columns: int = 1,
        compact: bool = False,
    ) -> list[str]:
        if not cards:
            return []
        prefix = f"{label}｜"
        prefix_width = _display_width(prefix)
        columns = max(1, min(int(columns), len(cards)))
        gap = " │ "
        content_width = max(8, display_width - prefix_width - 1)
        cell_width = max(
            8,
            (content_width - _display_width(gap) * (columns - 1)) // columns,
        )
        wrapped: list[list[str]] = []
        for index, card in enumerate(cards, 1):
            numbered = f"{index}. {card}"
            if not compact:
                wrapped.append(_wrap_display_line(numbered, cell_width, "   "))
                continue
            head, marker, elapsed = card.rpartition("｜")
            subtype, separator, name = head.partition("·")
            role_code = {
                "主管": "管", "证明": "证", "反例": "反", "探索": "探",
                "审计": "审", "验证": "验", "机械": "机", "Agent": "A",
            }.get(subtype, subtype[:1] or "A")
            compact_prefix = f"{index}{role_code}/"
            compact_suffix = f"/{elapsed}" if marker else ""
            name_width = max(
                0,
                cell_width
                - _display_width(compact_prefix)
                - _display_width(compact_suffix),
            )
            if name_width <= 0:
                fitted_name = ""
            elif _display_width(name) <= name_width:
                fitted_name = name
            elif name_width == 1:
                fitted_name = "…"
            else:
                fitted_name = _truncate_display(name, name_width - 1)
            wrapped.append([compact_prefix + fitted_name + compact_suffix])
        logical_rows = (len(wrapped) + columns - 1) // columns
        rendered: list[str] = []
        for row in range(logical_rows):
            cells = [
                wrapped[row * columns + column]
                for column in range(columns)
                if row * columns + column < len(wrapped)
            ]
            physical_rows = max(len(cell) for cell in cells)
            for physical_row in range(physical_rows):
                pieces: list[str] = []
                for cell_index, cell in enumerate(cells):
                    value = cell[physical_row] if physical_row < len(cell) else ""
                    if cell_index < len(cells) - 1:
                        value += " " * max(0, cell_width - _display_width(value))
                    pieces.append(value)
                line_prefix = prefix if not rendered else " " * prefix_width
                rendered.append(line_prefix + gap.join(pieces).rstrip())
        return rendered

    def _thread_detail_lines(
        self,
        grouped: dict[str, list[str]],
        *,
        display_width: int,
        max_rows: int | None,
    ) -> list[str]:
        groups = (
            ("director", "主管线程"),
            ("research", "研究线程"),
            ("audit", "审计线程"),
            ("mechanical", "机械线程"),
            ("other", "其他线程"),
        )
        largest_group = max((len(grouped[group]) for group, _ in groups), default=0)
        full_max_columns = max(
            1,
            min(largest_group, max(1, (display_width - 12) // 24)),
        )
        best: list[str] = []
        for columns in range(1, full_max_columns + 1):
            candidate: list[str] = []
            for group, label in groups:
                candidate.extend(self._pack_thread_cards(
                    label,
                    grouped[group],
                    display_width=display_width,
                    columns=columns,
                ))
            if not best or len(candidate) < len(best):
                best = candidate
            if max_rows is None or len(candidate) <= max_rows:
                return candidate

        compact_max_columns = max(
            1,
            min(largest_group, max(1, (display_width - 12) // 16)),
        )
        for columns in range(1, compact_max_columns + 1):
            candidate = []
            for group, label in groups:
                candidate.extend(self._pack_thread_cards(
                    label,
                    grouped[group],
                    display_width=display_width,
                    columns=columns,
                    compact=True,
                ))
            if not best or len(candidate) < len(best):
                best = candidate
            if max_rows is None or len(candidate) <= max_rows:
                return candidate

        all_cards = [card for group, _ in groups for card in grouped[group]]
        grid_max_columns = max(
            1,
            min(len(all_cards), max(1, (display_width - 12) // 12)),
        )
        for columns in range(1, grid_max_columns + 1):
            candidate = self._pack_thread_cards(
                "全部线程",
                all_cards,
                display_width=display_width,
                columns=columns,
                compact=True,
            )
            if not best or len(candidate) < len(best):
                best = candidate
            if max_rows is None or len(candidate) <= max_rows:
                return candidate
        return best

    def _slot_occupancy(self) -> dict[str, int]:
        counts = {"director": 0, "research": 0, "audit": 0, "mechanical": 0, "other": 0}
        for payload in self.active_jobs.values():
            counts[self._thread_group(str(payload.get("role") or ""))] += 1
        return counts

    def job_detail(self, job_id: str) -> str:
        payload = self.job_records.get(job_id)
        if not payload:
            return ""
        role = _ROLE_LABELS.get(str(payload.get("role") or ""), "Agent")
        claim = str(payload.get("claim_id") or "-")
        details = [f"{role}｜{claim}"]
        elapsed_seconds: float = 0
        try:
            elapsed_seconds = float(payload.get("elapsed_seconds") or 0)
        except (TypeError, ValueError):
            pass
        if elapsed_seconds <= 0:
            started = _parse_timestamp(payload.get("start_time"))
            ended = _parse_timestamp(payload.get("end_time")) or datetime.now().astimezone()
            if started is not None:
                current = ended.astimezone(started.tzinfo) if started.tzinfo else ended.replace(tzinfo=None)
                elapsed_seconds = max(0, (current - started).total_seconds())
        if elapsed_seconds > 0:
            details.append(f"用时 {_elapsed_text(elapsed_seconds)}")

        total_tokens = 0
        try:
            total_tokens = int(payload.get("total_tokens") or 0)
        except (TypeError, ValueError):
            pass
        usage = payload.get("token_usage") or {}
        if total_tokens <= 0:
            try:
                total_tokens = int(usage.get("total_tokens") or 0)
            except (TypeError, ValueError):
                pass
        thread_id = str(payload.get("thread_id") or "")
        if total_tokens <= 0 and thread_id:
            total_tokens = self.thread_tokens.get(thread_id, 0)
        if total_tokens > 0:
            details.append(f"{_token_text(total_tokens)} tokens")
        model = str(payload.get("model") or "")
        if model:
            effort = str(payload.get("reasoning_effort") or "-")
            details.append(f"模型 {model} / {effort}")
        return "｜".join(details)

    def lines(
        self,
        *,
        quiet_seconds: int,
        folded_summary: str = "",
        display_width: int = 120,
        max_panel_rows: int | None = None,
    ) -> list[str]:
        now = datetime.now().astimezone()
        elapsed = _elapsed_text(
            (now - self.started_at.astimezone()).total_seconds() if self.started_at else 0
        )
        status = {
            "STOPPED": "已停止",
            "DRAINING": "额度排空中",
            "FINALIZING": "生成报告中",
        }.get(self.state, "运行中")
        budget = _token_text(self.global_budget) if self.global_budget is not None else "未设上限"
        rate = f"｜Rate {self.rate_used}%" if self.rate_used is not None else ""
        # Role caps are independent; an incremental Director can run beside
        # research and audit after a meaningful state change.
        capacity = (
            self.max_director + self.max_research_workers + self.max_audit
            + self.max_mechanical_subworkers
        )
        active = len(self.active_jobs)
        occupancy = self._slot_occupancy()
        first = "┏━ 固定状态面板｜AUTONOMOUS MATH AI ━"
        second = (
            f"状态 {status}｜Run {self.run_id}｜运行 {elapsed}｜"
            f"Token {_token_text(self.total_tokens)}/{budget}{rate}"
        )
        third = (
            f"槽位占用｜主管 {occupancy['director']}/{self.max_director or '-'}｜"
            f"研究 {occupancy['research']}/{self.max_research_workers or '-'}｜"
            f"审计 {occupancy['audit']}/{self.max_audit or '-'}｜"
            f"机械 {occupancy['mechanical']}/{self.max_mechanical_subworkers or '-'}｜"
            f"总计 {active}/{capacity or '-'}"
        )
        grouped = self._active_thread_cards(now, display_width=display_width)
        max_detail_rows = (
            max(1, max_panel_rows - 6) if max_panel_rows is not None else None
        )
        detail_lines = self._thread_detail_lines(
            grouped,
            display_width=display_width,
            max_rows=max_detail_rows,
        )
        if not detail_lines:
            detail_lines.append("活动线程｜无")
        details: list[str] = [f"静默 {quiet_seconds} 秒", f"事件 {self.event_count}+{self.live_event_count}"]
        if folded_summary:
            details.append(f"已折叠 {folded_summary}")
        return [first, second, third, *detail_lines, "｜".join(details)]


class _RepeatFolder:
    def __init__(self, *, enabled: bool):
        self.enabled = enabled
        self.last_tool: tuple[str, str, str, str] | None = None
        self.last_at = 0.0
        self.repeat_count = 0
        self.folded_total = 0
        self.summary = ""

    def admit(self, event: dict[str, Any], *, live: bool) -> bool:
        if not self.enabled or not live:
            return True
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        if kind == "AGENT_ITEM_STARTED" and _tool_activity(payload, completed=False) is not None:
            # The fixed top panel already shows an in-progress tool.  The log
            # records one completion line instead of a start/completion pair.
            return False
        if kind == "AGENT_ITEM_COMPLETED":
            activity = _tool_activity(payload, completed=True)
            if activity is None:
                return True
            action = _describe_command(payload.get("command")) if payload.get("item_type") == "commandExecution" else str(payload.get("item_type") or activity[1])
            failed = activity[0] in {"工具失败"}
            signature = (
                str(payload.get("role") or ""), str(payload.get("claim_id") or ""),
                action, "failed" if failed else "completed",
            )
            now = time.monotonic()
            if signature == self.last_tool and now - self.last_at <= 15:
                self.repeat_count += 1
                self.folded_total += 1
                role = _ROLE_LABELS.get(signature[0], "Agent")
                self.summary = f"{role} {action} ×{self.repeat_count}"
                self.last_at = now
                return False
            self.last_tool = signature
            self.last_at = now
            self.repeat_count = 1
            self.summary = ""
            return True
        if kind not in {"AGENT_TEXT_COMPLETED", "AGENT_REASONING_SECTION"}:
            self.last_tool = None
            self.repeat_count = 0
        return True


class _HumanChatRenderer:
    """Stateful renderer for public agent text.

    App Server emits text as deltas.  The renderer prints the identifying
    prefix once per item and then appends indented body text as deltas arrive.
    Strict JSON result contracts remain in ``LIVE_EVENTS.jsonl``/``--json``;
    the default chat view represents them through bounded lifecycle summaries.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        width_provider: Callable[[], int] | None = None,
        folder: _RepeatFolder | None = None,
    ):
        self.stream = stream
        self.width_provider = width_provider or (lambda: 120)
        self.folder = folder
        self._started: set[tuple[str, str, str, str]] = set()
        self._structured: set[tuple[str, str, str, str]] = set()
        self._pending: dict[tuple[str, str, str, str], str] = {}
        self._current: tuple[str, str, str, str] | None = None
        self._last_sender: tuple[str, ...] | None = None
        self._at_line_start = True
        self._body_column = 0

    @staticmethod
    def _key(payload: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(payload.get("thread_id") or payload.get("job_id") or ""),
            str(payload.get("turn_id") or ""),
            str(payload.get("item_id") or payload.get("task_id") or ""),
            str(payload.get("channel") or ""),
        )

    @staticmethod
    def _is_structured_contract(text: str) -> bool:
        leading = text.lstrip()
        return leading.startswith(("{", "[", "```json", "```JSON"))

    @staticmethod
    def _sender_key(event: dict[str, Any], *, live: bool) -> tuple[str, ...]:
        payload = event.get("payload") or {}
        if not live:
            return ("system",)
        if str(event.get("kind") or "").startswith("LIVE_"):
            return ("monitor",)
        job_id = str(payload.get("job_id") or "")
        if job_id:
            return ("job", job_id)
        return (
            "agent",
            str(payload.get("role") or ""),
            str(payload.get("task_id") or ""),
            str(payload.get("thread_id") or ""),
        )

    def _separate_sender(self, event: dict[str, Any], *, live: bool) -> None:
        sender = self._sender_key(event, live=live)
        if self._last_sender is not None and sender != self._last_sender:
            self._finish_open_line()
            self.stream.write("\n")
        self._last_sender = sender

    def _finish_open_line(self) -> None:
        if self._current is not None and not self._at_line_start:
            self.stream.write("\n")
        self._current = None
        self._at_line_start = True
        self._body_column = 0

    def _write_body(self, text: str) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        width = max(30, self.width_provider() - 1)
        for char in normalized:
            if self._at_line_start:
                self.stream.write("  ")
                self._at_line_start = False
                self._body_column = 2
            if char == "\n":
                self.stream.write(char)
                self._at_line_start = True
                self._body_column = 0
                continue
            char_width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            )
            if self._body_column + char_width > width:
                self.stream.write("\n  ")
                self._body_column = 2
            self.stream.write(char)
            self._body_column += char_width
        self.stream.flush()

    def _render_chunk(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        channel_name = str(payload.get("channel") or "")
        if channel_name == "command_output":
            return
        key = self._key(payload)
        if key in self._structured:
            return
        text = str(payload.get("text") or "")
        if not text:
            return

        if key not in self._started:
            try:
                chunk_index = int(payload.get("chunk_index", 0) or 0)
            except (TypeError, ValueError):
                chunk_index = 0
            if chunk_index > 0:
                # The watcher attached after the message began.  A tail without
                # its identity/header is more confusing than omitting it.
                self._structured.add(key)
                return
            buffered = self._pending.get(key, "") + text
            if not buffered.strip():
                self._pending[key] = buffered
                return
            if channel_name == "agent_message" and self._is_structured_contract(buffered):
                self._pending.pop(key, None)
                self._structured.add(key)
                return
            text = self._pending.pop(key, "") + text
            self._separate_sender(event, live=True)
            self._finish_open_line()
            nature = {
                "agent_message": "回复", "reasoning_summary": "思路", "plan": "计划",
            }.get(channel_name, channel_name or "输出")
            print(
                _message_prefix(event, _agent_label(payload), nature),
                file=self.stream,
                flush=True,
            )
            self._started.add(key)
        elif self._current != key:
            self._finish_open_line()

        self._current = key
        self._write_body(text)

    def _complete_text(self, event: dict[str, Any]) -> None:
        key = self._key(event.get("payload") or {})
        self._pending.pop(key, None)
        if self._current == key:
            self._finish_open_line()
            self.stream.flush()

    def render(self, event: dict[str, Any], *, live: bool) -> None:
        if self.folder is not None and not self.folder.admit(event, live=live):
            return
        if live and event.get("kind") == "AGENT_TEXT_CHUNK":
            self._render_chunk(event)
            return
        if live and event.get("kind") == "AGENT_TEXT_COMPLETED":
            self._complete_text(event)
            return
        rendered = format_live_event(event) if live else format_chat_lifecycle_event(event)
        if not rendered:
            return
        self._separate_sender(event, live=live)
        self._finish_open_line()
        print(
            _wrap_human_message(rendered, self.width_provider()),
            file=self.stream,
            flush=True,
        )

    def close(self) -> None:
        self._finish_open_line()
        self.stream.flush()


class _JsonlFollower:
    def __init__(self, path: Path):
        self.path = path
        self.handle: BinaryIO = path.open("rb")
        self.pending = b""

    def initial(self, tail: int) -> list[dict[str, Any]]:
        records, self.pending = _initial_events(self.handle, self.path, tail)
        return records

    def poll(self) -> list[dict[str, Any]]:
        chunk = self.handle.read()
        if not chunk:
            return []
        self.pending += chunk
        parts = self.pending.split(b"\n")
        self.pending = parts.pop()
        records: list[dict[str, Any]] = []
        for raw in parts:
            event = _decode_line(raw, self.path)
            if event is not None:
                records.append(event)
        return records

    def close(self) -> None:
        self.handle.close()


def _watch_sort_key(value: tuple[dict[str, Any], bool]) -> tuple[str, int, int]:
    event, live = value
    return str(event.get("timestamp") or ""), 1 if live else 0, int(event.get("sequence") or 0)


def _print_watch_event(
    stream: TextIO, event: dict[str, Any], *, live: bool, raw_json: bool, chat: bool,
    renderer: _HumanChatRenderer | None = None,
) -> None:
    terminal_ui = stream if isinstance(stream, _TerminalMonitorUI) else None
    if terminal_ui is not None:
        terminal_ui.set_event_context(event)
    if raw_json:
        payload = dict(event)
        payload["stream"] = "live" if live else "lifecycle"
        print(json.dumps(payload, ensure_ascii=False), file=stream, flush=True)
        if terminal_ui is not None:
            terminal_ui.set_event_context(None)
        return
    if renderer is not None:
        renderer.render(event, live=live)
        if terminal_ui is not None:
            terminal_ui.set_event_context(None)
        return
    rendered = format_live_event(event) if live else (
        format_chat_lifecycle_event(event) if chat else format_event(event)
    )
    if rendered:
        print(rendered, file=stream, flush=True)
    if terminal_ui is not None:
        terminal_ui.set_event_context(None)


def watch_run(
    run_dir: Path,
    *,
    tail: int = 20,
    poll_seconds: float = 0.5,
    heartbeat_seconds: float = 30.0,
    chat: bool = False,
    chat_tail: int = 40,
    raw_json: bool = False,
    output: TextIO | None = None,
    ui_mode: str = "auto",
    color_mode: str = "auto",
    fold_repeats: bool = True,
) -> int:
    if tail < 0:
        raise ValueError("--tail must be non-negative")
    if poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    if heartbeat_seconds < 0:
        raise ValueError("--heartbeat-seconds must be non-negative")
    if chat_tail < 0:
        raise ValueError("--chat-tail must be non-negative")
    if ui_mode not in {"auto", "tui", "plain"}:
        raise ValueError("ui_mode must be auto, tui, or plain")
    if color_mode not in {"auto", "always", "never"}:
        raise ValueError("color_mode must be auto, always, or never")
    base_stream = output or sys.stdout
    path = run_dir / "EVENTS.jsonl"
    snapshot = build_status(run_dir)
    live_path = run_dir / "LIVE_EVENTS.jsonl"
    initial_lifecycle = load_events(path)
    initial_live = load_events(live_path) if live_path.is_file() else []
    is_tty = bool(getattr(base_stream, "isatty", lambda: False)())
    use_tui = bool(
        chat and not raw_json and ui_mode != "plain"
        and (ui_mode == "tui" or (is_tty and snapshot["state"] == "RUNNING"))
    )
    use_color = bool(
        not raw_json and color_mode != "never"
        and (color_mode == "always" or is_tty)
    )
    dashboard_state = _MonitorDashboardState(run_dir.name, initial_lifecycle, initial_live)
    terminal_ui = _TerminalMonitorUI(base_stream, color=use_color) if use_tui else None
    if terminal_ui is not None:
        terminal_ui.start()
        stream: TextIO = terminal_ui
    elif use_color:
        stream = _StyledStream(base_stream, color=True)
    else:
        stream = base_stream
    folder = _RepeatFolder(enabled=use_tui and fold_repeats)
    width_provider = (
        terminal_ui.current_width if terminal_ui is not None
        else lambda: shutil.get_terminal_size((120, 30)).columns if is_tty else 120
    )
    renderer = (
        _HumanChatRenderer(
            stream, width_provider=width_provider,
            folder=folder if use_tui else None,
        )
        if chat and not raw_json else None
    )

    def dashboard_lines(quiet_seconds: int = 0) -> list[str]:
        return dashboard_state.lines(
            quiet_seconds=quiet_seconds,
            folded_summary=folder.summary,
            display_width=terminal_ui.current_width() if terminal_ui is not None else 120,
            max_panel_rows=(
                terminal_ui.maximum_panel_rows() if terminal_ui is not None else None
            ),
        )

    if chat and not raw_json:
        state = "已停止" if snapshot["state"] == "STOPPED" else "运行中"
        clock = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"{clock} [监视器｜连接] 正在监视研究运行 {run_dir.name}", file=stream, flush=True)
        if terminal_ui is None:
            print(
                f"{clock} [监视器｜状态] {state}｜活动 Agent：{len(snapshot['active_jobs'])}｜"
                f"累计用量：{snapshot['token_usage']['totalTokens']} "
                f"tokens{'（观测下界）' if snapshot.get('token_usage_is_lower_bound') else ''}",
                file=stream, flush=True,
            )
        else:
            terminal_ui.refresh(dashboard_lines())
    else:
        print(f"Watching run {run_dir.name} ({path})", file=stream, flush=True)
        print(
            f"Attached state={snapshot['state']} events={snapshot['event_count']} "
            f"active_jobs={len(snapshot['active_jobs'])} "
            f"tokens={snapshot['token_usage']['totalTokens']}"
            f"{'(lower-bound)' if snapshot.get('token_usage_is_lower_bound') else ''} "
            f"live_events={snapshot['live_event_count']}",
            file=stream, flush=True,
        )
    lifecycle = _JsonlFollower(path)
    live = _JsonlFollower(live_path) if chat and live_path.is_file() else None
    try:
        combined = [(event, False) for event in lifecycle.initial(tail)]
        if live is not None:
            combined.extend((event, True) for event in live.initial(chat_tail))
        for event, is_live in sorted(combined, key=_watch_sort_key):
            _print_watch_event(
                stream, event, live=is_live, raw_json=raw_json, chat=chat,
                renderer=renderer,
            )
        if terminal_ui is not None:
            terminal_ui.refresh(dashboard_lines())
        if chat and live is None:
            print(
                f"{datetime.now().astimezone().strftime('%H:%M:%S')} "
                "[监视器｜等待] 正在等待多 Agent 活动流；旧运行可能不支持该视图。",
                file=stream,
            )
        if snapshot["state"] == "STOPPED" or any(
            event.get("kind") == "RUN_STOPPED" for event, is_live in combined if not is_live
        ):
            if renderer is not None:
                renderer.close()
            exit_message = (
                f"{datetime.now().astimezone().strftime('%H:%M:%S')} "
                "[监视器｜退出] 该研究运行已经结束。"
            ) if chat and not raw_json else "Run is already stopped; watcher exiting."
            if terminal_ui is not None:
                terminal_ui.refresh(dashboard_lines())
                terminal_ui.close()
                print(
                    _colorize_human(exit_message) if use_color else exit_message,
                    file=base_stream, flush=True,
                )
            else:
                print(exit_message, file=stream, flush=True)
            return 0
        stream.flush()
        last_event_at = time.monotonic()
        next_heartbeat = last_event_at + heartbeat_seconds if heartbeat_seconds else float("inf")
        while True:
            input_changed = bool(
                terminal_ui is not None and terminal_ui.poll_input(dashboard_state)
            )
            if chat and live is None and live_path.is_file():
                live = _JsonlFollower(live_path)
                new_live = live.initial(chat_tail)
            else:
                new_live = live.poll() if live is not None else []
            updates = [(event, False) for event in lifecycle.poll()]
            updates.extend((event, True) for event in new_live)
            stopped = False
            for event, is_live in sorted(updates, key=_watch_sort_key):
                if is_live:
                    dashboard_state.apply_live(event)
                else:
                    dashboard_state.apply_lifecycle(event)
                _print_watch_event(
                    stream, event, live=is_live, raw_json=raw_json, chat=chat,
                    renderer=renderer,
                )
                if not is_live and event.get("kind") == "RUN_STOPPED":
                    stopped = True
            if stopped:
                if renderer is not None:
                    renderer.close()
                exit_message = (
                    f"{datetime.now().astimezone().strftime('%H:%M:%S')} "
                    "[监视器｜退出] 研究运行已结束。"
                ) if chat and not raw_json else "Run stopped; watcher exiting."
                if terminal_ui is not None:
                    terminal_ui.refresh(dashboard_lines())
                    terminal_ui.close()
                    print(
                        _colorize_human(exit_message) if use_color else exit_message,
                        file=base_stream, flush=True,
                    )
                else:
                    print(exit_message, file=stream, flush=True)
                return 0
            if updates:
                last_event_at = time.monotonic()
                next_heartbeat = last_event_at + heartbeat_seconds if heartbeat_seconds else float("inf")
                if terminal_ui is not None:
                    terminal_ui.refresh(dashboard_lines())
                continue
            now = time.monotonic()
            if input_changed and terminal_ui is not None:
                terminal_ui.refresh(dashboard_lines(int(now - last_event_at)))
            if now >= next_heartbeat:
                quiet = int(now - last_event_at)
                if terminal_ui is not None:
                    terminal_ui.refresh(dashboard_lines(quiet))
                else:
                    if renderer is not None:
                        renderer.close()
                    print(
                        f"{datetime.now().astimezone().strftime('%H:%M:%S')} [监视器｜在线] "
                        f"已连续 {quiet} 秒没有新的 controller/Agent 事件；研究进程状态暂未变化",
                        file=stream, flush=True,
                    )
                next_heartbeat = now + heartbeat_seconds
            time.sleep(poll_seconds)
    finally:
        if renderer is not None:
            renderer.close()
        if terminal_ui is not None:
            terminal_ui.close()
        lifecycle.close()
        if live is not None:
            live.close()
