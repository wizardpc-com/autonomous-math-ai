from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autonomous_math_research.models import JobOutcome, ResearchTask, TokenUsage
from autonomous_math_research.research_record import (
    ResearchRecordStore,
    decision_reason_code,
)


class ResearchRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.runtime = self.project / "autonomous"
        self.run_dir = self.runtime / "runs" / "epoch-1"
        self.campaign_root = self.runtime / "campaigns" / "campaign-1"
        self.project.mkdir(parents=True)
        self.store = ResearchRecordStore(
            project_root=self.project,
            runtime_root=self.runtime,
            run_dir=self.run_dir,
            campaign_root=self.campaign_root,
            run_id="epoch-1",
            campaign_id="campaign-1",
            epoch_id="epoch-1",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_context_is_immutable_hash_verified_and_replayable(self) -> None:
        claim_graph = self.project / "claim_graph.json"
        claim_graph.write_text('{"claims": []}\n', encoding="utf-8")
        purpose = self.store.pin_purpose(
            "EVALUATION", default="NATURAL_RESEARCH", started_at="start",
        )
        manifest = self.store.freeze_context_before(
            started_at="start",
            run_purpose=purpose,
            sources={"claim_graph": claim_graph, "frontier_before": None},
            theme=None,
            runtime_provenance={"amr_version": "test", "codex_cli_version": None},
        )
        replay = self.store.replay_context()
        self.assertTrue(replay["verified"])
        self.assertEqual(replay["snapshots"]["claim_graph"], {"claims": []})
        self.assertEqual(manifest["run_purpose"], "EVALUATION")
        with self.assertRaises(ValueError):
            self.store.pin_purpose(
                "DEVELOPMENT", default="DEVELOPMENT", started_at="later",
            )
        snapshot = self.store.context_root / "claim_graph.json"
        snapshot.write_text('{"claims": ["tampered"]}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.store.replay_context()

    def test_decisions_results_and_metrics_are_orthogonal(self) -> None:
        task = ResearchTask(
            task_id="task-1", role="prover", target_claim="C1",
            exact_objective="Prove the exact bounded statement.", why_now="frontier",
            dependencies=[], expected_information_gain="HIGH",
            research_impact="HIGH", estimated_cost_tier="LOW",
            required_files=[], stop_conditions=["proof or counterexample"],
        )
        decision = self.store.director_decision(
            run_id="epoch-1", director_job_id="director-1", task=task,
            decision="SUPPRESSED", reason_code="KILL_GATED",
            reason_detail="exact audited method failure", stage="ADMISSION",
        )
        self.assertEqual(decision["reason_code"], "KILL_GATED")
        self.assertEqual(decision_reason_code("THEME_SCOPE_VIOLATION"), "THEME_EXCLUDED")

        outcome = JobOutcome(
            job_id="job-1", task_id=task.task_id, role=task.role,
            claim_id=task.target_claim, status="COMPLETED",
            result={
                "result_type": "PROOF", "main_finding": "exact proof",
                "status": "COMPLETED", "artifact_paths": [],
                "next_suggested_question": "audit", "evidence_level": "E0_SPECULATIVE",
                "asset_usage": [],
            },
            token_usage=TokenUsage(input_tokens=7, output_tokens=3, total_tokens=10),
        )
        result, _ = self.store.record_result(
            outcome=outcome, task=task,
            job_record={"artifact_hashes": {}, "elapsed_seconds": 2.0},
            asset_usage=[],
        )
        self.assertEqual(result["math_status"], "PROVED")
        self.assertEqual(result["audit_status"], "PENDING")
        self.assertEqual(result["authority_status"], "PENDING")

        events = [
            {"sequence": 1, "kind": "DIRECTOR_TASK_DECISION", "payload": decision},
            {"sequence": 2, "kind": "RESEARCH_RESULT_RECORDED", "payload": {
                "research_outcome": "PROVED_RESULT",
            }},
        ]
        metrics = self.store.metrics(
            run_id="epoch-1", campaign_id="campaign-1", epoch_id="epoch-1",
            events=events, jobs=[{
                "role": "prover", "token_usage": outcome.token_usage.to_dict(),
                "cost_usd": None, "elapsed_seconds": 2.0,
            }], mechanical_jobs=[], frontier_delta={"new_reusable_assets": ["A1"]},
            started_at="start", ended_at="end",
        )
        self.assertEqual(metrics["kill_gate_suppression"], 1)
        self.assertEqual(metrics["proved_results"], 1)
        self.assertEqual(metrics["new_assets"], 1)
        self.assertEqual(metrics["token_usage"]["total_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
