from __future__ import annotations

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from autonomous_math_research.app_server import (
    AppServerClient, ModelCapabilityError, ModelRoutePolicyError,
    attest_model_route, attest_reasoning_effort,
)
from autonomous_math_research.backend import AppServerBackend, _classify_failure
from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.config import load_config
from autonomous_math_research.model_probe import probe_model
from autonomous_math_research.models import ResearchTask, TokenUsage
from autonomous_math_research.native_handoff import export_inputs, import_result, seal_result
from autonomous_math_research.reasoning_health import ReasoningHealthMonitor
from autonomous_math_research.research_job import ResearchTurnPolicy
from autonomous_math_research.research_memory import ExternalResult, ResearchMemoryStore
from autonomous_math_research.storage import atomic_write_json, file_digest
from support import REPO, TempProjectMixin


PROFILE = REPO / "docs" / "examples" / "astra-research-profile.json"


class ModelCapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    def test_conflicting_observations_and_missing_telemetry(self):
        self.assertEqual(attest_model_route({}, "test", "gpt-6-astra"), "unobservable")
        self.assertIsNone(attest_reasoning_effort({}, "high"))
        with self.assertRaises(ModelRoutePolicyError):
            attest_model_route({"model": "gpt-6-astra", "actualModel": "gpt-5.6-sol"}, "test", "gpt-6-astra")
        with self.assertRaises(ModelCapabilityError):
            attest_reasoning_effort({"effort": "high", "reasoningEffort": "low"}, "high")

    async def test_pagination_model_efforts_and_no_silent_fallback(self):
        client = AppServerClient(codex_executable="unused")
        client.request = AsyncMock(side_effect=[
            {"data": [{"model": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "max"}]}], "nextCursor": "next"},
            {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}, {"reasoningEffort": "xhigh"}]}], "nextCursor": None},
        ])
        await client.validate_model_effort("gpt-6-astra", "xhigh")
        self.assertEqual(client.request.await_count, 2)
        self.assertEqual(client.request.call_args.args[1]["cursor"], "next")
        with self.assertRaisesRegex(ModelCapabilityError, "does not support"):
            await client.validate_model_effort("gpt-6-astra", "max")
        with self.assertRaisesRegex(ModelCapabilityError, "absent"):
            await client.validate_model_effort("missing", "high")
        self.assertEqual(client.request.await_count, 2)

    async def test_bad_catalogs_fail_closed(self):
        for payload in ({}, {"data": [None]}, {"data": [], "nextCursor": "same"}):
            client = AppServerClient(codex_executable="unused")
            client.request = AsyncMock(return_value=payload)
            with self.subTest(payload=payload), self.assertRaises(ModelCapabilityError):
                await client.validate_model_effort("gpt-6-astra", "high")
        client = AppServerClient(codex_executable="unused")
        client.request = AsyncMock(return_value={"data": [{"model": "gpt-6-astra"}]})
        with self.assertRaisesRegex(ModelCapabilityError, "UNKNOWN"):
            await client.validate_model_effort("gpt-6-astra", "high")

    async def test_unsupported_effort_blocks_wire_turn(self):
        client = AppServerClient(codex_executable="unused")
        client.request = AsyncMock(return_value={"data": [{"model": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]})
        from autonomous_math_research.model_probe import PROBE_SCHEMA
        with self.assertRaises(ModelCapabilityError) as captured:
            await client.start_turn(thread_id="test", prompt="test", cwd=Path.cwd(),
                                    model="gpt-6-astra", effort="max", output_schema=PROBE_SCHEMA,
                                    writable_roots=[], timeout=1)
        self.assertEqual(_classify_failure(captured.exception)[:2], ("model_capability", False))
        self.assertEqual([call.args[0] for call in client.request.call_args_list], ["model/list"])


class AstraProfileTests(TempProjectMixin, unittest.TestCase):
    def test_backend_keeps_unknown_and_thread_configuration_separate(self):
        from types import SimpleNamespace
        from autonomous_math_research.resources import schema_resource
        from autonomous_math_research.schema import load_schema
        with schema_resource("worker_result.schema.json") as path:
            schema = load_schema(path)
        result = {"status": "COMPLETED", "result_type": "NO_PROGRESS", "main_finding": "test",
                  "evidence_level": "E0_SPECULATIVE", "artifact_paths": [], "asset_usage": [],
                  "next_suggested_question": "test"}
        task = ResearchTask(task_id="route-test", role="prover", target_claim="C_ROOT", exact_objective="test",
                            why_now="test", dependencies=[], expected_information_gain="LOW", research_impact="LOW",
                            estimated_cost_tier="LOW", required_files=[], stop_conditions=["one turn"])
        for observed in (None, "gpt-6-astra"):
            config = load_config(self.project, profile_path=PROFILE)
            backend = AppServerBackend(config)
            started = {"thread": {"id": "fresh"}, "serviceTier": None}
            if observed:
                started["model"] = observed
            backend.client = SimpleNamespace(
                start_thread=AsyncMock(return_value=started),
                start_turn=AsyncMock(return_value=({"turn": {"id": "one", "status": "completed"}},
                    json.dumps(result), TokenUsage(total_tokens=100), "observed")),
            )
            outcome = asyncio.run(backend.run_job(job_id="test", task=task, prompt="test", output_schema=schema,
                workspace=self.root, writable_roots=[self.root], timeout=1, token_budget=1000, candidate_sink=AsyncMock()))
            self.assertTrue(outcome.succeeded, outcome.to_dict())
            self.assertEqual(outcome.model, "gpt-6-astra")
            self.assertEqual(outcome.observed_model, observed)
            self.assertEqual(outcome.model_observation_source, "thread/start configuration" if observed else None)
            self.assertIsNone(outcome.turn_history[0]["observed_model"])
            self.assertIsNone(outcome.observed_reasoning_effort)

    def test_bounded_live_probe_with_mocked_transport(self):
        from hashlib import sha256

        class FakeClient:
            observations = True
            tokens = 50
            calls = 0
            missing_model = False
            fail_turn = False

            def __init__(self, **kwargs):
                self.observe = kwargs["notification_handler"]

            async def start(self):
                return None

            async def close(self):
                return None

            async def validate_model_effort(self, model, effort):
                if self.missing_model:
                    raise ModelCapabilityError("missing model")

            async def start_thread(self, **kwargs):
                return {"thread": {"id": str(self.calls)}, "model": kwargs["model"], "serviceTier": None}

            async def start_turn(self, **kwargs):
                type(self).calls += 1
                kwargs["on_started"](str(self.calls))
                if self.fail_turn:
                    raise TimeoutError("bounded test deadline")
                workspace = kwargs["cwd"]
                digest = sha256((workspace / "input.bin").read_bytes()).hexdigest()
                (workspace / "output.txt").write_text(digest)
                self.observe({"method": "item/completed", "params": {"item": {"type": "commandExecution", "exitCode": 0}}})
                turn = {"id": str(self.calls), "status": "completed"}
                if self.observations:
                    turn.update(model=kwargs["model"], effort=kwargs["effort"])
                return {"turn": turn}, json.dumps({"status": "OK", "sha256": digest}), TokenUsage(total_tokens=self.tokens), "observed"

        config = load_config(self.project, profile_path=PROFILE)
        for observations, tokens, missing, failure, expected, count in (
            (True, 50, False, False, "PASS", 2),
            (False, 50, False, False, "INCOMPLETE_TELEMETRY_OR_BUDGET", 2),
            (True, 5000, False, False, "INCOMPLETE_TELEMETRY_OR_BUDGET", 1),
            (True, 50, True, False, "FAILED", 0),
            (True, 50, False, True, "FAILED", 1),
        ):
            FakeClient.observations, FakeClient.tokens = observations, tokens
            FakeClient.missing_model, FakeClient.fail_turn, FakeClient.calls = missing, failure, 0
            with self.subTest(expected=expected, count=count), patch("autonomous_math_research.model_probe.AppServerClient", FakeClient):
                report = asyncio.run(probe_model(config, live=True))
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["model_turns_started"], count)
                self.assertEqual(FakeClient.calls, count)
                if not observations:
                    self.assertEqual(report["route_status"], "UNKNOWN")
                    self.assertIsNone(report["observed_model"])

    def test_explicit_profile_preserves_legacy_and_mechanical_routes(self):
        legacy = load_config(self.project)
        astra = load_config(self.project, profile_path=PROFILE)
        for role in astra.raw["models"]:
            self.assertEqual(astra.route_for(role)["model"], "gpt-6-astra")
        self.assertEqual(legacy.route_for("smoke")["model"], "gpt-5.6-terra")
        self.assertEqual(astra.raw["policy"]["one_shot_compute_worker"], legacy.raw["policy"]["one_shot_compute_worker"])
        self.assertEqual(astra.raw["budgets"], legacy.raw["budgets"])
        self.assertFalse(astra.raw["execution"]["fast_mode"])
        with patch("autonomous_math_research.model_probe.AppServerClient") as client:
            report = asyncio.run(probe_model(astra))
        client.assert_not_called()
        self.assertEqual(report["status"], "LIVE_NOT_RUN")
        self.assertEqual(report["requested_model"], "gpt-6-astra")
        self.assertIsNone(report["observed_model"])
        self.assertEqual(report["model_turns_started"], 0)

    def test_short_reasoning_does_not_escalate_but_rejected_candidate_is_repaired(self):
        config = load_config(self.project, profile_path=PROFILE)
        health = ReasoningHealthMonitor(short_reasoning_tokens=600, repeated_token_tolerance=2,
                                        retry_limit=config.raw["engine"]["reasoning_health_retry_limit"])
        signal = health.observe(job_id="job", turn_index=1, effort="xhigh", usage=TokenUsage(reasoning_output_tokens=50),
                                telemetry="observed", max_effort_supported=True)
        self.assertEqual(signal.action, "DIAGNOSE_ONLY")
        policy = ResearchTurnPolicy(max_turns=2)
        arguments = {"result": {"result_type": "PROOF"}, "turn_index": 1,
                     "candidate_accepted": True, "canonical_progress": False, "health_signal": signal}
        self.assertFalse(policy.decide(**arguments).continue_same_thread)
        arguments.update(candidate_accepted=False, candidate_disposition={"status": "REJECTED", "reason": "unknown dependency", "auditor_queue_entered": False})
        self.assertTrue(policy.decide(**arguments).continue_same_thread)


class NativeHandoffTests(TempProjectMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.runtime = self.project / "autonomous"
        self.store = ResearchMemoryStore(self.project, self.runtime)
        self.graph_path = self.runtime / "state" / "claim_graph.json"
        self.trusted_path = self.runtime / "state" / "nightly_trusted.json"
        self.rebuild()
        task = ResearchTask(task_id="native-test", role="prover", target_claim="C_ROOT", exact_objective="Test the exact neutral target under the frozen definitions.",
                            why_now="One bounded obligation", dependencies=[], expected_information_gain="HIGH",
                            research_impact="HIGH", estimated_cost_tier="LOW", required_files=["claims/CLAIMS.md"],
                            stop_conditions=["Return a proof or one precise gap"], metadata={"allow_derived_claims": False, "independent_exploration": False})
        self.task = self.root / "task.json"
        atomic_write_json(self.task, task.to_dict())
        self.native = self.root / "native"
        self.bundle = self.root / "sealed"

    def rebuild(self):
        return self.store.reconcile(graph=ClaimGraph.load(self.graph_path), claim_graph_path=self.graph_path,
                                    trusted_state_path=self.trusted_path, final_claim_id="C_ROOT", theme=None,
                                    phase="MANUAL", campaign_id="test", epoch_id="test")

    def export(self):
        return export_inputs(self.project, self.task, self.native, budget=1000, profile=PROFILE)

    def seal(self):
        return seal_result(self.native, self.native / "result.template.json", self.bundle,
                           input_sha256=file_digest(self.native / "INPUT_MANIFEST.json"))

    def test_roundtrip_does_not_write_authority_or_create_pass(self):
        before = {path: file_digest(path) for path in (self.graph_path, self.trusted_path)}
        report = self.export()
        self.assertEqual(report["permission_isolation"], "SUPERVISED_PROCESS_SEPARATION")
        raw = json.loads((self.native / "result.template.json").read_text())
        raw["conclusion"] = "PROVED"
        raw["proof_refs"][0]["sha256"] = "1" * 64
        atomic_write_json(self.native / "result.template.json", raw)
        (self.native / "output" / "proof.md").write_text("Producer says PASS; this is only a candidate.")
        self.seal()
        report = import_result(self.project, self.bundle)
        self.assertFalse(report["candidate_queue_entered"])
        self.assertFalse(report["audit_receipt_created"])
        self.assertEqual(report, import_result(self.project, self.bundle))
        state, _ = self.rebuild()
        entry = next(row for row in state["frontier_entries"] if row.get("result_id") == "native-test")
        self.assertNotEqual(entry["route_status"], "DO_NOT_ROUTE")
        self.assertEqual(before, {path: file_digest(path) for path in before})

    def test_frozen_input_and_export_receipt_tampering_are_rejected(self):
        report = self.export()
        frozen = self.native / "input" / "claims" / "CLAIMS.md"
        frozen.write_text("changed")
        with self.assertRaisesRegex(ValueError, "frozen native input changed"):
            self.seal()
        packet_path = self.native / "INPUT_MANIFEST.json"
        packet = json.loads(packet_path.read_text())
        for row in packet["files"]:
            row["sha256"] = file_digest(self.native / row["path"])
        atomic_write_json(packet_path, packet)
        with self.assertRaisesRegex(ValueError, "retained export receipt"):
            seal_result(self.native, self.native / "result.template.json", self.bundle, input_sha256=report["input_sha256"])

    def test_self_audit_rejected_before_sealed_bundle_published(self):
        self.export()
        path = self.native / "result.template.json"
        raw = json.loads(path.read_text())
        raw["audit"]["verdict"] = "PASS"
        atomic_write_json(path, raw)
        with self.assertRaisesRegex(ValueError, "audit authority"):
            self.seal()
        self.assertFalse(self.bundle.exists())

    def test_source_drift_and_namespace_confusion(self):
        self.export()
        self.seal()
        source = self.project / "claims" / "CLAIMS.md"
        source.write_text(source.read_text(encoding="utf-8") + "\nchanged definition\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            import_result(self.project, self.bundle)
        path = self.bundle / "external_result.json"
        raw = json.loads(path.read_text())
        raw["dependencies"] = ["external-result-123"]
        atomic_write_json(path, raw)
        with self.assertRaisesRegex(ValueError, "ClaimGraph claim IDs"):
            import_result(self.project, self.bundle)

    def test_export_rejects_stale_frontier_and_mutated_registry(self):
        registry = json.loads(self.store.registry_path.read_text())
        registry["assets"] = [{"asset_id": "invented"}]
        atomic_write_json(self.store.registry_path, registry)
        with self.assertRaisesRegex(ValueError, "Registry is stale"):
            self.export()
        self.rebuild()
        raw = json.loads(self.graph_path.read_text())
        raw["claims"][0]["statement"] += " changed"
        atomic_write_json(self.graph_path, raw)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.export()

    def test_no_overwrite_path_escape_or_representation_rebinding(self):
        self.export()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        raw = json.loads((self.native / "result.template.json").read_text())
        raw["proof_refs"][0]["path"] = "../task.json"
        atomic_write_json(self.native / "result.template.json", raw)
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.seal()
        raw["proof_refs"][0]["path"] = "output/proof.md"
        raw["representation_id"] = "another-representation"
        atomic_write_json(self.native / "result.template.json", raw)
        self.seal()
        with self.assertRaisesRegex(ValueError, "representation"):
            import_result(self.project, self.bundle)

    def test_changed_sealed_evidence_and_conflicting_import_id(self):
        self.export()
        self.seal()
        imported = import_result(self.project, self.bundle)
        old_manifest = Path(imported["manifest"]).read_bytes()
        raw = json.loads((self.bundle / "external_result.json").read_text())
        raw["exact_statement"] += " a different statement"
        atomic_write_json(self.bundle / "external_result.json", raw)
        with self.assertRaisesRegex(ValueError, "different content"):
            import_result(self.project, self.bundle)
        self.assertEqual(old_manifest, Path(imported["manifest"]).read_bytes())
        (self.bundle / raw["proof_refs"][0]["path"]).write_text("changed evidence")
        with self.assertRaisesRegex(ValueError, "digest changed"):
            import_result(self.project, self.bundle)

    def test_import_recovers_after_evidence_copy_without_overwriting(self):
        self.export()
        self.seal()
        with patch("autonomous_math_research.native_handoff.os.link", side_effect=OSError("simulated publication crash")):
            with self.assertRaisesRegex(OSError, "simulated"):
                import_result(self.project, self.bundle)
        self.assertFalse((self.store.external_results_root / "native-test.json").exists())
        self.assertTrue(import_result(self.project, self.bundle)["imported"])

    def test_manifest_rewrite_during_validation_is_rejected(self):
        self.export()
        self.seal()
        load = ExternalResult.load

        def mutate_after_load(root, path):
            result = load(root, path)
            raw = json.loads(path.read_text())
            raw["audit"]["verdict"] = "PASS"
            atomic_write_json(path, raw)
            return result

        with patch("autonomous_math_research.native_handoff.ExternalResult.load", side_effect=mutate_after_load):
            with self.assertRaisesRegex(ValueError, "changed during validation"):
                import_result(self.project, self.bundle)
        self.assertFalse((self.store.external_results_root / "native-test.json").exists())
