from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..models import stable_hash, utc_now
from ..storage import append_jsonl, read_jsonl


class AuditLeaseStatus(StrEnum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    RETRY_WAIT = "RETRY_WAIT"
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


TERMINAL_AUDIT_LEASE_STATUSES = {
    AuditLeaseStatus.PASSED,
    AuditLeaseStatus.REJECTED,
    AuditLeaseStatus.UNRESOLVED,
}


@dataclass(slots=True)
class AuditLease:
    lease_id: str
    candidate_fingerprint: str
    audit_kind: str
    attempt: int
    status: str
    priority: float
    updated_at: str
    job_id: str | None = None


class AuditLeaseBook:
    def __init__(self, path: Path):
        self.path = path
        self._current: dict[tuple[str, str], AuditLease] = {}
        for event in read_jsonl(path):
            lease_raw = event.get("lease")
            if not isinstance(lease_raw, dict):
                continue
            lease = AuditLease(**lease_raw)
            self._current[(lease.candidate_fingerprint, lease.audit_kind)] = lease

    def _append(self, action: str, lease: AuditLease) -> AuditLease:
        lease.updated_at = utc_now()
        append_jsonl(self.path, {
            "schema_version": 1,
            "action": action,
            "lease": asdict(lease),
        })
        self._current[(lease.candidate_fingerprint, lease.audit_kind)] = lease
        return lease

    def ensure(
        self,
        candidate_fingerprint: str,
        audit_kind: str,
        *,
        priority: float,
    ) -> AuditLease:
        key = (candidate_fingerprint, audit_kind)
        current = self._current.get(key)
        if current is not None and current.status not in TERMINAL_AUDIT_LEASE_STATUSES:
            if priority > current.priority:
                current.priority = priority
                self._append("PRIORITIZED", current)
            return current
        attempt = 1 if current is None else current.attempt + 1
        lease = AuditLease(
            lease_id="audit-lease-" + stable_hash({
                "candidate_fingerprint": candidate_fingerprint,
                "audit_kind": audit_kind,
                "attempt": attempt,
            })[:20],
            candidate_fingerprint=candidate_fingerprint,
            audit_kind=audit_kind,
            attempt=attempt,
            status=AuditLeaseStatus.PENDING,
            priority=float(priority),
            updated_at=utc_now(),
        )
        return self._append("CREATED", lease)

    def get(self, candidate_fingerprint: str, audit_kind: str) -> AuditLease | None:
        return self._current.get((candidate_fingerprint, audit_kind))

    def activate(self, lease_id: str, job_id: str) -> AuditLease:
        lease = self._by_id(lease_id)
        if lease.status not in {AuditLeaseStatus.PENDING, AuditLeaseStatus.RETRY_WAIT}:
            raise ValueError(f"audit lease cannot activate from {lease.status}")
        lease.status = AuditLeaseStatus.ACTIVE
        lease.job_id = job_id
        return self._append("ACTIVATED", lease)

    def retry_wait(self, lease_id: str) -> AuditLease:
        lease = self._by_id(lease_id)
        if lease.status != AuditLeaseStatus.ACTIVE:
            raise ValueError(f"audit lease cannot retry from {lease.status}")
        lease.status = AuditLeaseStatus.RETRY_WAIT
        lease.job_id = None
        return self._append("RETRY_WAIT", lease)

    def finish(self, lease_id: str, verdict: str) -> AuditLease:
        lease = self._by_id(lease_id)
        if lease.status != AuditLeaseStatus.ACTIVE:
            raise ValueError(f"audit lease cannot finish from {lease.status}")
        mapping = {
            "PASS": AuditLeaseStatus.PASSED,
            "REJECT": AuditLeaseStatus.REJECTED,
            "UNRESOLVED": AuditLeaseStatus.UNRESOLVED,
        }
        if verdict not in mapping:
            raise ValueError(f"unsupported audit verdict: {verdict}")
        lease.status = mapping[verdict]
        return self._append("FINISHED", lease)

    def prioritize(self, candidate_fingerprint: str, priority: float) -> bool:
        changed = False
        for (fingerprint, _), lease in self._current.items():
            if fingerprint != candidate_fingerprint or lease.status in TERMINAL_AUDIT_LEASE_STATUSES:
                continue
            if priority > lease.priority:
                lease.priority = float(priority)
                self._append("PRIORITIZED", lease)
                changed = True
        return changed

    def recover_stale_active(self) -> int:
        """Move leases owned by a sealed/crashed epoch back to retry wait."""
        recovered = 0
        for lease in list(self._current.values()):
            if lease.status != AuditLeaseStatus.ACTIVE:
                continue
            lease.status = AuditLeaseStatus.RETRY_WAIT
            lease.job_id = None
            self._append("RECOVERED_RETRY_WAIT", lease)
            recovered += 1
        return recovered

    def _by_id(self, lease_id: str) -> AuditLease:
        for lease in self._current.values():
            if lease.lease_id == lease_id:
                return lease
        raise ValueError(f"unknown audit lease: {lease_id}")

    def by_id(self, lease_id: str) -> AuditLease:
        return self._by_id(lease_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            asdict(lease)
            for lease in sorted(self._current.values(), key=lambda item: item.lease_id)
        ]
