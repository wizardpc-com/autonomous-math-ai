from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.canonical_state import (
    render_markdown_state_block,
    validate_canonical_mathematical_state,
)
from autonomous_math_research.canonical_transition import (
    CanonicalTransitionStore, bytes_sha256, json_bytes,
)
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import ActiveJob, AutonomousController, RunResult
from autonomous_math_research.cli import (
    ResumeContext, _auto_epoch_allowed, _execute_epoch, _latest_run, _run_command,
)
from autonomous_math_research.engine.scheduler import DynamicScheduler
from autonomous_math_research.director_context import (
    DIRECTOR_PROMPT_HARD_LIMIT_BYTES,
    DIRECTOR_PROMPT_TARGET_BYTES,
    utf8_size,
)
from autonomous_math_research.eventing import CandidateInbox
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
from autonomous_math_research.models import (
    CandidateEvent, Claim, JobOutcome, ResearchTask, stable_hash,
)
from autonomous_math_research.project import (
    ProjectManifest,
    discover_workspace_root,
)
from autonomous_math_research.prompts import director_prompt
from autonomous_math_research.representation import (
    RepresentationContract,
    require_compatible_representations,
)
from autonomous_math_research.storage import ProjectLayout, file_digest
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

    def _resume_args(self, run_id: str, *, auto_epochs: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            project=self.project, workspace_root=None, hours=None, epoch_hours=None,
            max_director=None, max_research_workers=None, max_audit=None,
            max_mechanical_subworkers=None, budget=None, config=None, profile=None,
            dry_run=False, mock=False, auto_epochs=auto_epochs, resume=run_id,
            run_id=None, recover_candidates_from=None, campaign_id=None,
            previous_epoch_id=None,
        )

    def _prepare_crashed_second_epoch(
        self, *, legacy_ghost: bool = False, campaign_hours: float = 0.001,
    ) -> tuple[AutonomousController, CampaignStore, ResearchTask]:
        config = load_config(self.project)
        campaign_id = "resume-campaign"
        first_epoch = "resume-epoch-one"
        second_epoch = "resume-epoch-two"
        first_controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id=first_epoch, campaign_id=campaign_id,
            campaign_hours=campaign_hours, epoch_hours=0.00001,
        )
        first_controller._pin_run_inputs(0.00001, False)
        first_controller._write_compact_snapshot()
        campaign = CampaignStore(self.runtime, campaign_id)
        campaign.create(
            project_id=config.project_name,
            campaign_hours=campaign_hours,
            epoch_hours=0.00001,
        )
        campaign.append_epoch_started(
            epoch_id=first_epoch, previous_epoch_id=None, mode="mock",
        )
        campaign.append_epoch_sealed(
            epoch_id=first_epoch, elapsed_seconds=0.001, status="PAUSED",
            stopped_reason="epoch time limit reached",
            checkpoint_uri=f"epoch://{first_epoch}/state/compact_snapshot.json",
        )
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id=second_epoch, campaign_id=campaign_id,
            previous_epoch_id=first_epoch, campaign_hours=campaign_hours,
            epoch_hours=0.00001,
        )
        controller._pin_run_inputs(0.00001, False)
        baseline = controller.guard.snapshot()
        (controller.run_dir / "canonical_guard.before.json").write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        campaign.append_epoch_started(
            epoch_id=second_epoch, previous_epoch_id=first_epoch, mode="mock",
        )
        task = research_task("resume-stale-frontier", route="resume-route")
        controller.store.append("RUN_STARTED", {
            "execution_mode": "mock", "campaign_id": campaign_id,
            "epoch_id": second_epoch,
        })
        controller.store.append("EPOCH_CHECKPOINT_IMPORTED", {
            "campaign_id": campaign_id, "source_epoch_id": first_epoch,
            "research_tasks": 0, "candidate_frontier": 0,
        })
        controller.store.append("TASK_ACCEPTED", {
            "task_id": task.task_id, "fingerprint": task.fingerprint,
            "representation_id": task.representation_id, "task": task.to_dict(),
        })
        controller.store.append("JOB_STARTED", {
            "job_id": "stale-research-job", "task_id": task.task_id,
            "role": task.role, "claim_id": task.target_claim,
        })
        controller.store.append("DIRECTOR_REPLAN_REQUESTED", {
            "state_version": 28, "requested_version": 28,
            "reason": "pre-crash frontier",
        })
        corrupt_snapshot = controller.run_dir / "state" / "compact_snapshot.json"
        corrupt_snapshot.write_text(json.dumps({
            "pending_research": [], "pending_audits": [], "active_tasks": [],
            "controller_watermark": {
                "state_version": 0, "director_requested_version": 0,
                "director_applied_version": -1,
            },
        }) + "\n", encoding="utf-8")
        if legacy_ghost:
            ghost = CampaignStore(self.runtime, second_epoch)
            ghost.create(
                project_id=config.project_name,
                campaign_hours=campaign_hours, epoch_hours=0.00001,
            )
            ghost.append_epoch_started(
                epoch_id=second_epoch, previous_epoch_id=None, mode="mock",
            )
            ghost.append_epoch_sealed(
                epoch_id=second_epoch, elapsed_seconds=1.0, status="PAUSED",
                stopped_reason="mechanical subtask lifecycle invariant failed",
                checkpoint_uri=f"epoch://{second_epoch}/state/compact_snapshot.json",
            )
            controller.store.append("RUN_INPUT_PINNING_FAILED", {
                "error": "RUN_MANIFEST immutable inputs do not match the resumed controller",
            })
            controller.store.append("RUN_STOPPED", {
                "reason": "mechanical subtask lifecycle invariant failed",
                "internal_failure": True, "campaign_id": second_epoch,
                "epoch_id": second_epoch, "campaign_status": "PAUSED",
            })
        return controller, campaign, task

    def test_project_manifest_works_outside_harness_repository(self) -> None:
        (self.project / ".git").mkdir()
        manifest = ProjectManifest.load(self.project)
        self.assertEqual(manifest.project_id, "external-neutral-project")
        self.assertEqual(discover_workspace_root(self.project), self.project)

    def test_startup_refresh_embeds_latest_frontier_after_canonical_update(self) -> None:
        progress = self.project / "state" / "PROGRESS.md"
        progress.write_text("# Progress\n\nFRONTIER-V1\n", encoding="utf-8")
        graph_path = self.runtime / "state" / "claim_graph.json"
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
        graph_payload["claims"][0]["current_gaps"] = ["GRAPH-FRONTIER-V1"]
        graph_path.write_text(
            json.dumps(graph_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        first = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="canonical-frontier-v1", campaign_id="canonical-frontier-v1",
        )
        first._pin_run_inputs(0.01, True)
        first_snapshot = first._write_compact_snapshot().read_text(encoding="utf-8")
        self.assertIn("FRONTIER-V1", first_snapshot)

        progress.write_text("# Progress\n\nFRONTIER-V2\n", encoding="utf-8")
        graph_payload["claims"][0]["current_gaps"] = ["GRAPH-FRONTIER-V2"]
        graph_path.write_text(
            json.dumps(graph_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        overlay = self.project / "autonomous" / "prompts" / "director.md"
        overlay.write_text(
            "Stable constraint: exact arithmetic only. Stale frontier: FRONTIER-V1.\n",
            encoding="utf-8",
        )
        second = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="canonical-frontier-v2", campaign_id="canonical-frontier-v2",
        )
        second._pin_run_inputs(0.01, True)
        snapshot_path = second._write_compact_snapshot()
        snapshot_text = snapshot_path.read_text(encoding="utf-8")
        self.assertIn("FRONTIER-V2", snapshot_text)
        self.assertIn("GRAPH-FRONTIER-V2", snapshot_text)
        self.assertNotIn("GRAPH-FRONTIER-V1", snapshot_text)
        self.assertNotEqual(
            first._canonical_state["canonical_state_sha256"],
            second._canonical_state["canonical_state_sha256"],
        )
        prompt = director_prompt(
            self.project, snapshot_path, [], second._policy_view("director"),
            project_overlay=second._director_overlay,
        )
        self.assertNotIn("FRONTIER-V1", prompt)
        self.assertNotIn("FRONTIER-V2", prompt)
        self.assertIn("compact_state_path=", prompt)
        self.assertIn("full_context_archive_path=", prompt)
        self.assertLess(utf8_size(prompt), DIRECTOR_PROMPT_TARGET_BYTES)
        archive = second._latest_director_context_path.read_text(encoding="utf-8")
        self.assertIn("FRONTIER-V1", archive)
        self.assertIn("FRONTIER-V2", archive)
        capsule = (second.run_dir / "state" / "CORE_CAPSULE.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("FRONTIER-V2", capsule)

    def test_project_director_overlay_is_optional(self) -> None:
        (self.project / "autonomous" / "prompts" / "director.md").unlink()
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="no-director-overlay", campaign_id="no-director-overlay",
        )
        controller._pin_run_inputs(0.01, True)
        snapshot_path = controller._write_compact_snapshot()
        (self.project / "autonomous" / "prompts" / "director.md").write_text(
            "late unpinned overlay\n", encoding="utf-8",
        )
        prompt = director_prompt(
            self.project, snapshot_path, [], controller._policy_view("director"),
            project_overlay=controller._director_overlay,
        )

        self.assertIsNone(controller._director_overlay)
        self.assertIn("AMR DIRECTOR TURN", prompt)
        self.assertNotIn('"authority": "controller_claim_graph"', prompt)
        self.assertNotIn("late unpinned overlay", prompt)
        self.assertLess(utf8_size(prompt), DIRECTOR_PROMPT_HARD_LIMIT_BYTES)
        archive = controller._latest_director_context_path.read_text(encoding="utf-8")
        self.assertIn('"authority": "controller_claim_graph"', archive)
        self.assertNotIn("late unpinned overlay", archive)

    def test_canonical_drift_discards_previous_epoch_planning(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="planning-before-refresh", campaign_id="planning-refresh",
        )
        first._pin_run_inputs(0.01, True)
        first.pending_research = [research_task("stale-frontier-task")]
        first._write_compact_snapshot()

        (self.project / "state" / "PROGRESS.md").write_text(
            "# Progress\n\nA new canonical frontier supersedes the old plan.\n",
            encoding="utf-8",
        )
        second = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="planning-after-refresh", campaign_id="planning-refresh",
            previous_epoch_id="planning-before-refresh",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()

        self.assertEqual(second.pending_research, [])
        discarded = [
            event for event in second.store.replay()
            if event["kind"] == "STALE_PLANNING_STATE_DISCARDED"
        ]
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["payload"]["discarded_pending_research"], 1)

    def test_canonical_drift_with_audit_frontier_fails_closed(self) -> None:
        first = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="audit-before-refresh", campaign_id="audit-refresh",
        )
        first._pin_run_inputs(0.01, True)
        snapshot_path = first._write_compact_snapshot()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["candidate_audit_frontier"] = [{"sealed": "candidate"}]
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.project / "state" / "PROGRESS.md").write_text(
            "# Progress\n\nCanonical state changed during an audit frontier.\n",
            encoding="utf-8",
        )
        second = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="audit-after-refresh", campaign_id="audit-refresh",
            previous_epoch_id="audit-before-refresh",
        )
        second._pin_run_inputs(0.01, True)
        with self.assertRaisesRegex(ValueError, "automatic synchronization is unsafe"):
            second._import_previous_epoch_checkpoint()
        self.assertEqual(second.pending_research, [])

    @patch(
        "autonomous_math_research.provenance._codex_identity",
        return_value={
            "codex_cli_version": "test-codex",
            "app_server_schema_sha256": "1" * 64,
            "app_server_required_protocol_sha256": "2" * 64,
        },
    )
    def test_audited_transition_rebases_open_audit_checkpoint_and_legacy_v1(
        self, _codex_identity_mock,
    ) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=False,
            run_id="audited-rebase-first", campaign_id="audited-rebase",
        )
        first._pin_run_inputs(0.01, False)
        candidate = CandidateEvent(
            event_id="audited-rebase-candidate", producer_thread_id=None,
            producer_task_id="audited-rebase-task", claim_id="C_ROOT",
            parent_claim_id=None, type="KEY_LEMMA", impact="HIGH",
            concise_summary="candidate awaiting audit",
            exact_statement="One exact candidate remains under audit.",
            artifact_paths=[], reproduction_commands=[], dependency_impact=[],
        )
        first.audit_gate.register(candidate)
        first.graph.claims["C_ROOT"].priority["score"] = 0.9
        transition_id = first._commit_claim_state_transition(
            transition_kind="CONTROLLER_PRIORITY_UPDATE",
            authorization={
                "claim_id": "C_ROOT", "task_id": "audited-rebase-task",
                "reason": "tested audited state-neutral priority update",
                "trust_upgrade": False,
            },
        )
        snapshot_path = first._write_compact_snapshot()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        provenance = snapshot["snapshot_provenance"]

        self.assertEqual(provenance["schema_version"], 2)
        self.assertEqual(provenance["canonical_transition_id"], transition_id)
        self.assertNotEqual(
            provenance["planning_context_sha256"],
            first._canonical_state["planning_context_sha256"],
        )

        second = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=False,
            run_id="audited-rebase-second", campaign_id="audited-rebase",
            previous_epoch_id=first.run_id,
        )
        second._pin_run_inputs(0.01, False)
        second._import_previous_epoch_checkpoint()
        self.assertIn(candidate.fingerprint, second.audit_gate.states)
        rebound_snapshot = json.loads(
            second._write_compact_snapshot().read_text(encoding="utf-8")
        )
        self.assertEqual(
            rebound_snapshot["canonical_state"]["planning_mirror"][
                "disposition"
            ],
            "reuse_after_checkpoint_integrity_checks",
        )
        self.assertFalse(any(
            event["kind"] == "STALE_PLANNING_STATE_DISCARDED"
            for event in second.store.replay()
        ))

        snapshot["snapshot_provenance"]["trusted_state_sha256"] = "0" * 64
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tampered = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=False,
            run_id="audited-rebase-tampered", campaign_id="audited-rebase",
            previous_epoch_id=first.run_id,
        )
        tampered._pin_run_inputs(0.01, False)
        with self.assertRaisesRegex(ValueError, "canonical provenance is invalid"):
            tampered._import_previous_epoch_checkpoint()

        snapshot["snapshot_provenance"]["trusted_state_sha256"] = file_digest(
            self.runtime / "state" / "nightly_trusted.json"
        )
        snapshot["snapshot_provenance"]["canonical_transition_id"] = (
            "transition-forged"
        )
        snapshot["canonical_state"]["canonical_transition_id"] = (
            "transition-forged"
        )
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        forged = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=False,
            run_id="audited-rebase-forged", campaign_id="audited-rebase",
            previous_epoch_id=first.run_id,
        )
        forged._pin_run_inputs(0.01, False)
        with self.assertRaisesRegex(ValueError, "transition binding is invalid"):
            forged._import_previous_epoch_checkpoint()

        snapshot["snapshot_provenance"]["canonical_transition_id"] = transition_id
        snapshot["canonical_state"]["canonical_transition_id"] = transition_id
        snapshot["snapshot_provenance"] = {
            **snapshot["snapshot_provenance"],
            "schema_version": 1,
            "planning_context_sha256": first._canonical_state[
                "planning_context_sha256"
            ],
        }
        for field in (
            "claim_graph_sha256", "trusted_state_sha256",
            "canonical_transition_id",
        ):
            snapshot["snapshot_provenance"].pop(field, None)
        snapshot["canonical_state"]["planning_context_sha256"] = (
            first._canonical_state["planning_context_sha256"]
        )
        snapshot["canonical_state"].pop("trusted_state_sha256", None)
        snapshot["canonical_state"].pop("canonical_transition_id", None)
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        legacy = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=False,
            run_id="audited-rebase-legacy", campaign_id="audited-rebase",
            previous_epoch_id=first.run_id,
        )
        legacy._pin_run_inputs(0.01, False)
        legacy._import_previous_epoch_checkpoint()

        self.assertIn(candidate.fingerprint, legacy.audit_gate.states)
        reconciliation = [
            event for event in legacy.store.replay()
            if event["kind"]
            == "LEGACY_CHECKPOINT_CANONICAL_PROVENANCE_RECONCILED"
        ]
        self.assertEqual(len(reconciliation), 1)
        self.assertEqual(
            reconciliation[0]["payload"]["canonical_transition_id"],
            transition_id,
        )

    def test_canonical_change_after_refresh_blocks_snapshot_and_director(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="canonical-race", campaign_id="canonical-race",
        )
        controller._pin_run_inputs(0.01, True)
        (self.project / "state" / "PROGRESS.md").write_text(
            "# Progress\n\nChanged after refresh.\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "changed after startup refresh"):
            controller._write_compact_snapshot()
        self.assertFalse(controller._director_active)

    def test_claim_graph_change_after_refresh_blocks_snapshot_and_director(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="claim-graph-race", campaign_id="claim-graph-race",
        )
        controller._pin_run_inputs(0.01, True)
        graph_path = self.runtime / "state" / "claim_graph.json"
        payload = json.loads(graph_path.read_text(encoding="utf-8"))
        payload["claims"][0]["current_gaps"] = ["unaudited external rewrite"]
        graph_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "outside an audited transition"):
            controller._write_compact_snapshot()
        self.assertFalse(controller._director_active)

    def test_startup_refresh_does_not_rewrite_canonical_files(self) -> None:
        manifest = ProjectManifest.load(self.project)
        canonical_paths = {
            manifest.path,
            manifest.resolve(manifest.claim_graph),
            manifest.resolve(manifest.trusted_state),
            *(
                manifest.resolve(item)
                for items in manifest.canonical_inputs.values()
                for item in items
            ),
        }
        before = {path: file_digest(path) for path in canonical_paths}
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="canonical-read-only", campaign_id="canonical-read-only",
        )
        result = asyncio.run(controller.run(0.01, dry_run=True))
        after = {path: file_digest(path) for path in canonical_paths}

        self.assertFalse(result.internal_failure)
        self.assertEqual(after, before)
        run_manifest = json.loads(
            (controller.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(run_manifest["schema_version"], 13)
        self.assertEqual(run_manifest["runtime_provenance"]["amr_version"], "0.2.3")
        self.assertIn("canonical_state", run_manifest)

    def test_stale_trusted_binding_fails_before_model_turn(self) -> None:
        graph = self.runtime / "state" / "claim_graph.json"
        trusted = self.runtime / "state" / "nightly_trusted.json"
        trusted_payload = json.loads(trusted.read_text(encoding="utf-8"))
        trusted_payload["claim_graph_sha256"] = "0" * 64
        trusted.write_text(
            json.dumps(trusted_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = {
            graph: graph.read_bytes(),
            trusted: trusted.read_bytes(),
        }
        backend = MockCodexBackend()
        controller = AutonomousController(
            load_config(self.project), backend=backend, mock=True,
            run_id="stale-trusted-binding",
            campaign_id="stale-trusted-binding",
        )

        result = asyncio.run(controller.run(0.01, dry_run=False))

        self.assertTrue(result.internal_failure)
        self.assertIn("different ClaimGraph digest", result.stopped_reason)
        self.assertEqual(backend.calls, [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_conflicting_marked_markdown_fails_before_model_turn(self) -> None:
        manifest = ProjectManifest.load(self.project)
        graph_path = manifest.resolve(manifest.claim_graph)
        graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
        block = render_markdown_state_block(
            graph_payload, file_digest(graph_path),
        ).replace('"math_status": "OPEN"', '"math_status": "PROVED"', 1)
        claims = self.project / "claims" / "CLAIMS.md"
        claims.write_text(
            claims.read_text(encoding="utf-8") + "\n\n" + block + "\n",
            encoding="utf-8",
        )
        before = claims.read_bytes()
        backend = MockCodexBackend()
        controller = AutonomousController(
            load_config(self.project), backend=backend, mock=True,
            run_id="markdown-claim-conflict",
            campaign_id="markdown-claim-conflict",
        )

        result = asyncio.run(controller.run(0.01, dry_run=False))

        self.assertTrue(result.internal_failure)
        self.assertIn("Markdown state conflicts", result.stopped_reason)
        self.assertEqual(backend.calls, [])
        self.assertEqual(claims.read_bytes(), before)

    def test_canonical_transition_is_auditable_and_replayable(self) -> None:
        manifest = ProjectManifest.load(self.project)
        graph_path = manifest.resolve(manifest.claim_graph)
        trusted_path = manifest.resolve(manifest.trusted_state)
        claims = self.project / "claims" / "CLAIMS.md"
        progress = self.project / "state" / "PROGRESS.md"
        canonical_markdown_before = {
            claims: claims.read_bytes(),
            progress: progress.read_bytes(),
        }
        graph = ClaimGraph.load(graph_path)
        graph.claims["C_ROOT"].math_status = "PROVED"
        graph.claims["C_ROOT"].trust_status = "AUDITED_NIGHTLY"
        graph.claims["C_ROOT"].current_gaps = []
        after_graph = graph.to_payload(updated_at="2026-08-22T00:00:00Z")
        after_graph_bytes = json_bytes(after_graph)
        after_digest = bytes_sha256(after_graph_bytes)
        store = CanonicalTransitionStore(
            project_root=self.project,
            runtime_root=self.runtime,
        )
        targets = {
            graph_path: after_graph_bytes,
            trusted_path: json_bytes(
                json.loads(trusted_path.read_text(encoding="utf-8"))
            ),
        }

        transition_id = store.commit(
            targets=targets,
            authorization={
                "kind": "AUDITED_CLAIM_TRANSITION",
                "candidate_fingerprint": "candidate-audited",
                "audit_pass_count": 2,
                "audit_required": 2,
            },
            trusted_state_path=trusted_path,
            claim_graph_sha256=after_digest,
        )

        records = store.records()
        self.assertEqual([item["kind"] for item in records], ["PREPARED", "COMMITTED"])
        self.assertEqual(records[0]["transition_id"], transition_id)
        self.assertTrue(all(
            (store.root / transition_id / target["before_snapshot"]).is_file()
            and (store.root / transition_id / target["after_snapshot"]).is_file()
            for target in records[0]["targets"]
        ))
        self.assertEqual(
            json.loads(trusted_path.read_text(encoding="utf-8"))["claim_graph_sha256"],
            after_digest,
        )
        validate_canonical_mathematical_state(manifest)
        self.assertEqual(
            {path: path.read_bytes() for path in canonical_markdown_before},
            canonical_markdown_before,
        )

        prepared = records[0]
        store.ledger_path.write_text(
            json.dumps(prepared, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        graph_target = next(
            item for item in prepared["targets"]
            if item["path"].endswith("claim_graph.json")
        )
        graph_path.write_bytes(
            (store.root / transition_id / graph_target["before_snapshot"]).read_bytes()
        )

        self.assertEqual(store.recover(), [transition_id])
        self.assertEqual(file_digest(graph_path), after_digest)
        self.assertEqual(store.records()[-1]["kind"], "COMMITTED")

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

    def test_same_thread_dispatch_uses_role_specific_turn_limit(self) -> None:
        class SameThreadMockBackend(MockCodexBackend):
            def supports_same_thread_continuation(self, role: str) -> bool:
                return role in {"prover", "falsifier", "explorer"}

        async def scenario() -> None:
            config = load_config(self.project)
            config.raw["engine"]["research_max_turns"] = {
                "prover": 12, "falsifier": 8, "explorer": 6,
            }
            controller = AutonomousController(
                config, backend=SameThreadMockBackend(), mock=True,
                run_id="same-thread-role-limit",
                campaign_id="same-thread-role-limit",
            )
            controller._pin_run_inputs(0.01, True)
            controller.lifecycle.transition(
                LifecyclePhase.RUNNING, reason="test same-thread dispatch",
            )
            controller.pending_research = [
                research_task("same-thread-explorer", route="independent")
            ]

            await controller._launch_research(capacity=1, allow_exploration=True)

            started = [
                event for event in controller.store.replay()
                if event["kind"] == "JOB_STARTED"
            ]
            self.assertEqual(len(started), 1)
            self.assertTrue(started[0]["payload"]["same_thread_multi_turn"])
            self.assertEqual(started[0]["payload"]["max_turns"], 6)
            self.assertEqual(
                started[0]["payload"]["timeout"],
                started[0]["payload"]["per_turn_timeout"] * 6,
            )
            await asyncio.gather(*(
                active.future for active in controller.active.values()
            ))
            await controller._collect_completed()

        asyncio.run(scenario())

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

    def test_campaign_continuation_skips_an_unusable_bootstrap_checkpoint(self) -> None:
        store = CampaignStore(self.runtime, "campaign-checkpoint-selection")
        store.create(project_id="external-neutral-project")
        for epoch_id in ("usable-epoch", "failed-bootstrap-epoch"):
            snapshot = self.runtime / "runs" / epoch_id / "state/compact_snapshot.json"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text("{}\n", encoding="utf-8", newline="\n")
            store.append_epoch_started(
                epoch_id=epoch_id,
                previous_epoch_id=(None if epoch_id == "usable-epoch" else "usable-epoch"),
                mode="mock",
            )
            store.append_epoch_sealed(
                epoch_id=epoch_id,
                elapsed_seconds=1,
                status="PAUSED",
                stopped_reason=(
                    "epoch time limit reached"
                    if epoch_id == "usable-epoch"
                    else "bootstrap failed: epoch checkpoint import: invalid frontier"
                ),
                checkpoint_uri=f"epoch://{epoch_id}/state/compact_snapshot.json",
                checkpoint_usable=epoch_id == "usable-epoch",
            )

        self.assertEqual(store.latest_continuable_epoch(), "usable-epoch")
        store.require_current_continuation_source("usable-epoch")

    def test_campaign_continuation_rejects_unsealed_newer_epoch(self) -> None:
        store = CampaignStore(self.runtime, "campaign-unsealed-checkpoint")
        store.create(project_id="external-neutral-project")
        snapshot = self.runtime / "runs" / "usable-epoch" / "state/compact_snapshot.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("{}\n", encoding="utf-8", newline="\n")
        store.append_epoch_started(epoch_id="usable-epoch", previous_epoch_id=None, mode="mock")
        store.append_epoch_sealed(
            epoch_id="usable-epoch", elapsed_seconds=1, status="PAUSED",
            stopped_reason="epoch time limit reached",
            checkpoint_uri="epoch://usable-epoch/state/compact_snapshot.json",
        )
        store.append_epoch_started(
            epoch_id="still-active", previous_epoch_id="usable-epoch", mode="mock",
        )

        with self.assertRaisesRegex(ValueError, "unsealed epoch"):
            store.require_current_continuation_source("usable-epoch")

    def test_campaign_continuation_does_not_silently_skip_corrupt_checkpoint(self) -> None:
        store = CampaignStore(self.runtime, "campaign-corrupt-checkpoint")
        store.create(project_id="external-neutral-project")
        store.append_epoch_started(epoch_id="corrupt-epoch", previous_epoch_id=None, mode="mock")
        store.append_epoch_sealed(
            epoch_id="corrupt-epoch", elapsed_seconds=1, status="PAUSED",
            stopped_reason="epoch time limit reached",
            checkpoint_uri="epoch://corrupt-epoch/state/compact_snapshot.json",
        )

        with self.assertRaisesRegex(ValueError, "checkpoint is missing"):
            store.latest_continuable_epoch()

    def test_campaign_epoch_id_rejects_path_escape(self) -> None:
        store = CampaignStore(self.runtime, "campaign-1")
        store.create(project_id="external-neutral-project")
        with self.assertRaises(ValueError):
            store.append_epoch_sealed(
                epoch_id="../escape", elapsed_seconds=0, status="PAUSED",
                stopped_reason="invalid",
                checkpoint_uri="epoch://invalid/state/checkpoint.json",
            )

    def test_resume_context_hydrates_all_campaign_identity_before_controller(self) -> None:
        controller, _, _ = self._prepare_crashed_second_epoch()
        context = ResumeContext.load(self.project, controller.run_id)
        args = self._resume_args(controller.run_id)

        context.apply(args)

        self.assertEqual(context.campaign_id, "resume-campaign")
        self.assertEqual(context.previous_epoch_id, "resume-epoch-one")
        self.assertEqual(args.campaign_id, "resume-campaign")
        self.assertEqual(args.previous_epoch_id, "resume-epoch-one")
        self.assertTrue(args.mock)

    def test_pre_recovery_failure_preserves_snapshot_and_unsealed_epoch(self) -> None:
        controller, campaign, _ = self._prepare_crashed_second_epoch()
        snapshot = controller.run_dir / "state" / "compact_snapshot.json"
        snapshot_before = file_digest(snapshot)
        canonical_paths = [
            self.project / "claims" / "CLAIMS.md",
            self.project / "state" / "PROGRESS.md",
            self.runtime / "state" / "claim_graph.json",
            self.runtime / "state" / "nightly_trusted.json",
        ]
        canonical_before = {path: file_digest(path) for path in canonical_paths}

        with patch.object(
            AutonomousController, "_pin_run_inputs",
            side_effect=ValueError("injected pinning failure"),
        ):
            result, _ = asyncio.run(_execute_epoch(self._resume_args(controller.run_id)))

        self.assertTrue(result.internal_failure)
        self.assertIn("injected pinning failure", result.stopped_reason)
        self.assertEqual(file_digest(snapshot), snapshot_before)
        self.assertEqual(campaign.unsealed_epoch(), controller.run_id)
        self.assertFalse(any(
            item.get("kind") == "EPOCH_SEALED"
            and item.get("epoch_id") == controller.run_id
            for item in campaign.events()
        ))
        kinds = [event["kind"] for event in controller.store.replay()]
        self.assertEqual(kinds[-1], "ATTEMPT_FAILED")
        self.assertNotIn("RUN_STOPPED", kinds)
        self.assertEqual(
            {path: file_digest(path) for path in canonical_paths}, canonical_before,
        )

    def test_legacy_wrong_campaign_resume_rebuilds_frontier_append_only(self) -> None:
        controller, campaign, task = self._prepare_crashed_second_epoch(
            legacy_ghost=True,
        )
        self.assertEqual(_latest_run(self.project), controller.run_id)
        canonical_paths = [
            self.project / "claims" / "CLAIMS.md",
            self.project / "state" / "PROGRESS.md",
            self.runtime / "state" / "claim_graph.json",
            self.runtime / "state" / "nightly_trusted.json",
        ]
        canonical_before = {path: file_digest(path) for path in canonical_paths}

        result, _ = asyncio.run(_execute_epoch(self._resume_args(controller.run_id)))

        self.assertFalse(result.internal_failure, result.stopped_reason)
        self.assertEqual(result.campaign_id, "resume-campaign")
        self.assertIsNone(campaign.unsealed_epoch())
        sealed = next(
            item for item in campaign.events()
            if item.get("kind") == "EPOCH_SEALED"
            and item.get("epoch_id") == controller.run_id
        )
        self.assertGreaterEqual(sealed["elapsed_seconds"], 0.03)
        self.assertTrue(sealed["checkpoint_usable"])
        self.assertEqual(
            CampaignStore(self.runtime, controller.run_id).load().status,
            "SUPERSEDED",
        )
        events = controller.store.replay()
        kinds = [event["kind"] for event in events]
        self.assertIn("RESUME_METADATA_REBOUND", kinds)
        self.assertIn("RECOVERY_COMPLETED", kinds)
        stale_cancel = next(
            event for event in events
            if event["kind"] == "JOB_CANCELLED"
            and event["payload"].get("job_id") == "stale-research-job"
        )
        self.assertEqual(stale_cancel["payload"]["failure_kind"], "controller_restart")
        snapshot = json.loads(
            (controller.run_dir / "state" / "compact_snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertGreaterEqual(
            snapshot["controller_watermark"]["director_requested_version"], 28,
        )
        self.assertGreaterEqual(snapshot["snapshot_provenance"]["generation"], 29)
        self.assertTrue(snapshot["snapshot_provenance"]["rebuilt_from_events"])
        frontier_ids = {
            item["task_id"] for item in snapshot.get("pending_research", [])
        }
        self.assertIn(task.task_id, frontier_ids)
        self.assertEqual(
            {path: file_digest(path) for path in canonical_paths}, canonical_before,
        )

    def test_crashed_second_epoch_resume_seals_and_auto_starts_next_epoch(self) -> None:
        controller, campaign, _ = self._prepare_crashed_second_epoch(
            legacy_ghost=True, campaign_hours=0.00002,
        )
        args = self._resume_args(controller.run_id, auto_epochs=True)

        with patch("builtins.print") as output:
            exit_code = asyncio.run(_run_command(args))

        payload = json.loads(output.call_args_list[-1].args[0])
        starts = [
            event for event in campaign.events()
            if event.get("kind") == "EPOCH_STARTED"
        ]
        seals = [
            event for event in campaign.events()
            if event.get("kind") == "EPOCH_SEALED"
        ]
        resumed_seal = next(
            event for event in seals
            if event.get("epoch_id") == controller.run_id
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["epochs_run"], 2)
        self.assertEqual(payload["epoch_ids"][0], controller.run_id)
        self.assertEqual(len(starts), 3)
        self.assertEqual(len(seals), 3)
        self.assertTrue(resumed_seal["checkpoint_usable"])
        self.assertEqual(starts[-1]["previous_epoch_id"], controller.run_id)

    def test_manifest_v12_remains_resume_compatible(self) -> None:
        controller, _, _ = self._prepare_crashed_second_epoch()
        manifest_path = controller.run_dir / "RUN_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("runtime_provenance")
        manifest["schema_version"] = 12
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256")
        manifest["manifest_sha256"] = stable_hash(unsigned)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        context = ResumeContext.load(self.project, controller.run_id)

        self.assertEqual(context.campaign_id, "resume-campaign")

    def test_manifest_v13_source_change_fails_closed_without_sealing(self) -> None:
        controller, campaign, _ = self._prepare_crashed_second_epoch()
        manifest_path = controller.run_dir / "RUN_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["runtime_provenance"]["source_sha256"] = "0" * 64
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256")
        manifest["manifest_sha256"] = stable_hash(unsigned)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        result, _ = asyncio.run(_execute_epoch(self._resume_args(controller.run_id)))

        self.assertTrue(result.internal_failure)
        self.assertIn("runtime provenance differs", result.stopped_reason)
        self.assertEqual(campaign.unsealed_epoch(), controller.run_id)

    def test_codex_schema_change_is_accepted_only_with_same_required_protocol(self) -> None:
        controller, _, _ = self._prepare_crashed_second_epoch()
        manifest = json.loads(
            (controller.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8")
        )
        current = dict(manifest["runtime_provenance"])
        current["codex_cli_version"] = "codex-cli compatible-update"
        current["app_server_schema_sha256"] = "1" * 64
        resumed = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            resume=True, run_id=controller.run_id,
            campaign_id="resume-campaign", previous_epoch_id="resume-epoch-one",
            campaign_hours=0.001, epoch_hours=0.00001,
        )

        with patch(
            "autonomous_math_research.controller.capture_runtime_provenance",
            return_value=current,
        ):
            resumed._pin_run_inputs(0.00001, False)

        compatible = [
            event for event in resumed.store.replay()
            if event["kind"] == "RUNTIME_PROVENANCE_CHANGED_COMPATIBLE"
        ]
        self.assertEqual(len(compatible), 1)

    def test_run_stops_only_after_artifact_finalization_completes(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
        )

        def write_outcome(**kwargs):
            kinds = [event["kind"] for event in controller.store.replay()]
            self.assertIn("RUN_ARTIFACT_FINALIZATION_STARTED", kinds)
            self.assertNotIn("ATTEMPT_COMPLETED", kinds)
            self.assertNotIn("RUN_STOPPED", kinds)
            target = kwargs["outcome_dir"] / "OUTCOME.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# finalized\n", encoding="utf-8")
            return target

        with patch(
            "autonomous_math_research.controller.write_outcome_archive",
            side_effect=write_outcome,
        ):
            result = asyncio.run(controller.run(0.001, dry_run=True))

        kinds = [event["kind"] for event in controller.store.replay()]
        self.assertTrue(result.artifacts_finalized)
        self.assertEqual(kinds[-3:], [
            "RUN_ARTIFACT_FINALIZATION_COMPLETED",
            "ATTEMPT_COMPLETED",
            "RUN_STOPPED",
        ])
        self.assertTrue(json.loads(
            (
                self.runtime / "nightly" / controller.run_id / "RUN_SUMMARY.json"
            ).read_text(encoding="utf-8")
        )["artifacts_finalized"])

    def test_artifact_failure_is_terminal_and_blocks_auto_continuation(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
        )
        with patch(
            "autonomous_math_research.controller.write_outcome_archive",
            side_effect=OSError("injected archive failure"),
        ):
            result = asyncio.run(controller.run(0.001, dry_run=True))

        events = controller.store.replay()
        self.assertTrue(result.internal_failure)
        self.assertFalse(result.artifacts_finalized)
        self.assertIsNone(result.outcome_path)
        self.assertIn("injected archive failure", result.stopped_reason)
        self.assertEqual(events[-1]["kind"], "RUN_STOPPED")
        self.assertFalse(events[-1]["payload"]["artifacts_finalized"])
        self.assertIn("RUN_ARTIFACT_FINALIZATION_FAILED", {
            event["kind"] for event in events
        })
        checkpoint = controller.campaign_store.load()
        self.assertFalse(_auto_epoch_allowed(result, checkpoint))

    def test_artifact_interrupt_writes_structured_terminal_before_propagating(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
        )
        with (
            patch(
                "autonomous_math_research.controller.write_outcome_archive",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            asyncio.run(controller.run(0.001, dry_run=True))

        events = controller.store.replay()
        self.assertEqual(events[-2]["kind"], "ATTEMPT_INTERRUPTED")
        self.assertEqual(events[-1]["kind"], "RUN_STOPPED")
        self.assertTrue(events[-1]["payload"]["operator_interrupted"])
        self.assertFalse(events[-1]["payload"]["artifacts_finalized"])
        self.assertIsNone(controller.campaign_store.unsealed_epoch())

    def test_operator_cancel_drains_and_finalizes_before_exit_130(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
        )
        controller._operator_interrupted = True
        controller._stop_after_epoch = True

        def write_outcome(**kwargs):
            target = kwargs["outcome_dir"] / "OUTCOME.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# interrupted but finalized\n", encoding="utf-8")
            return target

        with (
            patch(
                "autonomous_math_research.controller.write_outcome_archive",
                side_effect=write_outcome,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            asyncio.run(controller.run(0.001, dry_run=True))

        events = controller.store.replay()
        self.assertEqual(controller.campaign_store.load().status, "STOPPED")
        self.assertEqual(events[-2]["kind"], "ATTEMPT_INTERRUPTED")
        self.assertTrue(events[-1]["payload"]["artifacts_finalized"])
        self.assertTrue(events[-1]["payload"]["operator_interrupted"])

    def test_auto_epochs_continue_only_at_clean_epoch_boundaries(self) -> None:
        config = load_config(self.project)
        calls: list[argparse.Namespace] = []

        async def execute(args: argparse.Namespace):
            calls.append(args)
            index = len(calls)
            campaign_id = args.campaign_id or "auto-campaign"
            epoch_id = f"auto-epoch-{index}"
            store = CampaignStore(self.runtime, campaign_id)
            store.create(
                project_id=config.project_name,
                campaign_hours=2 / 3600.0,
                epoch_hours=1 / 3600.0,
            )
            store.append_epoch_started(
                epoch_id=epoch_id,
                previous_epoch_id=args.previous_epoch_id,
                mode="mock",
            )
            checkpoint = self.runtime / "runs" / epoch_id / "state" / "compact_snapshot.json"
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text("{}\n", encoding="utf-8")
            final = index == 2
            reason = (
                "campaign time budget exhausted" if final
                else "epoch time limit reached"
            )
            status = "STOPPED" if final else "PAUSED"
            store.append_epoch_sealed(
                epoch_id=epoch_id,
                elapsed_seconds=1.0,
                status=status,
                stopped_reason=reason,
                checkpoint_uri=f"epoch://{epoch_id}/state/compact_snapshot.json",
            )
            return RunResult(
                run_id=epoch_id,
                report_path=self.runtime / f"{epoch_id}.md",
                stopped_reason=reason,
                job_count=0,
                event_count=0,
                run_mode="mock",
                campaign_id=campaign_id,
                epoch_id=epoch_id,
                campaign_status=status,
                artifacts_finalized=True,
            ), config

        args = argparse.Namespace(
            project=self.project,
            workspace_root=None,
            hours=2 / 3600.0,
            epoch_hours=1 / 3600.0,
            max_director=None,
            max_research_workers=None,
            max_audit=None,
            max_mechanical_subworkers=None,
            budget=None,
            config=None,
            profile=None,
            dry_run=False,
            mock=True,
            auto_epochs=True,
            resume="resumed-auto-epoch",
            run_id=None,
            recover_candidates_from=None,
            campaign_id=None,
            previous_epoch_id=None,
        )
        with (
            patch(
                "autonomous_math_research.cli._execute_epoch",
                side_effect=execute,
            ),
            patch("builtins.print"),
        ):
            exit_code = asyncio.run(_run_command(args))

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].campaign_id, "auto-campaign")
        self.assertEqual(calls[1].previous_epoch_id, "auto-epoch-1")

    def test_auto_epochs_stop_on_quota_or_internal_failure(self) -> None:
        from autonomous_math_research.cli import _auto_epoch_allowed

        checkpoint = type("Checkpoint", (), {"remaining_seconds": 100.0})()
        quota = RunResult(
            run_id="quota", report_path=self.runtime / "quota.md",
            stopped_reason="campaign paused: provider quota exhausted",
            job_count=0, event_count=0, run_mode="real",
            campaign_status="PAUSED",
        )
        failure = RunResult(
            run_id="failure", report_path=self.runtime / "failure.md",
            stopped_reason="controller internal error", job_count=0, event_count=0,
            run_mode="real", internal_failure=True, campaign_status="PAUSED",
        )

        self.assertFalse(_auto_epoch_allowed(quota, checkpoint))
        self.assertFalse(_auto_epoch_allowed(failure, checkpoint))

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

    def test_fresh_epoch_defers_pending_task_whose_route_was_paused(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-before-route-pause", campaign_id="campaign-route-pause",
        )
        first._pin_run_inputs(0.01, True)
        paused = research_task("paused-carry-forward", route="route-paused")
        open_task = research_task("open-carry-forward", route="route-open")
        first.pending_research = [paused, open_task]
        first._write_compact_snapshot()
        first.route_ledger.append(
            route_id="route-paused",
            representation_id=paused.representation_id,
            method_tags=[], status="PAUSED", failure_class=None,
            retry_condition="external-condition", evidence_refs=[], source="director",
        )

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="epoch-after-route-pause", campaign_id="campaign-route-pause",
            previous_epoch_id="epoch-before-route-pause",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()

        self.assertEqual(
            [task.task_id for task in second.pending_research],
            [open_task.task_id],
        )
        deferred = [
            event for event in second.store.replay()
            if event["kind"] == "TASK_DEFERRED_BY_ROUTE_POLICY"
        ]
        self.assertEqual([event["payload"]["task_id"] for event in deferred], [
            paused.task_id,
        ])

    def test_dispatch_rechecks_route_policy_and_releases_task_identity(self) -> None:
        async def scenario() -> None:
            controller = AutonomousController(
                load_config(self.project), backend=MockCodexBackend(), mock=True,
                run_id="dispatch-route-pause", campaign_id="dispatch-route-pause",
            )
            controller._pin_run_inputs(0.01, True)
            controller.lifecycle.transition(
                LifecyclePhase.RUNNING, reason="test scheduling",
            )
            task = research_task("pending-then-paused", route="route-paused")
            controller.pending_research = [task]
            controller.seen_task_fingerprints.add(task.fingerprint)
            controller.task_fingerprints_by_id[task.task_id] = task.fingerprint
            controller.route_ledger.append(
                route_id=task.route_family,
                representation_id=task.representation_id,
                method_tags=[], status="PAUSED", failure_class=None,
                retry_condition="external-condition", evidence_refs=[], source="director",
            )

            await controller._launch_research(capacity=8, allow_exploration=True)

            self.assertEqual(controller.pending_research, [])
            self.assertEqual(controller.active, {})
            self.assertNotIn(task.fingerprint, controller.seen_task_fingerprints)
            self.assertNotIn(task.task_id, controller.task_fingerprints_by_id)

        asyncio.run(scenario())

    def test_missing_independent_slot_requests_one_replan_per_frontier(self) -> None:
        async def scenario() -> None:
            controller = AutonomousController(
                load_config(self.project), backend=MockCodexBackend(), mock=True,
                run_id="independent-slot", campaign_id="independent-slot",
            )
            controller._pin_run_inputs(0.01, True)
            controller.lifecycle.transition(
                LifecyclePhase.RUNNING, reason="test scheduling",
            )
            controller.director_needed = False
            controller._director_active = True
            controller.pending_research = [
                research_task(f"regular-route-{index}", route=f"regular-route-{index}")
                for index in range(8)
            ]

            await controller._launch_research(capacity=8, allow_exploration=True)
            await controller._launch_research(capacity=8, allow_exploration=True)

            replans = [
                event for event in controller.store.replay()
                if event["kind"] == "DIRECTOR_REPLAN_REQUESTED"
                and event["payload"].get("reason")
                == "independent exploration slot has no eligible task"
            ]
            self.assertEqual(len(replans), 1)
            self.assertEqual(controller._state_version, 1)

        asyncio.run(scenario())

    def test_cancelled_active_research_is_retained_in_next_epoch_snapshot(self) -> None:
        async def scenario() -> None:
            config = load_config(self.project)
            first = AutonomousController(
                config, backend=MockCodexBackend(), mock=True,
                run_id="epoch-cancelled-active", campaign_id="campaign-cancelled-active",
            )
            first._pin_run_inputs(0.01, True)
            task = research_task("cancelled-active")
            future: asyncio.Future[JobOutcome] = asyncio.get_running_loop().create_future()
            first.active["job-cancelled-active"] = ActiveJob(
                logical_job_id="job-cancelled-active", task=task, future=future,
                started_monotonic=0.0, timeout=60.0, kind="research",
            )

            await first._cancel_active_jobs_before_backend_close(
                "controller interrupted by operator"
            )
            snapshot = json.loads(
                first._write_compact_snapshot().read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["task_id"] for item in snapshot["pending_research"]],
                [task.task_id],
            )

            second = AutonomousController(
                config, backend=MockCodexBackend(), mock=True,
                run_id="epoch-after-cancel", campaign_id="campaign-cancelled-active",
                previous_epoch_id="epoch-cancelled-active",
            )
            second._pin_run_inputs(0.01, True)
            second._import_previous_epoch_checkpoint()
            self.assertEqual(
                [item.task_id for item in second.pending_research], [task.task_id],
            )

        asyncio.run(scenario())

    def test_legacy_cancelled_active_research_is_recovered_from_events(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="legacy-cancelled-epoch", campaign_id="legacy-cancelled-campaign",
        )
        first._pin_run_inputs(0.01, True)
        task = research_task("legacy-cancelled-active")
        first.store.append("TASK_ACCEPTED", {
            "task_id": task.task_id,
            "fingerprint": task.fingerprint,
            "representation_id": task.representation_id,
            "task": task.to_dict(),
        })
        first.store.append("JOB_STARTED", {
            "job_id": "legacy-active-job", "task_id": task.task_id,
            "role": task.role, "claim_id": task.target_claim,
        })
        first.store.append("JOB_CANCELLED", {
            "job_id": "legacy-active-job", "task_id": task.task_id,
            "role": task.role, "claim_id": task.target_claim,
            "failure_kind": "cancelled",
            "exit_reason": "controller interrupted by operator",
        })
        pruned = research_task("intentionally-pruned")
        first.store.append("TASK_ACCEPTED", {
            "task_id": pruned.task_id,
            "fingerprint": pruned.fingerprint,
            "representation_id": pruned.representation_id,
            "task": pruned.to_dict(),
        })
        first.store.append("JOB_STARTED", {
            "job_id": "pruned-job", "task_id": pruned.task_id,
            "role": pruned.role, "claim_id": pruned.target_claim,
        })
        first.store.append("JOB_CANCELLED", {
            "job_id": "pruned-job", "task_id": pruned.task_id,
            "role": pruned.role, "claim_id": pruned.target_claim,
            "failure_kind": "cancelled",
            "exit_reason": "audited dependency pruning",
        })
        first.store.append("RUN_STOPPED", {
            "reason": "controller interrupted by operator",
            "internal_failure": False,
            "campaign_status": "PAUSED",
        })
        first._write_compact_snapshot()

        second = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="legacy-resumed-epoch", campaign_id="legacy-cancelled-campaign",
            previous_epoch_id="legacy-cancelled-epoch",
        )
        second._pin_run_inputs(0.01, True)
        second._import_previous_epoch_checkpoint()

        self.assertEqual(
            [item.task_id for item in second.pending_research], [task.task_id],
        )
        self.assertIn(
            "LEGACY_CANCELLED_TASK_IMPORTED",
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

    def test_candidate_inbox_keeps_its_startup_schema_when_package_resource_disappears(self) -> None:
        inbox = CandidateInbox(
            ProjectLayout(self.project),
            inbox_root=self.runtime / "runs" / "schema-pin" / "events" / "inbox",
            event_log=self.runtime / "runs" / "schema-pin" / "events" / "CANDIDATES.jsonl",
            candidate_root=self.runtime / "runs" / "schema-pin" / "candidates",
        )
        event = CandidateEvent(
            event_id="schema-pinned-candidate", producer_thread_id=None,
            producer_task_id="task", claim_id="C_ROOT", parent_claim_id=None,
            type="KEY_LEMMA", impact="HIGH", concise_summary="candidate",
            exact_statement="One exact candidate statement.", artifact_paths=[],
            reproduction_commands=[], dependency_impact=[],
        )

        with patch(
            "autonomous_math_research.eventing.schema_resource",
            side_effect=FileNotFoundError("simulated package replacement"),
        ):
            inbox.submit(event)
            found = inbox.poll()

        self.assertEqual([item.event_id for item in found], [event.event_id])


if __name__ == "__main__":
    unittest.main()
