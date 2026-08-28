from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .canonical_transition import CanonicalTransitionStore, json_bytes
from .claim_graph import ClaimGraph
from .models import CandidateEvent, ObligationStatus, stable_hash, utc_now
from .semantic_alignment import SemanticAlignment
from .storage import append_jsonl, file_digest, read_jsonl
from .storage.artifacts import PORTABLE_SCHEMES, portable_project_uri, resolve_portable_uri


RECONCILIATION_SCHEMA_VERSION = 1
RECONCILIATION_KINDS = frozenset({
    "TERMINAL_CLAIM",
    "PARTIAL_OBLIGATION",
    "NARROW_DERIVED_SUBCLAIM",
})
AUTHORITY_SYNC_IN_SYNC = "IN_SYNC"
AUTHORITY_SYNC_REQUIRED = "RECONCILIATION_REQUIRED"
AUTHORITY_SYNC_PENDING = "RECONCILIATION_PENDING"

_BUNDLE_KEYS = frozenset({
    "schema_version", "kind", "target_claim_id", "target_obligation_id",
    "candidate", "historical_proof_paths", "historical_audit_paths",
})
_STAGE_KEYS = frozenset({
    "schema_version", "kind", "reconciliation_id", "timestamp",
    "bundle_sha256", "bundle", "historical_evidence_hashes",
})
_APPLIED_KEYS = frozenset({
    "schema_version", "kind", "reconciliation_id", "bundle_sha256",
    "candidate_fingerprint", "affected_claim_id", "target_claim_id",
    "target_obligation_id", "semantic_receipt_fingerprint",
    "fresh_audit_receipt_fingerprints", "authority_sync_status",
})


def _exact_dict(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = set(value) if isinstance(value, dict) else set()
        raise ValueError(
            f"{label} fields differ; missing={sorted(keys - actual)}, "
            f"extra={sorted(actual - keys)}"
        )
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _path_list(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} must be a non-empty array of unique paths")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ReconciliationStage:
    reconciliation_id: str
    bundle_sha256: str
    reconciliation_kind: str
    target_claim_id: str
    target_obligation_id: str | None
    candidate_payload: dict[str, Any]
    historical_proof_paths: tuple[str, ...]
    historical_audit_paths: tuple[str, ...]
    historical_evidence_hashes: dict[str, str]

    @property
    def affected_claim_id(self) -> str:
        if self.reconciliation_kind == "PARTIAL_OBLIGATION":
            return self.target_claim_id
        return str(self.candidate_payload["claim_id"])

    def candidate_event(self) -> CandidateEvent:
        return CandidateEvent.from_dict(dict(self.candidate_payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "bundle_sha256": self.bundle_sha256,
            "reconciliation_kind": self.reconciliation_kind,
            "affected_claim_id": self.affected_claim_id,
            "target_claim_id": self.target_claim_id,
            "target_obligation_id": self.target_obligation_id,
            "candidate_fingerprint": self.candidate_event().fingerprint,
            "historical_evidence_hashes": dict(self.historical_evidence_hashes),
        }


class ReconciliationStore:
    """Append-only historical evidence staging plus canonical applied markers."""

    def __init__(self, *, project_root: Path, runtime_root: Path):
        self.project_root = project_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.root = self.runtime_root / "state" / "reconciliation"
        self.ledger_path = self.root / "STAGES.jsonl"
        self.applied_root = self.root / "applied"

    def _resolve_path(self, raw: str) -> Path:
        if raw.startswith(PORTABLE_SCHEMES):
            path = resolve_portable_uri(
                self.project_root, self.runtime_root, raw,
            )
        else:
            relative = PurePosixPath(raw)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != raw
                or (relative.parts and relative.parts[0].endswith(":"))
            ):
                raise ValueError(
                    "reconciliation evidence paths must be project-relative POSIX paths"
                )
            path = (self.project_root / raw).resolve()
        if not path.is_relative_to(self.project_root) or not path.is_file():
            raise ValueError(f"reconciliation evidence is unavailable: {raw}")
        return path

    def _portable_path(self, raw: str) -> str:
        return portable_project_uri(self.project_root, self._resolve_path(raw))

    def _normalize_bundle(self, value: Any) -> tuple[dict[str, Any], dict[str, str]]:
        raw = _exact_dict(value, _BUNDLE_KEYS, "reconciliation bundle")
        if raw["schema_version"] != RECONCILIATION_SCHEMA_VERSION:
            raise ValueError("unsupported reconciliation bundle schema_version")
        reconciliation_kind = _string(raw["kind"], "reconciliation kind")
        if reconciliation_kind not in RECONCILIATION_KINDS:
            raise ValueError("unsupported reconciliation kind")
        target_claim_id = _string(raw["target_claim_id"], "target_claim_id")
        target_obligation_id = raw["target_obligation_id"]
        if reconciliation_kind == "PARTIAL_OBLIGATION":
            target_obligation_id = _string(
                target_obligation_id, "partial reconciliation target_obligation_id",
            )
        elif target_obligation_id is not None:
            raise ValueError(
                "only PARTIAL_OBLIGATION may set target_obligation_id"
            )
        proof_paths = _path_list(
            raw["historical_proof_paths"], "historical_proof_paths",
        )
        audit_paths = _path_list(
            raw["historical_audit_paths"], "historical_audit_paths",
        )
        evidence_paths = tuple(dict.fromkeys((*proof_paths, *audit_paths)))
        evidence_hashes = {
            self._portable_path(path): file_digest(self._resolve_path(path))
            for path in evidence_paths
        }
        candidate_raw = raw["candidate"]
        if not isinstance(candidate_raw, dict):
            raise ValueError("reconciliation candidate must be an object")
        candidate_data = dict(candidate_raw)
        candidate_data["producer_task_id"] = "historical-reconciliation"
        candidate_data["producer_thread_id"] = None
        candidate_data["source_run_id"] = None
        artifact_paths = list(candidate_data.get("artifact_paths") or [])
        artifact_paths.extend(
            path for path in evidence_hashes if path not in artifact_paths
        )
        candidate_data["artifact_paths"] = artifact_paths
        candidate_data.pop("timestamp", None)
        candidate_data.pop("fingerprint", None)
        candidate_data["fingerprint_version"] = 1
        candidate_data["evidence_attempt_id"] = None
        candidate = CandidateEvent.from_dict(candidate_data)
        normalized = {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "kind": reconciliation_kind,
            "target_claim_id": target_claim_id,
            "target_obligation_id": target_obligation_id,
            "candidate": {
                key: value for key, value in candidate.to_dict().items()
                if key not in {"fingerprint", "timestamp"}
            },
            "historical_proof_paths": [self._portable_path(path) for path in proof_paths],
            "historical_audit_paths": [self._portable_path(path) for path in audit_paths],
        }
        return normalized, dict(sorted(evidence_hashes.items()))

    def stage(
        self,
        value: Any,
        *,
        claim_graph: ClaimGraph | None = None,
        semantic_alignment: SemanticAlignment | None = None,
    ) -> tuple[ReconciliationStage, bool]:
        bundle, hashes = self._normalize_bundle(value)
        bundle_sha256 = stable_hash(bundle)
        reconciliation_id = f"reconciliation-{bundle_sha256[:24]}"
        existing = {item.reconciliation_id: item for item in self.stages()}
        if reconciliation_id in existing:
            prior = existing[reconciliation_id]
            if prior.bundle_sha256 != bundle_sha256:
                raise ValueError("reconciliation id collision")
            return prior, False
        prepared = ReconciliationStage(
            reconciliation_id=reconciliation_id,
            bundle_sha256=bundle_sha256,
            reconciliation_kind=str(bundle["kind"]),
            target_claim_id=str(bundle["target_claim_id"]),
            target_obligation_id=bundle["target_obligation_id"],
            candidate_payload=dict(bundle["candidate"]),
            historical_proof_paths=tuple(bundle["historical_proof_paths"]),
            historical_audit_paths=tuple(bundle["historical_audit_paths"]),
            historical_evidence_hashes=hashes,
        )
        if (claim_graph is None) != (semantic_alignment is None):
            raise ValueError("reconciliation stage preflight requires both authorities")
        if claim_graph is not None and semantic_alignment is not None:
            self.validate_stage(
                prepared,
                claim_graph=claim_graph,
                semantic_alignment=semantic_alignment,
            )
        append_jsonl(self.ledger_path, {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "kind": "STAGED",
            "reconciliation_id": reconciliation_id,
            "timestamp": utc_now(),
            "bundle_sha256": bundle_sha256,
            "bundle": bundle,
            "historical_evidence_hashes": hashes,
        })
        return self.get(reconciliation_id), True

    def stages(self) -> tuple[ReconciliationStage, ...]:
        stages: list[ReconciliationStage] = []
        seen: set[str] = set()
        for record in read_jsonl(self.ledger_path):
            raw = _exact_dict(record, _STAGE_KEYS, "reconciliation stage")
            if (
                raw["schema_version"] != RECONCILIATION_SCHEMA_VERSION
                or raw["kind"] != "STAGED"
            ):
                raise ValueError("reconciliation stage record is invalid")
            reconciliation_id = _string(
                raw["reconciliation_id"], "reconciliation_id",
            )
            if reconciliation_id in seen:
                raise ValueError("reconciliation stage ledger contains duplicate ids")
            seen.add(reconciliation_id)
            bundle, hashes = self._normalize_bundle(raw["bundle"])
            bundle_sha256 = stable_hash(bundle)
            if (
                raw["bundle_sha256"] != bundle_sha256
                or reconciliation_id != f"reconciliation-{bundle_sha256[:24]}"
                or raw["historical_evidence_hashes"] != hashes
            ):
                raise ValueError("reconciliation stage digest or evidence changed")
            stages.append(ReconciliationStage(
                reconciliation_id=reconciliation_id,
                bundle_sha256=bundle_sha256,
                reconciliation_kind=str(bundle["kind"]),
                target_claim_id=str(bundle["target_claim_id"]),
                target_obligation_id=bundle["target_obligation_id"],
                candidate_payload=dict(bundle["candidate"]),
                historical_proof_paths=tuple(bundle["historical_proof_paths"]),
                historical_audit_paths=tuple(bundle["historical_audit_paths"]),
                historical_evidence_hashes=hashes,
            ))
        return tuple(stages)

    def get(self, reconciliation_id: str) -> ReconciliationStage:
        matches = [
            item for item in self.stages()
            if item.reconciliation_id == reconciliation_id
        ]
        if len(matches) != 1:
            raise ValueError(f"unknown reconciliation id: {reconciliation_id}")
        return matches[0]

    def validate_stage(
        self,
        stage: ReconciliationStage,
        *,
        claim_graph: ClaimGraph,
        semantic_alignment: SemanticAlignment,
    ) -> CandidateEvent:
        candidate = stage.candidate_event()
        target = claim_graph.claims.get(stage.target_claim_id)
        if target is None:
            raise ValueError(
                f"reconciliation target claim is absent: {stage.target_claim_id}"
            )
        if stage.reconciliation_kind == "TERMINAL_CLAIM":
            if candidate.claim_id != stage.target_claim_id or candidate.parent_claim_id is not None:
                raise ValueError("terminal reconciliation must target the exact claim")
            if (
                " ".join(candidate.exact_statement.split())
                != " ".join(target.statement.split())
                or sorted(candidate.assumptions) != sorted(target.assumptions)
                or sorted(candidate.dependencies) != sorted(target.dependencies)
            ):
                raise ValueError("terminal reconciliation candidate differs from ClaimGraph")
            if target.research_status in (
                claim_graph.semantics.terminal_positive
                | claim_graph.semantics.terminal_negative
            ):
                raise ValueError("terminal reconciliation target is already terminal")
        else:
            if (
                candidate.parent_claim_id != stage.target_claim_id
                or candidate.claim_id == stage.target_claim_id
            ):
                raise ValueError(
                    "partial and narrow reconciliation require a distinct child claim"
                )
            existing = claim_graph.claims.get(candidate.claim_id)
            if existing is not None and (
                " ".join(existing.statement.split())
                != " ".join(candidate.exact_statement.split())
                or existing.parent_claim_id != stage.target_claim_id
            ):
                raise ValueError("reconciliation child claim conflicts with ClaimGraph")
            if (
                stage.reconciliation_kind == "NARROW_DERIVED_SUBCLAIM"
                and existing is not None
                and existing.research_status in (
                    claim_graph.semantics.terminal_positive
                    | claim_graph.semantics.terminal_negative
                )
            ):
                raise ValueError("narrow reconciliation child is already terminal")
        if stage.reconciliation_kind == "PARTIAL_OBLIGATION":
            obligation = next((
                item for item in target.proof_obligations
                if item.obligation_id == stage.target_obligation_id
            ), None)
            if obligation is None:
                raise ValueError(
                    f"reconciliation proof obligation is absent: {stage.target_obligation_id}"
                )
            if obligation.status not in {
                ObligationStatus.OPEN, ObligationStatus.BLOCKED,
            }:
                raise ValueError("reconciliation proof obligation is already terminal")
        binding = semantic_alignment.claims.get(candidate.claim_id)
        if binding is None:
            raise ValueError(
                "historical reconciliation requires a semantic binding for its exact claim"
            )
        if (
            candidate.representation_id != binding.representation_id
            or tuple(candidate.semantic_bridge_ids) != binding.required_bridges
        ):
            raise ValueError(
                "reconciliation candidate does not match its RepresentationContract or bridge path"
            )
        declared_evidence: set[str] = set()
        for bridge_id in binding.required_bridges:
            bridge = semantic_alignment.bridges.get(bridge_id)
            if bridge is None:
                raise ValueError(
                    f"reconciliation semantic bridge is undeclared: {bridge_id}"
                )
            declared_evidence.update(
                portable_project_uri(
                    self.project_root,
                    (self.project_root / relative).resolve(),
                )
                for relative in bridge.evidence
            )
        if not declared_evidence.issubset(set(candidate.artifact_paths)):
            raise ValueError(
                "reconciliation candidate does not seal all declared bridge evidence"
            )
        for raw in candidate.artifact_paths:
            self._resolve_path(raw)
        return candidate

    def applied_marker_path(self, reconciliation_id: str) -> Path:
        return self.applied_root / f"{reconciliation_id}.json"

    def applied_marker(self, reconciliation_id: str) -> dict[str, Any] | None:
        path = self.applied_marker_path(reconciliation_id)
        if not path.is_file():
            return None
        return _exact_dict(
            json.loads(path.read_text(encoding="utf-8")),
            _APPLIED_KEYS,
            "reconciliation applied marker",
        )

    def applied_marker_bytes(
        self,
        stage: ReconciliationStage,
        *,
        candidate_fingerprint: str,
        semantic_receipt_fingerprint: str,
        fresh_audit_receipts: Iterable[dict[str, Any]],
    ) -> bytes:
        audit_fingerprints = sorted(
            stable_hash(dict(item)) for item in fresh_audit_receipts
        )
        if not audit_fingerprints:
            raise ValueError("reconciliation requires a fresh audit receipt")
        return json_bytes({
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "kind": "APPLIED",
            "reconciliation_id": stage.reconciliation_id,
            "bundle_sha256": stage.bundle_sha256,
            "candidate_fingerprint": candidate_fingerprint,
            "affected_claim_id": stage.affected_claim_id,
            "target_claim_id": stage.target_claim_id,
            "target_obligation_id": stage.target_obligation_id,
            "semantic_receipt_fingerprint": semantic_receipt_fingerprint,
            "fresh_audit_receipt_fingerprints": audit_fingerprints,
            "authority_sync_status": AUTHORITY_SYNC_IN_SYNC,
        })

    def _require_applied_authority(
        self,
        stage: ReconciliationStage,
        marker: dict[str, Any],
        transition_store: CanonicalTransitionStore,
    ) -> None:
        if (
            marker["schema_version"] != RECONCILIATION_SCHEMA_VERSION
            or marker["kind"] != "APPLIED"
            or marker["reconciliation_id"] != stage.reconciliation_id
            or marker["bundle_sha256"] != stage.bundle_sha256
            or marker["affected_claim_id"] != stage.affected_claim_id
            or marker["target_claim_id"] != stage.target_claim_id
            or marker["target_obligation_id"] != stage.target_obligation_id
            or marker["authority_sync_status"] != AUTHORITY_SYNC_IN_SYNC
        ):
            raise ValueError("reconciliation applied marker disagrees with its stage")
        marker_uri = portable_project_uri(
            self.project_root, self.applied_marker_path(stage.reconciliation_id),
        )
        matches = []
        for transaction in transition_store.verified_committed_transactions():
            authorization = transaction["authorization"]
            target_paths = {item["path"] for item in transaction["targets"]}
            if (
                authorization.get("kind") == "AUDITED_CLAIM_TRANSITION"
                and authorization.get("reconciliation_id") == stage.reconciliation_id
                and authorization.get("reconciliation_bundle_sha256")
                == stage.bundle_sha256
                and marker_uri in target_paths
            ):
                matches.append(transaction)
        if len(matches) != 1:
            raise ValueError(
                "reconciliation applied marker lacks one canonical transition authority"
            )

    def summary(
        self,
        *,
        transition_store: CanonicalTransitionStore | None = None,
        pending_reconciliation_id: str | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for stage in self.stages():
            marker = self.applied_marker(stage.reconciliation_id)
            if marker is not None and transition_store is not None:
                self._require_applied_authority(stage, marker, transition_store)
            status = (
                AUTHORITY_SYNC_IN_SYNC
                if marker is not None
                else AUTHORITY_SYNC_PENDING
                if stage.reconciliation_id == pending_reconciliation_id
                else AUTHORITY_SYNC_REQUIRED
            )
            items.append({**stage.to_dict(), "authority_sync_status": status})
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "stages": items,
            "claim_status": {
                item["affected_claim_id"]: item["authority_sync_status"]
                for item in items
                if item["authority_sync_status"] != AUTHORITY_SYNC_IN_SYNC
            },
        }

    def drift_claim_ids(
        self, *, transition_store: CanonicalTransitionStore | None = None,
    ) -> frozenset[str]:
        return frozenset(
            self.summary(transition_store=transition_store)["claim_status"]
        )
