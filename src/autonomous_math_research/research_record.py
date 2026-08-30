from __future__ import annotations

from hashlib import sha256
from importlib import metadata
from datetime import datetime
import json
import platform
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable

from .models import JobOutcome, ResearchTask, stable_hash
from .storage import (
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    file_digest,
    read_jsonl,
)


RESEARCH_CONTEXT_SCHEMA_VERSION = 1
UNIFIED_RESULT_SCHEMA_VERSION = 1
DIRECTOR_DECISION_SCHEMA_VERSION = 1
ASSET_USE_SCHEMA_VERSION = 1
AUDIT_KEY_TELEMETRY_SCHEMA_VERSION = 1
METRICS_SCHEMA_VERSION = 1
FINAL_RECORD_SCHEMA_VERSION = 1

RUN_PURPOSES = frozenset({"DEVELOPMENT", "NATURAL_RESEARCH", "EVALUATION"})
DIRECTOR_REASON_CODES = frozenset({
    "ADMITTED",
    "ALREADY_AUDITED",
    "CLAIM_CLOSED",
    "DUPLICATE_TASK",
    "KILL_GATED",
    "THEME_EXCLUDED",
    "WAIT_DEPENDENCY",
    "EXISTING_ASSET",
    "REPRESENTATION_BLOCKED",
    "AUDIT_CACHE_HIT",
    "BUDGET_REJECTED",
    "INVALID_TASK",
})
RESEARCH_OUTCOMES = frozenset({
    "PROVED_RESULT",
    "USEFUL_NEGATIVE_RESULT",
    "NEW_KILL_GATE",
    "NEW_REUSABLE_ASSET",
    "COMPUTATION_ONLY",
    "REPRESENTATION_BLOCKED",
    "DEPENDENCY_BLOCKED",
    "BOUNDED_SEARCH_EXHAUSTED",
    "NO_PROGRESS",
    "INVALID_RESULT",
})
ASSET_USE_STAGES = frozenset({"RETRIEVED", "LOADED", "USED", "CITED_IN_RESULT"})
AUDIT_MISS_REASONS = frozenset({
    "STATEMENT_CHANGED",
    "REPRESENTATION_CHANGED",
    "DEPENDENCY_CHANGED",
    "PROOF_CHANGED",
    "CERTIFICATE_CHANGED",
    "SOURCE_CHANGED",
    "AUDIT_POLICY_CHANGED",
    "NO_PREVIOUS_RECEIPT",
})


def decision_reason_code(detail: str) -> str:
    """Map controller diagnostics to a stable, intentionally small taxonomy."""
    value = str(detail or "").upper()
    if "THEME_" in value:
        return "THEME_EXCLUDED"
    if "KILL_GATED" in value or "DO_NOT_REPEAT" in value:
        return "KILL_GATED"
    if "CLAIM_ALREADY_TERMINAL" in value or "CLAIMGRAPH_CLAIM" in value:
        return "CLAIM_CLOSED"
    if "DO_NOT_ROUTE" in value or "ALREADY_CLOSES" in value:
        return "ALREADY_AUDITED"
    if "REPRESENTATION" in value and (
        "MISMATCH" in value or "BRIDGE" in value or "INCOMPATIBLE" in value
    ):
        return "REPRESENTATION_BLOCKED"
    if any(marker in value for marker in (
        "WAIT_DEPENDENCY", "DEPENDENCY", "INPUT_CLOSURE",
        "AUTHORITY_RECONCILIATION", "DEFERRED", "PAUSED",
    )):
        return "WAIT_DEPENDENCY"
    if "DUPLICATE" in value or "FINGERPRINT" in value or "ALREADY BOUND" in value:
        return "DUPLICATE_TASK"
    if "BUDGET" in value:
        return "BUDGET_REJECTED"
    if "EXISTING_ASSET" in value:
        return "EXISTING_ASSET"
    if "AUDIT_CACHE_HIT" in value:
        return "AUDIT_CACHE_HIT"
    return "INVALID_TASK"


def research_outcome(outcome: JobOutcome) -> str:
    if not outcome.succeeded:
        failure = str(outcome.failure_kind or "").upper()
        if "REPRESENTATION" in failure:
            return "REPRESENTATION_BLOCKED"
        if "DEPENDENCY" in failure or "INPUT_CLOSURE" in failure:
            return "DEPENDENCY_BLOCKED"
        return "INVALID_RESULT"
    result_type = str(outcome.result.get("result_type") or "NO_PROGRESS").upper()
    status = str(outcome.result.get("status") or "").upper()
    if result_type == "PROOF":
        return "PROVED_RESULT"
    if result_type in {"COUNTEREXAMPLE", "NEW_OBSTRUCTION"}:
        return "USEFUL_NEGATIVE_RESULT"
    if result_type in {"NEW_DETECTOR", "STRICT_REDUCTION"}:
        return "NEW_REUSABLE_ASSET"
    if result_type in {
        "CERTIFICATE", "CHECKER_RESULT", "EXPERIMENT_RESULT",
        "STRONGER_COMPUTATION", "COMPUTATIONAL_PATTERN", "EMPIRICAL_FINDING",
    }:
        return "COMPUTATION_ONLY"
    if result_type == "BLOCKED":
        finding = str(outcome.result.get("main_finding") or "").upper()
        if "REPRESENTATION" in finding:
            return "REPRESENTATION_BLOCKED"
        if "DEPENDENCY" in finding:
            return "DEPENDENCY_BLOCKED"
        return "NO_PROGRESS"
    if status == "NO_COUNTEREXAMPLE_WITHIN_SCOPE":
        return "BOUNDED_SEARCH_EXHAUSTED"
    return "NO_PROGRESS"


def _immutable_json(path: Path, payload: dict[str, Any]) -> None:
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise ValueError(f"immutable research record conflicts with existing bytes: {path}")
        return
    atomic_write_json(path, payload)


def _immutable_copy(source: Path, target: Path) -> dict[str, Any]:
    payload = source.read_bytes()
    digest = sha256(payload).hexdigest()
    if target.is_file():
        if target.read_bytes() != payload:
            raise ValueError(f"immutable research snapshot conflicts: {target}")
    else:
        atomic_write_bytes(target, payload)
    return {"path": target.name, "sha256": digest, "bytes": len(payload)}


def _git_state(project_root: Path) -> dict[str, Any]:
    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "-C", str(project_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    root = run("rev-parse", "--show-toplevel")
    head = run("rev-parse", "HEAD")
    if root.returncode or head.returncode:
        return {
            "available": False,
            "repository_root": None,
            "project_relative_path": None,
            "head": None,
            "dirty": None,
            "dirty_state_sha256": None,
        }
    repository_root = Path(root.stdout.decode("utf-8", errors="replace").strip()).resolve()
    status = subprocess.run(
        [
            "git", "-C", str(repository_root), "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--", str(project_root.resolve()),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if status.returncode:
        raise ValueError("target project Git dirty-state capture failed")
    dirty_bytes = status.stdout
    return {
        "available": True,
        "repository_root": str(repository_root),
        "project_relative_path": project_root.resolve().relative_to(repository_root).as_posix(),
        "head": head.stdout.decode("ascii", errors="replace").strip(),
        "dirty": bool(dirty_bytes),
        "dirty_state_sha256": sha256(dirty_bytes).hexdigest(),
    }


def _environment_versions(runtime_provenance: dict[str, Any]) -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in ("sympy", "numpy", "z3-solver", "sage-conf"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "amr_version": runtime_provenance.get("amr_version"),
        "codex_cli_version": runtime_provenance.get("codex_cli_version"),
        "cas_packages": packages,
    }


class ResearchRecordStore:
    """Immutable per-run research context plus append-only terminal snapshots."""

    def __init__(
        self,
        *,
        project_root: Path,
        runtime_root: Path,
        run_dir: Path,
        campaign_root: Path,
        run_id: str,
        campaign_id: str,
        epoch_id: str,
    ):
        self.project_root = project_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.run_dir = run_dir.resolve()
        self.campaign_root = campaign_root.resolve()
        self.run_id = run_id
        self.campaign_id = campaign_id
        self.epoch_id = epoch_id
        self.root = self.run_dir / "research_record"
        self.context_root = self.root / "context"
        self.result_root = self.root / "results"
        self.final_root = self.root / "final"
        self.context_manifest_path = self.root / "CONTEXT_MANIFEST.json"

    def pin_purpose(self, requested: str | None, *, default: str, started_at: str) -> str:
        purpose = str(requested or default).upper()
        if purpose not in RUN_PURPOSES:
            raise ValueError(f"run purpose must be one of {sorted(RUN_PURPOSES)}")
        path = self.campaign_root / "RESEARCH_PURPOSE.json"
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            pinned = str(existing.get("purpose") or "")
            if requested is not None and pinned != purpose:
                raise ValueError(
                    f"campaign purpose is already pinned as {pinned}; requested {purpose}"
                )
            if pinned not in RUN_PURPOSES:
                raise ValueError("campaign research purpose record is invalid")
            return pinned
        _immutable_json(path, {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "purpose": purpose,
            "created_at": started_at,
        })
        return purpose

    def freeze_context_before(
        self,
        *,
        started_at: str,
        run_purpose: str,
        sources: dict[str, Path | None],
        theme: dict[str, Any] | None,
        runtime_provenance: dict[str, Any],
    ) -> dict[str, Any]:
        if self.context_manifest_path.is_file():
            existing = json.loads(self.context_manifest_path.read_text(encoding="utf-8"))
            self.verify_context(existing)
            return existing
        snapshots: dict[str, dict[str, Any] | None] = {}
        for name, source in sorted(sources.items()):
            if source is None or not source.is_file():
                snapshots[name] = None
                continue
            snapshots[name] = _immutable_copy(source, self.context_root / f"{name}.json")
        if theme is None:
            snapshots["campaign_theme"] = None
        else:
            theme_path = self.context_root / "campaign_theme.json"
            _immutable_json(theme_path, theme)
            snapshots["campaign_theme"] = {
                "path": theme_path.name,
                "sha256": file_digest(theme_path),
                "bytes": theme_path.stat().st_size,
            }
        evidence_root = stable_hash({
            key: value["sha256"] if value is not None else None
            for key, value in sorted(snapshots.items())
        })
        payload = {
            "schema_version": RESEARCH_CONTEXT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "run_purpose": run_purpose,
            "started_at": started_at,
            "snapshots": snapshots,
            "target_project_git": _git_state(self.project_root),
            "environment": _environment_versions(runtime_provenance),
            "input_evidence_root_sha256": evidence_root,
            "schema_versions": {
                "director_decision": DIRECTOR_DECISION_SCHEMA_VERSION,
                "unified_result": UNIFIED_RESULT_SCHEMA_VERSION,
                "asset_use": ASSET_USE_SCHEMA_VERSION,
                "audit_key_telemetry": AUDIT_KEY_TELEMETRY_SCHEMA_VERSION,
                "metrics": METRICS_SCHEMA_VERSION,
                "final_record": FINAL_RECORD_SCHEMA_VERSION,
            },
        }
        payload["context_sha256"] = stable_hash(payload)
        _immutable_json(self.context_manifest_path, payload)
        return payload

    def verify_context(self, manifest: dict[str, Any] | None = None) -> None:
        value = manifest or json.loads(
            self.context_manifest_path.read_text(encoding="utf-8")
        )
        fingerprinted = dict(value)
        reported = str(fingerprinted.pop("context_sha256", ""))
        if not reported or stable_hash(fingerprinted) != reported:
            raise ValueError("research context manifest fingerprint is invalid")
        observed_root: dict[str, str | None] = {}
        for name, ref in value.get("snapshots", {}).items():
            if ref is None:
                observed_root[name] = None
                continue
            path = self.context_root / str(ref["path"])
            if not path.is_file() or file_digest(path) != str(ref["sha256"]):
                raise ValueError(f"research context snapshot changed: {name}")
            observed_root[name] = str(ref["sha256"])
        if stable_hash(dict(sorted(observed_root.items()))) != value.get(
            "input_evidence_root_sha256"
        ):
            raise ValueError("research context evidence root is invalid")

    def replay_context(self) -> dict[str, Any]:
        manifest = json.loads(self.context_manifest_path.read_text(encoding="utf-8"))
        self.verify_context(manifest)
        snapshots: dict[str, Any] = {}
        for name, ref in manifest["snapshots"].items():
            if ref is None:
                snapshots[name] = None
            else:
                snapshots[name] = json.loads(
                    (self.context_root / ref["path"]).read_text(encoding="utf-8")
                )
        return {
            "schema_version": RESEARCH_CONTEXT_SCHEMA_VERSION,
            "context_manifest": manifest,
            "snapshots": snapshots,
            "verified": True,
        }

    @staticmethod
    def director_decision(
        *,
        run_id: str,
        director_job_id: str,
        task: ResearchTask,
        decision: str,
        reason_code: str,
        reason_detail: str,
        stage: str,
    ) -> dict[str, Any]:
        if reason_code not in DIRECTOR_REASON_CODES:
            raise ValueError(f"unknown Director decision reason code: {reason_code}")
        payload = {
            "schema_version": DIRECTOR_DECISION_SCHEMA_VERSION,
            "decision_id": stable_hash({
                "run_id": run_id,
                "director_job_id": director_job_id,
                "task_fingerprint": task.fingerprint,
                "decision": decision,
                "stage": stage,
            }),
            "director_job_id": director_job_id,
            "candidate_task": task.to_dict(),
            "task_id": task.task_id,
            "task_fingerprint": task.fingerprint,
            "decision": decision,
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "stage": stage,
            "final_scope": {
                "claim_id": task.target_claim,
                "exact_objective": task.exact_objective,
                "route_family": task.route_family,
            } if decision == "ADMITTED" else None,
            "final_dependencies": list(task.dependencies) if decision == "ADMITTED" else [],
            "final_representation_id": (
                task.representation_id if decision == "ADMITTED" else None
            ),
        }
        return payload

    @staticmethod
    def asset_event(
        *,
        task_id: str,
        asset_id: str,
        stage: str,
        reason: str,
        result_id: str | None = None,
    ) -> dict[str, Any]:
        if stage not in ASSET_USE_STAGES:
            raise ValueError(f"invalid asset use stage: {stage}")
        return {
            "schema_version": ASSET_USE_SCHEMA_VERSION,
            "task_id": task_id,
            "asset_id": asset_id,
            "stage": stage,
            "reason": reason,
            "result_id": result_id,
        }

    def record_result(
        self,
        *,
        outcome: JobOutcome,
        task: ResearchTask,
        job_record: dict[str, Any] | None,
        asset_usage: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], Path]:
        result_type = str(outcome.result.get("result_type") or "NO_PROGRESS").upper()
        if result_type == "PROOF":
            math_status = "PROVED"
        elif result_type == "COUNTEREXAMPLE":
            math_status = "FALSIFIED"
        elif research_outcome(outcome) == "COMPUTATION_ONLY":
            math_status = "COMPUTATION_ONLY"
        else:
            math_status = "OPEN"
        artifacts = []
        for path, digest in sorted(dict((job_record or {}).get("artifact_hashes") or {}).items()):
            artifacts.append({
                "object_id": f"object:sha256:{digest}",
                "path": str(path),
                "sha256": str(digest),
            })
        proof_objects = artifacts if result_type == "PROOF" else []
        certificate_objects = artifacts if result_type in {
            "CERTIFICATE", "CHECKER_RESULT"
        } else []
        source_objects = [
            {
                "object_id": f"object:sha256:{item['sha256']}",
                "path": item["path"],
                "sha256": item["sha256"],
            }
            for item in (job_record or {}).get("required_file_access", [])
            if isinstance(item, dict) and item.get("sha256")
        ]
        core = {
            "schema_version": UNIFIED_RESULT_SCHEMA_VERSION,
            "exact_statement": task.exact_objective,
            "scope": {
                "claim_ids": [task.target_claim],
                "scope_ids": [
                    task.input_closure["canonical_object_id"]
                ] if (
                    isinstance(task.input_closure, dict)
                    and isinstance(task.input_closure.get("canonical_object_id"), str)
                    and task.input_closure.get("canonical_object_id")
                ) else [],
                "route_family": task.route_family,
                "input_closure": task.input_closure,
            },
            "math_status": math_status,
            "maturity_level": (
                "AUDITED_RESULT" if outcome.canonical_progress else "RESULT_CANDIDATE"
            ),
            "evidence_level": outcome.result.get("evidence_level", "E0_SPECULATIVE"),
            "representation_id": task.representation_id,
            "dependencies": list(task.dependencies),
            "proof_objects": proof_objects,
            "certificate_objects": certificate_objects,
            "source_objects": source_objects,
            "audit_key": None,
            "audit_status": "PASS" if outcome.canonical_progress else "PENDING",
            "authority_status": "PROMOTED" if outcome.canonical_progress else "PENDING",
            "routing_effect": "DO_NOT_ROUTE" if outcome.canonical_progress else "NONE",
            "assets_used": sorted({
                str(item["asset_id"]) for item in asset_usage
                if item.get("disposition") == "USED"
            }),
            "assets_created": [],
            "research_outcome": research_outcome(outcome),
            "producer": {
                "kind": "AUTONOMOUS_WORKER",
                "run_id": self.run_id,
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "job_id": outcome.job_id,
                "task_id": task.task_id,
                "role": task.role,
                "model": outcome.model,
                "provider": outcome.provider,
                "reasoning_effort": outcome.reasoning_effort,
            },
            "provenance": {
                "task_fingerprint": task.fingerprint,
                "artifact_hashes": dict((job_record or {}).get("artifact_hashes") or {}),
                "candidate_accepted": bool(outcome.candidate_accepted),
                "canonical_progress": bool(outcome.canonical_progress),
            },
            "cost": {
                "token_usage": outcome.token_usage.to_dict(),
                "cost_usd": outcome.cost_usd,
                "elapsed_seconds": (job_record or {}).get("elapsed_seconds"),
            },
        }
        result_id = "result:" + stable_hash(core)
        payload = {"result_id": result_id, **core}
        payload["content_sha256"] = stable_hash(payload)
        path = self.result_root / f"{payload['content_sha256']}.json"
        _immutable_json(path, payload)
        index_entry = {
            "schema_version": 1,
            "result_id": result_id,
            "content_sha256": payload["content_sha256"],
            "path": f"results/{path.name}",
            "producer_job_id": outcome.job_id,
        }
        index_path = self.root / "RESULT_INDEX.jsonl"
        if not any(
            row.get("content_sha256") == payload["content_sha256"]
            for row in read_jsonl(index_path)
        ):
            append_jsonl(index_path, index_entry)
        return payload, path

    def record_frontier_result(
        self, entry: dict[str, Any],
    ) -> tuple[dict[str, Any], Path]:
        conclusion = str(entry.get("conclusion") or "INCONCLUSIVE")
        math_status = {
            "PROVED": "PROVED",
            "REFUTED": "FALSIFIED",
            "COMPUTATION": "COMPUTATION_ONLY",
        }.get(conclusion, "OPEN")
        maturity = {
            "RESULT": "AUDITED_RESULT",
            "THEME": "VERIFIED_THEME_CLAIM",
            "FINAL": "FINAL_CONJECTURE_CLOSURE",
        }.get(str(entry.get("maturity_level") or "RESULT"), "RESULT_CANDIDATE")
        core = {
            "schema_version": UNIFIED_RESULT_SCHEMA_VERSION,
            "exact_statement": str(entry.get("exact_statement") or ""),
            "scope": {
                "claim_ids": list(entry.get("claim_ids") or []),
                "scope_ids": list(entry.get("scope_ids") or []),
                "route_family": None,
                "input_closure": None,
            },
            "math_status": math_status,
            "maturity_level": maturity,
            "evidence_level": str(entry.get("classification") or "EXTERNAL"),
            "representation_id": entry.get("representation_id"),
            "dependencies": list(entry.get("dependencies") or []),
            "proof_objects": [],
            "certificate_objects": [],
            "source_objects": [{
                "object_id": f"object:sha256:{entry.get('object_sha256')}",
                "path": entry.get("source_manifest"),
                "sha256": entry.get("object_sha256"),
            }],
            "audit_key": entry.get("audit_key"),
            "audit_status": (
                "PASS" if entry.get("route_status") in {"DO_NOT_ROUTE", "KILL_GATED"}
                else "PENDING"
            ),
            "authority_status": entry.get("authority_status"),
            "routing_effect": entry.get("route_status"),
            "assets_used": [],
            "assets_created": [],
            "research_outcome": (
                "PROVED_RESULT" if math_status == "PROVED" else
                "USEFUL_NEGATIVE_RESULT" if math_status == "FALSIFIED" else
                "NEW_KILL_GATE" if entry.get("route_status") == "KILL_GATED" else
                "COMPUTATION_ONLY" if math_status == "COMPUTATION_ONLY" else
                "NO_PROGRESS"
            ),
            "producer": {
                "kind": "EXTERNAL_INGESTION",
                "run_id": self.run_id,
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "external_result_id": entry.get("result_id"),
            },
            "provenance": entry.get("provenance") or {},
            "cost": {
                "token_usage": None,
                "cost_usd": None,
                "elapsed_seconds": None,
            },
        }
        result_id = str(entry.get("result_id") or ("result:" + stable_hash(core)))
        payload = {"result_id": result_id, **core}
        payload["content_sha256"] = stable_hash(payload)
        path = self.result_root / f"{payload['content_sha256']}.json"
        _immutable_json(path, payload)
        index_entry = {
            "schema_version": 1,
            "result_id": result_id,
            "content_sha256": payload["content_sha256"],
            "path": f"results/{path.name}",
            "producer_job_id": None,
        }
        index_path = self.root / "RESULT_INDEX.jsonl"
        if not any(
            row.get("content_sha256") == payload["content_sha256"]
            for row in read_jsonl(index_path)
        ):
            append_jsonl(index_path, index_entry)
        return payload, path

    @staticmethod
    def metrics(
        *,
        run_id: str,
        campaign_id: str,
        epoch_id: str,
        events: Iterable[dict[str, Any]],
        jobs: Iterable[dict[str, Any]],
        mechanical_jobs: Iterable[dict[str, Any]],
        frontier_delta: dict[str, Any] | None,
        started_at: str | None,
        ended_at: str,
    ) -> dict[str, Any]:
        event_rows = list(events)
        job_rows = list(jobs)
        mechanical_rows = list(mechanical_jobs)
        decisions = [
            item.get("payload") or {} for item in event_rows
            if item.get("kind") == "DIRECTOR_TASK_DECISION"
        ]
        asset_rows = [
            item.get("payload") or {} for item in event_rows
            if item.get("kind") == "ASSET_USE_RECORDED"
        ]
        audit_rows = [
            item.get("payload") or {} for item in event_rows
            if item.get("kind") == "AUDIT_KEY_DECISION"
        ]
        result_rows = [
            item.get("payload") or {} for item in event_rows
            if item.get("kind") == "RESEARCH_RESULT_RECORDED"
        ]

        usage_fields = (
            "input_tokens", "cached_input_tokens", "uncached_input_tokens",
            "cache_write_input_tokens", "output_tokens",
            "reasoning_output_tokens", "total_tokens",
        )
        token_usage = {key: 0 for key in usage_fields}
        known_cost = 0.0
        unknown_cost_jobs = 0
        research_tokens = 0
        audit_tokens = 0
        summed_job_wall = 0.0
        for job in job_rows:
            usage = job.get("token_usage") or {}
            for key in usage_fields:
                token_usage[key] += int(usage.get(key, 0) or 0)
            tokens = int(usage.get("total_tokens", 0) or 0)
            role = str(job.get("role") or "")
            if role in {"auditor", "evaluator_auditor"}:
                audit_tokens += tokens
            else:
                research_tokens += tokens
            if job.get("cost_usd") is None:
                unknown_cost_jobs += 1
            else:
                known_cost += float(job["cost_usd"])
            summed_job_wall += float(job.get("elapsed_seconds", 0.0) or 0.0)
        mechanical_tokens = sum(
            int((row.get("token_usage") or {}).get("total_tokens", 0) or 0)
            for row in mechanical_rows
        )
        outcomes = [str(item.get("research_outcome") or "") for item in result_rows]
        reasons = [str(item.get("reason_code") or "") for item in decisions]
        delta = frontier_delta or {}
        total_wall_seconds = None
        if started_at:
            try:
                start_value = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                end_value = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
                total_wall_seconds = max(0.0, (end_value - start_value).total_seconds())
            except ValueError:
                total_wall_seconds = None
        payload = {
            "schema_version": METRICS_SCHEMA_VERSION,
            "run_id": run_id,
            "campaign_id": campaign_id,
            "epoch_id": epoch_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "proposed_tasks": len({
                item.get("decision_id") for item in decisions
                if item.get("stage") == "ADMISSION"
            }),
            "admitted_tasks": sum(item.get("decision") == "ADMITTED" for item in decisions),
            "tasks_suppressed": sum(item.get("decision") != "ADMITTED" for item in decisions),
            "duplicate_suppression": reasons.count("DUPLICATE_TASK"),
            "closed_suppression": reasons.count("CLAIM_CLOSED") + reasons.count("ALREADY_AUDITED"),
            "kill_gate_suppression": reasons.count("KILL_GATED"),
            "theme_exclusions": reasons.count("THEME_EXCLUDED"),
            "dependency_blocks": reasons.count("WAIT_DEPENDENCY"),
            "representation_blocks": reasons.count("REPRESENTATION_BLOCKED"),
            "budget_rejections": reasons.count("BUDGET_REJECTED"),
            "assets_retrieved": sum(item.get("stage") == "RETRIEVED" for item in asset_rows),
            "assets_loaded": sum(item.get("stage") == "LOADED" for item in asset_rows),
            "assets_used": sum(item.get("stage") == "USED" for item in asset_rows),
            "assets_cited_in_results": sum(
                item.get("stage") == "CITED_IN_RESULT" for item in asset_rows
            ),
            "new_assets": len(delta.get("new_reusable_assets") or []),
            "audits_requested": sum(
                item.get("kind") == "AUDIT_QUEUED" for item in event_rows
            ) + sum(not bool(item.get("cache_hit")) for item in audit_rows),
            "audit_cache_hits": sum(bool(item.get("cache_hit")) for item in audit_rows),
            "audit_cache_misses": sum(not bool(item.get("cache_hit")) for item in audit_rows),
            "avoided_duplicate_audits": sum(bool(item.get("cache_hit")) for item in audit_rows),
            "audit_saved_model_calls": sum(
                int(item.get("estimated_saved_model_calls", 0) or 0)
                for item in audit_rows
            ),
            "audit_saved_tokens": sum(
                int(item.get("estimated_saved_tokens", 0) or 0)
                for item in audit_rows
            ),
            "audit_saved_wall_seconds": sum(
                float(item.get("estimated_saved_wall_seconds", 0.0) or 0.0)
                for item in audit_rows
            ),
            "audit_savings_unknown_receipts": sum(
                bool(item.get("cache_hit")) and (
                    item.get("estimated_saved_tokens") is None
                    or item.get("estimated_saved_wall_seconds") is None
                )
                for item in audit_rows
            ),
            "proved_results": outcomes.count("PROVED_RESULT"),
            "useful_negative_results": outcomes.count("USEFUL_NEGATIVE_RESULT"),
            "new_kill_gates": outcomes.count("NEW_KILL_GATE") + len(delta.get("new_kill_gates") or []),
            "computation_only_results": outcomes.count("COMPUTATION_ONLY"),
            "no_progress_jobs": outcomes.count("NO_PROGRESS"),
            "bounded_search_exhausted": outcomes.count("BOUNDED_SEARCH_EXHAUSTED"),
            "model_calls": len(job_rows),
            "mechanical_worker_calls": len(mechanical_rows),
            "retry_count": sum(
                item.get("kind") in {
                    "JOB_RETRY_QUEUED", "MECHANICAL_SUBTASK_RETRY_QUEUED"
                } for item in event_rows
            ),
            "token_usage": token_usage,
            "research_tokens": research_tokens,
            "audit_tokens": audit_tokens,
            "mechanical_tokens": mechanical_tokens,
            "known_cost_usd": round(known_cost, 12),
            "unknown_cost_jobs": unknown_cost_jobs,
            "summed_job_wall_seconds": summed_job_wall,
            "total_wall_seconds": total_wall_seconds,
            "frontier_delta_counts": {
                key: len(delta.get(key) or []) for key in (
                    "new_audited_results", "new_proved_results",
                    "new_falsified_results", "new_kill_gates",
                    "newly_closed_obligations", "new_external_ingestions",
                    "new_reusable_assets", "superseded_results",
                    "authority_drift", "unresolved_integration_items",
                )
            },
        }
        payload["metrics_sha256"] = stable_hash(payload)
        return payload

    def finalize(
        self,
        *,
        frontier_after_path: Path | None,
        frontier_delta: dict[str, Any] | None,
        events: list[dict[str, Any]],
        jobs: list[dict[str, Any]],
        mechanical_jobs: list[dict[str, Any]],
        ended_at: str,
        started_at: str | None,
    ) -> dict[str, Any]:
        watermark = int(events[-1].get("sequence", 0)) if events else 0
        snapshot_root = self.final_root / "snapshots"
        frontier_ref = None
        if frontier_after_path is not None and frontier_after_path.is_file():
            digest = file_digest(frontier_after_path)
            target = snapshot_root / f"frontier_after.{digest}.json"
            frontier_ref = _immutable_copy(frontier_after_path, target)
            frontier_ref["path"] = f"snapshots/{target.name}"
        delta_ref = None
        if frontier_delta is not None:
            digest = stable_hash(frontier_delta)
            target = snapshot_root / f"frontier_delta.{digest}.json"
            _immutable_json(target, frontier_delta)
            delta_ref = {
                "path": f"snapshots/{target.name}",
                "sha256": file_digest(target),
            }
        metrics = self.metrics(
            run_id=self.run_id,
            campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            events=events,
            jobs=jobs,
            mechanical_jobs=mechanical_jobs,
            frontier_delta=frontier_delta,
            started_at=started_at,
            ended_at=ended_at,
        )
        metrics_path = self.final_root / "metrics" / (
            f"{watermark:012d}.{metrics['metrics_sha256']}.json"
        )
        _immutable_json(metrics_path, metrics)
        result_index_path = self.root / "RESULT_INDEX.jsonl"
        result_index_sha = file_digest(result_index_path) if result_index_path.is_file() else None
        core = {
            "schema_version": FINAL_RECORD_SCHEMA_VERSION,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "event_watermark": watermark,
            "ended_at": ended_at,
            "context_manifest_sha256": file_digest(self.context_manifest_path),
            "frontier_after": frontier_ref,
            "frontier_delta": delta_ref,
            "result_index_sha256": result_index_sha,
            "metrics": {
                "path": metrics_path.relative_to(self.root).as_posix(),
                "sha256": file_digest(metrics_path),
            },
        }
        record_sha = stable_hash(core)
        payload = {**core, "final_record_sha256": record_sha}
        path = self.final_root / "records" / f"{watermark:012d}.{record_sha}.json"
        _immutable_json(path, payload)
        index_path = self.final_root / "INDEX.jsonl"
        if not any(row.get("final_record_sha256") == record_sha for row in read_jsonl(index_path)):
            append_jsonl(index_path, {
                "schema_version": 1,
                "event_watermark": watermark,
                "final_record_sha256": record_sha,
                "path": path.relative_to(self.root).as_posix(),
            })
        campaign_index = self.campaign_root / "RESEARCH_RECORDS.jsonl"
        if not any(
            row.get("final_record_sha256") == record_sha
            for row in read_jsonl(campaign_index)
        ):
            append_jsonl(campaign_index, {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "run_id": self.run_id,
                "event_watermark": watermark,
                "final_record_sha256": record_sha,
                "metrics_sha256": metrics["metrics_sha256"],
                "run_record_path": str(path),
            })
        return payload

    def inspect(self) -> dict[str, Any]:
        context = json.loads(self.context_manifest_path.read_text(encoding="utf-8"))
        self.verify_context(context)
        finals = read_jsonl(self.final_root / "INDEX.jsonl")
        return {
            "context_manifest": context,
            "final_records": finals,
            "result_index": read_jsonl(self.root / "RESULT_INDEX.jsonl"),
        }

    def latest_metrics(self) -> dict[str, Any]:
        finals = read_jsonl(self.final_root / "INDEX.jsonl")
        if not finals:
            raise ValueError("run has no finalized research metrics")
        latest = max(finals, key=lambda item: int(item["event_watermark"]))
        record = json.loads((self.root / latest["path"]).read_text(encoding="utf-8"))
        metrics_ref = record["metrics"]
        path = self.root / metrics_ref["path"]
        if file_digest(path) != metrics_ref["sha256"]:
            raise ValueError("research metrics snapshot changed")
        return json.loads(path.read_text(encoding="utf-8"))
