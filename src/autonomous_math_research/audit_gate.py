from __future__ import annotations

from dataclasses import dataclass, field

from .domain_semantics import DomainSemantics, domain_semantics_from_contract
from .models import (
    AuditResult, CandidateEvent, EvidenceLevel, Impact, TrustStatus, evidence_rank,
)


@dataclass(slots=True)
class CandidateAuditState:
    event: CandidateEvent
    required: int
    results: list[AuditResult] = field(default_factory=list)
    trust_status: str = TrustStatus.AUDIT_PENDING
    terminal: bool = False


class AuditGate:
    def __init__(
        self,
        high_threshold: str = "HIGH",
        critical_double_audit: bool = True,
        semantics: DomainSemantics | None = None,
    ):
        self.high_threshold = high_threshold
        self.critical_double_audit = critical_double_audit
        self.semantics = semantics or domain_semantics_from_contract(None)
        self.states: dict[str, CandidateAuditState] = {}

    def required_audits(self, event: CandidateEvent) -> int:
        ranks = {Impact.LOW: 0, Impact.MEDIUM: 1, Impact.HIGH: 2, Impact.CRITICAL: 3}
        threshold = Impact(self.high_threshold)
        configured = int(ranks[Impact(event.impact)] >= ranks[threshold])
        if event.impact == Impact.CRITICAL and self.critical_double_audit:
            configured = 2
        packed = self.semantics.required_independent_audits(
            event.impact, self.critical_double_audit,
        )
        return max(configured, packed)

    def register(self, event: CandidateEvent) -> CandidateAuditState:
        state = self.states.get(event.fingerprint)
        if state:
            return state
        state = CandidateAuditState(event=event, required=self.required_audits(event))
        self.states[event.fingerprint] = state
        return state

    def next_audit_kind(self, event: CandidateEvent) -> str | None:
        state = self.register(event)
        if state.terminal:
            return None
        if self.pass_count(event.fingerprint) >= state.required:
            return None
        counterexample_types = {"COUNTEREXAMPLE", "KEY_REFUTATION"}
        computation_types = {"COMPUTATIONAL_COLLISION", "COMPUTATIONAL_PATTERN"}
        if (
            self.semantics.requires_deterministic_checker(event.type)
            or self.semantics.requires_frozen_protocol(event.type)
        ):
            return "independent_evaluator"
        if not state.results:
            if event.type in counterexample_types:
                return "counterexample"
            if event.type in computation_types:
                return "independent_evaluator"
            return "proof"
        if event.type in counterexample_types | computation_types:
            return "independent_evaluator"
        return "adversarial"

    def record(self, result: AuditResult) -> str:
        state = self.states[result.candidate_fingerprint]
        if state.terminal:
            raise ValueError("candidate audit state is terminal")
        if " ".join(result.statement_checked.split()) != " ".join(state.event.exact_statement.split()):
            raise ValueError("auditor checked a different statement")
        expected_kind = self.next_audit_kind(state.event)
        if expected_kind is None:
            raise ValueError("candidate has no pending audit")
        if result.audit_kind != expected_kind:
            raise ValueError(f"expected {expected_kind} audit, got {result.audit_kind}")
        if any(item.audit_id == result.audit_id for item in state.results):
            raise ValueError("duplicate audit_id")
        if result.auditor_thread_id and any(
            item.auditor_thread_id == result.auditor_thread_id for item in state.results
        ):
            raise ValueError("critical audits must use independent threads")
        if result.verdict == "PASS" and (
            not result.checks or result.gaps or any(item.get("passed") is not True for item in result.checks)
        ):
            raise ValueError("PASS requires nonempty all-passing checks and no gaps")
        if result.verified_evidence_level == EvidenceLevel.E5_FORMAL:
            raise ValueError("natural-language audit cannot grant E5_FORMAL")
        proposed_rank = evidence_rank(state.event.proposed_evidence_level)
        verified_rank = evidence_rank(result.verified_evidence_level)
        redundant_exact_upgrade = (
            state.event.proposed_evidence_level == EvidenceLevel.E2_EXACT_TESTED
            and result.verified_evidence_level == EvidenceLevel.E3_REDUNDANT_EXACT
            and result.audit_kind == "independent_evaluator"
        )
        if verified_rank > proposed_rank and not redundant_exact_upgrade:
            raise ValueError("audit evidence level exceeds the candidate evidence without an allowed independent upgrade")
        if result.verdict == "PASS":
            self.semantics.transition_for(
                state.event.type, result.verified_evidence_level,
            )
        state.results.append(result)
        if result.verdict == "REJECT":
            state.trust_status = TrustStatus.REJECTED
            state.terminal = True
        elif result.verdict == "UNRESOLVED":
            # Preserve the candidate without converting an inconclusive audit
            # into a claim refutation.  A fresh Director may decide to
            # create a new audit attempt explicitly.
            state.trust_status = TrustStatus.AUDIT_PENDING
            state.terminal = False
        elif len([item for item in state.results if item.verdict == "PASS"]) >= state.required:
            state.trust_status = TrustStatus.AUDITED_NIGHTLY
            state.terminal = True
        else:
            state.trust_status = TrustStatus.AUDIT_1_PASS
        return state.trust_status

    def pass_count(self, fingerprint: str) -> int:
        return sum(result.verdict == "PASS" for result in self.states[fingerprint].results)

    def verified_evidence_level(self, fingerprint: str) -> str:
        passing = [
            result.verified_evidence_level
            for result in self.states[fingerprint].results
            if result.verdict == "PASS"
        ]
        if not passing:
            return EvidenceLevel.E0_SPECULATIVE
        return min(passing, key=evidence_rank)
