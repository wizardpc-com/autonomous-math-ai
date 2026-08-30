from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


# Bump this value whenever a wire schema, parser contract, or prompt contract
# changes.  It is persisted in run manifests so recovery never has to infer the
# protocol from whichever source file happens to be current.
OUTPUT_PROTOCOL_VERSION = 3

DIRECTOR_PLAN_KEYS = (
    "assessment",
    "spawn",
    "audit_priorities",
    "route_updates",
    "short_rationale",
)
WORKER_RESULT_KEYS = (
    "result_type",
    "main_finding",
    "status",
    "artifact_paths",
    "next_suggested_question",
    "evidence_level",
    "asset_usage",
)
AUDIT_RESULT_KEYS = (
    "verdict",
    "checks",
    "gaps",
    "notes",
    "verified_evidence_level",
)

LEGACY_DIRECTOR_PLAN_KEYS = (
    "assessment", "spawn", "prune_suggestions", "pause_suggestions",
    "priority_changes", "audit_requests", "diversification_decision",
    "run_decision", "short_rationale",
)
LEGACY_WORKER_RESULT_KEYS = (
    "result_type", "claim_id", "main_finding", "status", "impact",
    "artifact_paths", "next_suggested_question", "evidence_level",
)
LEGACY_AUDIT_RESULT_KEYS = (
    "audit_id", "candidate_fingerprint", "auditor_thread_id", "verdict",
    "audit_kind", "statement_checked", "checks", "gaps", "notes",
    "verified_evidence_level", "report_path", "timestamp",
)

OUTPUT_CONTRACT_KEYS = {
    "director_plan.schema.json": DIRECTOR_PLAN_KEYS,
    "worker_result.schema.json": WORKER_RESULT_KEYS,
    "audit_result.schema.json": AUDIT_RESULT_KEYS,
}

SUCCESS_JOB_STATUSES = frozenset({"completed", "success", "succeeded"})

# Retry categories are deliberately disjoint. Local/bootstrap failures never
# consume a retry; transient protocol failures and model/role protocol failures
# have independent finite budgets.
LOCAL_STRUCTURAL_FAILURES = frozenset({
    "invalid_output_schema",
    "bootstrap_failure",
    "canonical_guard",
    "service_tier_policy",
    "model_route_policy",
})
TRANSIENT_FAILURES = frozenset({
    "transport_transient",
    "protocol_transient",  # compatibility with already persisted runs
    "rate_limit",
})
MODEL_PROTOCOL_FAILURES = frozenset({
    "model_output_protocol",
    "model_output_validation",
    "role_semantic_validation",
    "director_no_runnable_work",
    # compatibility with older event names and controller-local validators
    "output_protocol",
    "output_validation",
    "invalid_director_result",
    "invalid_audit_result",
})


def contract_name(schema_path: str | Path) -> str | None:
    rendered = str(schema_path).replace("\\", "/")
    for name in OUTPUT_CONTRACT_KEYS:
        if name in rendered:
            return name
    return None


def render_contract_keys(keys: Iterable[str]) -> str:
    return ", ".join(f"`{key}`" for key in keys)


@dataclass(frozen=True, slots=True)
class JobLifecycleMetrics:
    jobs_started: int
    jobs_completed: int
    jobs_cancelled: int
    jobs_terminal: int
    active_job_ids: tuple[str, ...]
    duplicate_terminal_job_ids: tuple[str, ...]
    orphan_terminal_job_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobs_started": self.jobs_started,
            "jobs_completed": self.jobs_completed,
            "jobs_cancelled": self.jobs_cancelled,
            "jobs_terminal": self.jobs_terminal,
            "active_job_ids": list(self.active_job_ids),
            "duplicate_terminal_job_ids": list(self.duplicate_terminal_job_ids),
            "orphan_terminal_job_ids": list(self.orphan_terminal_job_ids),
        }


def job_lifecycle_metrics(events: Iterable[dict[str, Any]]) -> JobLifecycleMetrics:
    """Rebuild authoritative job counts from append-only lifecycle events.

    A job is terminal exactly once, at the first JOB_COMPLETED or JOB_CANCELLED
    event. Duplicate terminal events are retained as anomalies instead of being
    silently double-counted.
    """
    started: set[str] = set()
    completed: set[str] = set()
    cancelled: set[str] = set()
    first_terminal: dict[str, str] = {}
    duplicate_terminal: set[str] = set()
    for event in events:
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        job_id = str(payload.get("job_id") or "")
        if not job_id:
            continue
        if kind == "JOB_STARTED":
            started.add(job_id)
        elif kind in {"JOB_COMPLETED", "JOB_CANCELLED"}:
            if job_id in first_terminal:
                duplicate_terminal.add(job_id)
                continue
            first_terminal[job_id] = kind
            if kind == "JOB_COMPLETED":
                completed.add(job_id)
            else:
                cancelled.add(job_id)
    terminal = set(first_terminal)
    return JobLifecycleMetrics(
        jobs_started=len(started),
        jobs_completed=len(completed),
        jobs_cancelled=len(cancelled),
        jobs_terminal=len(terminal),
        active_job_ids=tuple(sorted(started - terminal)),
        duplicate_terminal_job_ids=tuple(sorted(duplicate_terminal)),
        orphan_terminal_job_ids=tuple(sorted(terminal - started)),
    )


@dataclass(frozen=True, slots=True)
class MechanicalLifecycleMetrics:
    requested: int
    attempts_started: int
    completed: int
    failed: int
    terminal: int
    active_subtasks: tuple[str, ...]
    duplicate_terminal_subtasks: tuple[str, ...]
    orphan_terminal_subtasks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "attempts_started": self.attempts_started,
            "completed": self.completed,
            "failed": self.failed,
            "terminal": self.terminal,
            "active_subtasks": list(self.active_subtasks),
            "duplicate_terminal_subtasks": list(self.duplicate_terminal_subtasks),
            "orphan_terminal_subtasks": list(self.orphan_terminal_subtasks),
        }


def mechanical_lifecycle_metrics(
    events: Iterable[dict[str, Any]],
) -> MechanicalLifecycleMetrics:
    requested: set[str] = set()
    started_attempts: set[str] = set()
    first_terminal: dict[str, str] = {}
    duplicate: set[str] = set()
    completed: set[str] = set()
    failed: set[str] = set()

    def key(payload: dict[str, Any]) -> str:
        parent = str(payload.get("parent_job_id") or "")
        subtask = str(payload.get("subtask_id") or "")
        return f"{parent}/{subtask}" if parent and subtask else ""

    for event in events:
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        subtask_key = key(payload)
        if not subtask_key:
            continue
        if kind == "MECHANICAL_SUBTASK_REQUESTED":
            requested.add(subtask_key)
        elif kind == "MECHANICAL_SUBTASK_STARTED":
            attempt_id = str(payload.get("mechanical_job_id") or "")
            if attempt_id:
                started_attempts.add(attempt_id)
        elif kind in {"MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED"}:
            if subtask_key in first_terminal:
                duplicate.add(subtask_key)
                continue
            first_terminal[subtask_key] = kind
            if kind == "MECHANICAL_SUBTASK_COMPLETED":
                completed.add(subtask_key)
            else:
                failed.add(subtask_key)
    terminal = set(first_terminal)
    return MechanicalLifecycleMetrics(
        requested=len(requested),
        attempts_started=len(started_attempts),
        completed=len(completed),
        failed=len(failed),
        terminal=len(terminal),
        active_subtasks=tuple(sorted(requested - terminal)),
        duplicate_terminal_subtasks=tuple(sorted(duplicate)),
        orphan_terminal_subtasks=tuple(sorted(terminal - requested)),
    )
