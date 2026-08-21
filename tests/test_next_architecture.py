from __future__ import annotations

import asyncio
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
from autonomous_math_research.models import CandidateEvent, Claim, JobOutcome, ResearchTask
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

    def test_turn_limit_checkpoints_task_for_next_epoch_without_stagnation(self) -> None:
        config = load_config(self.project)
        self.assertEqual(config.raw["engine"]["research_max_turns"], {
            "prover": 12, "falsifier": 12, "explorer": 12,
        })
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-turn-limit", campaign_id="campaign-turn-limit",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("long-proof-task")
        outcome = JobOutcome(
            job_id="job-long-proof",
            task_id=task.task_id,
            role=task.role,
            claim_id=task.target_claim,
            status="completed",
            result={
                "result_type": "NEW_LEMMA",
                "status": "OPEN",
                "main_finding": "A derived noncanonical intermediate step.",
                "next_suggested_question": "Resolve the next open obligation.",
                "artifact_paths": [],
                "evidence_level": "E0_SPECULATIVE",
            },
            turn_history=[{"turn_index": index} for index in range(1, 13)],
            logical_stop_reason="bounded same-thread turn limit reached",
        )

        first._accept_research_result(outcome, task)

        self.assertEqual(first.pending_research, [])
        self.assertEqual(len(first.deferred_research_continuations), 1)
        continued = first.deferred_research_continuations[0]
        self.assertNotEqual(continued.task_id, task.task_id)
        checkpoint_refs = [
            item for item in continued.required_files
            if "/state/research_checkpoints/" in item
        ]
        self.assertEqual(len(checkpoint_refs), 1)
        checkpoint = first.artifact_store.resolve_uri(checkpoint_refs[0])
        payload = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(payload["authority"], "derived_noncanonical")
        self.assertEqual(payload["trust_effect"], "none")
        self.assertEqual(payload["source_task_id"], task.task_id)
        self.assertEqual(payload["continuation_task_id"], continued.task_id)
        self.assertEqual(payload["turn_count"], 12)
        self.assertEqual(
            payload["proof_frontier"], first.graph.proof_frontier("C_ROOT"),
        )
        self.assertEqual(
            payload["current_obligation"],
            payload["proof_frontier"]["next_obligation_id"],
        )
        self.assertEqual(
            payload["next_obligation"],
            outcome.result["next_suggested_question"],
        )
        self.assertEqual(payload["completed_evidence"], {
            "candidate_accepted": False,
            "canonical_progress": False,
            "artifact_hashes": {},
        })
        self.assertEqual(first.stagnation.attempts, {})
        latest_route = first.route_ledger.records()[-1]
        self.assertEqual(latest_route["status"], "PAUSED")
        self.assertEqual(
            latest_route["retry_condition"], "next_epoch:epoch-turn-limit",
        )
        snapshot = json.loads(
            first._write_compact_snapshot().read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["task_id"] for item in snapshot["pending_research"]],
            [continued.task_id],
        )
        self.assertEqual(
            [item["task_id"] for item in snapshot["research_continuation_checkpoints"]],
            [continued.task_id],
        )

    def test_controller_token_boundary_checkpoints_without_route_failure(self) -> None:
        config = load_config(self.project)
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-token-boundary", campaign_id="campaign-token-boundary",
        )
        controller._pin_run_inputs(0.01, True)
        task = research_task("token-boundary-task")
        outcome = JobOutcome(
            job_id="job-token-boundary",
            task_id=task.task_id,
            role=task.role,
            claim_id=task.target_claim,
            status="completed",
            result={
                "result_type": "NO_PROGRESS",
                "status": "OPEN",
                "main_finding": "The current obligation remains incomplete.",
                "next_suggested_question": "Resume the current obligation.",
                "artifact_paths": [],
                "evidence_level": "E0_SPECULATIVE",
            },
            turn_history=[{"turn_index": 1}],
            logical_stop_reason="controller token budget reached",
        )

        controller._accept_research_result(outcome, task)

        self.assertEqual(len(controller.deferred_research_continuations), 1)
        self.assertEqual(controller.stagnation.attempts, {})
        route = controller.route_ledger.records()[-1]
        self.assertEqual(route["status"], "PAUSED")
        self.assertIsNone(route["failure_class"])

    def test_provider_quota_exact_task_survives_fresh_epoch_import(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="quota-epoch-one", campaign_id="quota-campaign",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("quota-carry-forward")
        outcome = JobOutcome(
            job_id="quota-job", task_id=task.task_id, role=task.role,
            claim_id=task.target_claim, status="ERROR", result={},
            failure_kind="provider_quota_exhausted", retryable=False,
            error="provider usage quota exhausted",
            server_error={"provider_reset_at": "2026-08-22T00:00:00Z"},
        )
        first._accept_research_result(outcome, task)
        first._write_compact_snapshot()

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="quota-epoch-two", campaign_id="quota-campaign",
            previous_epoch_id="quota-epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()

        self.assertEqual(
            [item.task_id for item in second.pending_research], [task.task_id],
        )
        self.assertFalse(second._internal_failure)

    def test_next_epoch_validates_and_imports_turn_limit_checkpoint(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-one", campaign_id="campaign-continuation",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("long-proof-task")
        outcome = JobOutcome(
            job_id="job-long-proof",
            task_id=task.task_id,
            role=task.role,
            claim_id=task.target_claim,
            status="completed",
            result={
                "result_type": "NO_PROGRESS",
                "status": "OPEN",
                "main_finding": "The obligation remains open.",
                "next_suggested_question": "Continue the exact same task.",
                "artifact_paths": [],
                "evidence_level": "E0_SPECULATIVE",
            },
            turn_history=[{"turn_index": index} for index in range(1, 13)],
            logical_stop_reason="bounded same-thread turn limit reached",
        )
        first._accept_research_result(outcome, task)
        continued = first.deferred_research_continuations[0]
        first._write_compact_snapshot()

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-two", campaign_id="campaign-continuation",
            previous_epoch_id="epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()

        self.assertEqual(
            [item.task_id for item in second.pending_research],
            [continued.task_id],
        )
        self.assertEqual(
            second.task_fingerprints_by_id[continued.task_id],
            continued.fingerprint,
        )
        events = [item["kind"] for item in second.store.replay()]
        self.assertIn("RESEARCH_CONTINUATION_IMPORTED", events)
        self.assertIn("TASK_ACCEPTED", events)
        self.assertEqual(second.route_ledger.records()[-1]["status"], "ACTIVE")

    def test_next_epoch_rejects_tampered_turn_limit_checkpoint(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-one", campaign_id="campaign-continuation-tamper",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("long-proof-task")
        outcome = JobOutcome(
            job_id="job-long-proof",
            task_id=task.task_id,
            role=task.role,
            claim_id=task.target_claim,
            status="completed",
            result={
                "result_type": "NO_PROGRESS",
                "status": "OPEN",
                "main_finding": "The obligation remains open.",
                "next_suggested_question": "Continue the exact same task.",
                "artifact_paths": [],
                "evidence_level": "E0_SPECULATIVE",
            },
            turn_history=[{"turn_index": index} for index in range(1, 13)],
            logical_stop_reason="bounded same-thread turn limit reached",
        )
        first._accept_research_result(outcome, task)
        continued = first.deferred_research_continuations[0]
        first._write_compact_snapshot()
        checkpoint_uri = next(
            item for item in continued.required_files
            if "/state/research_checkpoints/" in item
        )
        checkpoint_path = first.artifact_store.resolve_uri(checkpoint_uri)
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint["last_result"]["main_finding"] = "tampered"
        checkpoint_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-two", campaign_id="campaign-continuation-tamper",
            previous_epoch_id="epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        with self.assertRaisesRegex(ValueError, "checkpoint digest changed"):
            second._import_previous_epoch_checkpoint()

    def test_next_epoch_rejects_tampered_continuation_task_packet(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-one", campaign_id="campaign-continuation-task-tamper",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("long-proof-task")
        outcome = JobOutcome(
            job_id="job-long-proof",
            task_id=task.task_id,
            role=task.role,
            claim_id=task.target_claim,
            status="completed",
            result={
                "result_type": "NO_PROGRESS",
                "status": "OPEN",
                "main_finding": "The obligation remains open.",
                "next_suggested_question": "Continue the exact same task.",
                "artifact_paths": [],
                "evidence_level": "E0_SPECULATIVE",
            },
            turn_history=[{"turn_index": index} for index in range(1, 13)],
            logical_stop_reason="bounded same-thread turn limit reached",
        )
        first._accept_research_result(outcome, task)
        snapshot_path = first._write_compact_snapshot()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["pending_research"][0]["why_now"] = "tampered rationale"
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-two", campaign_id="campaign-continuation-task-tamper",
            previous_epoch_id="epoch-one",
        )
        second._pin_run_inputs(0.01, True)
        with self.assertRaisesRegex(ValueError, "continuation_task_sha256"):
            second._import_previous_epoch_checkpoint()

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

    def test_director_required_files_accept_portable_and_legacy_paths(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="required-file-admission", campaign_id="campaign-1",
        )
        campaign_file = self.runtime / "campaigns" / "campaign-1" / "input.txt"
        campaign_file.parent.mkdir(parents=True, exist_ok=True)
        campaign_file.write_text("campaign input\n", encoding="utf-8")
        epoch_file = self.runtime / "runs" / "source-epoch" / "input.txt"
        epoch_file.parent.mkdir(parents=True, exist_ok=True)
        epoch_file.write_text("epoch input\n", encoding="utf-8")

        references = [
            "project://claims/CLAIMS.md",
            "campaign://campaign-1/input.txt",
            "epoch://source-epoch/input.txt",
            "state/PROGRESS.md",
            str((self.project / "claims" / "CLAIMS.md").resolve()),
        ]
        for index, reference in enumerate(references):
            with self.subTest(reference=reference):
                task = research_task(f"required-file-{index}")
                task.required_files = [reference]
                self.assertIsNone(controller._validate_director_task(task))

    def test_director_required_files_fail_closed(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="required-file-rejection", campaign_id="campaign-1",
        )
        directory = self.project / "artifacts" / "directory-only"
        directory.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        references = [
            "project://claims/MISSING.md",
            "project://../outside.txt",
            "package://claims/CLAIMS.md",
            "artifacts/directory-only",
            str(outside.resolve()),
        ]
        for index, reference in enumerate(references):
            with self.subTest(reference=reference):
                task = research_task(f"invalid-required-file-{index}")
                task.required_files = [reference]
                self.assertIsNotNone(controller._validate_director_task(task))

    def test_portable_required_files_produce_runnable_director_plan(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="portable-director-plan", campaign_id="campaign-1",
        )
        controller.lifecycle.transition(LifecyclePhase.RUNNING, reason="test")
        controller.director_needed = False
        task = research_task("portable-task")
        task.required_files = [
            "project://claims/CLAIMS.md",
            "project://state/PROGRESS.md",
        ]
        task_payload = task.to_dict()
        task_payload.pop("output_contract")
        outcome = JobOutcome(
            job_id="director-portable", task_id="director-portable",
            role="director", claim_id="FRONTIER", status="completed",
            result={
                "assessment": "One bounded task is ready.",
                "spawn": [task_payload],
                "audit_priorities": [],
                "route_updates": [],
                "short_rationale": "Use the declared canonical inputs.",
            },
        )

        controller._accept_director_result(outcome)

        events = controller.store.replay()
        self.assertEqual([item.task_id for item in controller.pending_research], ["portable-task"])
        self.assertIn("TASK_ACCEPTED", [item["kind"] for item in events])
        self.assertNotIn("DIRECTOR_PLAN_REPAIR_REQUIRED", [item["kind"] for item in events])

    def test_director_cannot_rebind_an_existing_stable_task_id(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="stable-task-binding", campaign_id="campaign-1",
        )
        controller.lifecycle.transition(LifecyclePhase.RUNNING, reason="test")
        controller.director_needed = False
        original = research_task("stable-task")

        def outcome_for(task: ResearchTask, suffix: str) -> JobOutcome:
            task_payload = task.to_dict()
            task_payload.pop("output_contract")
            return JobOutcome(
                job_id=f"director-{suffix}", task_id=f"director-{suffix}",
                role="director", claim_id="FRONTIER", status="completed",
                result={
                    "assessment": "One bounded task is ready.",
                    "spawn": [task_payload],
                    "audit_priorities": [],
                    "route_updates": [],
                    "short_rationale": "Use one stable task binding.",
                },
            )

        controller._accept_director_result(outcome_for(original, "first"))
        rebound = research_task("stable-task")
        rebound.exact_objective = "Perform a different bounded check."
        self.assertNotEqual(original.fingerprint, rebound.fingerprint)

        controller._accept_director_result(outcome_for(rebound, "second"))

        self.assertEqual(controller.pending_research, [original])
        self.assertEqual(
            controller.task_fingerprints_by_id,
            {original.task_id: original.fingerprint},
        )
        collisions = [
            item for item in controller.store.replay()
            if item["kind"] == "TASK_REJECTED"
            and item["payload"].get("task_id") == original.task_id
        ]
        self.assertEqual(len(collisions), 1)
        self.assertIn("already bound", collisions[0]["payload"]["reason"])
        self.assertIn("new stable task_id", collisions[0]["payload"]["reason"])

    def test_research_packet_maps_portable_required_files_to_readable_paths(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="required-file-packet", campaign_id="campaign-1",
        )
        controller._pin_run_inputs(0.01, True)
        controller.lifecycle.transition(LifecyclePhase.RUNNING, reason="test")
        task = research_task("packet-task")
        task.role = "prover"
        task.required_files = ["project://claims/CLAIMS.md"]
        controller.pending_research = [task]
        controller._start_job = lambda *args, **kwargs: "job-packet"  # type: ignore[method-assign]

        asyncio.run(controller._launch_research(capacity=1, allow_exploration=False))

        packet_paths = list(
            (controller.run_dir / "jobs").glob(
                "packet-task--job-*/task_packet.json"
            )
        )
        self.assertEqual(len(packet_paths), 1)
        packet = json.loads(packet_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(packet["task"]["required_files"], ["project://claims/CLAIMS.md"])
        self.assertEqual(packet["required_file_access"], [{
            "reference": "project://claims/CLAIMS.md",
            "path": str((self.project / "claims" / "CLAIMS.md").resolve()),
        }])

    def test_missing_required_file_at_dispatch_replans_without_starting_worker(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="required-file-disappeared", campaign_id="campaign-1",
        )
        controller.lifecycle.transition(LifecyclePhase.RUNNING, reason="test")
        controller.director_needed = False
        source = self.project / "artifacts" / "temporary-input.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("temporary\n", encoding="utf-8")
        task = research_task("disappeared-task")
        task.role = "prover"
        task.required_files = ["project://artifacts/temporary-input.txt"]
        self.assertIsNone(controller._validate_director_task(task))
        controller.pending_research = [task]
        source.unlink()

        def unexpected_start(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("worker must not start with a missing required file")

        controller._start_job = unexpected_start  # type: ignore[method-assign]
        asyncio.run(controller._launch_research(capacity=1, allow_exploration=False))

        self.assertEqual(controller.pending_research, [])
        self.assertTrue(controller.director_needed)
        rejection = [
            item for item in controller.store.replay()
            if item["kind"] == "TASK_REJECTED"
        ][-1]
        self.assertEqual(rejection["payload"]["phase"], "dispatch")
        self.assertIn("unavailable", rejection["payload"]["reason"])

    def test_task_dependencies_are_claim_ids_not_task_ids(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="dependency-contract", campaign_id="campaign-1",
        )
        claim_dependency = research_task("claim-dependency")
        claim_dependency.dependencies = ["C_ROOT"]
        self.assertIsNone(controller._validate_director_task(claim_dependency))

        task_dependency = research_task("task-dependency")
        task_dependency.dependencies = ["earlier-task"]
        error = controller._validate_director_task(task_dependency)
        self.assertIsNotNone(error)
        self.assertIn("ClaimGraph claim ids", error or "")
        self.assertIn("not task ids", error or "")

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

    def test_core_capsule_checks_the_exact_bytes_that_are_written(self) -> None:
        graph = ClaimGraph.load(self.runtime / "state" / "claim_graph.json")
        capsule_path = self.runtime / "campaigns" / "exact-bytes" / "CORE_CAPSULE.json"
        capsule = write_core_capsule(
            capsule_path, graph=graph,
            recent_changes=[{"text": "neutral update"}],
            active_tasks=[], audit_leases=[], route_records=[], representations={},
        )
        expected = (
            json.dumps(
                capsule, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.assertEqual(capsule_path.read_bytes(), expected)
        self.assertEqual(capsule_path.stat().st_size, len(expected))

    def test_core_capsule_bounds_large_active_tasks_and_unicode_fields(self) -> None:
        graph = ClaimGraph.load(self.runtime / "state" / "claim_graph.json")
        capsule_path = self.runtime / "campaigns" / "large-active" / "CORE_CAPSULE.json"
        active_tasks = [
            {
                "task_id": f"active-{index:02d}",
                "role": "prover",
                "target_claim": "C_ROOT",
                "priority": float(100 - index),
                "exact_objective": "有限且可核验的中性目标" * 4000,
                "required_files": [f"project://sources/input-{item}.txt" for item in range(80)],
                "metadata": {f"field-{item}": "数据" * 500 for item in range(80)},
            }
            for index in range(24)
        ]
        capsule = write_core_capsule(
            capsule_path, graph=graph,
            recent_changes=[{"text": "变化" * 10000} for _ in range(30)],
            active_tasks=active_tasks,
            audit_leases=[], route_records=[], representations={},
        )

        self.assertLessEqual(capsule_path.stat().st_size, CORE_CAPSULE_MAX_BYTES)
        self.assertEqual(json.loads(capsule_path.read_text(encoding="utf-8")), capsule)
        self.assertGreater(capsule["compaction"]["truncated_values"], 0)
        self.assertGreater(capsule["compaction"]["dropped_counts"]["active_tasks"], 0)
        retained_priorities = [item["priority"] for item in capsule["active_tasks"]]
        self.assertEqual(retained_priorities, sorted(retained_priorities, reverse=True))
        self.assertEqual(retained_priorities[0], 100.0)

    def test_core_capsule_compaction_preserves_highest_priority_frontier(self) -> None:
        source_graph = ClaimGraph.load(self.runtime / "state" / "claim_graph.json")
        template = next(iter(source_graph.claims.values())).to_dict()
        claims: dict[str, Claim] = {}
        for index in range(50):
            raw = dict(template)
            raw.update({
                "claim_id": f"C_{index:02d}",
                "statement": f"Neutral claim {index}",
                "dependencies": [],
                "downstream_dependents": [],
                "current_gaps": ["待验证缺口" * 1000 for _ in range(3)],
                "priority": {"score": float(100 - index)},
                "proof_obligations": [],
            })
            claims[raw["claim_id"]] = Claim.from_dict(raw)
        graph = ClaimGraph(claims)
        capsule_path = self.runtime / "campaigns" / "frontier-priority" / "CORE_CAPSULE.json"

        capsule = write_core_capsule(
            capsule_path, graph=graph,
            recent_changes=[], active_tasks=[], audit_leases=[], route_records=[],
            representations={},
        )

        retained_ids = [item["claim_id"] for item in capsule["frontier"]]
        self.assertIn("C_00", retained_ids)
        self.assertNotIn("C_49", retained_ids)
        self.assertEqual(capsule["frontier"][0]["priority"], 100.0)
        self.assertGreater(capsule["compaction"]["dropped_counts"]["frontier"], 10)

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
