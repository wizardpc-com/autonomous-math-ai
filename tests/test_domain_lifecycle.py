from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.config import load_config
from autonomous_math_research.controller import (
    AutonomousController,
    build_mock_full_cycle_backend,
)
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.monitor import format_chat_lifecycle_event
from autonomous_math_research.outcomes import IMPORTANT_EVENT_KINDS
from autonomous_math_research.storage import ProjectLayout
from autonomous_math_research.validation import validate_project


class ThreeDomainLifecycleTests(unittest.TestCase):
    def test_nonmath_internal_failure_resolution_remains_visible(self) -> None:
        kind = "FINAL_CLAIM_RESOLVED_AFTER_INTERNAL_FAILURE"
        self.assertIn(kind, IMPORTANT_EVENT_KINDS)
        rendered = format_chat_lifecycle_event({
            "kind": kind,
            "timestamp": "2026-08-24T00:00:00Z",
            "payload": {
                "claim_id": "C_ROOT",
                "research_status": "CONFIRMED",
            },
        })
        self.assertIsNotNone(rendered)
        self.assertIn("仍按失败处理", str(rendered))

    def test_nonmath_reports_separate_terminal_state_from_finalization(self) -> None:
        from autonomous_math_research.claim_graph import ClaimGraph
        from autonomous_math_research.domain_semantics import (
            builtin_domain_contract,
        )
        from autonomous_math_research.models import Claim, TrustStatus
        from autonomous_math_research.reporting import render_nightly_report

        graph = ClaimGraph(
            {
                "C_ROOT": Claim(
                    claim_id="C_ROOT",
                    statement="A frozen protocol claim.",
                    assumptions=[],
                    math_status="CONFIRMED",
                    trust_status=TrustStatus.AUDITED_NIGHTLY,
                    dependencies=[],
                    downstream_dependents=[],
                    evidence_paths=[],
                    known_counterexamples=[],
                    current_gaps=[],
                    active_tasks=[],
                    last_meaningful_progress=None,
                    priority={"score": 1.0},
                )
            },
            domain_contract=builtin_domain_contract("empirical-research"),
        )
        report = render_nightly_report(
            run_id="failed-after-terminal",
            graph=graph,
            events=[{
                "kind": "FINAL_CLAIM_RESOLVED_AFTER_INTERNAL_FAILURE",
                "payload": {"claim_id": "C_ROOT"},
            }],
            jobs=[],
            stopped_reason="internal failure",
            internal_failure=True,
            final_claim_id="C_ROOT",
        )
        self.assertIn("审计确认领域终态：True", report)
        self.assertIn("有序收尾已启动：False", report)

    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"domain-lifecycle-{uuid4().hex}"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _run_domain(self, domain: str) -> tuple[AutonomousController, object]:
        project = self.root / domain
        initialize_project(project, domain=domain)
        evidence_path: Path | None = None
        if domain != "math-research":
            evidence_path = project / "sources" / "deterministic-smoke.json"
            evidence_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "domain": domain,
                    "protocol_frozen": True,
                    "checker": "deterministic-test-double",
                    "llm_calls": 0,
                }, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        config = load_config(project)
        graph_payload = json.loads(
            ProjectLayout(project).claim_graph_path.read_text(encoding="utf-8")
        )
        final_claim = graph_payload["claims"][0]
        controller = AutonomousController(
            config,
            backend=build_mock_full_cycle_backend(
                claim_id=final_claim["claim_id"],
                statement=final_claim["statement"],
                assumptions=list(final_claim.get("assumptions") or []),
                dependencies=list(final_claim.get("dependencies") or []),
                domain=domain,
                evidence_path=str(evidence_path) if evidence_path else None,
            ),
            mock=True,
        )
        result = asyncio.run(controller.run(0.01))
        return controller, result

    def test_math_certified_and_empirical_share_one_controller_lifecycle(self) -> None:
        expected = {
            "math-research": "PROVED",
            "certified-computational-research": "CERTIFIED",
            "empirical-research": "CONFIRMED",
        }
        for domain, final_status in expected.items():
            with self.subTest(domain=domain):
                controller, result = self._run_domain(domain)
                self.assertFalse(result.internal_failure)
                self.assertTrue(controller.final_claim_resolved)
                self.assertEqual(
                    controller.graph.claims["C_ROOT"].research_status,
                    final_status,
                )
                persisted = json.loads(
                    controller.active_graph_path.read_text(encoding="utf-8")
                )
                events = controller.store.replay()
                kinds = {event["kind"] for event in events}
                if domain == "math-research":
                    self.assertTrue(controller.final_conjecture_proved)
                    self.assertNotIn("domain", persisted)
                    self.assertEqual(persisted["claims"][0]["math_status"], "PROVED")
                    self.assertIn("FINAL_CONJECTURE_PROVED", kinds)
                else:
                    self.assertFalse(controller.final_conjecture_proved)
                    self.assertFalse(controller.final_conjecture_refuted)
                    self.assertEqual(persisted["domain"], domain)
                    self.assertEqual(
                        persisted["claims"][0]["research_status"], final_status
                    )
                    self.assertNotIn("math_status", persisted["claims"][0])
                    self.assertIn("FINAL_CLAIM_RESOLVED", kinds)
                    self.assertNotIn("FINAL_CONJECTURE_PROVED", kinds)
                    report = result.report_path.read_text(encoding="utf-8")
                    outcome = result.outcome_path.read_text(encoding="utf-8")
                    self.assertIn("最终研究主张", report)
                    self.assertIn("最终研究主张", outcome)
                    self.assertIn(final_status, report)

    def test_configured_pack_and_claim_graph_domain_must_match(self) -> None:
        project = self.root / "domain-mismatch"
        initialize_project(project, domain="empirical-research")
        graph_path = ProjectLayout(project).claim_graph_path
        payload = json.loads(graph_path.read_text(encoding="utf-8"))
        payload["domain"] = "certified-computational-research"
        graph_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

        with self.assertRaisesRegex(ValueError, "does not match configured policy pack"):
            validate_project(project)


if __name__ == "__main__":
    unittest.main()
