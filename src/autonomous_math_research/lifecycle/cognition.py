from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..claim_graph import ClaimGraph
from ..models import stable_hash, utc_now
from ..storage import append_jsonl, atomic_write_json, atomic_write_text, read_jsonl


CORE_CAPSULE_MAX_BYTES = 32 * 1024


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
        if latest.get("status") not in {"FAILED", "PAUSED"}:
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
) -> dict[str, Any]:
    frontier = sorted(
        (
            {
                "claim_id": claim.claim_id,
                "status": claim.math_status,
                "trust": claim.trust_status,
                "evidence": claim.evidence_level,
                "gaps": claim.current_gaps[:3],
                "priority": claim.priority.get("score", 0),
                "representation_id": representations.get(claim.claim_id),
            }
            for claim in graph.claims.values()
            if claim.math_status in {"OPEN", "PLAUSIBLE", "REDUCED_TO"}
        ),
        key=lambda item: float(item["priority"] or 0),
        reverse=True,
    )
    capsule = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "authority": "derived_noncanonical",
        "frontier": frontier[:40],
        "active_tasks": active_tasks[:24],
        "pending_audit_leases": [
            item for item in audit_leases
            if item.get("status") in {"PENDING", "ACTIVE", "RETRY_WAIT"}
        ][:40],
        "recent_changes": recent_changes[-30:],
        "recent_routes": route_records[-30:],
    }
    while True:
        encoded = (
            json.dumps(capsule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        if len(encoded) <= CORE_CAPSULE_MAX_BYTES:
            break
        reduced = False
        for key in ("recent_routes", "recent_changes", "frontier", "pending_audit_leases"):
            values = capsule[key]
            if values:
                del values[0]
                reduced = True
                break
        if not reduced:
            raise ValueError("CORE_CAPSULE cannot fit its 32 KiB contract")
    atomic_write_json(path, capsule)
    if path.stat().st_size > CORE_CAPSULE_MAX_BYTES:
        raise ValueError("CORE_CAPSULE exceeded 32 KiB after serialization")
    return capsule


def write_research_map(
    json_path: Path,
    markdown_path: Path,
    *,
    graph: ClaimGraph,
    route_records: list[dict[str, Any]],
    representations: dict[str, str],
) -> None:
    claims = [
        {
            "claim_id": claim.claim_id,
            "math_status": claim.math_status,
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
        "claims": claims,
        "routes": route_records,
    }
    atomic_write_json(json_path, payload)
    rows = [
        "# Research Map",
        "",
        "> Derived navigation only; claim graph and independent audits remain authoritative.",
        "",
        "| Claim | Math | Trust | Evidence | Representation |",
        "|---|---|---|---|---|",
    ]
    rows.extend(
        "| {claim_id} | {math_status} | {trust_status} | {evidence_level} | {representation_id} |".format(**item)
        for item in claims
    )
    atomic_write_text(markdown_path, "\n".join(rows) + "\n")
