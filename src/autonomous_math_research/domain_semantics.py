from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


_EVIDENCE_LEVELS = (
    "E0_SPECULATIVE",
    "E1_NUMERIC",
    "E2_EXACT_TESTED",
    "E3_REDUNDANT_EXACT",
    "E4_CERTIFIED",
    "E5_FORMAL",
)
_EVIDENCE_RANK = {level: rank for rank, level in enumerate(_EVIDENCE_LEVELS)}
_IMPACTS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_CONTRACT_KEYS = {
    "domain",
    "semantics_version",
    "claim_statuses",
    "initial_status",
    "candidate_status",
    "frontier_statuses",
    "terminal_positive",
    "terminal_negative",
    "dependency_satisfying",
    "dependency_refuting",
    "obligation_kind",
    "event_transitions",
    "audit_requirements",
}
_TRANSITION_KEYS = {"status", "trust_change", "min_evidence"}
_AUDIT_KEYS = {
    "impact_minimums",
    "critical_double_audit",
    "deterministic_checker_events",
    "frozen_protocol_events",
}


_BUILTIN_DOMAIN_CONTRACTS: dict[str, dict[str, Any]] = {
    "math-research": {
        "domain": "math-research",
        "semantics_version": 1,
        "claim_statuses": [
            "PROVED",
            "REDUCED_TO",
            "PLAUSIBLE",
            "FAILED",
            "OPEN",
            "COMPUTATION_ONLY",
        ],
        "initial_status": "OPEN",
        "candidate_status": "PLAUSIBLE",
        "frontier_statuses": ["OPEN", "PLAUSIBLE", "REDUCED_TO"],
        "terminal_positive": ["PROVED"],
        "terminal_negative": ["FAILED"],
        "dependency_satisfying": ["PROVED"],
        "dependency_refuting": ["FAILED"],
        "obligation_kind": "proof",
        "event_transitions": {
            "THEOREM_CANDIDATE": {
                "status": "PROVED", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "COUNTEREXAMPLE": {
                "status": "FAILED", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "KEY_LEMMA": {
                "status": "PROVED", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "KEY_REFUTATION": {
                "status": "FAILED", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "REDUCTION": {
                "status": "REDUCED_TO", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "OBSTRUCTION": {
                "status": "COMPUTATION_ONLY", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "EQUIVALENCE": {
                "status": "PROVED", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "COMPUTATIONAL_COLLISION": {
                "status": "COMPUTATION_ONLY", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "COMPUTATIONAL_PATTERN": {
                "status": "COMPUTATION_ONLY", "trust_change": True,
                "min_evidence": "E0_SPECULATIVE",
            },
            "REPRESENTATION_BRIDGE": {
                "status": None, "trust_change": False,
                "min_evidence": "E0_SPECULATIVE",
            },
        },
        "audit_requirements": {
            "impact_minimums": {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
            "critical_double_audit": True,
            "deterministic_checker_events": [],
            "frozen_protocol_events": [],
        },
    },
    "certified-computational-research": {
        "domain": "certified-computational-research",
        "semantics_version": 1,
        "claim_statuses": ["OPEN", "SUPPORTED", "REFUTED", "INCONCLUSIVE", "CERTIFIED"],
        "initial_status": "OPEN",
        "candidate_status": "SUPPORTED",
        "frontier_statuses": ["OPEN", "SUPPORTED", "INCONCLUSIVE"],
        "terminal_positive": ["CERTIFIED"],
        "terminal_negative": ["REFUTED"],
        "dependency_satisfying": ["CERTIFIED"],
        "dependency_refuting": ["REFUTED"],
        "obligation_kind": "certificate",
        "event_transitions": {
            "CHECKER_SUPPORT": {
                "status": "SUPPORTED", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "CHECKER_REFUTATION": {
                "status": "REFUTED", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "CERTIFICATE": {
                "status": "CERTIFIED", "trust_change": True,
                "min_evidence": "E4_CERTIFIED",
            },
            "INCONCLUSIVE": {
                "status": "INCONCLUSIVE", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "REPRESENTATION_BRIDGE": {
                "status": None, "trust_change": False,
                "min_evidence": "E0_SPECULATIVE",
            },
        },
        "audit_requirements": {
            "impact_minimums": {"LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 1},
            "critical_double_audit": True,
            "deterministic_checker_events": [
                "CHECKER_SUPPORT", "CHECKER_REFUTATION", "CERTIFICATE", "INCONCLUSIVE",
            ],
            "frozen_protocol_events": [],
        },
    },
    "empirical-research": {
        "domain": "empirical-research",
        "semantics_version": 1,
        "claim_statuses": [
            "OPEN", "SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE", "CONFIRMED", "REPLICATED",
        ],
        "initial_status": "OPEN",
        "candidate_status": "SUPPORTED",
        "frontier_statuses": ["OPEN", "SUPPORTED", "INCONCLUSIVE"],
        "terminal_positive": ["CONFIRMED", "REPLICATED"],
        "terminal_negative": ["NOT_SUPPORTED"],
        "dependency_satisfying": ["CONFIRMED", "REPLICATED"],
        "dependency_refuting": ["NOT_SUPPORTED"],
        "obligation_kind": "empirical_protocol",
        "event_transitions": {
            "EXPERIMENT_SUPPORT": {
                "status": "SUPPORTED", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "EXPERIMENT_NOT_SUPPORTED": {
                "status": "NOT_SUPPORTED", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "CONFIRMATION": {
                "status": "CONFIRMED", "trust_change": True,
                "min_evidence": "E3_REDUNDANT_EXACT",
            },
            "REPLICATION": {
                "status": "REPLICATED", "trust_change": True,
                "min_evidence": "E3_REDUNDANT_EXACT",
            },
            "INCONCLUSIVE": {
                "status": "INCONCLUSIVE", "trust_change": True,
                "min_evidence": "E2_EXACT_TESTED",
            },
            "REPRESENTATION_BRIDGE": {
                "status": None, "trust_change": False,
                "min_evidence": "E0_SPECULATIVE",
            },
        },
        "audit_requirements": {
            "impact_minimums": {"LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 1},
            "critical_double_audit": True,
            "deterministic_checker_events": [],
            "frozen_protocol_events": [
                "EXPERIMENT_SUPPORT", "EXPERIMENT_NOT_SUPPORTED", "CONFIRMATION",
                "REPLICATION", "INCONCLUSIVE",
            ],
        },
    },
}


def builtin_domain_contract(domain: str) -> dict[str, Any]:
    if not isinstance(domain, str):
        raise ValueError(f"unknown research domain: {domain}")
    try:
        return deepcopy(_BUILTIN_DOMAIN_CONTRACTS[domain])
    except KeyError as exc:
        raise ValueError(f"unknown research domain: {domain}") from exc


def _string_list(contract: Mapping[str, Any], key: str) -> list[str]:
    value = contract.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"domain semantics {key} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"domain semantics {key} contains duplicates")
    return list(value)


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, dict) or set(contract) != _CONTRACT_KEYS:
        raise ValueError(f"domain semantics keys must be exactly {sorted(_CONTRACT_KEYS)}")
    domain = contract.get("domain")
    if not isinstance(domain, str) or domain not in _BUILTIN_DOMAIN_CONTRACTS:
        raise ValueError(f"unknown research domain: {domain}")
    semantics_version = contract.get("semantics_version")
    if (
        not isinstance(semantics_version, int)
        or isinstance(semantics_version, bool)
        or semantics_version != 1
    ):
        raise ValueError("unsupported domain semantics_version")

    statuses = _string_list(contract, "claim_statuses")
    status_set = set(statuses)
    for key in (
        "frontier_statuses", "terminal_positive", "terminal_negative",
        "dependency_satisfying", "dependency_refuting",
    ):
        unknown = set(_string_list(contract, key)) - status_set
        if unknown:
            raise ValueError(f"domain semantics {key} has unknown statuses: {sorted(unknown)}")
    for key in ("initial_status", "candidate_status"):
        if not isinstance(contract.get(key), str) or contract[key] not in status_set:
            raise ValueError(f"domain semantics {key} must be a declared claim status")
    if set(contract["terminal_positive"]) & set(contract["terminal_negative"]):
        raise ValueError("positive and negative terminal statuses must be disjoint")
    if not isinstance(contract.get("obligation_kind"), str) or not contract["obligation_kind"]:
        raise ValueError("domain semantics obligation_kind must be a non-empty string")

    transitions = contract.get("event_transitions")
    if not isinstance(transitions, dict) or not transitions:
        raise ValueError("domain semantics event_transitions must be a non-empty object")
    for event_type, transition in transitions.items():
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("domain semantics event type must be a non-empty string")
        if not isinstance(transition, dict) or set(transition) != _TRANSITION_KEYS:
            raise ValueError(
                f"domain transition {event_type} keys must be exactly {sorted(_TRANSITION_KEYS)}"
            )
        if (
            transition["status"] is not None
            and (
                not isinstance(transition["status"], str)
                or transition["status"] not in status_set
            )
        ):
            raise ValueError(f"domain transition {event_type} has an unknown status")
        if not isinstance(transition["trust_change"], bool):
            raise ValueError(f"domain transition {event_type} trust_change must be boolean")
        if (
            not isinstance(transition["min_evidence"], str)
            or transition["min_evidence"] not in _EVIDENCE_RANK
        ):
            raise ValueError(f"domain transition {event_type} has an invalid evidence level")
    bridge = transitions.get("REPRESENTATION_BRIDGE")
    if bridge is None or bridge["status"] is not None or bridge["trust_change"]:
        raise ValueError("REPRESENTATION_BRIDGE must be a status/trust no-op")

    audit = contract.get("audit_requirements")
    if not isinstance(audit, dict) or set(audit) != _AUDIT_KEYS:
        raise ValueError(f"domain audit requirement keys must be exactly {sorted(_AUDIT_KEYS)}")
    minimums = audit["impact_minimums"]
    if not isinstance(minimums, dict) or set(minimums) != set(_IMPACTS):
        raise ValueError(f"impact_minimums keys must be exactly {list(_IMPACTS)}")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in minimums.values()):
        raise ValueError("impact_minimums values must be non-negative integers")
    if not isinstance(audit["critical_double_audit"], bool):
        raise ValueError("critical_double_audit must be boolean")
    for key in ("deterministic_checker_events", "frozen_protocol_events"):
        events = _string_list(audit, key)
        unknown = set(events) - set(transitions)
        if unknown:
            raise ValueError(f"domain audit {key} has unknown events: {sorted(unknown)}")

    normalized = deepcopy(contract)
    if normalized != _BUILTIN_DOMAIN_CONTRACTS[domain]:
        raise ValueError(f"domain semantics do not match pinned contract for {domain}")
    return normalized


@dataclass(frozen=True, slots=True)
class DomainSemantics:
    domain: str
    semantics_version: int
    claim_statuses: tuple[str, ...]
    initial_status: str
    candidate_status: str
    frontier_statuses: frozenset[str]
    terminal_positive: frozenset[str]
    terminal_negative: frozenset[str]
    dependency_satisfying: frozenset[str]
    dependency_refuting: frozenset[str]
    obligation_kind: str
    event_transitions: Mapping[str, Mapping[str, Any]]
    audit_requirements: Mapping[str, Any]

    def validate_status(self, status: str) -> str:
        if status not in self.claim_statuses:
            raise ValueError(f"invalid claim status for {self.domain}: {status}")
        return status

    def validate_event_type(self, event_type: str) -> str:
        if not isinstance(event_type, str) or event_type not in self.event_transitions:
            raise ValueError(f"unsupported event type for {self.domain}: {event_type}")
        return event_type

    def transition_for(self, event_type: str, verified_evidence_level: str) -> dict[str, Any]:
        self.validate_event_type(event_type)
        transition = self.event_transitions[event_type]
        if (
            not isinstance(verified_evidence_level, str)
            or verified_evidence_level not in _EVIDENCE_RANK
        ):
            raise ValueError(f"invalid evidence level: {verified_evidence_level}")
        minimum = str(transition["min_evidence"])
        if _EVIDENCE_RANK[verified_evidence_level] < _EVIDENCE_RANK[minimum]:
            raise ValueError(
                f"{event_type} requires at least {minimum} evidence for {self.domain}"
            )
        return dict(transition)

    def is_frontier(self, status: str) -> bool:
        self.validate_status(status)
        return status in self.frontier_statuses

    def dependency_is_satisfied(self, status: str) -> bool:
        self.validate_status(status)
        return status in self.dependency_satisfying

    def dependency_is_refuting(self, status: str) -> bool:
        self.validate_status(status)
        return status in self.dependency_refuting

    def final_outcome(self, status: str) -> str | None:
        self.validate_status(status)
        if status in self.terminal_positive:
            return "positive"
        if status in self.terminal_negative:
            return "negative"
        return None

    def required_independent_audits(
        self,
        impact: str,
        critical_double_audit: bool,
    ) -> int:
        if impact not in _IMPACTS:
            raise ValueError(f"invalid impact: {impact}")
        if not isinstance(critical_double_audit, bool):
            raise ValueError("critical_double_audit must be boolean")
        required = int(self.audit_requirements["impact_minimums"][impact])
        if (
            impact == "CRITICAL"
            and critical_double_audit
            and self.audit_requirements["critical_double_audit"]
        ):
            required = max(required, 2)
        return required

    def requires_deterministic_checker(self, event_type: str) -> bool:
        self.validate_event_type(event_type)
        return event_type in self.audit_requirements["deterministic_checker_events"]

    def requires_frozen_protocol(self, event_type: str) -> bool:
        self.validate_event_type(event_type)
        return event_type in self.audit_requirements["frozen_protocol_events"]


def domain_semantics_from_contract(contract: dict[str, Any] | None) -> DomainSemantics:
    normalized = _validate_contract(
        builtin_domain_contract("math-research") if contract is None else contract
    )
    transitions = MappingProxyType({
        key: MappingProxyType(dict(value))
        for key, value in normalized["event_transitions"].items()
    })
    audit = normalized["audit_requirements"]
    audit_proxy = MappingProxyType({
        "impact_minimums": MappingProxyType(dict(audit["impact_minimums"])),
        "critical_double_audit": audit["critical_double_audit"],
        "deterministic_checker_events": tuple(audit["deterministic_checker_events"]),
        "frozen_protocol_events": tuple(audit["frozen_protocol_events"]),
    })
    return DomainSemantics(
        domain=normalized["domain"],
        semantics_version=normalized["semantics_version"],
        claim_statuses=tuple(normalized["claim_statuses"]),
        initial_status=normalized["initial_status"],
        candidate_status=normalized["candidate_status"],
        frontier_statuses=frozenset(normalized["frontier_statuses"]),
        terminal_positive=frozenset(normalized["terminal_positive"]),
        terminal_negative=frozenset(normalized["terminal_negative"]),
        dependency_satisfying=frozenset(normalized["dependency_satisfying"]),
        dependency_refuting=frozenset(normalized["dependency_refuting"]),
        obligation_kind=normalized["obligation_kind"],
        event_transitions=transitions,
        audit_requirements=audit_proxy,
    )
