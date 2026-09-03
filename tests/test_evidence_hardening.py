from __future__ import annotations

from copy import deepcopy
import unittest

from autonomous_math_research.audit_gate import AuditGate
from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.models import (
    AuditResult,
    CandidateEvent,
    EvidenceLevel,
    MathStatus,
    TrustStatus,
)


def make_claim(*, evidence: str = EvidenceLevel.E4_CERTIFIED) -> object:
    from autonomous_math_research.models import Claim

    return Claim(
        claim_id="C",
        statement="For every admissible object, property P holds.",
        assumptions=["object is admissible"],
        math_status=MathStatus.PROVED,
        trust_status=TrustStatus.CANONICAL_TRUSTED,
        dependencies=["BASE"],
        downstream_dependents=[],
        evidence_paths=["proofs/existing.md"],
        known_counterexamples=[],
        current_gaps=[],
        active_tasks=[],
        last_meaningful_progress="2026-08-16",
        priority={"score": 1.0},
        source_status="PROVED_INFORMALLY / E4_CERTIFIED",
        evidence_level=evidence,
    )


def make_base() -> object:
    from autonomous_math_research.models import Claim

    return Claim(
        claim_id="BASE",
        statement="Base fact.",
        assumptions=[],
        math_status=MathStatus.PROVED,
        trust_status=TrustStatus.CANONICAL_TRUSTED,
        dependencies=[],
        downstream_dependents=[],
        evidence_paths=[],
        known_counterexamples=[],
        current_gaps=[],
        active_tasks=[],
        last_meaningful_progress=None,
        priority={"score": 0.1},
    )


def candidate(
    *,
    event_type: str = "THEOREM_CANDIDATE",
    impact: str = "CRITICAL",
    assumptions: list[str] | None = None,
    dependencies: list[str] | None = None,
    evidence: str = EvidenceLevel.E4_CERTIFIED,
) -> CandidateEvent:
    return CandidateEvent.from_dict({
        "event_id": f"candidate-{event_type.lower()}",
        "producer_thread_id": "producer-thread",
        "producer_task_id": "producer-task",
        "claim_id": "C",
        "type": event_type,
        "impact": impact,
        "concise_summary": "bounded candidate",
        "exact_statement": "For every admissible object, property P holds.",
        "artifact_paths": ["proofs/candidate.md"],
        "reproduction_commands": [],
        "dependency_impact": [],
        "assumptions": ["object is admissible"] if assumptions is None else assumptions,
        "dependencies": ["BASE"] if dependencies is None else dependencies,
        "proposed_evidence_level": evidence,
    })


def audit(
    event: CandidateEvent,
    *,
    audit_id: str,
    kind: str,
    verdict: str = "PASS",
    evidence: str = EvidenceLevel.E4_CERTIFIED,
) -> AuditResult:
    return AuditResult.from_dict({
        "audit_id": audit_id,
        "candidate_fingerprint": event.fingerprint,
        "auditor_thread_id": f"auditor-{audit_id}",
        "verdict": verdict,
        "audit_kind": kind,
        "statement_checked": event.exact_statement,
        "checks": [{
            "name": "exact statement",
            "passed": verdict == "PASS",
            "detail": "independent bounded test",
        }],
        "gaps": [] if verdict == "PASS" else ["candidate not established"],
        "notes": [],
        "report_path": None,
        "verified_evidence_level": evidence,
    })


class EvidenceIsolationTests(unittest.TestCase):
    def graph(self) -> ClaimGraph:
        return ClaimGraph({"BASE": make_base(), "C": make_claim()})

    def test_registration_does_not_mutate_existing_claim(self) -> None:
        graph = self.graph()
        before = deepcopy(graph.claims["C"].to_dict())

        graph.mark_candidate(candidate())

        self.assertEqual(graph.claims["C"].to_dict(), before)

    def test_existing_claim_requires_exact_assumptions_and_dependencies(self) -> None:
        graph = self.graph()
        with self.assertRaisesRegex(ValueError, "assumptions"):
            graph.mark_candidate(candidate(assumptions=[]))
        with self.assertRaisesRegex(ValueError, "dependencies"):
            graph.mark_candidate(candidate(dependencies=[]))

    def test_partial_then_final_pass_is_atomic_and_never_downgrades(self) -> None:
        graph = self.graph()
        event = candidate(evidence=EvidenceLevel.E2_EXACT_TESTED)
        before = deepcopy(graph.claims["C"].to_dict())
        graph.mark_candidate(event)

        graph.apply_audit_pass(event, 1, 2, EvidenceLevel.E2_EXACT_TESTED)
        self.assertEqual(graph.claims["C"].to_dict(), before)

        graph.apply_audit_pass(event, 2, 2, EvidenceLevel.E2_EXACT_TESTED)
        claim = graph.claims["C"]
        self.assertEqual(claim.trust_status, TrustStatus.AUDITED_NIGHTLY)
        self.assertEqual(claim.evidence_level, EvidenceLevel.E4_CERTIFIED)
        self.assertEqual(
            claim.evidence_paths,
            ["proofs/candidate.md", "proofs/existing.md"],
        )

    def test_reject_and_unresolved_do_not_mutate_claim(self) -> None:
        for verdict in ("REJECT", "UNRESOLVED"):
            with self.subTest(verdict=verdict):
                graph = self.graph()
                event = candidate(impact="HIGH")
                before = deepcopy(graph.claims["C"].to_dict())
                graph.mark_candidate(event)
                graph.apply_audit_reject(event, verdict)
                self.assertEqual(graph.claims["C"].to_dict(), before)

    def test_reject_marks_only_candidate_created_evidence_as_rejected(self) -> None:
        graph = self.graph()
        event = CandidateEvent.from_dict({
            "event_id": "candidate-derived",
            "producer_thread_id": "producer-thread",
            "producer_task_id": "producer-task",
            "claim_id": "DERIVED",
            "parent_claim_id": "C",
            "type": "KEY_LEMMA",
            "impact": "HIGH",
            "concise_summary": "bounded derived candidate",
            "exact_statement": "A derived statement remains to be proved.",
            "artifact_paths": ["proofs/candidate.md"],
            "reproduction_commands": [],
            "dependency_impact": [],
            "assumptions": [],
            "dependencies": ["C"],
            "proposed_evidence_level": EvidenceLevel.E2_EXACT_TESTED,
        })
        graph.mark_candidate(event)
        before = deepcopy(graph.claims["DERIVED"].to_dict())

        changed = graph.apply_audit_reject(event, "independent audit rejected evidence")

        claim = graph.claims["DERIVED"]
        self.assertTrue(changed)
        self.assertEqual(claim.math_status, before["math_status"])
        self.assertEqual(claim.evidence_level, before["evidence_level"])
        self.assertEqual(claim.current_gaps, before["current_gaps"])
        self.assertTrue(claim.proof_obligations)
        self.assertTrue(all(item.status == "OPEN" for item in claim.proof_obligations))
        self.assertEqual(claim.trust_status, TrustStatus.REJECTED)

    def test_only_passed_counterexample_can_mark_failed(self) -> None:
        graph = self.graph()
        event = candidate(event_type="COUNTEREXAMPLE", impact="HIGH")
        before = deepcopy(graph.claims["C"].to_dict())
        graph.mark_candidate(event)
        graph.apply_audit_reject(event, "bad witness")
        self.assertEqual(graph.claims["C"].to_dict(), before)

        graph.apply_audit_pass(event, 1, 1, EvidenceLevel.E2_EXACT_TESTED)
        self.assertEqual(graph.claims["C"].math_status, MathStatus.FAILED)
        self.assertEqual(graph.claims["C"].trust_status, TrustStatus.AUDITED_NIGHTLY)


class TerminalAuditTests(unittest.TestCase):
    def test_pass_and_reject_are_terminal(self) -> None:
        cases = (
            ("PASS", TrustStatus.AUDITED_NIGHTLY),
            ("REJECT", TrustStatus.REJECTED),
        )
        for verdict, expected in cases:
            with self.subTest(verdict=verdict):
                event = candidate(impact="HIGH")
                gate = AuditGate()
                gate.register(event)
                result = audit(
                    event,
                    audit_id=f"first-{verdict.lower()}",
                    kind="proof",
                    verdict=verdict,
                )
                self.assertEqual(gate.record(result), expected)
                self.assertTrue(gate.states[event.fingerprint].terminal)
                self.assertIsNone(gate.next_audit_kind(event))
                with self.assertRaisesRegex(ValueError, "terminal"):
                    gate.record(audit(
                        event,
                        audit_id=f"late-{verdict.lower()}",
                        kind="proof",
                    ))

    def test_unresolved_remains_available_for_fresh_audit(self) -> None:
        event = candidate(impact="HIGH")
        gate = AuditGate()
        gate.register(event)
        result = audit(
            event, audit_id="first-unresolved", kind="proof", verdict="UNRESOLVED",
        )
        self.assertEqual(gate.record(result), TrustStatus.AUDIT_PENDING)
        self.assertFalse(gate.states[event.fingerprint].terminal)
        self.assertEqual(gate.next_audit_kind(event), "adversarial")

    def test_critical_first_pass_is_not_terminal(self) -> None:
        event = candidate(impact="CRITICAL")
        gate = AuditGate()
        gate.register(event)
        self.assertEqual(
            gate.record(audit(event, audit_id="one", kind="proof")),
            TrustStatus.AUDIT_1_PASS,
        )
        self.assertFalse(gate.states[event.fingerprint].terminal)
        self.assertEqual(gate.next_audit_kind(event), "adversarial")


if __name__ == "__main__":
    unittest.main()
