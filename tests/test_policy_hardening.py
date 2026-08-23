from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.app_server import (
    AppServerClient, ModelRoutePolicyError, ServiceTierPolicyError,
    attest_model_route, attest_no_service_tier, attest_service_tier,
)
from autonomous_math_research.backend import AppServerBackend, MockCodexBackend
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import ActiveJob, AutonomousController
from autonomous_math_research.director_context import DIRECTOR_PROMPT_HARD_LIMIT_BYTES
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import (
    JobOutcome, ResearchTask, TokenUsage, stable_hash,
)
from autonomous_math_research.policy import pin_policy_manifest
from autonomous_math_research.provenance import capture_runtime_provenance
from autonomous_math_research.resources import schema_resource


RUNTIME = Path(__file__).resolve().parent / "_runtime"
STRICT_EMPTY_SCHEMA = {
    "type": "object", "properties": {}, "required": [], "additionalProperties": False,
}


def _worker_schema() -> dict[str, object]:
    with schema_resource("worker_result.schema.json") as path:
        return json.loads(path.read_text(encoding="utf-8"))


def _director_schema() -> dict[str, object]:
    with schema_resource("director_plan.schema.json") as path:
        return json.loads(path.read_text(encoding="utf-8"))


def _rehash(manifest: dict[str, object]) -> None:
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = stable_hash(body)


class PolicyManifestHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME / f"amr-policy-hardening-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=False)
        self.project = self.root / "neutral-project"
        initialize_project(self.project)
        self.config = load_config(self.project)
        self.manifest_path = self.root / "policy" / "MANIFEST.json"
        self.manifest, _ = pin_policy_manifest(self.config, self.manifest_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _write(self, manifest: dict[str, object]) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_resume_rejects_stale_self_hash(self) -> None:
        tampered = dict(self.manifest)
        tampered["precedence"] = "a chat transcript is authoritative"
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "precedence|fingerprint"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)

    def test_resume_rejects_rehashed_wrong_stable_core(self) -> None:
        tampered = json.loads(json.dumps(self.manifest))
        tampered["stable_core"] = "persistent_chat_thread"
        _rehash(tampered)
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "stable_core"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)

    def test_resume_rejects_rehashed_missing_role(self) -> None:
        tampered = json.loads(json.dumps(self.manifest))
        tampered["role_references"].pop("auditor")
        _rehash(tampered)
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "roles"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)

    def test_resume_rejects_rehashed_reference_outside_skill(self) -> None:
        tampered = json.loads(json.dumps(self.manifest))
        entry = tampered["role_references"]["auditor"][0]
        entry["uri"] = "package://autonomous_math_research/../outside.md"
        entry["snapshot_path"] = "files/../outside.md"
        _rehash(tampered)
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "portable|rebound"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)

    def test_resume_rejects_rehashed_extra_schema_field(self) -> None:
        tampered = json.loads(json.dumps(self.manifest))
        tampered["hidden_authority"] = "producer transcript"
        _rehash(tampered)
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "exactly"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)

    def test_resume_rejects_rehashed_broker_client_rebinding(self) -> None:
        tampered = json.loads(json.dumps(self.manifest))
        entry = tampered["one_shot_compute_worker"]["broker_client"]
        entry["uri"] = tampered["one_shot_compute_worker"][
            "contract_definitions"
        ]["uri"]
        entry["snapshot_path"] = tampered["one_shot_compute_worker"][
            "contract_definitions"
        ]["snapshot_path"]
        entry["sha256"] = tampered["one_shot_compute_worker"][
            "contract_definitions"
        ]["sha256"]
        _rehash(tampered)
        self._write(tampered)
        with self.assertRaisesRegex(ValueError, "broker_client URI is rebound"):
            pin_policy_manifest(self.config, self.manifest_path, resume=True)


_MISSING = object()


class _TierClient:
    def __init__(
        self,
        *,
        thread_tier: object = None,
        turn_tier: object = _MISSING,
        thread_model: str | None = None,
        turn_model: str | None = None,
        include_thread_tier: bool = True,
    ):
        self.thread_tier = thread_tier
        self.turn_tier = turn_tier
        self.thread_model = thread_model
        self.turn_model = turn_model
        self.include_thread_tier = include_thread_tier
        self.goal_calls = 0
        self.turn_calls = 0
        self.start_thread_calls = 0
        self.start_thread_kwargs: dict[str, object] = {}
        self.start_turn_kwargs: list[dict[str, object]] = []

    async def start_thread(self, **kwargs):  # type: ignore[no-untyped-def]
        self.start_thread_kwargs = dict(kwargs)
        self.start_thread_calls += 1
        response: dict[str, object] = {"thread": {"id": "thread-tier"}}
        if self.include_thread_tier:
            response["serviceTier"] = self.thread_tier
        if self.thread_model is not None:
            response["model"] = self.thread_model
        return response

    async def set_goal(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        self.goal_calls += 1
        return {}

    async def start_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.turn_calls += 1
        self.start_turn_kwargs.append(dict(kwargs))
        callback = kwargs.get("on_started")
        if callback:
            callback("turn-tier")
        turn = {"id": "turn-tier", "status": "completed"}
        if self.turn_tier is not _MISSING:
            turn["serviceTier"] = self.turn_tier
        if self.turn_model is not None:
            turn["model"] = self.turn_model
        return (
            {"turn": turn},
            json.dumps({
                "result_type": "NO_PROGRESS",
                "main_finding": "bounded tier test",
                "status": "NO_PROGRESS",
                "artifact_paths": [],
                "next_suggested_question": "none",
                "evidence_level": "E0_SPECULATIVE",
            }),
            TokenUsage(),
            "unknown",
        )


def _task() -> ResearchTask:
    return ResearchTask(
        task_id="tier-task", role="prover", target_claim="TIER-CLAIM",
        exact_objective="Return without using a service tier.", why_now="policy test",
        dependencies=[], expected_information_gain="HIGH",
        mathematical_impact="LOW", estimated_cost_tier="LOW", required_files=[],
        stop_conditions=["return"], output_contract="worker_result.schema.json",
    )


def _director_task() -> ResearchTask:
    return ResearchTask(
        task_id="director-prompt-guard", role="director", target_claim="FRONTIER",
        exact_objective="Return one bounded plan.", why_now="guard regression",
        dependencies=[], expected_information_gain="portfolio decision",
        mathematical_impact="HIGH", estimated_cost_tier="MEDIUM", required_files=[],
        stop_conditions=["return"], output_contract="director_plan.schema.json",
    )


async def _candidate_sink(event):  # type: ignore[no-untyped-def]
    del event


class ServiceTierHardeningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.workspace = RUNTIME / f"amr-tier-hardening-{uuid4().hex}"
        self.workspace.mkdir(parents=True, exist_ok=False)
        self.project = self.workspace / "neutral-project"
        initialize_project(self.project)
        self.backend = AppServerBackend(load_config(self.project))

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace)

    def test_attestation_distinguishes_none_unobservable_and_violation(self) -> None:
        self.assertEqual(attest_no_service_tier({}, "test"), "unobservable")
        self.assertEqual(attest_no_service_tier({"serviceTier": None}, "test"), "none")
        self.assertEqual(attest_no_service_tier({"serviceTier": ""}, "test"), "none")
        with self.assertRaises(ServiceTierPolicyError):
            attest_no_service_tier({"serviceTier": "priority"}, "test")
        self.assertEqual(
            attest_service_tier({"serviceTier": "priority"}, "test", "fast"),
            "priority",
        )
        with self.assertRaises(ServiceTierPolicyError):
            attest_service_tier({}, "test", "fast")
        self.assertEqual(
            attest_model_route({}, "test", "gpt-5.6-sol"), "unobservable"
        )
        self.assertEqual(
            attest_model_route(
                {"model": "gpt-5.6-sol"}, "test", "gpt-5.6-sol"
            ),
            "gpt-5.6-sol",
        )
        with self.assertRaises(ModelRoutePolicyError):
            attest_model_route(
                {"model": "gpt-5.6-terra"}, "test", "gpt-5.6-sol"
            )

    async def test_thread_tier_violation_stops_before_goal_and_turn(self) -> None:
        client = _TierClient(thread_tier="priority")
        self.backend.client = client  # type: ignore[assignment]
        outcome = await self.backend.run_job(
            job_id="job-tier-thread", task=_task(), prompt="unused", output_schema=_worker_schema(),
            workspace=self.workspace, writable_roots=[self.workspace], timeout=1,
            token_budget=100, candidate_sink=_candidate_sink,
        )
        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.observed_service_tier, "priority")
        self.assertIn("service tier policy violation", outcome.error or "")
        self.assertEqual(client.goal_calls, 0)
        self.assertEqual(client.turn_calls, 0)

    async def test_turn_tier_violation_is_error_and_preserves_tier(self) -> None:
        client = _TierClient(thread_tier=None, turn_tier="priority")
        self.backend.client = client  # type: ignore[assignment]
        outcome = await self.backend.run_job(
            job_id="job-tier-turn", task=_task(), prompt="unused", output_schema=_worker_schema(),
            workspace=self.workspace, writable_roots=[self.workspace], timeout=1,
            token_budget=100, candidate_sink=_candidate_sink,
        )
        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.observed_service_tier, "priority")
        self.assertIn("turn/completed", outcome.error or "")
        # Autonomous jobs never arm an App Server goal: native goal
        # continuations would escape controller turn ownership.
        self.assertEqual(client.goal_calls, 0)
        self.assertEqual(client.turn_calls, 1)

    async def test_explicit_fast_requests_both_rpcs_and_accepts_priority_alias(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["execution"]["fast_mode"] = True
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        backend = AppServerBackend(load_config(self.project))
        client = _TierClient(thread_tier="priority")
        backend.client = client  # type: ignore[assignment]

        outcome = await backend.run_job(
            job_id="job-tier-fast", task=_task(), prompt="unused",
            output_schema=_worker_schema(), workspace=self.workspace,
            writable_roots=[self.workspace], timeout=1, token_budget=100,
            candidate_sink=_candidate_sink,
        )

        self.assertTrue(outcome.succeeded, outcome.error)
        self.assertEqual(outcome.requested_service_tier, "fast")
        self.assertEqual(outcome.observed_service_tier, "priority")
        self.assertEqual(client.start_thread_kwargs["service_tier"], "fast")
        self.assertEqual(client.start_turn_kwargs[0]["service_tier"], "fast")

    async def test_explicit_fast_requires_thread_start_confirmation(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["execution"]["fast_mode"] = True
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        backend = AppServerBackend(load_config(self.project))
        client = _TierClient(thread_tier=None)
        backend.client = client  # type: ignore[assignment]

        outcome = await backend.run_job(
            job_id="job-tier-fast-unconfirmed", task=_task(), prompt="unused",
            output_schema=_worker_schema(), workspace=self.workspace,
            writable_roots=[self.workspace], timeout=1, token_budget=100,
            candidate_sink=_candidate_sink,
        )

        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.failure_kind, "service_tier_policy")
        self.assertEqual(client.turn_calls, 0)

    async def test_fast_transport_failure_preserves_root_cause_without_telemetry(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["execution"]["fast_mode"] = True
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        config = load_config(self.project)
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="fast-transport", campaign_id="fast-transport",
        )
        task = _task()
        model, effort = config.model_for(task.role)
        route = config.route_for(task.role)
        outcome = JobOutcome(
            job_id="job-fast-transport", task_id=task.task_id, role=task.role,
            claim_id=task.target_claim, status="ERROR", result={},
            model=model, reasoning_effort=effort,
            provider=str(route["provider"]), provider_profile=route.get("profile"),
            requested_service_tier="fast", observed_service_tier=None,
            error="transport closed before thread/start response",
            failure_kind="transport_transient", retryable=True,
        )
        future = asyncio.get_running_loop().create_future()
        future.set_result(outcome)
        controller.active[outcome.job_id] = ActiveJob(
            outcome.job_id, task, future, 0.0, 60.0, "research",
            workspace=str(self.workspace), workspace_metadata={},
            model=model, reasoning_effort=effort,
            provider=str(route["provider"]), provider_profile=route.get("profile"),
            requested_service_tier="fast",
        )

        with patch.object(controller, "_request_director"):
            await controller._collect_completed()

        self.assertEqual(controller.completed_jobs[0]["failure_kind"], "transport_transient")
        self.assertTrue(controller.completed_jobs[0]["retryable"])
        self.assertFalse(controller._internal_failure)
        self.assertNotIn(
            "SERVICE_TIER_POLICY_VIOLATION",
            {event["kind"] for event in controller.store.replay()},
        )

    async def test_app_server_client_serializes_fast_on_thread_and_turn(self) -> None:
        class CaptureClient(AppServerClient):
            def __init__(self) -> None:
                super().__init__(codex_executable="unused")
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def request(  # type: ignore[override]
                self,
                method: str,
                params: dict[str, object] | None = None,
                timeout: float = 60,
            ) -> object:
                del timeout
                assert params is not None
                self.calls.append((method, dict(params)))
                if method == "thread/start":
                    return {
                        "thread": {"id": "thread-fast"},
                        "activePermissionProfile": {
                            "id": self.permission_profile,
                        },
                    }
                if method == "turn/start":
                    return {"turn": {"id": "turn-fast", "status": "completed"}}
                raise AssertionError(f"unexpected request: {method}")

        client = CaptureClient()
        await client.start_thread(
            model="gpt-5.6-sol", cwd=self.workspace,
            writable_roots=[self.workspace], service_tier="fast",
        )
        await client.start_turn(
            thread_id="thread-fast", prompt="{}", cwd=self.workspace,
            model="gpt-5.6-sol", effort="high", output_schema=STRICT_EMPTY_SCHEMA,
            writable_roots=[self.workspace], timeout=1, service_tier="fast",
        )

        self.assertEqual(client.calls[0][1]["serviceTier"], "fast")
        self.assertEqual(client.calls[1][1]["serviceTier"], "fast")

    def test_fast_runtime_preflight_requires_all_app_server_fields(self) -> None:
        capability = {
            "service_tier": {
                "thread_start_supports_clear": True,
                "turn_start_supports_clear": True,
                "thread_start_reports_tier": False,
            },
        }
        with (
            patch(
                "autonomous_math_research.provenance._codex_capability",
                return_value=capability,
            ),
            self.assertRaisesRegex(ValueError, "thread_start_reports_tier"),
        ):
            capture_runtime_provenance(
                include_codex=True, require_fast_service_tier=True,
            )

    def test_fast_runtime_preflight_reprobes_each_epoch(self) -> None:
        def capability(schema_sha256: str) -> dict[str, object]:
            return {
                "codex_version": f"codex-cli {schema_sha256[:4]}",
                "schema_sha256": schema_sha256,
                "methods": {},
                "notifications": {},
                "thread_token_usage_fields": [],
                "thread_goal_fields": [],
                "thread_start_fields": ["serviceTier"],
                "turn_start_fields": ["serviceTier"],
                "service_tier": {
                    "thread_start_supports_clear": True,
                    "turn_start_supports_clear": True,
                    "thread_start_reports_tier": True,
                },
                "sandbox_policy_variants": [],
            }

        with patch(
            "autonomous_math_research.provenance.inspect_generated_schema",
            side_effect=[capability("1" * 64), capability("2" * 64)],
        ) as probe:
            first = capture_runtime_provenance(
                include_codex=True, require_fast_service_tier=True,
            )
            second = capture_runtime_provenance(
                include_codex=True, require_fast_service_tier=True,
            )

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(first["app_server_schema_sha256"], "1" * 64)
        self.assertEqual(second["app_server_schema_sha256"], "2" * 64)

    async def test_thread_model_mismatch_stops_before_goal_and_turn(self) -> None:
        client = _TierClient(thread_model="gpt-5.6-terra")
        self.backend.client = client  # type: ignore[assignment]
        outcome = await self.backend.run_job(
            job_id="job-model-thread", task=_task(), prompt="unused",
            output_schema=_worker_schema(), workspace=self.workspace,
            writable_roots=[self.workspace], timeout=1, token_budget=100,
            candidate_sink=_candidate_sink,
        )
        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.failure_kind, "model_route_policy")
        self.assertFalse(outcome.retryable)
        self.assertEqual(outcome.server_error["observed_model"], "gpt-5.6-terra")
        self.assertEqual(client.goal_calls, 0)
        self.assertEqual(client.turn_calls, 0)

    async def test_oversize_director_prompt_is_rejected_before_thread_start(self) -> None:
        client = _TierClient()
        self.backend.client = client  # type: ignore[assignment]
        outcome = await self.backend.run_job(
            job_id="job-director-prompt-guard", task=_director_task(),
            prompt="x" * DIRECTOR_PROMPT_HARD_LIMIT_BYTES,
            output_schema=_director_schema(), workspace=self.workspace,
            writable_roots=[self.workspace], timeout=1, token_budget=100,
            candidate_sink=_candidate_sink,
        )
        self.assertEqual(outcome.status, "ERROR")
        self.assertEqual(outcome.failure_kind, "director_prompt_too_large")
        self.assertFalse(outcome.retryable)
        self.assertEqual(client.start_thread_calls, 0)
        self.assertEqual(client.turn_calls, 0)


if __name__ == "__main__":
    unittest.main()
