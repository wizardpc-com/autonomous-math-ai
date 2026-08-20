from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import AutonomousController
from autonomous_math_research.engine.scheduler import DynamicScheduler
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.lifecycle.audit_lease import AuditLeaseBook
from autonomous_math_research.lifecycle.campaign import CampaignStore
from autonomous_math_research.lifecycle.cognition import (
    CORE_CAPSULE_MAX_BYTES,
    RouteLedger,
    write_core_capsule,
    write_research_map,
)
from autonomous_math_research.lifecycle.state import (
    LifecyclePhase,
    MonotoneLifecycle,
)
from autonomous_math_research.models import CandidateEvent, JobOutcome, ResearchTask
from autonomous_math_research.project import (
    ProjectManifest,
    discover_workspace_root,
)
from autonomous_math_research.representation import (
    RepresentationContract,
    require_compatible_representations,
)
from autonomous_math_research.storage import ProjectLayout
from autonomous_math_research.storage_layer.artifacts import ArtifactStore
from autonomous_math_research.storage_layer.steering import (
    append_steering,
    ingest_asset,
)


def representation(branch: str) -> RepresentationContract:
    return RepresentationContract.from_dict({
        "branch": branch,
        "localization": "none",
        "saturation": "none",
        "normalization": "primitive",
        "content": "retained",
        "exceptional_factors": [],
        "combination_scope": "same-branch",
    })


def research_task(
    task_id: str,
    *,
    gain: str = "HIGH",
    route: str = "route-a",
    contract: RepresentationContract | None = None,
) -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        role="explorer",
        target_claim="C_ROOT",
        exact_objective="Perform one bounded check.",
        why_now="architecture regression",
        dependencies=[],
        expected_information_gain=gain,
        mathematical_impact="HIGH",
        estimated_cost_tier="LOW",
        required_files=[],
        stop_conditions=["return the finite result"],
        route_family=route,
        metadata={"allow_derived_claims": False},
        representation=(contract or RepresentationContract.legacy()).to_dict(),
    )


class NextArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"next-architecture-{uuid4().hex}"
        self.root.mkdir()
        self.project = initialize_project(self.root / "external-neutral-project")
        self.runtime = self.project / "autonomous"

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_project_manifest_works_outside_harness_repository(self) -> None:
        (self.project / ".git").mkdir()
        manifest = ProjectManifest.load(self.project)
        self.assertEqual(manifest.project_id, "external-neutral-project")
        self.assertEqual(discover_workspace_root(self.project), self.project)

    def test_run_id_rejects_path_escape(self) -> None:
        layout = ProjectLayout(self.project)
        with self.assertRaises(ValueError):
            layout.run_dir("../outside")

    def test_monotone_lifecycle_rejects_continuation_after_drain(self) -> None:
        lifecycle = MonotoneLifecycle()
        lifecycle.transition(LifecyclePhase.RUNNING, reason="start")
        lifecycle.transition(LifecyclePhase.DRAINING_BUDGET, reason="budget")
        with self.assertRaises(ValueError):
            lifecycle.transition(LifecyclePhase.RUNNING, reason="late continuation")

    def test_identical_representation_combines_without_bridge(self) -> None:
        value = representation("branch-a")
        require_compatible_representations(value, value)

    def test_cross_branch_combination_requires_audited_bridge(self) -> None:
        left = representation("branch-a")
        right = representation("branch-b")
        with self.assertRaisesRegex(ValueError, "REPRESENTATION_BRIDGE"):
            require_compatible_representations(left, right)
        pair = {tuple(sorted((left.representation_id, right.representation_id)))}
        require_compatible_representations(
            left, right, audited_bridge_ids=pair,
        )

    def test_representation_changes_task_fingerprint(self) -> None:
        first = research_task("same", contract=representation("branch-a"))
        second = research_task("same", contract=representation("branch-b"))
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_audit_lease_deduplicates_and_only_raises_priority(self) -> None:
        book = AuditLeaseBook(self.runtime / "campaigns" / "c" / "AUDIT_LEASES.jsonl")
        first = book.ensure("fingerprint", "proof", priority=0.4)
        same = book.ensure("fingerprint", "proof", priority=0.9)
        self.assertEqual(first.lease_id, same.lease_id)
        self.assertEqual(same.priority, 0.9)
        self.assertEqual(len(book.snapshot()), 1)

    def test_audit_lease_state_machine_is_fail_closed(self) -> None:
        book = AuditLeaseBook(self.runtime / "campaigns" / "c" / "AUDIT_LEASES.jsonl")
        lease = book.ensure("fingerprint", "proof", priority=0.5)
        book.activate(lease.lease_id, "job-1")
        book.retry_wait(lease.lease_id)
        book.activate(lease.lease_id, "job-2")
        book.finish(lease.lease_id, "UNRESOLVED")
        with self.assertRaises(ValueError):
            book.activate(lease.lease_id, "job-3")

    def test_dynamic_scheduler_reduces_only_new_research_admission(self) -> None:
        scheduler = DynamicScheduler(max_research=8, max_audit=8)
        tasks = [research_task("task")]
        self.assertEqual(scheduler.decide(
            pending_audits=0, active_audits=0, tasks=tasks, route_counts={},
        ).target_research, 8)
        self.assertEqual(scheduler.decide(
            pending_audits=8, active_audits=0, tasks=tasks, route_counts={},
        ).target_research, 4)
        self.assertEqual(scheduler.decide(
            pending_audits=16, active_audits=0, tasks=tasks, route_counts={},
        ).target_research, 2)

    def test_high_pressure_admits_high_gain_or_new_representation(self) -> None:
        scheduler = DynamicScheduler(max_research=8, max_audit=8)
        known = representation("known")
        low_known = research_task("low-known", gain="LOW", contract=known)
        high_known = research_task("high-known", gain="HIGH", contract=known)
        low_new = research_task("low-new", gain="LOW", contract=representation("new"))
        known_ids = {known.representation_id}
        self.assertFalse(scheduler.eligible_under_pressure(
            low_known, pressure=2.0, known_representation_ids=known_ids,
        ))
        self.assertTrue(scheduler.eligible_under_pressure(
            high_known, pressure=2.0, known_representation_ids=known_ids,
        ))
        self.assertTrue(scheduler.eligible_under_pressure(
            low_new, pressure=2.0, known_representation_ids=known_ids,
        ))

    def test_failed_route_waits_for_declared_retry_condition(self) -> None:
        ledger = RouteLedger(self.runtime / "campaigns" / "c" / "ROUTE_LEDGER.jsonl")
        ledger.append(
            route_id="route-a", representation_id="rep:a", method_tags=["finite"],
            status="FAILED", failure_class="exhausted",
            retry_condition="new_evidence:C_ROOT", evidence_refs=[], source="worker",
        )
        self.assertFalse(ledger.route_is_retryable("route-a", set()))
        self.assertTrue(ledger.route_is_retryable(
            "route-a", {"new_evidence:C_ROOT"},
        ))

    def test_campaign_epoch_records_are_append_only_and_idempotent(self) -> None:
        store = CampaignStore(self.runtime, "campaign-1")
        store.create(project_id="external-neutral-project")
        store.append_epoch_started(epoch_id="epoch-1", previous_epoch_id=None, mode="mock")
        store.append_epoch_started(epoch_id="epoch-1", previous_epoch_id=None, mode="mock")
        store.append_epoch_sealed(
            epoch_id="epoch-1", elapsed_seconds=10, status="PAUSED",
            stopped_reason="epoch complete", checkpoint_uri="epoch://epoch-1/state/checkpoint.json",
        )
        checkpoint = store.load()
        self.assertEqual(checkpoint.epochs, ("epoch-1",))
        self.assertEqual(checkpoint.elapsed_epoch_seconds, 10)
        self.assertEqual(checkpoint.status, "PAUSED")

        store.append_epoch_started(
            epoch_id="epoch-2", previous_epoch_id="epoch-1", mode="mock",
        )
        self.assertEqual(store.load().status, "ACTIVE")

    def test_campaign_epoch_id_rejects_path_escape(self) -> None:
        store = CampaignStore(self.runtime, "campaign-1")
        store.create(project_id="external-neutral-project")
        with self.assertRaises(ValueError):
            store.append_epoch_sealed(
                epoch_id="../escape", elapsed_seconds=0, status="PAUSED",
                stopped_reason="invalid",
                checkpoint_uri="epoch://invalid/state/checkpoint.json",
            )

    def test_fresh_epoch_imports_valid_pending_research(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-one", campaign_id="campaign-1",
        )
        first._pin_run_inputs(0.01, True)
        first.pending_research = [research_task("carry-forward")]
        first._write_compact_snapshot()
        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-two", campaign_id="campaign-1",
            previous_epoch_id="epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()
        self.assertEqual(
            [task.task_id for task in second.pending_research],
            ["carry-forward"],
        )
        self.assertIn(
            "EPOCH_CHECKPOINT_IMPORTED",
            [event["kind"] for event in second.store.replay()],
        )

    def test_fresh_epoch_rejects_invalid_pending_research(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-one", campaign_id="campaign-1",
        )
        first._pin_run_inputs(0.01, True)
        first.pending_research = [research_task("carry-forward")]
        snapshot_path = first._write_compact_snapshot()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["pending_research"][0]["expected_information_gain"] = "UNBOUNDED"
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-two", campaign_id="campaign-1",
            previous_epoch_id="epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        with self.assertRaisesRegex(ValueError, "pending research task is invalid"):
            second._import_previous_epoch_checkpoint()

    def test_compact_snapshot_exposes_representation_compatibility(self) -> None:
        config = load_config(self.project)
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="representation-view", campaign_id="campaign-1",
        )
        controller._pin_run_inputs(0.01, True)
        snapshot = json.loads(
            controller._write_compact_snapshot().read_text(encoding="utf-8")
        )
        compatibility = snapshot["representation_compatibility"]
        legacy = RepresentationContract.legacy()
        self.assertEqual(
            compatibility["claims_by_representation_id"][legacy.representation_id],
            ["C_ROOT"],
        )
        self.assertEqual(
            compatibility["known_contracts"][legacy.representation_id],
            legacy.to_dict(),
        )
        self.assertEqual(compatibility["contract_missing_for_ids"], [])
        self.assertEqual(compatibility["audited_bridges"], [])
        self.assertEqual(snapshot["route_state"], [])

    def test_trusted_representation_contract_id_must_match_content(self) -> None:
        trusted_path = self.project / "autonomous" / "state" / "nightly_trusted.json"
        trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
        trusted["representation_contracts"] = {
            "rep:not-the-content-hash": representation("specialized").to_dict(),
        }
        trusted_path.write_text(
            json.dumps(trusted, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "id does not match its content"):
            AutonomousController(
                load_config(self.project), backend=MockCodexBackend(), mock=True,
                run_id="bad-representation-state", campaign_id="campaign-1",
            )

    def test_rejected_tasks_with_route_updates_trigger_bounded_replan(self) -> None:
        config = load_config(self.project)
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="repair-plan", campaign_id="campaign-1",
        )
        controller.lifecycle.transition(LifecyclePhase.RUNNING, reason="test")
        controller.director_needed = False
        task = research_task("incompatible", contract=representation("specialized"))
        task.dependencies = ["C_ROOT"]
        task_payload = task.to_dict()
        task_payload.pop("output_contract")
        plan = {
            "assessment": "One bounded specialized route is proposed.",
            "spawn": [task_payload],
            "audit_priorities": [],
            "route_updates": [{
                "route_id": "route-note", "action": "PAUSE",
                "reason": "record a durable route decision", "retry_condition": None,
            }],
            "short_rationale": "Exercise semantic admission repair.",
        }
        outcome = JobOutcome(
            job_id="director-1", task_id="director-1", role="director",
            claim_id="FRONTIER", status="completed", result=plan,
        )

        controller._accept_director_result(outcome)

        events = controller.store.replay()
        self.assertEqual(controller.pending_research, [])
        self.assertTrue(controller.director_needed)
        self.assertIsNone(controller.scheduler_stop_reason)
        self.assertEqual(controller.director_retry_counts["model_protocol"], 1)
        self.assertIn("TASK_REJECTED", [item["kind"] for item in events])
        self.assertIn("DIRECTOR_PLAN_REPAIR_REQUIRED", [item["kind"] for item in events])
        self.assertIn("DIRECTOR_RETRY_QUEUED", [item["kind"] for item in events])
        self.assertEqual(controller.director_constraints[0]["action"], "REPAIR_PLAN")
        self.assertEqual(
            controller.director_constraints[0]["rejected_tasks"][0]["task_id"],
            "incompatible",
        )
        self.assertFalse(controller.route_ledger.route_is_retryable("route-note", set()))

        controller.director_needed = False
        controller._accept_director_result(outcome)

        self.assertFalse(controller._internal_failure)
        self.assertEqual(controller.lifecycle.phase, LifecyclePhase.DRAINING_EPOCH)
        self.assertIn("director failed after bounded retries", controller.scheduler_stop_reason)
        self.assertIn(
            "DIRECTOR_FAILURE_ISOLATED",
            [item["kind"] for item in controller.store.replay()],
        )

    def test_core_capsule_and_research_map_are_noncanonical_and_bounded(self) -> None:
        graph = ClaimGraph.load(self.runtime / "state" / "claim_graph.json")
        capsule_path = self.runtime / "campaigns" / "c" / "CORE_CAPSULE.json"
        capsule = write_core_capsule(
            capsule_path, graph=graph,
            recent_changes=[{"text": "x" * 5000} for _ in range(30)],
            active_tasks=[], audit_leases=[], route_records=[], representations={},
        )
        self.assertLessEqual(capsule_path.stat().st_size, CORE_CAPSULE_MAX_BYTES)
        self.assertEqual(capsule["authority"], "derived_noncanonical")
        json_path = capsule_path.with_name("RESEARCH_MAP.json")
        markdown_path = capsule_path.with_name("RESEARCH_MAP.md")
        write_research_map(
            json_path, markdown_path, graph=graph,
            route_records=[], representations={},
        )
        self.assertEqual(
            json.loads(json_path.read_text(encoding="utf-8"))["authority"],
            "derived_noncanonical",
        )

    def test_ingested_asset_uses_portable_content_addressed_uri(self) -> None:
        CampaignStore(self.runtime, "campaign-1").create(
            project_id="external-neutral-project",
        )
        source = self.root / "input.txt"
        source.write_text("finite input\n", encoding="utf-8")
        record = ingest_asset(
            self.runtime, "campaign-1", source, description="finite local input",
        )
        self.assertTrue(record["uri"].startswith("campaign://campaign-1/assets/"))
        self.assertNotIn(str(source.resolve()), json.dumps(record))

    def test_human_steering_cannot_write_trust_or_arbitrary_tasks(self) -> None:
        CampaignStore(self.runtime, "campaign-1").create(
            project_id="external-neutral-project",
        )
        record = append_steering(
            self.runtime, "campaign-1", kind="REQUEST_AUDIT",
            note="independent check", claim_id="C_ROOT", audit_kind="proof",
        )
        self.assertEqual(record["kind"], "REQUEST_AUDIT")
        with self.assertRaises(ValueError):
            append_steering(
                self.runtime, "campaign-1", kind="SPAWN_TASK",
                note="not an allowed steering operation",
            )

    def test_sealed_candidate_bundle_survives_producer_edit(self) -> None:
        epoch_root = self.runtime / "runs" / "epoch-1"
        store = ArtifactStore(
            self.project, "campaign-1", "epoch-1", epoch_root,
        )
        source = self.project / "artifacts" / "evidence.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("accepted bytes\n", encoding="utf-8")
        event = CandidateEvent(
            event_id="candidate", producer_thread_id=None,
            producer_task_id="task", claim_id="C_ROOT", parent_claim_id=None,
            type="KEY_LEMMA", impact="HIGH", concise_summary="candidate",
            exact_statement="One exact candidate statement.",
            artifact_paths=["artifacts/evidence.txt"],
            reproduction_commands=[], dependency_impact=[],
        )
        hashes = store.seal_candidate(event)
        source.write_text("producer changed source\n", encoding="utf-8")
        self.assertTrue(store.verify(hashes)[0])
        self.assertTrue(all(path.startswith("epoch://epoch-1/") for path in event.artifact_paths))


if __name__ == "__main__":
    unittest.main()
