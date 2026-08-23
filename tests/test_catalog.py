from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.catalog import (
    build_semantic_index,
    rebuild_catalog,
)
from autonomous_math_research.cli import main
from autonomous_math_research.outcomes import write_outcome_archive
from autonomous_math_research.storage import EventStore, file_digest


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"catalog-{uuid4().hex}"
        self.project = self.root / "sample-project"
        self.autonomous = self.project / "autonomous"
        for name in ("runs", "outcomes", "nightly", "candidates", "audits"):
            (self.autonomous / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _make_run(self, run_id: str = "run-001", *, terminal: bool = True) -> Path:
        run_dir = self.autonomous / "runs" / run_id
        run_dir.mkdir(parents=True)
        artifact = run_dir / "jobs" / "job-1" / "evidence.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"verified": true}\n', encoding="utf-8", newline="\n")
        store = EventStore(run_dir / "EVENTS.jsonl", run_id)
        store.append("RUN_STARTED", {
            "execution_mode": "mock", "project_name": self.project.name,
        })
        store.append("JOB_STARTED", {
            "job_id": "job-1", "task_id": "task-1", "role": "explorer",
            "claim_id": "CLAIM-1", "model": "strong-model",
            "reasoning_effort": "high", "requested_service_tier": None,
            "workspace": str(artifact.parent),
        })
        store.append("JOB_BOUND", {
            "job_id": "job-1", "thread_id": "thread-1", "turn_id": "turn-1",
        })
        store.append("JOB_COMPLETED", {
            "job_id": "job-1", "task_id": "task-1", "role": "explorer",
            "claim_id": "CLAIM-1", "status": "SUCCESS",
            "model": "strong-model", "reasoning_effort": "high",
            "requested_service_tier": None, "observed_service_tier": None,
            "thread_id": "thread-1", "turn_id": "turn-1",
            "token_telemetry": "synthetic",
            "token_usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            "artifact_paths": [str(artifact)],
            "raw_output": "must-not-be-catalogued sk-secretvalue123",
            "server_error": {"authorization": "Bearer private-token"},
            "result": {"main_finding": "not copied into catalog"},
        })
        store.append("MECHANICAL_SUBTASK_REQUESTED", {
            "parent_job_id": "job-1", "parent_task_id": "task-1",
            "parent_role": "explorer", "subtask_id": "mechanical-1",
        })
        store.append("MECHANICAL_SUBTASK_STARTED", {
            "mechanical_job_id": "mechanical-job-1", "parent_job_id": "job-1",
            "parent_task_id": "task-1", "parent_role": "explorer",
            "subtask_id": "mechanical-1", "attempt": 1,
            "model": "gpt-5.3-codex-spark", "reasoning_effort": "high",
            "service_tier": None,
        })
        store.append("MECHANICAL_SUBTASK_COMPLETED", {
            "mechanical_job_id": "mechanical-job-1", "parent_job_id": "job-1",
            "parent_task_id": "task-1", "parent_role": "explorer",
            "subtask_id": "mechanical-1", "status": "SUCCESS",
            "actual_model": "gpt-5.3-codex-spark", "reasoning_effort": "high",
            "service_tier": None, "token_telemetry": "synthetic",
            "token_usage": {"total_tokens": 4}, "artifact_paths": [str(artifact)],
        })
        store.append("CANDIDATE_PROCESSED", {
            "event_id": "candidate-event-1", "fingerprint": "fingerprint-1",
            "claim_id": "CLAIM-1", "impact": "MEDIUM",
            "proposed_evidence_level": "E2_EXACT_TESTED",
            "source_run_id": run_id,
        })
        store.append("AUDIT_RECORDED", {
            "audit_id": "audit-1", "candidate_fingerprint": "fingerprint-1",
            "audit_kind": "REPRODUCTION", "verdict": "UNRESOLVED",
            "trust_status": "UNTRUSTED_CANDIDATE",
            "verified_evidence_level": "E2_EXACT_TESTED",
        })
        store.append("TRUST_STATE_CHANGED", {
            "claim_id": "CLAIM-1", "math_status": "OPEN",
            "trust_status": "AUDIT_1_PASS", "evidence_level": "E2_EXACT_TESTED",
        })
        if terminal:
            store.append("RUN_STOPPED", {
                "execution_mode": "mock", "run_outcome": "mock-validation",
                "internal_failure": False,
                "reason": "normal stop api_key=must-not-survive",
            })
        return run_dir

    def test_semantic_index_is_bounded_redacted_and_links_lifecycle(self) -> None:
        run_dir = self._make_run()
        # The semantic catalog depends only on the authoritative lifecycle log;
        # a damaged optional live feed must not break run finalization/indexing.
        (run_dir / "LIVE_EVENTS.jsonl").write_text("{broken}\n", encoding="utf-8")
        index = build_semantic_index(project_root=self.project, run_dir=run_dir)

        self.assertEqual(index["summary"]["lifecycle_state"], "TERMINAL")
        self.assertEqual(index["summary"]["jobs_started"], 1)
        self.assertEqual(index["summary"]["jobs_terminal"], 1)
        self.assertEqual(index["jobs"][0]["thread_id"], "thread-1")
        self.assertEqual(index["jobs"][0]["token_usage"]["total_tokens"], 8)
        self.assertEqual(index["mechanical_subtasks"][0]["token_usage"]["total_tokens"], 4)
        self.assertEqual(index["mechanical_subtasks"][0]["service_tier"], None)
        self.assertEqual(index["derivation"]["raw_model_output_included"], False)
        self.assertTrue(index["source_events"]["sha256"])

        encoded = json.dumps(index, ensure_ascii=False)
        self.assertNotIn("sk-secretvalue123", encoded)
        self.assertNotIn("private-token", encoded)
        self.assertNotIn("must-not-survive", encoded)
        self.assertIn("[REDACTED]", encoded)
        artifact = next(row for row in index["artifacts"] if row["kind"] == "file")
        self.assertEqual(artifact["relations"], ["job:job-1", "mechanical:job-1:mechanical-1"])

    def test_outcome_v2_separates_append_only_logs_from_immutable_hashes(self) -> None:
        run_dir = self._make_run()
        report = self.autonomous / "nightly" / run_dir.name / "NIGHTLY_REPORT.md"
        report.parent.mkdir(parents=True)
        report.write_text("# report\n", encoding="utf-8", newline="\n")
        events = EventStore(run_dir / "EVENTS.jsonl", run_dir.name).replay()
        outcome_dir = self.autonomous / "outcomes" / run_dir.name
        progress: list[dict] = []

        write_outcome_archive(
            project_root=self.project,
            outcome_dir=outcome_dir,
            run_dir=run_dir,
            report_path=report,
            run_id=run_dir.name,
            execution_mode="mock",
            run_outcome="mock-validation",
            stopped_reason="normal stop",
            internal_failure=False,
            jobs=[],
            events=events,
            final_claim_id=None,
            final_claim=None,
            final_conjecture_proved=False,
            progress=progress.append,
        )

        intermediate = json.loads(
            (outcome_dir / "INTERMEDIATE_INDEX.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(intermediate), {
            "schema_version", "run_id", "generated_at", "project",
            "event_snapshot", "records", "skipped_references",
        })
        self.assertEqual(intermediate["schema_version"], 2)
        self.assertTrue(
            intermediate["event_snapshot"]["logs_excluded_from_file_hashes"]
        )
        indexed_paths = {row["path"] for row in intermediate["records"]}
        self.assertNotIn(
            f"autonomous/runs/{run_dir.name}/EVENTS.jsonl", indexed_paths,
        )
        hashing = [item for item in progress if item["stage"] == "hashing"]
        self.assertEqual(hashing[0]["completed"], 0)
        self.assertEqual(hashing[-1]["completed"], hashing[-1]["total"])
        self.assertEqual(progress[-1]["stage"], "outcome")
        semantic = json.loads(
            (outcome_dir / "SEMANTIC_INDEX.json").read_text(encoding="utf-8")
        )
        self.assertEqual(semantic["run_id"], run_dir.name)
        self.assertIn("SEMANTIC_INDEX.json", (outcome_dir / "OUTCOME.md").read_text(encoding="utf-8"))

    def test_catalog_is_deterministic_and_does_not_modify_run_sources(self) -> None:
        terminal = self._make_run("run-terminal", terminal=True)
        active = self._make_run("run-nonterminal", terminal=False)
        before = {
            terminal.name: file_digest(terminal / "EVENTS.jsonl"),
            active.name: file_digest(active / "EVENTS.jsonl"),
        }

        first = rebuild_catalog(self.project)
        runs_path = self.autonomous / "catalog" / "RUN_CATALOG.jsonl"
        objects_path = self.autonomous / "catalog" / "RESEARCH_OBJECTS.jsonl"
        first_runs = runs_path.read_bytes()
        first_objects = objects_path.read_bytes()
        summary_path = self.autonomous / "catalog" / "CATALOG_SUMMARY.json"
        first_summary = summary_path.read_bytes()
        second = rebuild_catalog(self.project)

        self.assertEqual(first["runs"], 2)
        self.assertEqual(first["runs_by_lifecycle_state"], {
            "NONTERMINAL": 1, "TERMINAL": 1,
        })
        self.assertEqual(first_runs, runs_path.read_bytes())
        self.assertEqual(first_objects, objects_path.read_bytes())
        self.assertEqual(first_summary, summary_path.read_bytes())
        self.assertEqual(first["objects"], second["objects"])
        self.assertEqual(before, {
            terminal.name: file_digest(terminal / "EVENTS.jsonl"),
            active.name: file_digest(active / "EVENTS.jsonl"),
        })
        rows = [json.loads(line) for line in runs_path.read_text(encoding="utf-8").splitlines()]
        active_row = next(row for row in rows if row["run_id"] == "run-nonterminal")
        self.assertIsNone(active_row["source_events"]["sha256"])
        self.assertEqual(active_row["lifecycle_state"], "NONTERMINAL")
        self.assertFalse((self.autonomous / "outcomes" / "run-nonterminal").exists())

    def test_catalog_cli_uses_existing_entrypoint_without_model_calls(self) -> None:
        self._make_run()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["catalog", "--project", str(self.project), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["runs"], 1)
        self.assertTrue((self.autonomous / "catalog" / "CATALOG_SUMMARY.json").is_file())

    def test_semantic_index_rejects_foreign_run_events(self) -> None:
        run_dir = self._make_run()
        events = EventStore(run_dir / "EVENTS.jsonl", run_dir.name).replay()
        events[0] = {**events[0], "run_id": "different-run"}
        with self.assertRaisesRegex(ValueError, "foreign run ids"):
            build_semantic_index(
                project_root=self.project, run_dir=run_dir, events=events,
            )


if __name__ == "__main__":
    unittest.main()
