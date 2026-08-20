from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.app_server import (
    ModelRoutePolicyError, ServiceTierPolicyError, attest_model_route,
    attest_no_service_tier,
)
from autonomous_math_research.backend import AppServerBackend
from autonomous_math_research.config import load_config
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import ResearchTask, TokenUsage, stable_hash
from autonomous_math_research.policy import pin_policy_manifest
from autonomous_math_research.resources import schema_resource


RUNTIME = Path(__file__).resolve().parent / "_runtime"
STRICT_EMPTY_SCHEMA = {
    "type": "object", "properties": {}, "required": [], "additionalProperties": False,
}


def _worker_schema() -> dict[str, object]:
    with schema_resource("worker_result.schema.json") as path:
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


class _TierClient:
    def __init__(
        self,
        *,
        thread_tier: object = None,
        turn_tier: object = None,
        thread_model: str | None = None,
        turn_model: str | None = None,
    ):
        self.thread_tier = thread_tier
        self.turn_tier = turn_tier
        self.thread_model = thread_model
        self.turn_model = turn_model
        self.goal_calls = 0
        self.turn_calls = 0

    async def start_thread(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        thread = {"id": "thread-tier", "serviceTier": self.thread_tier}
        if self.thread_model is not None:
            thread["model"] = self.thread_model
        return {"thread": thread}

    async def set_goal(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        self.goal_calls += 1
        return {}

    async def start_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.turn_calls += 1
        callback = kwargs.get("on_started")
        if callback:
            callback("turn-tier")
        turn = {
                "id": "turn-tier", "status": "completed",
                "serviceTier": self.turn_tier,
            }
        if self.turn_model is not None:
            turn["model"] = self.turn_model
        return (
            {"turn": turn},
            "{}",
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
        self.assertIn("no-fast/no-priority policy violation", outcome.error or "")
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


if __name__ == "__main__":
    unittest.main()
