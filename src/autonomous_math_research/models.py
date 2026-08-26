from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Any

from .contracts import (
    DIRECTOR_PLAN_KEYS, LEGACY_DIRECTOR_PLAN_KEYS, SUCCESS_JOB_STATUSES,
)
from .representation import RepresentationContract


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


class MathStatus(StrEnum):
    PROVED = "PROVED"
    REDUCED_TO = "REDUCED_TO"
    PLAUSIBLE = "PLAUSIBLE"
    # The persisted v1/v2 wire value is FAILED.  REFUTED is the preferred
    # name in controller code so mathematical falsity is not confused with an
    # execution failure; FAILED remains a compatibility alias.
    REFUTED = "FAILED"
    FAILED = "FAILED"
    OPEN = "OPEN"
    COMPUTATION_ONLY = "COMPUTATION_ONLY"


class TrustStatus(StrEnum):
    UNTRUSTED_CANDIDATE = "UNTRUSTED_CANDIDATE"
    AUDIT_PENDING = "AUDIT_PENDING"
    AUDIT_1_PASS = "AUDIT_1_PASS"
    AUDIT_2_PASS = "AUDIT_2_PASS"
    AUDITED_NIGHTLY = "AUDITED_NIGHTLY"
    REJECTED = "REJECTED"
    FORMALLY_VERIFIED = "FORMALLY_VERIFIED"
    CANONICAL_TRUSTED = "CANONICAL_TRUSTED"


class EvidenceLevel(StrEnum):
    E0_SPECULATIVE = "E0_SPECULATIVE"
    E1_NUMERIC = "E1_NUMERIC"
    E2_EXACT_TESTED = "E2_EXACT_TESTED"
    E3_REDUNDANT_EXACT = "E3_REDUNDANT_EXACT"
    E4_CERTIFIED = "E4_CERTIFIED"
    E5_FORMAL = "E5_FORMAL"


class ExecutionStatus(StrEnum):
    """Transport/controller lifecycle status, never a mathematical verdict."""

    COMPLETED = "COMPLETED"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"
    ERROR = "ERROR"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


class ObligationStatus(StrEnum):
    OPEN = "OPEN"
    BLOCKED = "BLOCKED"
    DISCHARGED = "DISCHARGED"
    REFUTED = "REFUTED"


EVIDENCE_RANK = {level.value: rank for rank, level in enumerate(EvidenceLevel)}


def infer_evidence_level(source_status: str | None) -> str:
    source = str(source_status or "")
    for level in reversed(list(EvidenceLevel)):
        if level.value in source:
            return level.value
    if "FORMALLY_VERIFIED" in source:
        return EvidenceLevel.E5_FORMAL
    return EvidenceLevel.E0_SPECULATIVE


def evidence_rank(level: str) -> int:
    try:
        return EVIDENCE_RANK[level]
    except KeyError as exc:
        raise ValueError(f"invalid evidence level: {level}") from exc


class Impact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EventType(StrEnum):
    THEOREM_CANDIDATE = "THEOREM_CANDIDATE"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    KEY_LEMMA = "KEY_LEMMA"
    KEY_REFUTATION = "KEY_REFUTATION"
    REDUCTION = "REDUCTION"
    OBSTRUCTION = "OBSTRUCTION"
    EQUIVALENCE = "EQUIVALENCE"
    COMPUTATIONAL_COLLISION = "COMPUTATIONAL_COLLISION"
    COMPUTATIONAL_PATTERN = "COMPUTATIONAL_PATTERN"
    REPRESENTATION_BRIDGE = "REPRESENTATION_BRIDGE"
    CHECKER_SUPPORT = "CHECKER_SUPPORT"
    CHECKER_REFUTATION = "CHECKER_REFUTATION"
    CERTIFICATE = "CERTIFICATE"
    EXPERIMENT_SUPPORT = "EXPERIMENT_SUPPORT"
    EXPERIMENT_NOT_SUPPORTED = "EXPERIMENT_NOT_SUPPORTED"
    CONFIRMATION = "CONFIRMATION"
    REPLICATION = "REPLICATION"
    INCONCLUSIVE = "INCONCLUSIVE"


class Role(StrEnum):
    DIRECTOR = "director"
    PROVER = "prover"
    FALSIFIER = "falsifier"
    EXPLORER = "explorer"
    AUDITOR = "auditor"
    EVALUATOR_AUDITOR = "evaluator_auditor"


class LifecyclePhase(StrEnum):
    BOOTSTRAP = "BOOTSTRAP"
    RUNNING = "RUNNING"
    DRAINING_FAILURE = "DRAINING_FAILURE"
    DRAINING_BUDGET = "DRAINING_BUDGET"
    DRAINING_EPOCH = "DRAINING_EPOCH"
    FINALIZING = "FINALIZING"
    SEALED = "SEALED"
    COMPLETED = "COMPLETED"


TERMINAL_LIFECYCLE_PHASES = frozenset({
    LifecyclePhase.SEALED, LifecyclePhase.COMPLETED,
})


@dataclass(slots=True, init=False)
class ResearchTask:
    task_id: str
    role: str
    target_claim: str
    exact_objective: str
    why_now: str
    dependencies: list[str]
    expected_information_gain: str
    research_impact: str
    estimated_cost_tier: str
    required_files: list[str]
    stop_conditions: list[str]
    output_contract: str = "worker_result.schema.json"
    priority: float = 0.5
    route_family: str = "main"
    modifies_code: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    representation: dict[str, Any] = field(
        default_factory=lambda: RepresentationContract.legacy().to_dict()
    )

    def __init__(
        self,
        task_id: str,
        role: str,
        target_claim: str,
        exact_objective: str,
        why_now: str,
        dependencies: list[str],
        expected_information_gain: str,
        research_impact: str | None = None,
        estimated_cost_tier: str | None = None,
        required_files: list[str] | None = None,
        stop_conditions: list[str] | None = None,
        output_contract: str = "worker_result.schema.json",
        priority: float = 0.5,
        route_family: str = "main",
        modifies_code: bool = False,
        metadata: dict[str, Any] | None = None,
        representation: dict[str, Any] | None = None,
        *,
        mathematical_impact: str | None = None,
    ):
        if research_impact is None:
            research_impact = mathematical_impact
        elif mathematical_impact is not None and mathematical_impact != research_impact:
            raise ValueError("research_impact and mathematical_impact disagree")
        if research_impact is None:
            raise TypeError("missing required research impact")
        if estimated_cost_tier is None:
            raise TypeError("missing required estimated_cost_tier")
        if required_files is None:
            raise TypeError("missing required required_files")
        if stop_conditions is None:
            raise TypeError("missing required stop_conditions")
        self.task_id = task_id
        self.role = role
        self.target_claim = target_claim
        self.exact_objective = exact_objective
        self.why_now = why_now
        self.dependencies = dependencies
        self.expected_information_gain = expected_information_gain
        self.research_impact = research_impact
        self.estimated_cost_tier = estimated_cost_tier
        self.required_files = required_files
        self.stop_conditions = stop_conditions
        self.output_contract = output_contract
        self.priority = priority
        self.route_family = route_family
        self.modifies_code = modifies_code
        self.metadata = {} if metadata is None else dict(metadata)
        self.representation = (
            RepresentationContract.legacy().to_dict()
            if representation is None else representation
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchTask":
        allowed = {f.name for f in cls.__dataclass_fields__.values()} | {"mathematical_impact"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        required = {
            "task_id", "role", "target_claim", "exact_objective", "why_now",
            "dependencies", "expected_information_gain",
            "estimated_cost_tier", "required_files", "stop_conditions",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing task fields: {sorted(missing)}")
        impact_fields = {"research_impact", "mathematical_impact"} & set(data)
        if not impact_fields:
            raise ValueError("missing task field: research_impact")
        if len(impact_fields) == 2 and data["research_impact"] != data["mathematical_impact"]:
            raise ValueError("research_impact and mathematical_impact disagree")
        if data["role"] not in {r.value for r in Role if r not in {Role.DIRECTOR, Role.AUDITOR, Role.EVALUATOR_AUDITOR}}:
            raise ValueError(f"unsupported research role: {data['role']}")
        if not data["stop_conditions"]:
            raise ValueError("task requires at least one stop condition")
        impact = data.get("research_impact", data.get("mathematical_impact"))
        if impact not in {item.value for item in Impact}:
            raise ValueError("invalid task research impact")
        if data["estimated_cost_tier"] not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("invalid task cost tier")
        if data["expected_information_gain"] not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("expected_information_gain must be LOW, MEDIUM, or HIGH")
        if not 0 <= float(data.get("priority", 0.5)) <= 1:
            raise ValueError("task priority must be between 0 and 1")
        normalized = dict(data)
        normalized["research_impact"] = impact
        normalized.pop("mathematical_impact", None)
        normalized.setdefault("output_contract", "worker_result.schema.json")
        normalized.setdefault("representation", RepresentationContract.legacy().to_dict())
        representation = RepresentationContract.from_dict(normalized["representation"])
        normalized["representation"] = representation.to_dict()
        if normalized["output_contract"] != "worker_result.schema.json":
            raise ValueError("research task output contract must be worker_result.schema.json")
        return cls(**normalized)

    @property
    def mathematical_impact(self) -> str:
        """Compatibility alias for pre-domain task callers and persisted plans."""
        return self.research_impact

    @mathematical_impact.setter
    def mathematical_impact(self, value: str) -> None:
        self.research_impact = value

    @property
    def representation_contract(self) -> RepresentationContract:
        return RepresentationContract.from_dict(self.representation)

    @property
    def representation_id(self) -> str:
        return self.representation_contract.representation_id

    @property
    def fingerprint(self) -> str:
        return stable_hash({
            "role": self.role,
            "target_claim": self.target_claim,
            "exact_objective": " ".join(self.exact_objective.split()),
            "dependencies": sorted(self.dependencies),
            "required_files": sorted(self.required_files),
            "representation_id": self.representation_id,
        })

    @property
    def is_independent_exploration(self) -> bool:
        """Whether this task satisfies the scheduler's diversification reserve."""
        return self.route_family == "independent" or self.metadata.get("independent_exploration") is True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def derived_claim_id(
    parent_claim_id: str,
    exact_statement: str,
    assumptions: list[str],
    dependencies: list[str],
) -> str:
    """Return the only claim id accepted for a worker-discovered subclaim.

    The id is content-addressed so a worker cannot use the derived-claim lane to
    smuggle an unrelated mutable identifier past the producer assignment gate.
    """
    suffix = stable_hash({
        "parent_claim_id": parent_claim_id,
        "exact_statement": " ".join(exact_statement.split()),
        "assumptions": sorted(" ".join(item.split()) for item in assumptions),
        "dependencies": sorted(dependencies),
    })[:16].upper()
    return f"{parent_claim_id}::DERIVED::{suffix}"


@dataclass(slots=True)
class DirectorPlan:
    assessment: str
    spawn: list[ResearchTask]
    audit_priorities: list[dict[str, Any]]
    route_updates: list[dict[str, Any]]
    short_rationale: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DirectorPlan":
        required = set(DIRECTOR_PLAN_KEYS)
        if set(data) != required:
            raise ValueError(f"director v2 keys must be exactly {sorted(required)}")
        return cls(
            assessment=str(data["assessment"]),
            spawn=[ResearchTask.from_dict(item) for item in data["spawn"]],
            audit_priorities=list(data["audit_priorities"]),
            route_updates=list(data["route_updates"]),
            short_rationale=str(data["short_rationale"]),
        )

    @classmethod
    def from_legacy_replay(cls, data: dict[str, Any]) -> "DirectorPlan":
        if set(data) != set(LEGACY_DIRECTOR_PLAN_KEYS):
            raise ValueError("historical Director v1 keys are invalid")
        audit_priorities = [
            {
                "candidate_fingerprint": str(item.get("candidate_fingerprint") or ""),
                "priority": 1.0,
                "reason": str(item.get("reason") or "historical audit request"),
            }
            for item in data["audit_requests"]
        ]
        return cls(
            assessment=str(data["assessment"]),
            spawn=[ResearchTask.from_dict(item) for item in data["spawn"]],
            audit_priorities=audit_priorities,
            route_updates=[],
            short_rationale=str(data["short_rationale"]),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        return result


@dataclass(slots=True)
class CandidateEvent:
    event_id: str
    producer_thread_id: str | None
    producer_task_id: str
    claim_id: str
    type: str
    impact: str
    concise_summary: str
    exact_statement: str
    artifact_paths: list[str]
    reproduction_commands: list[str]
    dependency_impact: list[str]
    parent_claim_id: str | None = None
    source_run_id: str | None = None
    assumptions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    proposed_evidence_level: str = EvidenceLevel.E0_SPECULATIVE
    timestamp: str = field(default_factory=utc_now)
    representation: dict[str, Any] = field(
        default_factory=lambda: RepresentationContract.legacy().to_dict()
    )
    bridge_representation_ids: list[str] = field(default_factory=list)
    semantic_bridge_ids: list[str] = field(default_factory=list)
    evidence_receipts: list[dict[str, str]] = field(default_factory=list)
    fingerprint_version: int = 1
    evidence_attempt_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateEvent":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(
                "candidate event cannot set controller-owned trust state; "
                f"unknown fields: {sorted(unknown)}"
            )
        required = allowed - {
            "timestamp", "producer_thread_id", "assumptions", "dependencies",
            "proposed_evidence_level", "parent_claim_id", "source_run_id",
            "representation",
            "bridge_representation_ids",
            "semantic_bridge_ids",
            "evidence_receipts",
            "fingerprint_version",
            "evidence_attempt_id",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing candidate fields: {sorted(missing)}")
        if data["type"] not in {e.value for e in EventType}:
            raise ValueError(f"unsupported candidate type: {data['type']}")
        if data["impact"] not in {e.value for e in Impact}:
            raise ValueError(f"unsupported impact: {data['impact']}")
        normalized = dict(data)
        normalized.setdefault("producer_thread_id", None)
        normalized.setdefault("parent_claim_id", None)
        normalized.setdefault("source_run_id", None)
        normalized.setdefault("assumptions", [])
        normalized.setdefault("dependencies", [])
        normalized.setdefault("proposed_evidence_level", EvidenceLevel.E0_SPECULATIVE)
        normalized.setdefault("representation", RepresentationContract.legacy().to_dict())
        normalized.setdefault("bridge_representation_ids", [])
        normalized.setdefault("semantic_bridge_ids", [])
        normalized.setdefault("evidence_receipts", [])
        normalized.setdefault("fingerprint_version", 1)
        normalized.setdefault("evidence_attempt_id", None)
        if type(normalized["fingerprint_version"]) is not int or normalized[
            "fingerprint_version"
        ] not in {1, 2}:
            raise ValueError("fingerprint_version must be 1 or 2")
        attempt_id = normalized["evidence_attempt_id"]
        if normalized["fingerprint_version"] == 1:
            if attempt_id is not None:
                raise ValueError("legacy candidate fingerprint cannot set evidence_attempt_id")
        elif (
            not isinstance(attempt_id, str)
            or not re.fullmatch(r"attempt-[0-9a-f]{64}", attempt_id)
        ):
            raise ValueError("candidate v2 requires a valid evidence_attempt_id")
        normalized["representation"] = RepresentationContract.from_dict(
            normalized["representation"]
        ).to_dict()
        bridge_ids = normalized["bridge_representation_ids"]
        if not isinstance(bridge_ids, list) or any(
            not isinstance(item, str) or not item.startswith("rep:")
            for item in bridge_ids
        ):
            raise ValueError("bridge_representation_ids must contain representation ids")
        if normalized["type"] == EventType.REPRESENTATION_BRIDGE:
            if len(set(bridge_ids)) != 2:
                raise ValueError("REPRESENTATION_BRIDGE must bind exactly two representations")
        elif bridge_ids:
            raise ValueError("only REPRESENTATION_BRIDGE may set bridge_representation_ids")
        semantic_bridge_ids = normalized["semantic_bridge_ids"]
        if not isinstance(semantic_bridge_ids, list) or any(
            not isinstance(item, str) or not item.startswith("bridge:")
            for item in semantic_bridge_ids
        ):
            raise ValueError("semantic_bridge_ids must contain semantic bridge ids")
        if len(semantic_bridge_ids) != len(set(semantic_bridge_ids)):
            raise ValueError("semantic_bridge_ids contains duplicates")
        receipts = normalized["evidence_receipts"]
        if not isinstance(receipts, list):
            raise ValueError("evidence_receipts must be an array")
        normalized_receipts: list[dict[str, str]] = []
        for index, receipt in enumerate(receipts):
            if not isinstance(receipt, dict) or set(receipt) != {
                "kind", "manifest_path", "run_id",
            }:
                raise ValueError(f"evidence_receipts[{index}] fields are invalid")
            kind = receipt["kind"]
            if kind not in {"deterministic_checker_run", "experiment_run"}:
                raise ValueError(f"evidence_receipts[{index}].kind is invalid")
            manifest_path = receipt["manifest_path"]
            if (
                not isinstance(manifest_path, str)
                or not manifest_path
                or "\\" in manifest_path
            ):
                raise ValueError(
                    f"evidence_receipts[{index}].manifest_path must be project-relative"
                )
            normalized_path = PurePosixPath(manifest_path)
            if (
                normalized_path.is_absolute()
                or ".." in normalized_path.parts
                or normalized_path.as_posix() != manifest_path
                or (normalized_path.parts and normalized_path.parts[0].endswith(":"))
            ):
                raise ValueError(
                    f"evidence_receipts[{index}].manifest_path must be project-relative"
                )
            run_id = receipt["run_id"]
            if (
                not isinstance(run_id, str)
                or not run_id.startswith("run-")
                or len(run_id) != 68
                or any(ch not in "0123456789abcdef" for ch in run_id[4:])
            ):
                raise ValueError(f"evidence_receipts[{index}].run_id is invalid")
            normalized_receipts.append({
                "kind": kind,
                "manifest_path": manifest_path,
                "run_id": run_id,
            })
        if len({
            (item["kind"], item["manifest_path"], item["run_id"])
            for item in normalized_receipts
        }) != len(normalized_receipts):
            raise ValueError("evidence_receipts contains duplicates")
        normalized["evidence_receipts"] = normalized_receipts
        evidence_rank(str(normalized["proposed_evidence_level"]))
        obj = cls(**normalized)
        if obj.parent_claim_id and obj.claim_id == "AUTO_DERIVED":
            obj.claim_id = derived_claim_id(
                obj.parent_claim_id, obj.exact_statement,
                obj.assumptions, obj.dependencies,
            )
        return obj

    @property
    def fingerprint(self) -> str:
        payload: dict[str, Any] = {
            "claim_id": self.claim_id,
            "parent_claim_id": self.parent_claim_id,
            "type": self.type,
            "exact_statement": " ".join(self.exact_statement.split()),
            "assumptions": sorted(" ".join(item.split()) for item in self.assumptions),
            "dependencies": sorted(self.dependencies),
            "representation_id": self.representation_id,
            "bridge_representation_ids": sorted(self.bridge_representation_ids),
        }
        if self.semantic_bridge_ids:
            payload["semantic_bridge_ids"] = list(self.semantic_bridge_ids)
        if self.evidence_receipts:
            payload["evidence_receipts"] = sorted(
                self.evidence_receipts,
                key=lambda item: (item["kind"], item["manifest_path"], item["run_id"]),
            )
        if self.fingerprint_version == 2:
            payload["fingerprint_version"] = 2
            payload["evidence_attempt_id"] = self.evidence_attempt_id
        return stable_hash(payload)

    @property
    def representation_contract(self) -> RepresentationContract:
        return RepresentationContract.from_dict(self.representation)

    @property
    def representation_id(self) -> str:
        return self.representation_contract.representation_id

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["fingerprint"] = self.fingerprint
        return result


@dataclass(slots=True)
class AuditResult:
    audit_id: str
    candidate_fingerprint: str
    auditor_thread_id: str | None
    verdict: str
    audit_kind: str
    statement_checked: str
    checks: list[dict[str, Any]]
    gaps: list[str]
    notes: list[str]
    report_path: str | None
    verified_evidence_level: str = EvidenceLevel.E0_SPECULATIVE
    semantic_authority_context: dict[str, Any] | None = None
    timestamp: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditResult":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown audit fields: {sorted(unknown)}")
        required = allowed - {
            "timestamp", "auditor_thread_id", "report_path", "verified_evidence_level",
            "semantic_authority_context",
            # Legacy run replay did not distinguish blocking gaps from
            # non-blocking audit notes. New App Server output schemas require
            # this field, while local recovery defaults it safely.
            "notes",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing audit fields: {sorted(missing)}")
        if data["verdict"] not in {"PASS", "REJECT", "UNRESOLVED"}:
            raise ValueError(f"invalid audit verdict: {data['verdict']}")
        normalized = dict(data)
        normalized.setdefault("auditor_thread_id", None)
        normalized.setdefault("report_path", None)
        normalized.setdefault("notes", [])
        normalized.setdefault("verified_evidence_level", EvidenceLevel.E0_SPECULATIVE)
        normalized.setdefault("semantic_authority_context", None)
        if normalized["semantic_authority_context"] is not None and not isinstance(
            normalized["semantic_authority_context"], dict
        ):
            raise ValueError("semantic audit authority context must be an object")
        evidence_rank(str(normalized["verified_evidence_level"]))
        return cls(**normalized)

    @classmethod
    def from_wire_v2(
        cls,
        data: dict[str, Any],
        *,
        audit_id: str,
        candidate_fingerprint: str,
        auditor_thread_id: str | None,
        audit_kind: str,
        statement_checked: str,
        report_path: str | None = None,
        semantic_authority_context: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> "AuditResult":
        expected = {
            "verdict", "checks", "gaps", "notes", "verified_evidence_level",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError(f"audit v2 keys must be exactly {sorted(expected)}")
        return cls.from_dict({
            "audit_id": audit_id,
            "candidate_fingerprint": candidate_fingerprint,
            "auditor_thread_id": auditor_thread_id,
            "verdict": data["verdict"],
            "audit_kind": audit_kind,
            "statement_checked": statement_checked,
            "checks": data["checks"],
            "gaps": data["gaps"],
            "notes": data["notes"],
            "verified_evidence_level": data["verified_evidence_level"],
            "semantic_authority_context": semantic_authority_context,
            "report_path": report_path,
            "timestamp": timestamp or utc_now(),
        })

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProofObligation:
    obligation_id: str
    statement: str
    status: str
    dependencies: list[str]
    evidence_paths: list[str]
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProofObligation":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"unknown proof obligation fields: {sorted(unknown)}")
        required = allowed - {"created_at", "updated_at"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing proof obligation fields: {sorted(missing)}")
        normalized = dict(data)
        normalized.setdefault("created_at", None)
        normalized.setdefault("updated_at", None)
        value = cls(**normalized)
        if value.status not in {item.value for item in ObligationStatus}:
            raise ValueError(f"invalid proof obligation status: {value.status}")
        if not value.obligation_id.strip() or not value.statement.strip():
            raise ValueError("proof obligation id and statement must be non-empty")
        if len(value.dependencies) != len(set(value.dependencies)):
            raise ValueError(f"duplicate proof obligation dependencies: {value.obligation_id}")
        return value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Claim:
    claim_id: str
    statement: str
    assumptions: list[str]
    math_status: str
    trust_status: str
    dependencies: list[str]
    downstream_dependents: list[str]
    evidence_paths: list[str]
    known_counterexamples: list[str]
    current_gaps: list[str]
    active_tasks: list[str]
    last_meaningful_progress: str | None
    priority: dict[str, Any]
    parent_claim_id: str | None = None
    source_status: str | None = None
    evidence_level: str = EvidenceLevel.E0_SPECULATIVE
    proof_obligations: list[ProofObligation] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        semantics: Any | None = None,
    ) -> "Claim":
        if semantics is None:
            from .domain_semantics import domain_semantics_from_contract

            semantics = domain_semantics_from_contract(None)
        normalized = dict(data)
        if "research_status" in normalized:
            if (
                "math_status" in normalized
                and normalized["math_status"] != normalized["research_status"]
            ):
                raise ValueError("research_status and math_status disagree")
            normalized["math_status"] = normalized.pop("research_status")
        normalized.setdefault("parent_claim_id", None)
        normalized.setdefault("evidence_level", infer_evidence_level(normalized.get("source_status")))
        normalized["proof_obligations"] = [
            item if isinstance(item, ProofObligation) else ProofObligation.from_dict(item)
            for item in normalized.get("proof_obligations", [])
        ]
        obj = cls(**normalized)
        semantics.validate_status(obj.math_status)
        if obj.trust_status not in {x.value for x in TrustStatus}:
            raise ValueError(f"invalid trust status for {obj.claim_id}")
        evidence_rank(obj.evidence_level)
        return obj

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if result.get("parent_claim_id") is None:
            result.pop("parent_claim_id", None)
        return result

    @property
    def research_status(self) -> str:
        return self.math_status

    @research_status.setter
    def research_status(self, value: str) -> None:
        self.math_status = value


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    uncached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if self.uncached_input_tokens == 0 and (
            self.input_tokens or self.cached_input_tokens
        ):
            self.uncached_input_tokens = max(
                0, self.input_tokens - self.cached_input_tokens,
            )

    @classmethod
    def from_app_server(cls, data: dict[str, Any]) -> "TokenUsage":
        input_tokens = int(data.get("inputTokens", 0))
        cached_input_tokens = int(data.get("cachedInputTokens", 0))
        return cls(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            uncached_input_tokens=int(data.get(
                "uncachedInputTokens",
                max(0, input_tokens - cached_input_tokens),
            )),
            cache_write_input_tokens=int(data.get("cacheWriteInputTokens", 0)),
            output_tokens=int(data.get("outputTokens", 0)),
            reasoning_output_tokens=int(data.get("reasoningOutputTokens", 0)),
            total_tokens=int(data.get("totalTokens", 0)),
        )

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(slots=True)
class JobOutcome:
    job_id: str
    task_id: str
    role: str
    claim_id: str
    status: str
    result: dict[str, Any]
    thread_id: str | None = None
    turn_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    provider: str | None = None
    provider_profile: str | None = None
    requested_service_tier: str | None = None
    observed_service_tier: str | None = None
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    token_telemetry: str = "unknown"
    cost_usd: float | None = None
    cost_telemetry: str = "unknown"
    artifact_paths: list[str] = field(default_factory=list)
    error: str | None = None
    failure_kind: str | None = None
    retryable: bool = False
    server_error: dict[str, Any] | None = None
    terminal_event: dict[str, Any] | None = None
    raw_output: str | None = None
    turn_history: list[dict[str, Any]] = field(default_factory=list)
    canonical_progress: bool = False
    candidate_accepted: bool = False
    logical_stop_reason: str | None = None
    continuation_budget_stop_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        """A role parser may run only after the envelope itself succeeded."""
        return (
            str(self.status or "").strip().lower() in SUCCESS_JOB_STATUSES
            and not self.error
            and not self.failure_kind
        )

    @property
    def failure_message(self) -> str:
        if self.error:
            return str(self.error)
        if isinstance(self.server_error, dict):
            message = self.server_error.get("message")
            if message:
                return str(message)
        kind = self.failure_kind or "job_failure"
        return f"{kind}: status={self.status or 'unknown'}"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["token_usage"] = self.token_usage.to_dict()
        return result
