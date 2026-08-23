from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..claim_graph import ClaimGraph
from ..models import stable_hash, utc_now
from ..storage import append_jsonl, atomic_write_json, atomic_write_text, read_jsonl


CORE_CAPSULE_MAX_BYTES = 32 * 1024
_CAPSULE_TEXT_MAX_BYTES = 512
_CAPSULE_CONTAINER_MAX_ITEMS = 24
_CAPSULE_MAX_DEPTH = 4


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    marker = "…[truncated]"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    prefix = encoded[:budget].decode("utf-8", errors="ignore")
    return prefix + marker, True


def _bounded_capsule_value(
    value: Any,
    stats: dict[str, int],
    *,
    depth: int = 0,
) -> Any:
    """Return a deterministic JSON-safe bounded view for a derived capsule."""
    if isinstance(value, str):
        bounded, truncated = _truncate_utf8(value, _CAPSULE_TEXT_MAX_BYTES)
        if truncated:
            stats["truncated_values"] += 1
        return bounded
    if isinstance(value, dict):
        if depth >= _CAPSULE_MAX_DEPTH:
            stats["truncated_values"] += 1
            return "<depth-limit>"
        items = sorted(
            ((str(key), item) for key, item in value.items()),
            key=lambda pair: pair[0],
        )
        if len(items) > _CAPSULE_CONTAINER_MAX_ITEMS:
            stats["truncated_values"] += 1
            items = items[:_CAPSULE_CONTAINER_MAX_ITEMS]
        return {
            key: _bounded_capsule_value(item, stats, depth=depth + 1)
            for key, item in items
        }
    if isinstance(value, (list, tuple)):
        if depth >= _CAPSULE_MAX_DEPTH:
            stats["truncated_values"] += 1
            return "<depth-limit>"
        items = list(value)
        if len(items) > _CAPSULE_CONTAINER_MAX_ITEMS:
            stats["truncated_values"] += 1
            items = items[:_CAPSULE_CONTAINER_MAX_ITEMS]
        return [
            _bounded_capsule_value(item, stats, depth=depth + 1)
            for item in items
        ]
    return value


def _priority_key(value: dict[str, Any], identity_key: str) -> tuple[float, str]:
    try:
        priority = float(value.get("priority", 0) or 0)
    except (TypeError, ValueError):
        priority = 0.0
    return (-priority, str(value.get(identity_key) or ""))


def _serialize_core_capsule(capsule: dict[str, Any]) -> bytes:
    return (
        json.dumps(capsule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


class RouteLedger:
    def __init__(self, path: Path):
        self.path = path

    def append(
        self,
        *,
        route_id: str,
        representation_id: str,
        method_tags: list[str],
        status: str,
        failure_class: str | None,
        retry_condition: str | None,
        evidence_refs: list[str],
        source: str,
    ) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "route_event_id": "route:" + stable_hash({
                "route_id": route_id,
                "representation_id": representation_id,
                "status": status,
                "failure_class": failure_class,
                "retry_condition": retry_condition,
                "evidence_refs": evidence_refs,
                "source": source,
                "timestamp": utc_now(),
            }),
            "timestamp": utc_now(),
            "route_id": route_id,
            "representation_id": representation_id,
            "method_tags": sorted(set(method_tags)),
            "status": status,
            "failure_class": failure_class,
            "retry_condition": retry_condition,
            "evidence_refs": list(evidence_refs),
            "source": source,
        }
        append_jsonl(self.path, record)
        return record

    def records(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def route_is_retryable(self, route_id: str, satisfied_conditions: set[str]) -> bool:
        records = [item for item in self.records() if item.get("route_id") == route_id]
        if not records:
            return True
        latest = records[-1]
        condition = latest.get("retry_condition")
        if latest.get("status") not in {"FAILED", "PAUSED", "PAUSE"}:
            return True
        return bool(condition and condition in satisfied_conditions)


def write_core_capsule(
    path: Path,
    *,
    graph: ClaimGraph,
    recent_changes: list[dict[str, Any]],
    active_tasks: list[dict[str, Any]],
    audit_leases: list[dict[str, Any]],
    route_records: list[dict[str, Any]],
    representations: dict[str, str],
    canonical_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stats = {"truncated_values": 0}
    frontier_source = [
        {
            "claim_id": claim.claim_id,
            "status": claim.research_status,
            "trust": claim.trust_status,
            "evidence": claim.evidence_level,
            "gaps": claim.current_gaps[:3],
            "priority": claim.priority.get("score", 0),
            "representation_id": representations.get(claim.claim_id),
        }
        for claim in graph.claims.values()
        if graph.semantics.is_frontier(claim.research_status)
    ]
    frontier_source.sort(key=lambda item: _priority_key(item, "claim_id"))
    active_source = sorted(
        active_tasks,
        key=lambda item: _priority_key(item, "task_id"),
    )
    pending_audit_source = sorted(
        (
            item for item in audit_leases
            if item.get("status") in {"PENDING", "ACTIVE", "RETRY_WAIT"}
        ),
        key=lambda item: _priority_key(item, "lease_id"),
    )
    source_counts = {
        "frontier": len(frontier_source),
        "active_tasks": len(active_source),
        "pending_audit_leases": len(pending_audit_source),
        "recent_changes": len(recent_changes),
        "recent_routes": len(route_records),
    }
    selected = {
        "frontier": frontier_source[:40],
        "active_tasks": active_source[:24],
        "pending_audit_leases": pending_audit_source[:40],
        "recent_changes": recent_changes[-30:],
        "recent_routes": route_records[-30:],
    }
    bounded = {
        key: [_bounded_capsule_value(item, stats) for item in values]
        for key, values in selected.items()
    }
    dropped_counts = {
        key: source_counts[key] - len(selected[key])
        for key in source_counts
    }
    capsule = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "authority": "derived_noncanonical",
        "canonical_state": _bounded_capsule_value(
            canonical_state or {}, stats,
        ),
        **bounded,
        "compaction": {
            "source_counts": source_counts,
            "dropped_counts": dropped_counts,
            "truncated_values": stats["truncated_values"],
        },
    }
    if graph.semantics.domain != "math-research":
        capsule["domain"] = graph.semantics.domain
    while True:
        encoded = _serialize_core_capsule(capsule)
        if len(encoded) <= CORE_CAPSULE_MAX_BYTES:
            break
        reduced = False
        for key in (
            "recent_routes", "recent_changes", "pending_audit_leases",
            "active_tasks", "frontier",
        ):
            values = capsule[key]
            if values:
                if key in {"recent_routes", "recent_changes"}:
                    del values[0]
                else:
                    values.pop()
                capsule["compaction"]["dropped_counts"][key] += 1
                reduced = True
                break
        if not reduced:
            raise ValueError("CORE_CAPSULE cannot fit its 32 KiB contract")
    atomic_write_text(path, encoded.decode("utf-8"))
    if path.read_bytes() != encoded:
        raise ValueError("CORE_CAPSULE write did not match its validated serialization")
    return capsule


def write_research_map(
    json_path: Path,
    markdown_path: Path,
    *,
    graph: ClaimGraph,
    route_records: list[dict[str, Any]],
    representations: dict[str, str],
    canonical_state: dict[str, Any] | None = None,
) -> None:
    status_field = (
        "math_status"
        if graph.semantics.domain == "math-research"
        else "research_status"
    )
    claims = [
        {
            "claim_id": claim.claim_id,
            status_field: claim.research_status,
            "trust_status": claim.trust_status,
            "evidence_level": claim.evidence_level,
            "dependencies": claim.dependencies,
            "representation_id": representations.get(claim.claim_id),
        }
        for claim in sorted(graph.claims.values(), key=lambda item: item.claim_id)
    ]
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "authority": "derived_noncanonical",
        "canonical_state_sha256": (
            canonical_state or {}
        ).get("canonical_state_sha256"),
        "planning_context_sha256": (
            canonical_state or {}
        ).get("planning_context_sha256"),
        "claims": claims,
        "routes": route_records,
    }
    if graph.semantics.domain != "math-research":
        payload["domain"] = graph.semantics.domain
    atomic_write_json(json_path, payload)
    rows = [
        "# Research Map",
        "",
        "> Derived navigation only; claim graph and independent audits remain authoritative.",
        "> ClaimGraph supplies the current frontier; startup-frozen inputs are context only.",
        "",
        (
            "| Claim | Math | Trust | Evidence | Representation |"
            if graph.semantics.domain == "math-research"
            else "| Claim | Research status | Trust | Evidence | Representation |"
        ),
        "|---|---|---|---|---|",
    ]
    rows.extend(
        (
            "| {claim_id} | {status} | {trust_status} | {evidence_level} | {representation_id} |"
        ).format(status=item[status_field], **item)
        for item in claims
    )
    atomic_write_text(markdown_path, "\n".join(rows) + "\n")
