from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .claim_graph import ClaimGraph
from .contracts import job_lifecycle_metrics, mechanical_lifecycle_metrics
from .models import MathStatus, TrustStatus
from .storage import atomic_write_text


def render_nightly_report(
    *,
    run_id: str,
    graph: ClaimGraph,
    events: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    stopped_reason: str,
    mechanical_jobs: list[dict[str, Any]] | None = None,
    capability_snapshot: dict[str, Any] | None = None,
    policy_manifest: dict[str, Any] | None = None,
    policy_status: dict[str, Any] | None = None,
    promotion_allowed: bool = True,
    execution_mode: str = "real",
    run_outcome: str = "completed real run",
    internal_failure: bool = False,
    final_claim_id: str | None = None,
    final_conjecture_proved: bool = False,
    final_conjecture_refuted: bool = False,
    campaign_id: str | None = None,
    epoch_id: str | None = None,
    campaign_status: str | None = None,
) -> str:
    mechanical_jobs = list(mechanical_jobs or [])
    lifecycle = job_lifecycle_metrics(events)
    mechanical_lifecycle = mechanical_lifecycle_metrics(events)
    claims = list(graph.claims.values())
    trust_changes = [e for e in events if e.get("kind") == "TRUST_STATE_CHANGED"]
    trusted_ids = {
        str(e.get("payload", {}).get("claim_id")) for e in trust_changes
        if e.get("payload", {}).get("trust_status") in {TrustStatus.AUDITED_NIGHTLY, TrustStatus.FORMALLY_VERIFIED}
    }
    rejected_candidates = [e for e in events if e.get("kind") == "CANDIDATE_REJECTED"]
    recovered_candidates = [e for e in events if e.get("kind") == "CANDIDATE_RESCUED_FROM_RUN"]
    trusted_new = [c for c in claims if c.claim_id in trusted_ids]
    failed = [c for c in claims if c.claim_id in trusted_ids and c.math_status == MathStatus.FAILED]
    computation = [c for c in claims if c.math_status == MathStatus.COMPUTATION_ONLY]
    plausible = [c for c in claims if c.trust_status in {TrustStatus.UNTRUSTED_CANDIDATE, TrustStatus.AUDIT_PENDING, TrustStatus.AUDIT_1_PASS}]
    open_claims = [c for c in claims if c.math_status in {MathStatus.OPEN, MathStatus.REDUCED_TO, MathStatus.PLAUSIBLE}]
    pruned = [e for e in events if e.get("kind") == "DEPENDENCY_PRUNED"]
    tokens_by_role: dict[str, int] = defaultdict(int)
    useful_by_role: Counter[str] = Counter()
    tokens_by_model: dict[str, int] = defaultdict(int)
    observed_service_tiers: Counter[str] = Counter()
    mechanical_model_attestations: Counter[str] = Counter()
    telemetry_states: Counter[str] = Counter()
    unknown_telemetry_by_role: Counter[str] = Counter()
    unknown_telemetry_by_model: Counter[str] = Counter()
    start_times: list[datetime] = []
    end_times: list[datetime] = []
    all_cost_records = [*jobs, *mechanical_jobs]
    cost_records = [job for job in all_cost_records if not job.get("cache_reused")]
    for job in cost_records:
        role = str(job.get("role", "unknown"))
        usage = job.get("token_usage") or {}
        counted_tokens = int(usage.get("total_tokens", 0))
        tokens_by_role[role] += counted_tokens
        actual_model = job.get("actual_model")
        requested_model = job.get("requested_model") or job.get("model")
        model = str(
            actual_model
            or (
                f"requested:{requested_model} (actual unobservable)"
                if requested_model and role == "mechanical_subworker"
                else requested_model or "unknown"
            )
        )
        tokens_by_model[model] += counted_tokens
        if role == "mechanical_subworker":
            mechanical_model_attestations[
                str(job.get("model_route_attestation") or "unobservable")
            ] += 1
        observed_service_tiers[str(job.get("observed_service_tier") or "unobservable")] += 1
        telemetry_state = str(job.get("token_telemetry") or "unknown")
        telemetry_states[telemetry_state] += 1
        if telemetry_state == "unknown":
            unknown_telemetry_by_role[role] += 1
            unknown_telemetry_by_model[model] += 1
        if job.get("useful") or (
            role == "mechanical_subworker"
            and job.get("status") not in {"TOOL_ERROR", "REJECTED", "BLOCKED"}
            and not job.get("cache_reused")
        ):
            useful_by_role[role] += 1
        try:
            if job.get("start_time"):
                start_times.append(datetime.fromisoformat(str(job["start_time"]).replace("Z", "+00:00")))
            if job.get("end_time"):
                end_times.append(datetime.fromisoformat(str(job["end_time"]).replace("Z", "+00:00")))
        except ValueError:
            pass
    total_tokens = sum(tokens_by_role.values())
    wall_seconds = (
        max(0.0, (max(end_times) - min(start_times)).total_seconds())
        if start_times and end_times else 0.0
    )

    def claim_lines(items: list[Any], empty: str = "- 无") -> str:
        if not items:
            return empty
        return "\n".join(
            f"- `{c.claim_id}` — {c.statement}"
            f"（{c.math_status} / {c.trust_status} / {c.evidence_level}）"
            for c in items
        )

    graph_changes = [e for e in events if e.get("kind") in {"TRUST_STATE_CHANGED", "DEPENDENCY_PRUNED", "PRIORITY_CHANGED"}]
    seeds = [e for e in events if e.get("kind") == "DIRECTOR_PLAN_ACCEPTED"]
    promotable = [
        claim for claim in trusted_new
        if claim.math_status != MathStatus.COMPUTATION_ONLY
    ] if promotion_allowed else []
    trusted_display = trusted_new if promotion_allowed else []
    failed_display = failed if promotion_allowed else []
    forbidden = plausible + computation + ([] if promotion_allowed else trusted_new)
    capability_note = "未记录"
    if capability_snapshot:
        supported = sorted(key for key, value in capability_snapshot.items() if isinstance(value, dict) and value.get("supported"))
        capability_note = ", ".join(supported) or "无已确认端点"
    if promotion_allowed:
        no_trusted_message = "- 无；本次运行没有新增通过审计的可信结果。"
        no_failed_message = "- 无；本次运行没有新增通过审计的严格否定。"
        no_promotion_message = "- 无；没有满足人工晋升条件的新结果。"
    elif execution_mode in {"mock", "smoke", "dry-run"}:
        no_trusted_message = f"- 无；{execution_mode} validation 不产生数学可信结果。"
        no_failed_message = f"- 无；{execution_mode} validation 不产生正式否定。"
        no_promotion_message = f"- 无；{execution_mode} 结果不得晋升。"
    else:
        no_trusted_message = "- 无；本次真实运行失败，未形成可声明的新可信结果。"
        no_failed_message = "- 无；本次真实运行失败，未形成可声明的严格否定。"
        no_promotion_message = "- 无；失败的真实运行不得自动晋升结果。"
    if not cost_records:
        telemetry_summary = "未启动模型 turn；token telemetry 不适用"
    else:
        rendered_states = ", ".join(
            f"{state}={count}" for state, count in sorted(telemetry_states.items())
        )
        telemetry_summary = rendered_states or "unknown"
    token_total_label = (
        f"记录总 tokens：{total_tokens}"
        if not telemetry_states.get("unknown")
        else f"记录 tokens 下界：{total_tokens}（{telemetry_states['unknown']} 个 job 未观察到 telemetry，不能断言为 0）"
    )

    def role_token_line(role: str, tokens: int) -> str:
        if unknown_telemetry_by_role[role]:
            return (
                f"- {role}: 记录 token 下界 {tokens}"
                f"（{unknown_telemetry_by_role[role]} 个 job telemetry unknown）"
            )
        return f"- {role}: {tokens} tokens"

    def model_token_line(model: str, tokens: int) -> str:
        if unknown_telemetry_by_model[model]:
            return (
                f"- model `{model}`: 记录 token 下界 {tokens}"
                f"（{unknown_telemetry_by_model[model]} 个 job telemetry unknown）"
            )
        return f"- model `{model}`: {tokens} tokens"

    def efficiency_line(role: str, tokens: int) -> str:
        useful = useful_by_role[role]
        if unknown_telemetry_by_role[role]:
            return (
                f"- {role}: {useful} 个 useful outcome / token telemetry unknown"
                f"（记录下界 {tokens}）"
            )
        if useful:
            return (
                f"- {role}: {useful} 个 useful outcome / {tokens} tokens"
                f"（{(tokens / useful):.1f} tokens/useful）"
            )
        return f"- {role}: 0 个 useful outcome / {tokens} tokens"
    lines = [
        f"# Autonomous Math AI Nightly Report — {run_id}",
        "",
        f"执行模式：{execution_mode}",
        f"运行结果：{run_outcome}",
        f"内部失败：{internal_failure}",
        f"Campaign：{campaign_id or run_id}",
        f"Epoch：{epoch_id or run_id}",
        f"Campaign 状态：{campaign_status or 'UNKNOWN'}",
        "",
        f"停止原因：{stopped_reason}",
        "",
        "## 【最终猜想与收尾状态】", "",
        f"- 最终目标 claim：`{final_claim_id or '未配置'}`",
        f"- 审计确认最终证明并触发有序收尾：{final_conjecture_proved}",
        f"- 审计确认最终反例并触发有序收尾：{final_conjecture_refuted}",
        "- worker 自述不会触发最终收尾；只接受 controller 审计门后的 claim graph 状态。",
        "",
        "## 【本夜审计后可相信的新结果】", "",
        claim_lines(trusted_display, no_trusted_message), "",
        "## 【已严格否定】", "",
        claim_lines(failed_display, no_failed_message), "",
        "## 【仅计算支持】", "", claim_lines(computation), "",
        "## 【PLAUSIBLE / 未审计】", "", claim_lines(plausible), "",
        "## 【被拒绝的 candidate】", "",
        *(
            f"- `{item.get('payload', {}).get('claim_id', 'unknown')}` / "
            f"`{item.get('payload', {}).get('fingerprint', 'unknown')}` — "
            f"{item.get('payload', {}).get('reason', 'rejected by audit gate')}"
            for item in rejected_candidates
        ),
        *(["- 无"] if not rejected_candidates else []), "",
        "## 【从旧协议 run 恢复的 candidate】", "",
        *(
            f"- `{item.get('payload', {}).get('claim_id', 'unknown')}` ← parent "
            f"`{item.get('payload', {}).get('parent_claim_id', 'unknown')}`，source run "
            f"`{item.get('payload', {}).get('source_run_id', 'unknown')}`；按 untrusted derived claim 重新审计"
            for item in recovered_candidates
        ),
        *(["- 无"] if not recovered_candidates else []), "",
        "## 【被剪枝的路线】", "",
        *(f"- {item['payload']}" for item in pruned),
        *( ["- 无"] if not pruned else [] ), "",
        "## 【仍然开放的核心 gap】", "", claim_lines(open_claims), "",
        "## 【claim graph 变化】", "",
        *(f"- `{item['kind']}`: {item['payload']}" for item in graph_changes),
        *( ["- 无"] if not graph_changes else [] ), "",
        "## 【最有价值的新 research seeds】", "",
        *(f"- {item['payload'].get('short_rationale', 'fresh Director plan')}" for item in seeds[-5:]),
        *( ["- 无"] if not seeds else [] ), "",
        "## 【token / model / runtime 使用】", "",
        f"- {token_total_label}",
        f"- token telemetry：{telemetry_summary}",
        f"- 记录 wall runtime：{wall_seconds:.2f} 秒",
        *(role_token_line(role, count) for role, count in sorted(tokens_by_role.items())),
        *(model_token_line(model, count) for model, count in sorted(tokens_by_model.items())),
        f"- App Server 已确认能力：{capability_note}", "",
        "## Research policy", "",
        f"- policy：{(policy_manifest or {}).get('policy_name', '未记录')}",
        f"- manifest SHA-256：{(policy_manifest or {}).get('manifest_sha256', '未记录')}",
        f"- stable core：{(policy_manifest or {}).get('stable_core', '未记录')}",
        f"- source drift：{bool((policy_status or {}).get('source_drift', False))}",
        "- requested service tier：null（App Server request 显式清除 tier override）",
        "- one-shot mechanical route：Spark/high/null；仅永久 unavailable/access denied 时切换 Luna/medium/null。",
        "- mechanical actual-model attestation：" + (
            ", ".join(
                f"{state} ({count} jobs)"
                for state, count in sorted(mechanical_model_attestations.items())
            )
            if mechanical_model_attestations
            else "unobservable（没有 mechanical job）"
        ),
        "- mechanical worker 结果只是机械执行证据，不自动构成证明或独立审计 verdict。",
        "- observed service tier：" + (
            ", ".join(f"{tier} ({count} jobs)" for tier, count in sorted(observed_service_tiers.items()))
            if observed_service_tiers else "unobservable（没有 job telemetry）"
        ), "",
        "## 【每单位成本产生的信息】", "",
        *(efficiency_line(role, tokens) for role, tokens in sorted(tokens_by_role.items())),
        *( ["- 无可计算记录"] if not tokens_by_role else [] ), "",
        "## 【建议晋升正式项目的结果】", "",
        claim_lines(promotable, no_promotion_message), "",
        "## 【禁止晋升的结果】", "",
        claim_lines(forbidden, "" if rejected_candidates else "- 无"),
        *(
            f"- rejected candidate `{item.get('payload', {}).get('fingerprint', 'unknown')}`"
            for item in rejected_candidates
        ), "",
        "## 【下一轮建议】", "",
        "- 由 fresh Director 读取本报告与 compact snapshot 后重新评估；不要从未审计 candidate 直接改写 canonical claims/proofs/state。",
        "",
        "## Run trace", "",
        f"- `EVENTS.jsonl` 保存 {len(events)} 个 append-only 状态转换。",
        (
            f"- jobs：started={lifecycle.jobs_started}，"
            f"terminal={lifecycle.jobs_terminal}（completed={lifecycle.jobs_completed}，"
            f"cancelled={lifecycle.jobs_cancelled}）；计数统一由 lifecycle events 重建。"
        ),
        (
            f"- mechanical subtasks：requested={mechanical_lifecycle.requested}，"
            f"attempts_started={mechanical_lifecycle.attempts_started}，"
            f"terminal={mechanical_lifecycle.terminal}（completed={mechanical_lifecycle.completed}，"
            f"failed={mechanical_lifecycle.failed}）。"
        ),
        f"- 保存 {len(jobs)} 个 terminal job 记录；完整推导与计算应留在各 job artifact 目录。",
        f"- 保存 {len(mechanical_jobs)} 个 mechanical subtask terminal 记录及其父任务关联。",
        "",
    ]
    return "\n".join(lines)


def write_report(path: Path, **kwargs: Any) -> Path:
    atomic_write_text(path, render_nightly_report(**kwargs))
    return path
