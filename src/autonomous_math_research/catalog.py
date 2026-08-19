from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Iterable

from .models import utc_now
from .monitor import build_status, load_events
from .storage import ProjectLayout, atomic_write_json, atomic_write_text, file_digest


SEMANTIC_INDEX_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1

JOB_TERMINAL_KINDS = {"JOB_COMPLETED", "JOB_CANCELLED"}
MECHANICAL_TERMINAL_KINDS = {
    "MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED",
}
MECHANICAL_EVENT_PREFIX = "MECHANICAL_SUBTASK_"
TOKEN_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
SENSITIVE_FILE_NAMES = {
    "credentials", "credentials.json", "id_rsa", "id_ed25519",
    "known_hosts", "netrc", ".netrc",
}

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)


def _safe_text(value: Any, limit: int = 500) -> str | None:
    if value is None or value == "":
        return None
    text = " ".join(str(value).split())
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _token_usage(value: Any) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    return {key: _safe_int(raw.get(key)) for key in TOKEN_KEYS}


def _portable_path(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    project = project_root.resolve()
    if not resolved.is_relative_to(project):
        raise ValueError(f"path escapes project boundary: {resolved}")
    return resolved.relative_to(project).as_posix()


def _is_sensitive_path(path: Path) -> bool:
    lowered = [part.casefold() for part in path.parts]
    if any(part in {".git", ".codex", ".ssh"} for part in lowered):
        return True
    name = path.name.casefold()
    return (
        name in SENSITIVE_FILE_NAMES
        or name.startswith(".env")
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _reference_values(payload: dict[str, Any]) -> Iterable[str]:
    for field in ("artifact_paths", "artifacts"):
        values = payload.get(field) or []
        if isinstance(values, (str, Path, dict)):
            values = [values]
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, (str, Path)):
                yield str(item)
            elif isinstance(item, dict):
                raw = (
                    item.get("path") or item.get("artifact_path")
                    or item.get("report_path")
                )
                if raw:
                    yield str(raw)
    if payload.get("report_path"):
        yield str(payload["report_path"])
    result = payload.get("result")
    if isinstance(result, dict):
        yield from _reference_values(result)


class _ArtifactCollector:
    """Collect explicit semantic references without crawling historical worktrees."""

    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self._records: dict[str, dict[str, Any]] = {}
        self._skipped: list[dict[str, str]] = []

    def add(self, raw: str | Path, *, relation: str, base: Path | None = None) -> str | None:
        text = str(raw).strip()
        if not text:
            return None
        try:
            candidate = Path(text)
            if candidate.is_absolute():
                resolved = candidate.resolve()
            else:
                root = (base or self.project_root).resolve()
                resolved = (root / candidate).resolve()
                if not resolved.exists() and root != self.project_root:
                    project_candidate = (self.project_root / candidate).resolve()
                    if project_candidate.exists():
                        resolved = project_candidate
        except (OSError, ValueError):
            self._skipped.append({
                "reference": "<invalid-path>", "relation": relation,
                "reason": "invalid or unreadable path",
            })
            return None
        if not resolved.is_relative_to(self.project_root):
            self._skipped.append({
                "reference": f"<outside-project>/{candidate.name}",
                "relation": relation,
                "reason": "outside project boundary",
            })
            return None
        portable = _portable_path(resolved, self.project_root)
        if _is_sensitive_path(resolved):
            self._skipped.append({
                "reference": portable, "relation": relation,
                "reason": "sensitive path excluded",
            })
            return None
        try:
            record = self._records.setdefault(portable, {
                "path": portable,
                "exists": resolved.exists(),
                "kind": "missing",
                "relations": set(),
            })
            record["relations"].add(relation)
            if resolved.is_file():
                record.update({
                    "exists": True,
                    "kind": "file",
                    "bytes": resolved.stat().st_size,
                    "sha256": file_digest(resolved),
                })
            elif resolved.is_dir():
                record.update({"exists": True, "kind": "directory"})
        except OSError:
            usable = record.get("kind") in {"file", "directory"}
            if not usable:
                self._records.pop(portable, None)
            self._skipped.append({
                "reference": portable, "relation": relation,
                "reason": "unreadable while indexing",
            })
            return portable if usable else None
        return portable

    @property
    def records(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in self._records.values():
            row = dict(record)
            row["relations"] = sorted(row["relations"])
            result.append(row)
        return sorted(result, key=lambda row: str(row["path"]).casefold())

    @property
    def skipped(self) -> list[dict[str, str]]:
        return sorted(
            self._skipped,
            key=lambda row: (row["reference"].casefold(), row["relation"]),
        )


def _base_path(payload: dict[str, Any], project_root: Path) -> Path:
    raw = payload.get("cwd") or payload.get("workspace")
    if not raw:
        return project_root
    try:
        candidate = Path(str(raw))
        resolved = (
            candidate.resolve()
            if candidate.is_absolute() else (project_root / candidate).resolve()
        )
    except (OSError, ValueError):
        return project_root
    return resolved if resolved.is_relative_to(project_root.resolve()) else project_root


def _add_payload_artifacts(
    collector: _ArtifactCollector,
    payload: dict[str, Any],
    *,
    relation: str,
    project_root: Path,
) -> list[str]:
    base = _base_path(payload, project_root)
    paths = {
        portable
        for raw in _reference_values(payload)
        if (portable := collector.add(raw, relation=relation, base=base)) is not None
    }
    return sorted(paths, key=str.casefold)


def _run_times(events: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    started = next(
        (str(event.get("timestamp")) for event in events if event.get("kind") == "RUN_STARTED"),
        None,
    )
    stopped = next(
        (
            str(event.get("timestamp")) for event in reversed(events)
            if event.get("kind") == "RUN_STOPPED"
        ),
        None,
    )
    return started, stopped


def build_semantic_index(
    *,
    project_root: Path,
    run_dir: Path,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a derived, bounded index without mutating any run source file."""
    project = project_root.resolve()
    run = run_dir.resolve()
    layout = ProjectLayout(project)
    runs_root = layout.runs_root.resolve()
    if not run.is_relative_to(runs_root):
        raise ValueError(f"run directory escapes project runs boundary: {run}")
    event_path = run / "EVENTS.jsonl"
    if not event_path.is_file():
        raise ValueError(f"run has no EVENTS.jsonl: {run.name}")
    records = list(events) if events is not None else load_events(event_path)
    foreign_run_ids = sorted({
        str(event.get("run_id"))
        for event in records
        if event.get("run_id") and str(event.get("run_id")) != run.name
    })
    if foreign_run_ids:
        raise ValueError(
            f"event stream for {run.name} contains foreign run ids: {foreign_run_ids}"
        )
    status = build_status(run, records, include_live=False)
    collector = _ArtifactCollector(project)

    jobs: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    trust_changes: list[dict[str, Any]] = []
    mechanical: dict[tuple[str, str], dict[str, Any]] = {}

    for event in records:
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        sequence = _safe_int(event.get("sequence"))
        timestamp = str(event.get("timestamp") or "") or None

        if kind == "JOB_STARTED" and payload.get("job_id"):
            job_id = str(payload["job_id"])
            jobs[job_id] = {
                "job_id": job_id,
                "task_id": payload.get("task_id"),
                "role": payload.get("role"),
                "claim_id": payload.get("claim_id"),
                "status": "NONTERMINAL",
                "model": payload.get("model"),
                "reasoning_effort": payload.get("reasoning_effort"),
                "requested_service_tier": payload.get("requested_service_tier"),
                "started_at": payload.get("start_time") or timestamp,
                "started_sequence": sequence,
                "thread_id": None,
                "turn_id": None,
                "artifact_paths": [],
                "token_telemetry": "unknown",
                "token_usage": _token_usage({}),
            }
            continue

        if kind == "JOB_BOUND" and payload.get("job_id"):
            job_id = str(payload["job_id"])
            job = jobs.setdefault(job_id, {"job_id": job_id, "status": "NONTERMINAL"})
            job["thread_id"] = payload.get("thread_id")
            if payload.get("turn_id"):
                job["turn_id"] = payload.get("turn_id")
            continue

        if kind in JOB_TERMINAL_KINDS and payload.get("job_id"):
            job_id = str(payload["job_id"])
            job = jobs.setdefault(job_id, {"job_id": job_id})
            job.update({
                "task_id": payload.get("task_id", job.get("task_id")),
                "role": payload.get("role", job.get("role")),
                "claim_id": payload.get("claim_id", job.get("claim_id")),
                "status": str(
                    payload.get("status")
                    or ("CANCELLED" if kind == "JOB_CANCELLED" else "UNKNOWN")
                ),
                "model": payload.get("model", job.get("model")),
                "reasoning_effort": payload.get(
                    "reasoning_effort", job.get("reasoning_effort")
                ),
                "requested_service_tier": payload.get(
                    "requested_service_tier", job.get("requested_service_tier")
                ),
                "observed_service_tier": payload.get("observed_service_tier"),
                "thread_id": payload.get("thread_id") or job.get("thread_id"),
                "turn_id": payload.get("turn_id") or job.get("turn_id"),
                "failure_kind": payload.get("failure_kind"),
                "retryable": payload.get("retryable"),
                "completed_at": payload.get("end_time") or timestamp,
                "terminal_sequence": sequence,
                "token_telemetry": str(payload.get("token_telemetry") or "unknown"),
                "token_usage": _token_usage(payload.get("token_usage")),
                "artifact_paths": _add_payload_artifacts(
                    collector, payload, relation=f"job:{job_id}", project_root=project,
                ),
            })
            continue

        if kind in {"CANDIDATE_PROCESSED", "CANDIDATE_REJECTED"}:
            fingerprint = str(payload.get("fingerprint") or "")
            row = {
                "event_id": payload.get("event_id"),
                "fingerprint": fingerprint or None,
                "claim_id": payload.get("claim_id"),
                "parent_claim_id": payload.get("parent_claim_id"),
                "status": "PROCESSED" if kind == "CANDIDATE_PROCESSED" else "REJECTED",
                "impact": payload.get("impact"),
                "proposed_evidence_level": payload.get("proposed_evidence_level"),
                "source_run_id": payload.get("source_run_id"),
                "sequence": sequence,
                "timestamp": timestamp,
                "reason_present": bool(payload.get("reason")),
                "artifact_paths": [],
            }
            if fingerprint:
                alternatives = (
                    layout.candidates_root / f"{fingerprint}.json",
                    run / "candidates" / f"{fingerprint}.json",
                )
                selected = next((path for path in alternatives if path.is_file()), alternatives[0])
                portable = collector.add(selected, relation=f"candidate:{fingerprint}")
                if portable:
                    row["artifact_paths"].append(portable)
            candidates.append(row)
            continue

        if kind == "AUDIT_RECORDED":
            audit_id = str(payload.get("audit_id") or "")
            fingerprint = str(payload.get("candidate_fingerprint") or "")
            row = {
                "audit_id": audit_id or None,
                "candidate_fingerprint": fingerprint or None,
                "audit_kind": payload.get("audit_kind"),
                "verdict": payload.get("verdict"),
                "trust_status": payload.get("trust_status"),
                "verified_evidence_level": payload.get("verified_evidence_level"),
                "sequence": sequence,
                "timestamp": timestamp,
                "artifact_paths": [],
            }
            if payload.get("report_path"):
                portable = collector.add(
                    str(payload["report_path"]), relation=f"audit:{audit_id or fingerprint}",
                    base=project,
                )
                if portable:
                    row["artifact_paths"].append(portable)
            if audit_id and fingerprint:
                alternatives = (
                    layout.audits_root / fingerprint / f"{audit_id}.json",
                    run / "audits" / fingerprint / f"{audit_id}.json",
                )
                selected = next((path for path in alternatives if path.is_file()), alternatives[0])
                portable = collector.add(selected, relation=f"audit:{audit_id}")
                if portable and portable not in row["artifact_paths"]:
                    row["artifact_paths"].append(portable)
            row["artifact_paths"].sort(key=str.casefold)
            audits.append(row)
            continue

        if kind == "TRUST_STATE_CHANGED":
            trust_changes.append({
                "claim_id": payload.get("claim_id"),
                "math_status": payload.get("math_status"),
                "trust_status": payload.get("trust_status"),
                "evidence_level": payload.get("evidence_level"),
                "sequence": sequence,
                "timestamp": timestamp,
            })
            continue

        if kind.startswith(MECHANICAL_EVENT_PREFIX):
            parent_job_id = str(payload.get("parent_job_id") or "")
            subtask_id = str(payload.get("subtask_id") or payload.get("task_id") or "")
            if not parent_job_id or not subtask_id:
                continue
            key = (parent_job_id, subtask_id)
            item = mechanical.setdefault(key, {
                "parent_job_id": parent_job_id,
                "parent_task_id": payload.get("parent_task_id"),
                "parent_role": payload.get("parent_role"),
                "subtask_id": subtask_id,
                "status": "REQUESTED",
                "requested_sequence": sequence,
                "requested_at": timestamp,
                "attempts": [],
                "artifact_paths": [],
                "token_telemetry": "unknown",
                "token_usage": _token_usage({}),
            })
            if kind in {"MECHANICAL_SUBTASK_STARTED", "MECHANICAL_SUBTASK_LEASE_REATTACHED"}:
                item["attempts"].append({
                    "event": kind,
                    "attempt": _safe_int(payload.get("attempt")),
                    "mechanical_job_id": payload.get("mechanical_job_id"),
                    "model": payload.get("model") or payload.get("actual_model"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                    "service_tier": payload.get("service_tier"),
                    "sequence": sequence,
                    "timestamp": timestamp,
                })
                item["status"] = "STARTED"
            elif kind == "MECHANICAL_SUBTASK_FALLBACK":
                item["fallback"] = {
                    "from_model": payload.get("from_model"),
                    "to_model": payload.get("to_model"),
                    "to_reasoning_effort": payload.get("to_reasoning_effort"),
                    "service_tier": payload.get("service_tier"),
                    "sequence": sequence,
                }
            elif kind in MECHANICAL_TERMINAL_KINDS:
                item.update({
                    "status": str(payload.get("status") or kind.removeprefix(MECHANICAL_EVENT_PREFIX)),
                    "actual_model": payload.get("actual_model") or payload.get("model"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                    "service_tier": payload.get("service_tier"),
                    "failure_kind": payload.get("failure_kind"),
                    "retryable": payload.get("retryable"),
                    "terminal_sequence": sequence,
                    "completed_at": timestamp,
                    "token_telemetry": str(payload.get("token_telemetry") or "unknown"),
                    "token_usage": _token_usage(payload.get("token_usage")),
                    "artifact_paths": _add_payload_artifacts(
                        collector, payload,
                        relation=f"mechanical:{parent_job_id}:{subtask_id}",
                        project_root=project,
                    ),
                })

    for job in jobs.values():
        job.setdefault("task_id", None)
        job.setdefault("role", None)
        job.setdefault("claim_id", None)
        job.setdefault("artifact_paths", [])
        job.setdefault("token_telemetry", "unknown")
        job.setdefault("token_usage", _token_usage({}))
        job.setdefault("thread_id", None)
        job.setdefault("turn_id", None)

    started_at, stopped_at = _run_times(records)
    terminal = stopped_at is not None
    event_counts = Counter(str(event.get("kind") or "UNKNOWN") for event in records)
    source = {
        "path": _portable_path(event_path, project),
        "last_indexed_sequence": status.get("last_sequence"),
        "last_indexed_timestamp": status.get("last_timestamp"),
        "terminal_snapshot": terminal,
        "sha256": file_digest(event_path) if terminal else None,
    }
    summary = {
        key: status.get(key)
        for key in (
            "run_id", "execution_mode", "run_outcome", "internal_failure",
            "event_count", "jobs_started", "jobs_completed", "jobs_cancelled",
            "jobs_terminal", "mechanical_subtasks", "token_usage",
            "token_telemetry", "token_usage_is_lower_bound",
        )
    }
    summary.update({
        "lifecycle_state": "TERMINAL" if terminal else "NONTERMINAL",
        "started_at": started_at,
        "stopped_at": stopped_at,
        "stop_reason": _safe_text(status.get("stop_reason")),
    })
    return {
        "schema_version": SEMANTIC_INDEX_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "project": (
            layout.manifest.project_id if layout.manifest is not None else project.name
        ),
        "run_id": run.name,
        "derivation": {
            "kind": "derived_metadata",
            "source_files_mutated": False,
            "scope": "explicit event-linked artifacts only; no recursive worktree crawl",
            "raw_model_output_included": False,
            "raw_server_errors_included": False,
        },
        "source_events": source,
        "summary": summary,
        "event_counts": dict(sorted(event_counts.items())),
        "jobs": sorted(jobs.values(), key=lambda row: str(row["job_id"])),
        "candidates": sorted(
            candidates,
            key=lambda row: (row["sequence"], str(row.get("fingerprint") or "")),
        ),
        "audits": sorted(
            audits, key=lambda row: (row["sequence"], str(row.get("audit_id") or "")),
        ),
        "trust_changes": sorted(trust_changes, key=lambda row: row["sequence"]),
        "mechanical_subtasks": sorted(
            mechanical.values(),
            key=lambda row: (str(row["parent_job_id"]), str(row["subtask_id"])),
        ),
        "artifacts": collector.records,
        "skipped_references": collector.skipped,
    }


def write_semantic_index(
    *,
    project_root: Path,
    outcome_dir: Path,
    run_dir: Path,
    events: list[dict[str, Any]] | None = None,
) -> Path:
    project = project_root.resolve()
    target_dir = outcome_dir.resolve()
    outcomes_root = ProjectLayout(project).outcomes_root.resolve()
    if not target_dir.is_relative_to(outcomes_root):
        raise ValueError(f"outcome directory escapes project boundary: {target_dir}")
    index = build_semantic_index(project_root=project, run_dir=run_dir, events=events)
    target = target_dir / "SEMANTIC_INDEX.json"
    atomic_write_json(target, index)
    return target


def _jsonl(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    )


def rebuild_catalog(project_root: Path) -> dict[str, Any]:
    """Rebuild cross-run catalogs; each output file is atomically replaced."""
    project = project_root.resolve()
    layout = ProjectLayout(project)
    runs_root = layout.runs_root
    if not runs_root.is_dir():
        raise ValueError(f"project has no autonomous runs directory: {runs_root}")
    run_dirs = sorted(
        (
            path for path in runs_root.iterdir()
            if path.is_dir() and (path / "EVENTS.jsonl").is_file()
        ),
        key=lambda path: path.name,
    )

    run_rows: list[dict[str, Any]] = []
    object_rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        index = build_semantic_index(project_root=project, run_dir=run_dir)
        summary = index["summary"]
        run_rows.append({
            "schema_version": CATALOG_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "lifecycle_state": summary["lifecycle_state"],
            "started_at": summary["started_at"],
            "stopped_at": summary["stopped_at"],
            "execution_mode": summary["execution_mode"],
            "run_outcome": summary["run_outcome"],
            "internal_failure": summary["internal_failure"],
            "stop_reason": summary["stop_reason"],
            "event_count": summary["event_count"],
            "jobs_started": summary["jobs_started"],
            "jobs_terminal": summary["jobs_terminal"],
            "mechanical_subtasks": summary["mechanical_subtasks"],
            "token_usage": summary["token_usage"],
            "token_telemetry": summary["token_telemetry"],
            "token_usage_is_lower_bound": summary["token_usage_is_lower_bound"],
            "has_outcome": (layout.outcomes_root / run_dir.name / "OUTCOME.md").is_file(),
            "has_semantic_index": (
                layout.outcomes_root / run_dir.name / "SEMANTIC_INDEX.json"
            ).is_file(),
            "has_nightly_report": (
                layout.nightly_root / run_dir.name / "NIGHTLY_REPORT.md"
            ).is_file(),
            "run_path": _portable_path(run_dir, project),
            "source_events": index["source_events"],
        })
        for object_type, key in (
            ("job", "jobs"),
            ("candidate", "candidates"),
            ("audit", "audits"),
            ("trust_change", "trust_changes"),
            ("mechanical_subtask", "mechanical_subtasks"),
            ("artifact", "artifacts"),
        ):
            for value in index[key]:
                object_rows.append({
                    **value,
                    "schema_version": CATALOG_SCHEMA_VERSION,
                    "object_type": object_type,
                    "run_id": run_dir.name,
                })

    object_rows.sort(key=lambda row: (
        str(row["run_id"]), str(row["object_type"]),
        str(
            row.get("job_id") or row.get("event_id") or row.get("audit_id")
            or row.get("subtask_id") or row.get("path") or row.get("sequence") or ""
        ),
    ))
    catalog_root = layout.catalog_root
    resolved_catalog_root = catalog_root.resolve()
    if not resolved_catalog_root.is_relative_to(project):
        raise ValueError(
            f"catalog directory escapes project boundary: {resolved_catalog_root}"
        )
    catalog_root.mkdir(parents=True, exist_ok=True)
    runs_path = catalog_root / "RUN_CATALOG.jsonl"
    objects_path = catalog_root / "RESEARCH_OBJECTS.jsonl"
    summary_path = catalog_root / "CATALOG_SUMMARY.json"
    runs_text = _jsonl(run_rows)
    objects_text = _jsonl(object_rows)
    content_sha256 = sha256(
        runs_text.encode("utf-8") + b"\0" + objects_text.encode("utf-8")
    ).hexdigest()
    atomic_write_text(runs_path, runs_text)
    atomic_write_text(objects_path, objects_text)

    modes = Counter(str(row.get("execution_mode") or "unknown") for row in run_rows)
    states = Counter(str(row.get("lifecycle_state") or "unknown") for row in run_rows)
    kinds = Counter(str(row["object_type"]) for row in object_rows)
    generated_at = utc_now()
    if summary_path.is_file():
        try:
            previous = json.loads(summary_path.read_text(encoding="utf-8"))
            if previous.get("content_sha256") == content_sha256:
                generated_at = str(previous.get("generated_at") or generated_at)
        except (OSError, json.JSONDecodeError):
            pass
    summary = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "generated_at": generated_at,
        "content_sha256": content_sha256,
        "project": (
            layout.manifest.project_id if layout.manifest is not None else project.name
        ),
        "derivation": {
            "kind": "rebuildable_derived_metadata",
            "source_files_mutated": False,
            "source_of_truth": "<runtime_root>/runs/<run-id>/EVENTS.jsonl",
            "nonterminal_does_not_assert_process_liveness": True,
        },
        "runs": len(run_rows),
        "runs_by_lifecycle_state": dict(sorted(states.items())),
        "runs_by_execution_mode": dict(sorted(modes.items())),
        "objects": len(object_rows),
        "objects_by_type": dict(sorted(kinds.items())),
        "files": {
            "runs": _portable_path(runs_path, project),
            "objects": _portable_path(objects_path, project),
        },
    }
    atomic_write_json(summary_path, summary)
    return {
        **summary,
        "catalog_root": _portable_path(catalog_root, project),
        "summary_path": _portable_path(summary_path, project),
    }
