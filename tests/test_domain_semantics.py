from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from autonomous_math_research.audit_gate import AuditGate
from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.domain_semantics import (
    builtin_domain_contract,
    domain_semantics_from_contract,
)
from autonomous_math_research.models import (
    AuditResult, CandidateEvent,
    Claim,
    EvidenceLevel,
    MathStatus,
    ResearchTask,
    TrustStatus,
)


def claim(status: str = "OPEN", *, claim_id: str = "C") -> Claim:
    return Claim(
        claim_id=claim_id,
        statement="The frozen claim.",
        assumptions=[],
        math_status=status,
        trust_status=TrustStatus.UNTRUSTED_CANDIDATE,
        dependencies=[],
        downstream_dependents=[],
        evidence_paths=[],
        known_counterexamples=[],
        current_gaps=["independent evidence pending"],
        active_tasks=[],
        last_meaningful_progress=None,
        priority={"score": 1.0},
    )


def event(event_type: str, *, claim_id: str = "C") -> CandidateEvent:
    payload = {
        "event_id": f"E-{event_type}",
        "producer_task_id": "T",
        "claim_id": claim_id,
        "type": event_type,
        "impact": "HIGH",
        "concise_summary": "bounded evidence",
        "exact_statement": "The frozen claim.",
        "artifact_paths": ["artifacts/result.json"],
        "reproduction_commands": ["checker --verify artifacts/result.json"],
        "dependency_impact": [],
    }
    if event_type == "REPRESENTATION_BRIDGE":
        payload["bridge_representation_ids"] = ["rep:one", "rep:two"]
    return CandidateEvent.from_dict(payload)


def task_payload(*, impact_field: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "T",
        "role": "explorer",
        "target_claim": "C",
        "exact_objective": "Run one bounded check.",
        "why_now": "frontier",
        "dependencies": [],
        "expected_information_gain": "HIGH",
        "estimated_cost_tier": "LOW",
        "required_files": [],
        "stop_conditions": ["return the result"],
    }
    payload[impact_field] = "HIGH"
    return payload


class DomainContractTests(unittest.TestCase):
    def test_exact_builtin_domains_and_statuses(self) -> None:
        expected = {
            "math-research": {
                "PROVED", "REDUCED_TO", "PLAUSIBLE", "FAILED", "OPEN",
                "COMPUTATION_ONLY",
            },
            "certified-computational-research": {
                "OPEN", "SUPPORTED", "REFUTED", "INCONCLUSIVE", "CERTIFIED",
            },
            "empirical-research": {
                "OPEN", "SUPPORTED", "NOT_SUPPORTED", "INCONCLUSIVE",
                "CONFIRMED", "REPLICATED",
            },
        }
        for domain, statuses in expected.items():
            contract = builtin_domain_contract(domain)
            self.assertEqual(set(contract["claim_statuses"]), statuses)
            self.assertEqual(domain_semantics_from_contract(contract).domain, domain)

    def test_unknown_and_modified_contracts_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown research domain"):
            builtin_domain_contract("physics-research")
        modified = builtin_domain_contract("empirical-research")
        modified["claim_statuses"].append("PROVED")
        with self.assertRaisesRegex(ValueError, "pinned contract"):
            domain_semantics_from_contract(modified)

    def test_adapter_methods_own_domain_decisions(self) -> None:
        math = domain_semantics_from_contract(None)
        self.assertTrue(math.is_frontier("REDUCED_TO"))
        self.assertTrue(math.dependency_is_satisfied("PROVED"))
        self.assertTrue(math.dependency_is_refuting("FAILED"))
        self.assertEqual(math.final_outcome("PROVED"), "positive")
        self.assertEqual(math.final_outcome("FAILED"), "negative")
        self.assertIsNone(math.final_outcome("OPEN"))
        self.assertEqual(math.required_independent_audits("HIGH", True), 0)
        self.assertEqual(math.required_independent_audits("CRITICAL", True), 2)
        transition = math.event_transition("THEOREM_CANDIDATE")
        self.assertEqual(transition["status"], "PROVED")
        transition["status"] = "FAILED"
        self.assertEqual(
            math.event_transition("THEOREM_CANDIDATE")["status"], "PROVED",
        )
        with self.assertRaisesRegex(
            ValueError, "unsupported event type for math-research: CERTIFICATE",
        ):
            math.event_transition("CERTIFICATE")

        certified = domain_semantics_from_contract(
            builtin_domain_contract("certified-computational-research")
        )
        self.assertTrue(certified.requires_deterministic_checker("CERTIFICATE"))
        with self.assertRaisesRegex(ValueError, "requires at least E4_CERTIFIED"):
            certified.transition_for("CERTIFICATE", EvidenceLevel.E2_EXACT_TESTED)

        empirical = domain_semantics_from_contract(
            builtin_domain_contract("empirical-research")
        )
        self.assertTrue(empirical.requires_frozen_protocol("REPLICATION"))
        self.assertNotIn("PROVED", empirical.claim_statuses)

    def test_domain_audit_minimums_extend_config_without_math_regression(self) -> None:
        math = domain_semantics_from_contract(None)
        certified = domain_semantics_from_contract(
            builtin_domain_contract("certified-computational-research")
        )
        self.assertEqual(
            AuditGate(high_threshold="CRITICAL", semantics=math).required_audits(
                event("THEOREM_CANDIDATE")
            ),
            0,
        )
        self.assertEqual(
            AuditGate(high_threshold="CRITICAL", semantics=certified).required_audits(
                event("CHECKER_SUPPORT")
            ),
            1,
        )

    def test_audit_recovery_rejects_cross_domain_candidate(self) -> None:
        gate = AuditGate(semantics=domain_semantics_from_contract(None))
        with self.assertRaisesRegex(
            ValueError, "unsupported event type for math-research: CERTIFICATE",
        ):
            gate.register(event("CERTIFICATE"))
        self.assertEqual(gate.states, {})

    def test_insufficient_auditor_evidence_cannot_terminalize_domain_gate(self) -> None:
        cases = (
            (
                "certified-computational-research", "CERTIFICATE",
                EvidenceLevel.E4_CERTIFIED, EvidenceLevel.E2_EXACT_TESTED,
                "E4_CERTIFIED",
            ),
            (
                "empirical-research", "CONFIRMATION",
                EvidenceLevel.E2_EXACT_TESTED, EvidenceLevel.E2_EXACT_TESTED,
                "E3_REDUNDANT_EXACT",
            ),
        )
        for domain, event_type, proposed, verified, required in cases:
            with self.subTest(domain=domain):
                semantics = domain_semantics_from_contract(
                    builtin_domain_contract(domain)
                )
                candidate = event(event_type)
                candidate.proposed_evidence_level = proposed
                gate = AuditGate(semantics=semantics)
                state = gate.register(candidate)
                result = AuditResult.from_dict({
                    "audit_id": f"audit-{event_type.lower()}",
                    "candidate_fingerprint": candidate.fingerprint,
                    "auditor_thread_id": "independent-thread",
                    "verdict": "PASS",
                    "audit_kind": "independent_evaluator",
                    "statement_checked": candidate.exact_statement,
                    "checks": [{
                        "name": "bounded reproduction",
                        "passed": True,
                        "detail": "evidence did not reach the domain floor",
                    }],
                    "gaps": [],
                    "notes": [],
                    "report_path": None,
                    "verified_evidence_level": verified,
                })
                with self.assertRaisesRegex(ValueError, f"requires at least {required}"):
                    gate.record(result)
                self.assertEqual(state.results, [])
                self.assertFalse(state.terminal)
                self.assertEqual(state.trust_status, TrustStatus.AUDIT_PENDING)


class ResearchImpactCompatibilityTests(unittest.TestCase):
    def test_legacy_task_input_migrates_to_canonical_field(self) -> None:
        old = ResearchTask.from_dict(task_payload(impact_field="mathematical_impact"))
        self.assertEqual(old.research_impact, "HIGH")
        self.assertEqual(old.mathematical_impact, "HIGH")
        self.assertIn("research_impact", old.to_dict())
        self.assertNotIn("mathematical_impact", old.to_dict())

    def test_new_and_legacy_constructor_keywords_are_supported(self) -> None:
        new = ResearchTask(**task_payload(impact_field="research_impact"))
        old = ResearchTask(**task_payload(impact_field="mathematical_impact"))
        self.assertEqual(new.research_impact, old.research_impact)
        conflicting = task_payload(impact_field="research_impact")
        conflicting["mathematical_impact"] = "LOW"
        with self.assertRaisesRegex(ValueError, "disagree"):
            ResearchTask.from_dict(conflicting)


class DomainClaimGraphTests(unittest.TestCase):
    def test_three_domain_claim_lifecycles_share_the_same_core(self) -> None:
        cases = (
            ("math-research", "THEOREM_CANDIDATE", EvidenceLevel.E0_SPECULATIVE, "PROVED"),
            (
                "certified-computational-research", "CERTIFICATE",
                EvidenceLevel.E4_CERTIFIED, "CERTIFIED",
            ),
            (
                "empirical-research", "REPLICATION",
                EvidenceLevel.E3_REDUNDANT_EXACT, "REPLICATED",
            ),
        )
        for domain, event_type, evidence_level, expected_status in cases:
            with self.subTest(domain=domain):
                semantics = domain_semantics_from_contract(
                    builtin_domain_contract(domain)
                )
                graph = ClaimGraph({"C": claim()}, semantics=semantics)
                candidate = event(event_type)
                graph.mark_candidate(candidate)
                graph.apply_audit_pass(candidate, 1, 1, evidence_level)
                self.assertEqual(graph.claims["C"].research_status, expected_status)
                self.assertEqual(semantics.final_outcome(expected_status), "positive")
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "claim_graph.json"
                    graph.save(path)
                    recovered = ClaimGraph.load(path)
                    self.assertEqual(
                        recovered.claims["C"].research_status, expected_status
                    )

    def test_math_wire_format_and_obligations_remain_compatible(self) -> None:
        graph = ClaimGraph({"C": claim()})
        payload = graph.to_payload(updated_at="2026-08-24T00:00:00Z")
        self.assertNotIn("domain", payload)
        self.assertEqual(payload["claims"][0]["math_status"], MathStatus.OPEN)
        self.assertNotIn("research_status", payload["claims"][0])
        self.assertTrue(graph.claims["C"].proof_obligations)
        self.assertEqual(graph.proof_frontier("C"), graph.research_frontier("C"))
        snapshot = graph.compact_snapshot([], {}, [])
        self.assertIn("strictly_refuted", snapshot)
        self.assertNotIn("domain", snapshot)

    def test_certified_graph_uses_research_status_and_no_proof_obligations(self) -> None:
        semantics = domain_semantics_from_contract(
            builtin_domain_contract("certified-computational-research")
        )
        graph = ClaimGraph({"C": claim()}, semantics=semantics)
        self.assertEqual(graph.claims["C"].proof_obligations, [])
        math_claim = claim()
        ClaimGraph({"C": math_claim})
        with self.assertRaisesRegex(ValueError, "cannot contain proof obligations"):
            ClaimGraph({"C": math_claim}, semantics=semantics)
        graph.apply_audit_pass(
            event("CERTIFICATE"), 1, 1, EvidenceLevel.E4_CERTIFIED,
        )
        self.assertEqual(graph.claims["C"].research_status, "CERTIFIED")
        payload = graph.to_payload(updated_at="2026-08-24T00:00:00Z")
        self.assertEqual(payload["domain"], "certified-computational-research")
        self.assertEqual(payload["claims"][0]["research_status"], "CERTIFIED")
        self.assertNotIn("math_status", payload["claims"][0])
        self.assertNotIn("proof_obligations", payload["claims"][0])
        self.assertEqual(
            graph.research_frontier("C")["obligation_kind"], "certificate"
        )
        snapshot = graph.compact_snapshot([], {}, [])
        self.assertEqual(snapshot["domain"], "certified-computational-research")
        self.assertIn("strictly_negative", snapshot)
        self.assertNotIn("strictly_refuted", snapshot)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim_graph.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            recovered = ClaimGraph.load(path)
            self.assertEqual(recovered.semantics.domain, semantics.domain)
            self.assertEqual(recovered.claims["C"].research_status, "CERTIFIED")
            with self.assertRaisesRegex(ValueError, "does not match"):
                ClaimGraph.load(
                    path,
                    domain_contract=builtin_domain_contract("empirical-research"),
                )

    def test_explicit_null_or_empty_graph_domain_fails_closed(self) -> None:
        for raw_domain in (None, "", "   "):
            with self.subTest(domain=raw_domain), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "claim_graph.json"
                payload = ClaimGraph({"C": claim()}).to_payload()
                payload["domain"] = raw_domain
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "non-empty string"):
                    ClaimGraph.load(path)
                with self.assertRaisesRegex(ValueError, "non-empty string"):
                    ClaimGraph.load(
                        path,
                        domain_contract=builtin_domain_contract(
                            "certified-computational-research"
                        ),
                    )

    def test_empirical_evidence_never_becomes_mathematical_proof(self) -> None:
        semantics = domain_semantics_from_contract(
            builtin_domain_contract("empirical-research")
        )
        with self.assertRaisesRegex(ValueError, "invalid claim status"):
            ClaimGraph({"C": claim("PROVED")}, semantics=semantics)
        graph = ClaimGraph({"C": claim()}, semantics=semantics)
        graph.apply_audit_pass(
            event("EXPERIMENT_SUPPORT"), 1, 1, EvidenceLevel.E2_EXACT_TESTED,
        )
        self.assertEqual(graph.claims["C"].research_status, "SUPPORTED")
        graph.apply_audit_pass(
            event("CONFIRMATION"), 1, 1, EvidenceLevel.E3_REDUNDANT_EXACT,
        )
        self.assertEqual(graph.claims["C"].research_status, "CONFIRMED")
        self.assertNotEqual(graph.claims["C"].research_status, "PROVED")
        self.assertEqual(graph.claims["C"].proof_obligations, [])

    def test_cross_domain_event_fails_before_candidate_mutation(self) -> None:
        semantics = domain_semantics_from_contract(
            builtin_domain_contract("empirical-research")
        )
        graph = ClaimGraph({}, semantics=semantics)
        with self.assertRaisesRegex(ValueError, "unsupported event type"):
            graph.mark_candidate(event("CERTIFICATE", claim_id="NEW"))
        self.assertEqual(graph.claims, {})

    def test_representation_bridge_audit_is_a_complete_claim_noop(self) -> None:
        graph = ClaimGraph({"C": claim()})
        before = deepcopy(graph.claims["C"].to_dict())
        graph.apply_audit_pass(
            event("REPRESENTATION_BRIDGE"), 1, 1, EvidenceLevel.E3_REDUNDANT_EXACT,
        )
        self.assertEqual(graph.claims["C"].to_dict(), before)

    def test_nonmath_opposite_terminal_transition_fails_before_mutation(self) -> None:
        semantics = domain_semantics_from_contract(
            builtin_domain_contract("certified-computational-research")
        )
        terminal = claim("CERTIFIED")
        terminal.trust_status = TrustStatus.CANONICAL_TRUSTED
        graph = ClaimGraph({"C": terminal}, semantics=semantics)
        before = deepcopy(graph.claims["C"].to_dict())
        with self.assertRaisesRegex(ValueError, "conflicts with terminal status"):
            graph.apply_audit_pass(
                event("CHECKER_REFUTATION"), 1, 1, EvidenceLevel.E2_EXACT_TESTED,
            )
        self.assertEqual(graph.claims["C"].to_dict(), before)

    def test_math_direct_graph_transition_preserves_legacy_behavior(self) -> None:
        terminal = claim("PROVED")
        terminal.trust_status = TrustStatus.CANONICAL_TRUSTED
        graph = ClaimGraph({"C": terminal})
        graph.apply_audit_pass(
            event("COUNTEREXAMPLE"), 1, 1, EvidenceLevel.E2_EXACT_TESTED,
        )
        self.assertEqual(graph.claims["C"].research_status, "FAILED")

    def test_insufficient_certificate_evidence_fails_before_mutation(self) -> None:
        semantics = domain_semantics_from_contract(
            builtin_domain_contract("certified-computational-research")
        )
        graph = ClaimGraph({"C": claim()}, semantics=semantics)
        before = deepcopy(graph.claims["C"].to_dict())
        with self.assertRaisesRegex(ValueError, "requires at least E4_CERTIFIED"):
            graph.apply_audit_pass(
                event("CERTIFICATE"), 1, 1, EvidenceLevel.E2_EXACT_TESTED,
            )
        self.assertEqual(graph.claims["C"].to_dict(), before)


if __name__ == "__main__":
    unittest.main()
