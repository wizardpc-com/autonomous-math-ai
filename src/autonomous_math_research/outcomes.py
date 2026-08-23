from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable

from .catalog import write_semantic_index
from .contracts import job_lifecycle_metrics, mechanical_lifecycle_metrics
from .models import Claim, utc_now
from .storage import ProjectLayout, atomic_write_json, atomic_write_text, file_digest


IMPORTANT_EVENT_KINDS = {
    "SCHEMA_PREFLIGHT_FAILED",
    "BOOTSTRAP_FAILED",
    "DIRECTOR_PLAN_ACCEPTED",
    "DIRECTOR_STOP_DECLARED",
    "DIRECTOR_STOP_DEFERRED",
    "DIRECTOR_INCREMENTAL_LAUNCHED",
    "DIRECTOR_INCREMENTAL_SUGGESTIONS_DEFERRED",
    "DIRECTOR_RESULT_STALE_DISCARDED",
    "DIRECTOR_REJECTED",
    "DIRECTOR_CONTINUATION_REQUIRED",
    "CANDIDATE_PROCESSED",
    "CANDIDATE_REJECTED",
    "AUDIT_RECORDED",
    "AUDIT_RESULT_DOWNGRADED",
    "AUDIT_FAILURE_ISOLATED",
    "CANDIDATE_ARTIFACT_DRIFT",
    "TRUST_STATE_CHANGED",
    "CLAIM_CONFLICT_DETECTED",
    "FINAL_CONJECTURE_PROVED",
    "FINAL_CONJECTURE_REFUTED",
    "FINALIZATION_STARTED",
    "FINALIZATION_COMPLETED",
    "SCHEDULER_STOPPED",
    "CONTROLLER_ERROR",
    "INTERNAL_FAILURE_DRAIN_STARTED",
    "JOB_LIFECYCLE_INVARIANT_FAILED",
    "MECHANICAL_SUBTASK_REQUESTED",
    "MECHANICAL_SUBTASK_STARTED",
    "MECHANICAL_SUBTASK_LEASE_REATTACHED",
    "MECHANICAL_SUBTASK_FALLBACK",
    "MECHANICAL_ROUTE_UNAVAILABLE",
    "MECHANICAL_ROUTE_CACHE_PERSIST_FAILED",
    "MECHANICAL_SUBTASK_COMPLETED",
    "MECHANICAL_SUBTASK_FAILED",
    "MECHANICAL_BROKER_INTEGRITY_FAILURE",
    "MECHANICAL_LIFECYCLE_INVARIANT_FAILED",
}


def _short(value: Any, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _portable_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(project_root.resolve()):
        return resolved.relative_to(project_root.resolve()).as_posix()
    return str(resolved)


def _iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from sorted(item for item in path.rglob("*") if item.is_file())


def _artifact_references(
    project_root: Path,
    run_dir: Path,
    report_path: Path,
    jobs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    layout = ProjectLayout(project_root)
    candidates: set[Path] = set(_iter_files(run_dir))
    if report_path.is_file():
        candidates.add(report_path)
    mutable_logs = {
        (run_dir / "EVENTS.jsonl").resolve(),
        (run_dir / "LIVE_EVENTS.jsonl").resolve(),
    }

    raw_references: list[str] = []
    for job in jobs:
        raw_references.extend(str(item) for item in job.get("artifact_paths", []) or [])
        raw_references.extend(str(item) for item in job.get("artifacts", []) or [])
        result = job.get("result") or {}
        raw_references.extend(str(item) for item in result.get("artifact_paths", []) or [])
        raw_references.extend(str(item) for item in result.get("artifacts", []) or [])
        if result.get("report_path"):
            raw_references.append(str(result["report_path"]))
    for event in events:
        payload = event.get("payload") or {}
        if event.get("kind") == "CANDIDATE_PROCESSED" and payload.get("fingerprint"):
            fingerprint = str(payload["fingerprint"])
            alternatives = [
                layout.candidates_root / f"{fingerprint}.json",
                run_dir / "candidates" / f"{fingerprint}.json",
            ]
            existing = [path for path in alternatives if path.is_file()]
            raw_references.extend(str(path) for path in (existing or alternatives[:1]))
        if event.get("kind") == "AUDIT_RECORDED":
            fingerprint = str(payload.get("candidate_fingerprint") or "")
            audit_id = str(payload.get("audit_id") or "")
            if fingerprint and audit_id:
                alternatives = [
                    layout.audits_root / fingerprint / f"{audit_id}.json",
                    run_dir / "audits" / fingerprint / f"{audit_id}.json",
                ]
                existing = [path for path in alternatives if path.is_file()]
                raw_references.extend(str(path) for path in (existing or alternatives[:1]))

    skipped: list[dict[str, str]] = []
    project = project_root.resolve()
    for raw in raw_references:
        if raw.startswith("project://"):
            resolved = (project / raw.removeprefix("project://")).resolve()
        elif raw.startswith("epoch://"):
            tail = Path(raw.removeprefix("epoch://"))
            resolved = (run_dir.parent / tail).resolve()
        elif raw.startswith("campaign://"):
            tail = Path(raw.removeprefix("campaign://"))
            resolved = (layout.campaigns_root / tail).resolve()
        else:
            path = Path(raw)
            resolved = path.resolve() if path.is_absolute() else (project / path).resolve()
        if not resolved.is_relative_to(project):
            skipped.append({"path": str(resolved), "reason": "outside project boundary"})
            continue
        if not resolved.exists():
            skipped.append({"path": _portable_path(resolved, project), "reason": "not found at finalization"})
            continue
        candidates.update(_iter_files(resolved))

    records = []
    ordered = sorted(candidates, key=lambda item: str(item).casefold())
    total = sum(path.resolve() not in mutable_logs for path in ordered)
    interval = max(1, total // 20)
    completed = 0
    if progress is not None:
        progress({"stage": "hashing", "completed": 0, "total": total})
    for path in ordered:
        resolved = path.resolve()
        if resolved in mutable_logs:
            continue
        completed += 1
        try:
            if not resolved.is_relative_to(project):
                skipped.append({"path": str(resolved), "reason": "outside project boundary"})
                continue
            records.append({
                "path": _portable_path(resolved, project),
                "bytes": resolved.stat().st_size,
                "sha256": file_digest(resolved),
            })
        except OSError as exc:
            skipped.append({"path": str(path), "reason": f"unreadable at finalization: {exc}"})
        if progress is not None and (
            completed == total or completed % interval == 0
        ):
            progress({
                "stage": "hashing", "completed": completed, "total": total,
            })
    return records, skipped


def _event_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    kind = str(event.get("kind", "UNKNOWN"))
    if kind == "DIRECTOR_PLAN_ACCEPTED":
        return _short(payload.get("short_rationale") or payload.get("assessment"))
    if kind in {"CANDIDATE_PROCESSED", "CANDIDATE_REJECTED", "TRUST_STATE_CHANGED"}:
        detail = payload.get("reason") or payload.get("math_status") or payload.get("impact")
        return _short(f"{payload.get('claim_id', 'unknown')}: {detail or ''}")
    if kind == "AUDIT_RECORDED":
        return _short(
            f"{payload.get('candidate_fingerprint', 'unknown')}: "
            f"{payload.get('verdict', 'unknown')} / {payload.get('audit_kind', 'unknown')}"
        )
    return _short(
        payload.get("reason") or payload.get("error") or payload.get("claim_id") or payload
    )


def write_outcome_archive(
    *,
    project_root: Path,
    outcome_dir: Path,
    run_dir: Path,
    report_path: Path,
    run_id: str,
    execution_mode: str,
    run_outcome: str,
    stopped_reason: str,
    internal_failure: bool,
    jobs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    final_claim_id: str | None,
    final_claim: Claim | None,
    final_conjecture_proved: bool,
    final_conjecture_refuted: bool = False,
    mechanical_jobs: list[dict[str, Any]] | None = None,
    campaign_id: str | None = None,
    epoch_id: str | None = None,
    campaign_status: str | None = None,
    project_id: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Write a human review summary plus a hash index of preserved intermediates."""
    outcome_dir.mkdir(parents=True, exist_ok=True)
    display_project = project_id or project_root.name
    mechanical_jobs = list(mechanical_jobs or [])
    records, skipped = _artifact_references(
        project_root, run_dir, report_path, [*jobs, *mechanical_jobs], events,
        progress,
    )
    index_path = outcome_dir / "INTERMEDIATE_INDEX.json"
    atomic_write_json(index_path, {
        "schema_version": 2,
        "run_id": run_id,
        "generated_at": utc_now(),
        "project": display_project,
        "event_snapshot": {
            "last_sequence": int(events[-1].get("sequence", 0)) if events else 0,
            "append_only_logs": [
                _portable_path(run_dir / "EVENTS.jsonl", project_root),
                _portable_path(run_dir / "LIVE_EVENTS.jsonl", project_root),
            ],
            "logs_excluded_from_file_hashes": True,
        },
        "records": records,
        "skipped_references": skipped,
    })
    if progress is not None:
        progress({"stage": "semantic_index", "completed": 0, "total": 1})
    write_semantic_index(
        project_root=project_root,
        outcome_dir=outcome_dir,
        run_dir=run_dir,
        events=events,
    )
    if progress is not None:
        progress({"stage": "semantic_index", "completed": 1, "total": 1})

    event_counts = Counter(str(event.get("kind", "UNKNOWN")) for event in events)
    lifecycle = job_lifecycle_metrics(events)
    mechanical_lifecycle = mechanical_lifecycle_metrics(events)
    trusted = [
        event for event in events if event.get("kind") == "TRUST_STATE_CHANGED"
    ]
    rejected = [
        event for event in events if event.get("kind") == "CANDIDATE_REJECTED"
    ]
    timeline = [event for event in events if event.get("kind") in IMPORTANT_EVENT_KINDS]
    timeline_limit = 100
    shown_timeline = timeline[:timeline_limit]

    if final_claim is None:
        final_state = "未配置或 claim graph 中不存在"
        final_statement = "未记录"
    else:
        final_state = f"{final_claim.math_status} / {final_claim.trust_status} / {final_claim.evidence_level}"
        final_statement = final_claim.statement

    cost_records = [
        job for job in [*jobs, *mechanical_jobs] if not job.get("cache_reused")
    ]
    token_lower_bound = sum(
        int((job.get("token_usage") or {}).get("total_tokens", 0))
        for job in cost_records
    )
    unknown_telemetry = sum(
        str(job.get("token_telemetry") or "unknown") in {"unknown", "partial"}
        for job in cost_records
    )
    token_line = (
        f"记录 tokens 下界：{token_lower_bound}；{unknown_telemetry} 个 job telemetry unknown，不能断言为 0。"
        if unknown_telemetry else f"记录 tokens：{token_lower_bound}。"
    )
    result_boundary = (
        "- 本次是 failed real run；下列内容只是已持久化的 job、中间候选与错误摘要，"
        "不得据此声明新可信结果或自动晋升。"
        if execution_mode == "real" and internal_failure else None
    )

    job_lines = []
    for job in jobs[:200]:
        result = job.get("result") or {}
        finding = (
            result.get("main_finding") or result.get("short_rationale")
            or result.get("assessment") or result.get("verdict")
            or job.get("error") or job.get("exit_reason") or "无摘要"
        )
        evidence = result.get("evidence_level") or result.get("verified_evidence_level") or "未声明"
        job_lines.append(
            f"- `{job.get('role', 'unknown')}` / `{job.get('claim_id', 'unknown')}` / "
            f"`{job.get('status', 'unknown')}` / `{evidence}`：{_short(finding)}"
        )
    if len(jobs) > 200:
        job_lines.append(f"- 另有 {len(jobs) - 200} 个 job；完整记录见 `EVENTS.jsonl` 与 jobs 目录。")

    mechanical_lines = []
    for job in mechanical_jobs[:200]:
        result = job.get("result") or {}
        finding = (
            "; ".join(str(item) for item in result.get("key_findings", [])[:3])
            or job.get("error") or job.get("status") or "无摘要"
        )
        mechanical_lines.append(
            f"- parent `{job.get('parent_role', 'unknown')}` / "
            f"`{job.get('parent_job_id', 'unknown')}` → subtask "
            f"`{job.get('subtask_id', 'unknown')}` / `{job.get('status', 'unknown')}` / "
            f"model `{job.get('model') or '未启动'}`：{_short(finding)}"
        )
    if len(mechanical_jobs) > 200:
        mechanical_lines.append(
            f"- 另有 {len(mechanical_jobs) - 200} 个机械子工记录；完整内容见 EVENTS.jsonl。"
        )

    trust_lines = [
        f"- `{item.get('payload', {}).get('claim_id', 'unknown')}` → "
        f"`{item.get('payload', {}).get('math_status', 'unknown')}` / "
        f"`{item.get('payload', {}).get('trust_status', 'unknown')}` / "
        f"`{item.get('payload', {}).get('evidence_level', 'unknown')}`"
        for item in trusted
    ]
    reject_lines = [
        f"- `{item.get('payload', {}).get('claim_id', 'unknown')}`："
        f"{_short(item.get('payload', {}).get('reason', '未记录原因'))}"
        for item in rejected
    ]
    timeline_lines = [
        f"- #{item.get('sequence', '?')} `{item.get('kind', 'UNKNOWN')}`：{_event_detail(item)}"
        for item in shown_timeline
    ]
    if len(timeline) > timeline_limit:
        timeline_lines.append(
            f"- 另有 {len(timeline) - timeline_limit} 个主要事件；完整过程保留在 `EVENTS.jsonl`。"
        )

    relative_run = f"../../runs/{run_id}"
    relative_report = f"../../nightly/{run_id}/NIGHTLY_REPORT.md"
    lines = [
        f"# 自动化成果复核 — {run_id}",
        "",
        f"- 项目：`{display_project}`",
        f"- Campaign：`{campaign_id or run_id}`",
        f"- Epoch：`{epoch_id or run_id}`",
        f"- Campaign 状态：`{campaign_status or 'UNKNOWN'}`",
        f"- 执行模式：`{execution_mode}`",
        f"- 运行结果：`{run_outcome}`",
        f"- 内部失败：`{str(internal_failure).lower()}`",
        f"- 停止原因：{stopped_reason}",
        f"- {token_line}",
        (
            f"- jobs：started={lifecycle.jobs_started}，"
            f"terminal={lifecycle.jobs_terminal}（completed={lifecycle.jobs_completed}，"
            f"cancelled={lifecycle.jobs_cancelled}）。"
        ),
        (
            f"- mechanical subtasks：requested={mechanical_lifecycle.requested}，"
            f"attempts={mechanical_lifecycle.attempts_started}，"
            f"terminal={mechanical_lifecycle.terminal}。"
        ),
        "",
        "## 最终猜想状态",
        "",
        f"- 目标 claim：`{final_claim_id or '未配置'}`",
        f"- 精确陈述：{final_statement}",
        f"- 当前状态：`{final_state}`",
        f"- 最终证明是否触发收尾：`{str(final_conjecture_proved).lower()}`",
        f"- 最终反例是否触发收尾：`{str(final_conjecture_refuted).lower()}`",
        "- 只有 controller 审计门确认的最终 claim 才能触发收尾；worker 自述不计入。",
        "",
        "## 持久化 job 与中间成果摘要",
        "",
        *([result_boundary] if result_boundary else []),
        *(job_lines or ["- 未启动模型 job；本次没有模型研究成果。"]),
        "",
        "## 受控机械子工摘要",
        "",
        *(mechanical_lines or ["- 未请求机械子工。"]),
        "- 机械子工输出只是父角色可检查的执行证据；不会自行提升 claim 或形成审计 verdict。",
        "",
        "## 审计后信任变化",
        "",
        *(trust_lines or ["- 无新增审计通过的信任状态变化。"]),
        "",
        "## 被拒绝或未采纳的候选",
        "",
        *(reject_lines or ["- 无。"]),
        "",
        "## 大概过程",
        "",
        f"- Director 计划：{event_counts['DIRECTOR_PLAN_ACCEPTED']} 次；候选进入：{event_counts['CANDIDATE_PROCESSED']} 次；审计记录：{event_counts['AUDIT_RECORDED']} 次。",
        *(timeline_lines or ["- 无模型过程事件；见 dry-run/bootstrap 记录。"]),
        "",
        "## 中间步骤与材料",
        "",
        f"- [完整 append-only 事件]({relative_run}/EVENTS.jsonl)",
        f"- [实时消息与工具活动]({relative_run}/LIVE_EVENTS.jsonl)",
        f"- [固定运行配置与协议]({relative_run}/RUN_MANIFEST.json)",
        f"- [完整 nightly report]({relative_report})",
        "- `jobs/`、`candidates/`、`audits/`、`state/`、`policy/` 均保留在对应 run 目录或项目 autonomous 目录；不会因生成本摘要而删除或覆盖。",
        f"- `INTERMEDIATE_INDEX.json` 索引 {len(records)} 个最终可见不可变文件的路径、大小与 SHA-256；append-only 事件日志按 event watermark 单独标记。",
        "- `SEMANTIC_INDEX.json` 只整理事件关联的 job、candidate、audit、mechanical subtask 与 artifact；不递归复制 worktree。",
        *(
            [f"- 有 {len(skipped)} 个引用在收尾时缺失或越过项目边界；详见索引中的 `skipped_references`。"]
            if skipped else []
        ),
        "",
        "## 复核边界",
        "",
        "- mock/dry-run 结果只验证工程协议，不构成数学证明。",
        "- 未进入 `TRUST_STATE_CHANGED` 的 worker 结果均仍是候选或过程材料。",
        "- 中间材料的完整内容以索引所指向的原文件为准。",
        "",
    ]
    outcome_path = outcome_dir / "OUTCOME.md"
    atomic_write_text(outcome_path, "\n".join(lines))
    if progress is not None:
        progress({"stage": "outcome", "completed": 1, "total": 1})
    return outcome_path
