from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.config import load_config
from autonomous_math_research.contracts import mechanical_lifecycle_metrics
from autonomous_math_research.controller import (
    ActiveJob, AutonomousController, MechanicalRequestState,
)
from autonomous_math_research.delegate_mechanical_task import (
    _request_for_packet, _validate_response,
)
from autonomous_math_research.mechanical import (
    FALLBACK_MECHANICAL_ROUTE,
    MECHANICAL_PARENT_ROLES,
    MechanicalExecution,
    MechanicalTaskRejected,
    PRIMARY_MECHANICAL_ROUTE,
    SubprocessMechanicalRunner,
    _runner_usage,
    validate_mechanical_request,
    validate_mechanical_task_packet,
)
from autonomous_math_research.models import (
    LifecyclePhase, ResearchTask, TokenUsage, stable_hash,
)
from autonomous_math_research.monitor import _MonitorDashboardState, build_status
from autonomous_math_research.policy import pin_policy_manifest
from autonomous_math_research.reporting import render_nightly_report
from autonomous_math_research.schema import (
    load_schema,
    validate,
    validate_output_schema_compatibility,
)
from autonomous_math_research.storage import EventStore, atomic_write_json, file_digest
from support import REPO, TempProjectMixin


PACKAGE_SOURCE = REPO / "src" / "autonomous_math_research"
POLICY_ROOT = PACKAGE_SOURCE / "resources" / "policy_packs" / "math-research"
RUNNER_PATH = POLICY_ROOT / "scripts" / "run_worker.py"
TASK_SCHEMA_PATH = POLICY_ROOT / "references" / "worker-task.schema.json"
RESULT_SCHEMA_PATH = POLICY_ROOT / "references" / "worker-result.schema.json"


def valid_packet(*, task_id: str = "mechanical-1", input_file: str = "AGENTS.md") -> dict:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "task_kind": "finite_exact_computation",
        "objective": "Compute the specified finite table exactly.",
        "mathematical_statement": "For n in {0,1,2}, record n squared.",
        "input_files": [input_file],
        "allowed_tools": ["python"],
        "bounds": {
            "finite": True,
            "description": "Three explicitly listed integer inputs.",
            "parameters": [{"name": "n", "value": "0..2"}],
        },
        "timeout_seconds": 30,
        "expected_artifacts": ["artifacts/result.json"],
        "success_condition": "Artifact contains exactly [0, 1, 4].",
        "falsification_condition": "A deterministic replay produces another value.",
        "stop_condition": "Stop after the three values and one replay check.",
        "verification_steps": ["Run the same finite loop twice and compare bytes."],
        "requires_mathematical_judgment": False,
        "project_id": "neutral-project",
        "notes": "No interpretation or follow-up task is permitted.",
    }


def request_envelope(
    packet: dict,
    *,
    parent_job_id: str,
    parent_task_id: str,
    parent_role: str,
) -> dict:
    value = {
        "schema_version": 1,
        "parent_job_id": parent_job_id,
        "parent_task_id": parent_task_id,
        "parent_role": parent_role,
        "submitted_at": "2026-08-18T00:00:00Z",
        "task_packet": packet,
    }
    value["request_sha256"] = stable_hash(value)
    return value


def parent_task(task_id: str, role: str) -> ResearchTask:
    contract = (
        "director_plan.schema.json"
        if role == "director"
        else "audit_result.schema.json"
        if "auditor" in role
        else "worker_result.schema.json"
    )
    return ResearchTask(
        task_id=task_id,
        role=role,
        target_claim="C",
        exact_objective="Perform the parent role without changing C.",
        why_now="mechanical broker test",
        dependencies=[],
        expected_information_gain="MEDIUM",
        mathematical_impact="LOW",
        estimated_cost_tier="LOW",
        required_files=[],
        stop_conditions=["return"],
        output_contract=contract,
    )


class SequenceMechanicalRunner:
    def __init__(self, executions: list[MechanicalExecution]):
        self.executions = list(executions)
        self.calls: list[dict] = []

    async def run(
        self, *, packet_path: Path, output_root: Path, timeout_seconds: int,
        route: str = "primary",
    ) -> MechanicalExecution:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        call_number = len(self.calls) + 1
        run_dir = output_root / f"fake-{call_number}"
        artifact = run_dir / "artifacts/result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("[0,1,4]\n", encoding="utf-8", newline="\n")
        self.calls.append({
            "packet": packet,
            "packet_path": str(packet_path),
            "output_root": str(output_root),
            "timeout_seconds": timeout_seconds,
            "route": route,
        })
        if not self.executions:
            raise AssertionError("fake mechanical runner received an unexpected call")
        execution = copy.deepcopy(self.executions.pop(0))
        execution.runner_directory = str(run_dir)
        if execution.status not in {"TOOL_ERROR", "REJECTED", "BLOCKED"}:
            execution.artifacts = [str(artifact)]
            execution.result = {
                "task_id": packet["task_id"],
                "status": execution.status,
                "mechanical_evidence_only": True,
            }
        return execution


class LeaseRecoveryRunner(SequenceMechanicalRunner):
    def __init__(self) -> None:
        super().__init__([])
        self.recover_calls: list[dict] = []

    async def recover(
        self,
        *,
        packet_path: Path,
        output_root: Path,
        receipt_path: Path,
        timeout_seconds: int,
    ) -> MechanicalExecution:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        run_dir = output_root / "recovered-existing-attempt"
        artifact = run_dir / "artifacts/result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("[0,1,4]\n", encoding="utf-8", newline="\n")
        self.recover_calls.append({
            "packet_path": str(packet_path),
            "output_root": str(output_root),
            "receipt_path": str(receipt_path),
            "timeout_seconds": timeout_seconds,
        })
        return MechanicalExecution(
            status="COMPLETED",
            result={
                "task_id": packet["task_id"],
                "status": "COMPLETED",
                "mechanical_evidence_only": True,
            },
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort=PRIMARY_MECHANICAL_ROUTE["reasoning_effort"],
            token_usage=TokenUsage(total_tokens=7, input_tokens=5, output_tokens=2),
            token_telemetry="observed",
            artifacts=[str(artifact)],
            runner_directory=str(run_dir),
        )


def success_execution(*, tokens: int = 10) -> MechanicalExecution:
    return MechanicalExecution(
        status="COMPLETED",
        result={},
        model=PRIMARY_MECHANICAL_ROUTE["model"],
        reasoning_effort=PRIMARY_MECHANICAL_ROUTE["reasoning_effort"],
        token_usage=TokenUsage(total_tokens=tokens, input_tokens=max(0, tokens - 2), output_tokens=2),
        token_telemetry="observed",
    )


class MechanicalContractTests(unittest.TestCase):
    def test_task_and_result_schemas_use_the_shared_app_server_gate(self) -> None:
        for path in (TASK_SCHEMA_PATH, RESULT_SCHEMA_PATH):
            schema = load_schema(path)
            validate_output_schema_compatibility(schema, schema_path=path)
        validate(valid_packet(), load_schema(TASK_SCHEMA_PATH))

    def test_all_six_parent_roles_can_submit_the_same_bounded_contract(self) -> None:
        for role in sorted(MECHANICAL_PARENT_ROLES):
            envelope = request_envelope(
                valid_packet(task_id=f"mechanical-{role}"),
                parent_job_id=f"job-{role}",
                parent_task_id=f"task-{role}",
                parent_role=role,
            )
            validated = validate_mechanical_request(
                envelope,
                repository_root=REPO,
                expected_parent_job_id=f"job-{role}",
                expected_parent_task_id=f"task-{role}",
                expected_parent_role=role,
                maximum_timeout_seconds=60,
            )
            self.assertEqual(validated["parent_role"], role)

    def test_judgment_recursive_network_and_path_escape_tasks_are_rejected(self) -> None:
        judgment = valid_packet()
        judgment["requires_mathematical_judgment"] = True
        with self.assertRaisesRegex(MechanicalTaskRejected, "mathematical judgment"):
            validate_mechanical_task_packet(judgment, repository_root=REPO)

        strategy = valid_packet()
        strategy["objective"] = "Choose a proof strategy for the open conjecture."
        with self.assertRaisesRegex(MechanicalTaskRejected, "strategy/judgment"):
            validate_mechanical_task_packet(strategy, repository_root=REPO)

        direct_proof = valid_packet()
        direct_proof["objective"] = "Prove the theorem using any valid argument."
        with self.assertRaisesRegex(MechanicalTaskRejected, "strategy/judgment"):
            validate_mechanical_task_packet(direct_proof, repository_root=REPO)

        hidden_strategy = valid_packet()
        hidden_strategy["verification_steps"] = [
            "Choose a proof strategy, then record the selected lemma."
        ]
        with self.assertRaisesRegex(MechanicalTaskRejected, "strategy/judgment"):
            validate_mechanical_task_packet(hidden_strategy, repository_root=REPO)

        hidden_direct_proof = valid_packet()
        hidden_direct_proof["falsification_condition"] = (
            "Prove the theorem if the finite replay does not fail."
        )
        with self.assertRaisesRegex(MechanicalTaskRejected, "strategy/judgment"):
            validate_mechanical_task_packet(hidden_direct_proof, repository_root=REPO)

        statement_only = valid_packet()
        statement_only["mathematical_statement"] = (
            "Prove that the listed finite identity holds for n in {0,1,2}."
        )
        validate_mechanical_task_packet(statement_only, repository_root=REPO)

        limitation = valid_packet()
        limitation["notes"] = (
            "This finite enumeration does not prove the general statement."
        )
        validate_mechanical_task_packet(limitation, repository_root=REPO)

        recursive = valid_packet()
        recursive["allowed_tools"] = ["codex exec", "web browser"]
        with self.assertRaisesRegex(MechanicalTaskRejected, "forbidden recursive/network"):
            validate_mechanical_task_packet(recursive, repository_root=REPO)

        escaped = valid_packet()
        escaped["expected_artifacts"] = ["../canonical.json"]
        with self.assertRaisesRegex(MechanicalTaskRejected, "artifacts"):
            validate_mechanical_task_packet(escaped, repository_root=REPO)

        sensitive = valid_packet()
        sensitive["input_files"] = [".git/config"]
        with self.assertRaisesRegex(MechanicalTaskRejected, "authentication/VCS"):
            validate_mechanical_task_packet(sensitive, repository_root=REPO)

    def test_local_apply_patch_is_allowed_but_app_connectors_are_rejected(self) -> None:
        packet = valid_packet()
        packet["task_kind"] = "code_modification"
        packet["allowed_tools"] = ["python", "apply_patch"]
        validate_mechanical_task_packet(packet, repository_root=REPO)

        packet["allowed_tools"] = ["python", "App connector"]
        with self.assertRaisesRegex(MechanicalTaskRejected, "forbidden"):
            validate_mechanical_task_packet(packet, repository_root=REPO)

    def test_skipped_cached_route_is_not_counted_as_unknown_telemetry(self) -> None:
        usage, telemetry = _runner_usage({
            "selected_model": FALLBACK_MECHANICAL_ROUTE["model"],
            "worker_started": True,
            "route_attempts": [
                {
                    "model": PRIMARY_MECHANICAL_ROUTE["model"],
                    "probe_attempted": False,
                    "probe_usage": None,
                    "worker_usage": None,
                },
                {
                    "model": FALLBACK_MECHANICAL_ROUTE["model"],
                    "probe_attempted": True,
                    "probe_usage": {"total_tokens": 2},
                    "worker_usage": {"total_tokens": 5},
                },
            ],
        })
        # No paid capability probe exists: only the actual worker usage counts.
        self.assertEqual(usage.total_tokens, 5)
        self.assertEqual(telemetry, "observed")


class WorkerRouteTests(TempProjectMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        spec = importlib.util.spec_from_file_location("mechanical_worker_runner_tests", RUNNER_PATH)
        assert spec is not None and spec.loader is not None
        cls.runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner_module)

    def setUp(self) -> None:
        super().setUp()
        (self.root / "AGENTS.md").write_text("fixture\n", encoding="utf-8")
        self.runner_module.REPO_ROOT = self.root

    def _packet_path(self, task_id: str) -> Path:
        packet = valid_packet(task_id=task_id)
        path = self.root / f"{task_id}.json"
        atomic_write_json(path, packet)
        return path

    @staticmethod
    def _valid_result(packet: dict) -> dict:
        return {
            "task_id": packet["task_id"],
            "status": "COMPLETED",
            "evidence_level": "E2_EXACT_TESTED",
            "objective": packet["objective"],
            "completed_scope": "n=0..2",
            "key_findings": ["The exact table is [0, 1, 4]."],
            "counterexample": None,
            "artifacts": ["artifacts/result.json"],
            "commands": ["python replay.py"],
            "toolchain": ["Python"],
            "interpretation": "Finite execution evidence only.",
            "limitations": ["No proof beyond the stated finite scope."],
            "blocked_on": None,
            "observations": [],
        }

    def _fake_worker_run(self, command: list[str], **kwargs):
        stdout_path = kwargs["stdout_path"]
        stderr_path = kwargs["stderr_path"]
        run_dir = stdout_path.parent
        packet = json.loads((run_dir / "task_packet.json").read_text(encoding="utf-8"))
        artifact = run_dir / "artifacts/result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("[0,1,4]\n", encoding="utf-8", newline="\n")
        last_message = Path(command[command.index("--output-last-message") + 1])
        atomic_write_json(last_message, self._valid_result(packet))
        stdout_path.write_text(
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            }) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        stderr_path.write_text("", encoding="utf-8")
        return 0, False, 0.01

    def _invoke_main(self, task_id: str, **mocks) -> tuple[int, dict]:
        packet_path = self._packet_path(task_id)
        output_root = self.root / f"worker-output-{task_id}"
        receipt_path = output_root.parent / "receipts" / f"{task_id}.json"
        model_status_path = output_root.parent / "model-status" / f"{task_id}.json"
        argv = [
            "run_worker.py", str(packet_path), "--output-root", str(output_root),
            "--timeout", "30", "--broker-managed",
            "--broker-receipt", str(receipt_path),
            "--model-status-path", str(model_status_path),
        ]
        with patch.object(sys, "argv", argv), patch.object(
            self.runner_module, "find_codex", return_value=Path("codex")
        ), patch.object(
            self.runner_module, "inspect_codex", return_value="codex-test"
        ), patch.object(
            self.runner_module, "run_one_shot",
            side_effect=mocks.get("run_one_shot", self._fake_worker_run),
        ) as run_one_shot, patch.object(
            self.runner_module, "cached_model_unavailable",
            side_effect=mocks["cached_model_unavailable"],
        ) as cached, patch.object(
            self.runner_module, "record_model_unavailable", return_value={}
        ) as record, patch("builtins.print"):
            code = self.runner_module.main()
        runner_files = list(output_root.rglob("runner.json"))
        self.assertEqual(len(runner_files), 1)
        metadata = json.loads(runner_files[0].read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "exited")
        self.assertEqual(Path(receipt["run_directory"]), runner_files[0].parent)
        metadata["_mock_calls"] = {
            "run_one_shot": run_one_shot.call_count,
            "cached": cached.call_args_list,
            "probe": [],
            "record": record.call_args_list,
        }
        return code, metadata

    def test_fixed_route_command_is_spark_high_then_luna_medium_with_null_tier(self) -> None:
        module = self.runner_module
        self.assertEqual(module.FIXED_WORKER_ROUTES, (
            {"model": "gpt-5.3-codex-spark", "reasoning_effort": "high", "service_tier": None},
            {"model": "gpt-5.6-luna", "reasoning_effort": "medium", "service_tier": None},
        ))
        command = module.codex_command(
            Path("codex"),
            model=module.DEFAULT_WORKER_MODEL,
            reasoning_effort=module.DEFAULT_WORKER_REASONING_EFFORT,
            service_tier=None,
            cwd=self.root,
            schema=RESULT_SCHEMA_PATH,
            last_message=self.root / "last.json",
            permission_profile=module.WORKER_PERMISSION_PROFILE,
            workspace_access="write",
        )
        rendered = " ".join(command)
        self.assertIn("model_reasoning_effort=\"high\"", rendered)
        self.assertIn("service_tier=null", rendered)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--strict-config", command)
        self.assertIn("approval_policy=\"never\"", rendered)
        self.assertNotIn("--sandbox", command)
        self.assertIn('default_permissions=\"mechanical-one-shot\"', rendered)
        self.assertIn('filesystem.:root="deny"', rendered)
        self.assertIn('filesystem.:minimal="read"', rendered)
        self.assertIn(
            'permissions.mechanical-one-shot.filesystem.:workspace_roots={ "." = "write" }',
            command,
        )
        self.assertNotIn('filesystem.\":workspace_roots\".\".\"', rendered)
        self.assertIn("permissions.mechanical-one-shot.network.enabled=false", rendered)
        self.assertIn("shell_environment_policy.ignore_default_excludes=false", rendered)
        self.assertIn("CODEX_HOME", rendered)
        self.assertIn("features.multi_agent=false", rendered)
        self.assertIn("features.plugins=false", rendered)
        self.assertNotIn("gpt-5.6-sol", rendered)
        self.assertNotIn("gpt-5.6-terra", rendered)

    def test_route_config_can_start_directly_on_luna_without_route_rotation(self) -> None:
        route_config = self.root / "route-config.json"
        atomic_write_json(route_config, {
            "schema_version": 2,
            "start_route": "fallback",
            "primary_route": {
                "provider": "codex",
                "model": PRIMARY_MECHANICAL_ROUTE["model"],
                "reasoning_effort": "high",
                "service_tier": None,
                "profile": "local-login",
            },
            "fallback_route": {
                "provider": "codex",
                "model": FALLBACK_MECHANICAL_ROUTE["model"],
                "reasoning_effort": "medium",
                "service_tier": None,
                "profile": "local-login",
            },
        })

        primary, fallback, start_route = self.runner_module.load_worker_routes(
            route_config, broker_managed=True,
        )

        self.assertEqual(primary["model"], PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(fallback["model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertEqual(start_route, "fallback")

    @unittest.skipUnless(os.name == "nt", "Windows npm wrapper behavior")
    def test_windows_npm_wrapper_uses_node_entry_without_cmd_quote_rewriting(self) -> None:
        npm_root = self.root / "npm"
        wrapper = npm_root / "codex.cmd"
        node = npm_root / "node.exe"
        entry = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        entry.parent.mkdir(parents=True)
        wrapper.write_text("stub\n", encoding="utf-8")
        node.write_text("stub\n", encoding="utf-8")
        entry.write_text("stub\n", encoding="utf-8")

        prefix = self.runner_module.codex_prefix(wrapper.resolve())

        self.assertEqual(prefix, [str(node.resolve()), str(entry.resolve())])
        self.assertNotIn((os.environ.get("COMSPEC") or "cmd.exe"), prefix)

    def test_direct_runner_reuses_the_no_judgment_no_recursion_boundary(self) -> None:
        local_edit = valid_packet()
        local_edit["task_kind"] = "code_modification"
        local_edit["allowed_tools"] = ["python", "apply_patch"]
        self.runner_module.validate_task_packet(local_edit)
        recursive = valid_packet()
        recursive["allowed_tools"] = ["codex exec"]
        with self.assertRaisesRegex(self.runner_module.ContractError, "forbidden capability"):
            self.runner_module.validate_task_packet(recursive)
        app_connector = valid_packet()
        app_connector["allowed_tools"] = ["App connector"]
        with self.assertRaisesRegex(self.runner_module.ContractError, "forbidden capability"):
            self.runner_module.validate_task_packet(app_connector)
        strategy = valid_packet()
        strategy["mathematical_statement"] = "Choose a proof strategy for an open claim."
        with self.assertRaisesRegex(self.runner_module.ContractError, "strategy/judgment"):
            self.runner_module.validate_task_packet(strategy)
        hidden_strategy = valid_packet()
        hidden_strategy["verification_steps"] = [
            "Choose a proof strategy, then record the selected lemma."
        ]
        with self.assertRaisesRegex(self.runner_module.ContractError, "strategy/judgment"):
            self.runner_module.validate_task_packet(hidden_strategy)
        statement_only = valid_packet()
        statement_only["mathematical_statement"] = (
            "Prove that the listed finite identity holds for n in {0,1,2}."
        )
        self.runner_module.validate_task_packet(statement_only)
        sensitive = valid_packet()
        sensitive["input_files"] = [".git/config"]
        with self.assertRaisesRegex(
            self.runner_module.ContractError, "authentication/VCS",
        ):
            self.runner_module.validate_task_packet(sensitive)

    def test_cached_spark_unavailable_skips_probe_and_uses_luna_once(self) -> None:
        def cached(*, model, **_kwargs):
            return {"model": model} if model == PRIMARY_MECHANICAL_ROUTE["model"] else None

        code, metadata = self._invoke_main(
            "cached-spark",
            cached_model_unavailable=cached,
            probe_model=lambda _codex, **kwargs: (
                True,
                {"supported": True, "usage": None, "model": kwargs["model"]},
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(metadata["selected_model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertEqual(metadata["selected_reasoning_effort"], "medium")
        self.assertIsNone(metadata["selected_service_tier"])
        self.assertIsNone(metadata["actual_model"])
        self.assertEqual(metadata["model_route_attestation"], "unobservable")
        self.assertEqual(metadata["_mock_calls"]["probe"], [])
        self.assertEqual(metadata["_mock_calls"]["run_one_shot"], 1)
        self.assertEqual(len(metadata["_mock_calls"]["record"]), 0)

    def test_spark_provider_failures_request_one_luna_fallback(self) -> None:
        def permanent(_command, **kwargs):
            kwargs["stdout_path"].write_text(
                json.dumps({
                    "type": "turn.failed",
                    "error": {"code": "model_not_found", "message": "access denied"},
                }) + "\n", encoding="utf-8", newline="\n",
            )
            kwargs["stderr_path"].write_text("", encoding="utf-8")
            return 1, False, 0.01

        code, metadata = self._invoke_main(
            "permanent-spark",
            cached_model_unavailable=lambda **_kwargs: None,
            probe_model=None,
            run_one_shot=permanent,
        )
        self.assertEqual(code, 4)
        self.assertEqual(metadata["selected_model"], PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(metadata["_mock_calls"]["probe"], [])
        self.assertEqual(len(metadata["_mock_calls"]["record"]), 1)
        self.assertIsNotNone(metadata["fallback"])
        self.assertTrue(metadata["fallback"]["continuation_required"])

        def transient(_command, **kwargs):
            kwargs["stdout_path"].write_text(
                json.dumps({
                    "type": "turn.failed",
                    "error": {"code": "server_error", "message": "temporary timeout"},
                }) + "\n", encoding="utf-8", newline="\n",
            )
            kwargs["stderr_path"].write_text("", encoding="utf-8")
            return 1, False, 0.01

        code, metadata = self._invoke_main(
            "transient-spark",
            cached_model_unavailable=lambda **_kwargs: None,
            probe_model=None,
            run_one_shot=transient,
        )
        self.assertEqual(code, 4)
        self.assertEqual(metadata["selected_model"], PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(metadata["_mock_calls"]["probe"], [])
        self.assertEqual(len(metadata["_mock_calls"]["record"]), 0)
        self.assertEqual(metadata["fallback"]["to_model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertTrue(metadata["fallback"]["continuation_required"])
        self.assertTrue(metadata["failure"]["retryable"])

    def test_spark_usage_limit_is_nonretryable_quota_not_unavailability(self) -> None:
        def quota(_command, **kwargs):
            kwargs["stdout_path"].write_text(
                json.dumps({
                    "type": "turn.failed",
                    "error": {
                        "code": "usage_limit_reached",
                        "message": (
                            "You've hit your usage limit for Spark. "
                            "Try again at Aug 23rd, 2026 2:54 PM."
                        ),
                    },
                }) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            kwargs["stderr_path"].write_text("", encoding="utf-8")
            return 1, False, 0.01

        code, metadata = self._invoke_main(
            "spark-quota",
            cached_model_unavailable=lambda **_kwargs: None,
            probe_model=None,
            run_one_shot=quota,
        )
        self.assertEqual(code, 4)
        self.assertEqual(metadata["selected_model"], PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(metadata["_mock_calls"]["record"], [])
        self.assertEqual(metadata["fallback"]["to_model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertTrue(metadata["fallback"]["continuation_required"])
        self.assertEqual(metadata["failure"]["kind"], "provider_quota_exhausted")
        self.assertFalse(metadata["failure"]["retryable"])
        self.assertEqual(
            metadata["failure"]["provider_reset_at"],
            "Aug 23rd, 2026 2:54 PM",
        )

    def test_only_explicit_access_denial_is_cached_as_unavailable(self) -> None:
        stdout = self.root / "probe.jsonl"
        stderr = self.root / "probe.stderr"
        stdout.write_text(
            json.dumps({
                "type": "turn.failed",
                "error": {"code": "model_not_found", "message": "not available to this project"},
            }) + "\n",
            encoding="utf-8",
        )
        stderr.write_text("", encoding="utf-8")
        self.assertTrue(
            self.runner_module.classify_probe_failure(stdout, stderr)["cache_as_unavailable"]
        )
        stdout.write_text(
            json.dumps({
                "type": "turn.failed",
                "error": {"code": "server_error", "message": "temporary network timeout"},
            }) + "\n",
            encoding="utf-8",
        )
        self.assertFalse(
            self.runner_module.classify_probe_failure(stdout, stderr)["cache_as_unavailable"]
        )
        stderr.write_text(
            "Access denied while opening a local diagnostic file.", encoding="utf-8",
        )
        self.assertFalse(
            self.runner_module.classify_probe_failure(stdout, stderr)["cache_as_unavailable"]
        )

    def test_cli_service_tier_attestation_distinguishes_unknown_null_and_violation(self) -> None:
        path = self.root / "tier-events.jsonl"
        path.write_text('{"type":"turn.started"}\n', encoding="utf-8", newline="\n")
        self.assertEqual(
            self.runner_module.cli_service_tier_attestation(path)["status"],
            "unobservable",
        )
        path.write_text(
            '{"type":"thread.started","thread":{"serviceTier":null}}\n',
            encoding="utf-8", newline="\n",
        )
        self.assertEqual(
            self.runner_module.cli_service_tier_attestation(path)["status"],
            "none",
        )
        path.write_text(
            '{"type":"thread.started","thread":{"service_tier":"priority"}}\n',
            encoding="utf-8", newline="\n",
        )
        attestation = self.runner_module.cli_service_tier_attestation(path)
        self.assertEqual(attestation["status"], "violation")
        self.assertEqual(attestation["observed"], ["priority"])

    def test_cli_model_route_attestation_rejects_reroutes_and_never_invents_actual(self) -> None:
        path = self.root / "model-events.jsonl"
        path.write_text(
            '{"type":"turn.completed","usage":{"total_tokens":1}}\n',
            encoding="utf-8", newline="\n",
        )
        unobservable = self.runner_module.cli_model_route_attestation(
            path,
            requested_model="gpt-5.3-codex-spark",
            requested_reasoning_effort="high",
        )
        self.assertEqual(unobservable["status"], "unobservable")
        self.assertIsNone(unobservable["actual_model"])

        path.write_text(
            '{"type":"thread.started","thread":{"model":"gpt-5.3-codex-spark",'
            '"reasoning_effort":"high"}}\n',
            encoding="utf-8", newline="\n",
        )
        matched = self.runner_module.cli_model_route_attestation(
            path,
            requested_model="gpt-5.3-codex-spark",
            requested_reasoning_effort="high",
        )
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["actual_model"], "gpt-5.3-codex-spark")

        path.write_text(
            '{"type":"model/rerouted","fromModel":"gpt-5.3-codex-spark",'
            '"toModel":"gpt-5.6-terra"}\n',
            encoding="utf-8", newline="\n",
        )
        rerouted = self.runner_module.cli_model_route_attestation(
            path,
            requested_model="gpt-5.3-codex-spark",
            requested_reasoning_effort="high",
        )
        self.assertEqual(rerouted["status"], "violation")
        self.assertEqual(len(rerouted["reroute_events"]), 1)

    def test_actual_service_tier_violation_never_retries_or_falls_back(self) -> None:
        def violation(command, **kwargs):
            result = self._fake_worker_run(command, **kwargs)
            kwargs["stdout_path"].write_text(
                '{"type":"thread.started","thread":{"serviceTier":"priority"}}\n',
                encoding="utf-8", newline="\n",
            )
            return result

        code, metadata = self._invoke_main(
            "tier-policy",
            cached_model_unavailable=lambda **_kwargs: None,
            probe_model=None,
            run_one_shot=violation,
        )
        self.assertEqual(code, 4)
        self.assertEqual(metadata["failure"]["kind"], "service_tier_policy")
        self.assertEqual(metadata["_mock_calls"]["probe"], [])
        self.assertEqual(len(metadata["_mock_calls"]["record"]), 0)
        self.assertIsNone(metadata["fallback"])

    def test_actual_model_reroute_violation_never_retries_or_falls_back(self) -> None:
        def violation(command, **kwargs):
            result = self._fake_worker_run(command, **kwargs)
            kwargs["stdout_path"].write_text(
                '{"type":"model/rerouted","fromModel":"gpt-5.3-codex-spark",'
                '"toModel":"gpt-5.6-terra"}\n',
                encoding="utf-8", newline="\n",
            )
            return result

        code, metadata = self._invoke_main(
            "model-route-policy",
            cached_model_unavailable=lambda **_kwargs: None,
            probe_model=None,
            run_one_shot=violation,
        )
        self.assertEqual(code, 4)
        self.assertEqual(metadata["failure"]["kind"], "model_route_policy")
        self.assertEqual(metadata["_mock_calls"]["probe"], [])
        self.assertEqual(len(metadata["_mock_calls"]["record"]), 0)
        self.assertIsNone(metadata["fallback"])

    def test_monitor_marks_unknown_mechanical_usage_as_a_lower_bound(self) -> None:
        lifecycle = [{
            "kind": "RUN_STARTED",
            "timestamp": "2026-08-21T00:00:00+00:00",
            "payload": {
                "global_budget": 500_000_000,
                "mechanical_budget": 1_500_000_000,
                "max_director": 1,
                "max_research_workers": 8,
                "max_audit": 2,
                "max_mechanical_subworkers": None,
                "mechanical_effective_resource_cap": 8,
            },
        }, {
            "kind": "MECHANICAL_SUBTASK_FAILED",
            "timestamp": "2026-08-21T00:01:00+00:00",
            "payload": {
                "parent_job_id": "parent-1",
                "subtask_id": "mechanical-1",
                "token_usage": {"total_tokens": 0},
                "token_telemetry": "unknown",
            },
        }]
        state = _MonitorDashboardState("run-1", lifecycle, [])

        lines = state.lines(quiet_seconds=0)

        self.assertIn("机械 ≥0（1次用量未知）/1500.00M", lines[1])

    def test_worker_process_output_is_redacted_before_artifact_write(self) -> None:
        stdout = self.root / "redacted.stdout"
        stderr = self.root / "redacted.stderr"
        code, timed_out, _runtime = self.runner_module.run_one_shot(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "print('Authorization: Bearer unit-bearer-secret'); "
                    "print('sk-' + 'unit-test-secret', file=sys.stderr); "
                    "print('{\\\"access_token\\\":\\\"unit-access-secret\\\"}', "
                    "file=sys.stderr); "
                    "print('AWS_SECRET_ACCESS_KEY=unit-aws-secret', file=sys.stderr); "
                    "print('client_secret: unit-client-secret', file=sys.stderr)"
                ),
            ],
            prompt="",
            stdout_path=stdout,
            stderr_path=stderr,
            timeout=10,
        )
        self.assertEqual(code, 0)
        self.assertFalse(timed_out)
        persisted = stdout.read_text(encoding="utf-8") + stderr.read_text(encoding="utf-8")
        self.assertNotIn("unit-bearer-secret", persisted)
        self.assertNotIn("sk-" + "unit-test-secret", persisted)
        self.assertNotIn("unit-access-secret", persisted)
        self.assertNotIn("unit-aws-secret", persisted)
        self.assertNotIn("unit-client-secret", persisted)
        self.assertIn("[REDACTED]", persisted)

        last_message = self.root / "result.raw.json"
        last_message.write_text(
            '{"access_token":"unit-last-message-secret"}',
            encoding="utf-8", newline="",
        )
        self.runner_module.sanitize_sensitive_file(last_message)
        sanitized = last_message.read_text(encoding="utf-8")
        self.assertNotIn("unit-last-message-secret", sanitized)
        self.assertEqual(json.loads(sanitized)["access_token"], "[REDACTED]")

    def test_worker_process_scrubs_secret_environment_but_keeps_codex_login_path(self) -> None:
        stdout = self.root / "isolated-env.stdout"
        stderr = self.root / "isolated-env.stderr"
        fake_codex_home = str(self.root / "fake-codex-home")
        with patch.dict(os.environ, {
            "UNIT_TEST_API_KEY": "unit-secret-api-key",
            "UNIT_TEST_TOKEN": "unit-secret-token",
            "CODEX_HOME": fake_codex_home,
        }, clear=False):
            code, timed_out, _runtime = self.runner_module.run_one_shot(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json,os; print(json.dumps({"
                        "'api_absent': os.getenv('UNIT_TEST_API_KEY') is None,"
                        "'token_absent': os.getenv('UNIT_TEST_TOKEN') is None,"
                        f"'codex_home_retained': os.getenv('CODEX_HOME') == {fake_codex_home!r}"
                        "}))"
                    ),
                ],
                prompt="",
                stdout_path=stdout,
                stderr_path=stderr,
                timeout=10,
            )
        self.assertEqual(code, 0)
        self.assertFalse(timed_out)
        value = json.loads(stdout.read_text(encoding="utf-8"))
        self.assertEqual(value, {
            "api_absent": True,
            "token_absent": True,
            "codex_home_retained": True,
        })
        self.assertNotIn("unit-secret", stderr.read_text(encoding="utf-8"))

    def test_worker_environment_removes_recursive_codex_entrypoints(self) -> None:
        codex_dir = self.root / "codex-bin"
        safe_dir = self.root / "safe-bin"
        codex_dir.mkdir()
        safe_dir.mkdir()
        codex_name = "codex.cmd" if os.name == "nt" else "codex"
        codex = codex_dir / codex_name
        codex.write_text("stub", encoding="utf-8")
        with patch.dict(
            os.environ,
            {"PATH": os.pathsep.join((str(codex_dir), str(safe_dir)))},
            clear=False,
        ):
            environment = self.runner_module.isolated_worker_environment(
                blocked_executable=codex,
            )
        self.assertEqual(environment["PATH"], str(safe_dir))
        self.assertEqual(environment["MATH_MECHANICAL_ONE_SHOT"], "1")

    def test_persisted_codex_command_does_not_expose_recursive_path(self) -> None:
        codex = (self.root / "codex-bin" / "codex.cmd").resolve()
        command = [
            os.environ.get("COMSPEC", "cmd.exe"), "/c", str(codex),
            "exec", "--model", "gpt-5.3-codex-spark",
        ]
        public = self.runner_module.public_codex_command(command, codex)
        self.assertNotIn(str(codex), public)
        self.assertIn("<codex-cli>", public)

    def test_worker_event_gate_rejects_recursive_tools_and_commands(self) -> None:
        path = self.root / "worker-activity.jsonl"
        path.write_text(
            json.dumps({
                "type": "item.started",
                "item": {"type": "command_execution", "command": "python verify.py"},
            }) + "\n",
            encoding="utf-8", newline="\n",
        )
        self.assertIsNone(self.runner_module.forbidden_worker_activity(path))
        path.write_text(
            json.dumps({
                "type": "item.started",
                "item": {"type": "command_execution", "command": "codex exec -"},
            }) + "\n",
            encoding="utf-8", newline="\n",
        )
        violation = self.runner_module.forbidden_worker_activity(path)
        self.assertIsNotNone(violation)
        self.assertIn("recursive", violation["reason"])
        path.write_text(
            json.dumps({
                "type": "item.started",
                "item": {"type": "collabToolCall", "tool": "spawn_agent"},
        }) + "\n",
            encoding="utf-8", newline="\n",
        )
        self.assertIsNotNone(self.runner_module.forbidden_worker_activity(path))

    def test_worker_event_gate_enforces_packet_tools_and_isolated_file_scope(self) -> None:
        path = self.root / "worker-capability-activity.jsonl"
        packet = valid_packet()
        run_dir = self.root / "isolated-worker-run"
        run_dir.mkdir()

        def write_command(command: str) -> None:
            path.write_text(
                json.dumps({
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": command},
                }) + "\n",
                encoding="utf-8", newline="\n",
            )

        write_command("python inputs/check.py")
        self.assertIsNone(self.runner_module.forbidden_worker_activity(
            path, packet=packet, run_dir=run_dir,
        ))
        write_command("sage inputs/check.sage")
        unapproved = self.runner_module.forbidden_worker_activity(
            path, packet=packet, run_dir=run_dir,
        )
        self.assertIsNotNone(unapproved)
        self.assertIn("allowed_tools", unapproved["reason"])
        write_command("python ../state/claim_graph.json")
        escaped = self.runner_module.forbidden_worker_activity(
            path, packet=packet, run_dir=run_dir,
        )
        self.assertIsNotNone(escaped)
        self.assertIn("escape", escaped["reason"])
        write_command("python -c \"print(open('$env:CODEX_HOME/auth.json').read())\"")
        secret = self.runner_module.forbidden_worker_activity(
            path, packet=packet, run_dir=run_dir,
        )
        self.assertIsNotNone(secret)
        self.assertIn("authentication", secret["reason"])


class MechanicalControllerTests(TempProjectMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        raw = json.loads(self.config.config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 7
        raw["scheduler"]["max_mechanical_subworkers"] = 3
        raw["policy"]["one_shot_compute_worker"]["enabled"] = True
        self.config.config_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self.config = load_config(self.project)
        (self.project / "AGENTS.md").write_text("fixture\n", encoding="utf-8")
        self.parent_futures: list[asyncio.Task] = []

    async def asyncTearDown(self) -> None:
        for future in self.parent_futures:
            if not future.done():
                future.cancel()
        if self.parent_futures:
            await asyncio.gather(*self.parent_futures, return_exceptions=True)

    def test_parent_helper_reuses_exact_request_and_binds_full_response_identity(self) -> None:
        root = self.root / "helper-idempotence"
        config = {
            "parent_job_id": "job-helper",
            "parent_task_id": "task-helper",
            "parent_role": "prover",
        }
        packet = valid_packet(task_id="helper-subtask")
        request_path = root / "requests/helper-subtask.json"
        first = _request_for_packet(config, packet, request_path)
        second = _request_for_packet(config, copy.deepcopy(packet), request_path)
        self.assertEqual(first, second)
        changed = copy.deepcopy(packet)
        changed["objective"] = "A different objective must not reuse this id."
        with self.assertRaisesRegex(ValueError, "different request or parent binding"):
            _request_for_packet(config, changed, request_path)

        response = {
            "schema_version": 1,
            "parent_job_id": "job-helper",
            "parent_task_id": "task-helper",
            "parent_role": "prover",
            "subtask_id": "helper-subtask",
            "status": "COMPLETED",
            "model": PRIMARY_MECHANICAL_ROUTE["model"],
            "reasoning_effort": "high",
            "service_tier": None,
            "token_usage": TokenUsage(total_tokens=7).to_dict(),
            "token_telemetry": "observed",
            "result": {},
            "artifacts": [],
            "runner_directory": None,
            "fallback": None,
            "error": None,
            "failure_kind": None,
            "retryable": False,
        }
        self.assertEqual(
            _validate_response(response, config=config, subtask_id="helper-subtask"),
            response,
        )
        wrong_parent = dict(response)
        wrong_parent["parent_job_id"] = "job-other"
        with self.assertRaisesRegex(ValueError, "parent_job_id"):
            _validate_response(
                wrong_parent, config=config, subtask_id="helper-subtask",
            )

    def _activate_parent(
        self,
        controller: AutonomousController,
        *,
        role: str,
        suffix: str,
        packet: dict | None = None,
        parent_task_id: str | None = None,
    ) -> tuple[str, Path, Path]:
        if controller.lifecycle.phase is LifecyclePhase.BOOTSTRAP:
            controller.lifecycle.transition(
                LifecyclePhase.RUNNING, reason="unit test broker dispatch",
            )
        task_id = parent_task_id or f"parent-{suffix}"
        assigned = parent_task(task_id, role)
        workspace, _writable, metadata = controller.workspace.create_job_workspace(
            f"workspace-{suffix}"
        )
        (workspace / "AGENTS.md").write_text(
            "neutral parent workspace fixture\n", encoding="utf-8", newline="\n",
        )
        parent_job_id = f"job-{suffix}"
        future = asyncio.create_task(asyncio.Event().wait())
        self.parent_futures.append(future)
        kind = "director" if role == "director" else "audit" if "auditor" in role else "research"
        controller.active[parent_job_id] = ActiveJob(
            parent_job_id,
            assigned,
            future,
            time.monotonic(),
            300,
            kind,
            str(workspace),
            "2026-08-18T00:00:00Z",
            metadata,
        )
        command = controller.workspace.install_mechanical_broker_client(
            workspace,
            parent_job_id=parent_job_id,
            parent_task_id=task_id,
            parent_role=role,
            parent_timeout_seconds=300,
            enabled=True,
            broker_client_source=(
                PACKAGE_SOURCE / "delegate_mechanical_task.py"
            ),
            broker_client_sha256=file_digest(
                PACKAGE_SOURCE / "delegate_mechanical_task.py"
            ),
        )
        controller.active[parent_job_id].broker_client_sha256 = file_digest(
            workspace / "delegate_mechanical_task.py"
        )
        controller.active[parent_job_id].broker_config_sha256 = file_digest(
            controller.workspace.mechanical_broker_config_path(workspace)
        )
        self.assertIn("delegate_mechanical_task.py", command)
        task_packet = packet or valid_packet(
            task_id=f"subtask-{suffix}",
            input_file="AGENTS.md",
        )
        envelope = request_envelope(
            task_packet,
            parent_job_id=parent_job_id,
            parent_task_id=task_id,
            parent_role=role,
        )
        request_path = workspace / "mechanical_broker/requests" / f"{task_packet['task_id']}.json"
        response_path = (
            controller.workspace.mechanical_broker_response_root(workspace)
            / f"{task_packet['task_id']}.json"
        )
        self.assertFalse(response_path.is_relative_to(workspace))
        self.assertFalse((workspace / "mechanical_broker.json").exists())
        self.assertTrue(
            controller.workspace.mechanical_broker_config_path(workspace).is_file()
        )
        atomic_write_json(request_path, envelope)
        return parent_job_id, request_path, response_path

    async def test_parent_workspace_inputs_are_accepted_without_project_root_fallback(self) -> None:
        runner = SequenceMechanicalRunner([success_execution(tokens=11)])
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=runner,
        )
        packet = valid_packet(
            task_id="workspace-input",
            input_file="candidate_bundle/check.py",
        )
        _job, request_path, response_path = self._activate_parent(
            controller, role="auditor", suffix="workspace-input", packet=packet,
        )
        workspace = request_path.resolve().parents[2]
        source = workspace / "candidate_bundle/check.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('bounded replay')\n", encoding="utf-8", newline="\n")

        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["packet"]["input_files"], [
            "candidate_bundle/check.py",
        ])
        self.assertEqual(json.loads(response_path.read_text(encoding="utf-8"))["status"], "COMPLETED")

    async def test_parent_workspace_input_cannot_fall_back_to_project_root(self) -> None:
        runner = SequenceMechanicalRunner([])
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=runner,
        )
        project_only = self.project / "project-only-input.txt"
        project_only.write_text("must remain inaccessible\n", encoding="utf-8", newline="\n")
        packet = valid_packet(
            task_id="project-root-fallback",
            input_file="project-only-input.txt",
        )
        _job, _request_path, response_path = self._activate_parent(
            controller, role="explorer", suffix="project-root-fallback", packet=packet,
        )

        await controller._poll_mechanical_requests()

        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "REJECTED")
        self.assertEqual(response["failure_kind"], "ineligible_mechanical_task")
        self.assertEqual(runner.calls, [])

    async def test_subprocess_runner_uses_the_parent_workspace_as_its_input_root(self) -> None:
        workspace = self.project / "autonomous/runs/runner-root/jobs/parent"
        packet_path = workspace / "mechanical_subtasks/packets/task.attempt-1.json"
        output_root = workspace / "mechanical_subtasks/runs"
        source = workspace / "candidate_bundle/check.py"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("print('bounded replay')\n", encoding="utf-8", newline="\n")
        atomic_write_json(
            packet_path,
            valid_packet(task_id="runner-root", input_file="candidate_bundle/check.py"),
        )
        runner = SubprocessMechanicalRunner(self.project)
        runner.script = RUNNER_PATH
        runner.expected_hashes = {runner.script: runner._digest(runner.script)}
        captured: dict = {}

        class FakeProcess:
            returncode = 0
            pid = 12345

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_subprocess(*command: str, **kwargs: object) -> FakeProcess:
            captured["command"] = command
            captured["kwargs"] = kwargs
            return FakeProcess()

        with patch(
            "autonomous_math_research.mechanical.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ):
            execution = await runner.run(
                packet_path=packet_path,
                output_root=output_root,
                timeout_seconds=30,
            )

        self.assertEqual(execution.failure_kind, "runner_protocol")
        self.assertEqual(Path(str(captured["kwargs"]["cwd"])).resolve(), workspace.resolve())
        environment = captured["kwargs"]["env"]
        self.assertIsInstance(environment, dict)
        self.assertEqual(
            Path(str(environment["MATH_WORKER_REPOSITORY_ROOT"])).resolve(),
            workspace.resolve(),
        )

    async def test_subprocess_runner_rejects_mismatched_attempt_roots(self) -> None:
        workspace = self.project / "autonomous/runs/runner-mismatch/jobs/parent"
        packet_path = workspace / "another-directory/task.json"
        output_root = workspace / "mechanical_subtasks/runs"
        atomic_write_json(packet_path, valid_packet(task_id="runner-mismatch"))
        runner = SubprocessMechanicalRunner(self.project)
        runner.script = RUNNER_PATH
        runner.expected_hashes = {runner.script: runner._digest(runner.script)}

        with self.assertRaisesRegex(MechanicalTaskRejected, "attempt workspace"):
            await runner.run(
                packet_path=packet_path,
                output_root=output_root,
                timeout_seconds=30,
            )

    @staticmethod
    async def _drain_mechanical(controller: AutonomousController, rounds: int = 4) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)
            await controller._collect_mechanical_completed()
            if not controller.active_mechanical and not controller.pending_mechanical:
                return

    async def test_director_research_and_audit_share_broker_events_tokens_report_and_guard(self) -> None:
        runner = SequenceMechanicalRunner([
            success_execution(tokens=11), success_execution(tokens=12), success_execution(tokens=13),
        ])
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=runner,
        )
        controller.store.append("RUN_STARTED", {
            "global_budget": 2_000_000_000,
            "max_director": 1,
            "max_research_workers": 8,
            "max_audit": 8,
            "max_mechanical_subworkers": 3,
        })
        baseline = controller.guard.snapshot()
        responses = []
        for role in ("director", "prover", "auditor"):
            _job, _request, response = self._activate_parent(
                controller, role=role, suffix=role,
            )
            responses.append(response)

        await controller._poll_mechanical_requests()
        self.assertEqual(len(controller.active), 3)
        self.assertLessEqual(len(controller.active_mechanical), 3)
        await self._drain_mechanical(controller)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(controller.guard.verify(), [])
        self.assertEqual(controller.guard.baseline, baseline)
        self.assertTrue(all(path.is_file() for path in responses))
        self.assertTrue(all(
            json.loads(path.read_text(encoding="utf-8"))["service_tier"] is None
            for path in responses
        ))

        records = controller.store.replay()
        metrics = mechanical_lifecycle_metrics(records)
        self.assertEqual((metrics.requested, metrics.attempts_started, metrics.terminal), (3, 3, 3))
        self.assertEqual(metrics.active_subtasks, ())
        self.assertEqual(metrics.duplicate_terminal_subtasks, ())
        self.assertEqual(metrics.orphan_terminal_subtasks, ())
        self.assertEqual(controller.mechanical_governor.by_role["mechanical_subworker"], 36)
        status = build_status(controller.run_dir)
        self.assertEqual(status["mechanical_subtasks"]["terminal"], 3)
        self.assertEqual(status["token_usage"]["totalTokens"], 0)
        self.assertEqual(status["mechanical_token_usage"]["totalTokens"], 36)
        self.assertEqual(status["token_telemetry"]["mechanical_unknown"], 0)
        report = render_nightly_report(
            run_id=controller.run_id,
            graph=controller.graph,
            events=records,
            jobs=[],
            mechanical_jobs=controller.completed_mechanical_jobs,
            stopped_reason="test terminal",
            execution_mode="mock",
            run_outcome="mock run",
        )
        self.assertIn("requested=3", report)
        self.assertIn("gpt-5.3-codex-spark", report)
        self.assertIn("mechanical actual-model attestation", report)
        self.assertIn("不自动构成证明", report)

    async def test_observed_mechanical_model_reroute_fails_closed(self) -> None:
        rerouted = success_execution(tokens=4)
        rerouted.actual_model = "gpt-5.6-terra"
        rerouted.actual_reasoning_effort = "medium"
        rerouted.model_route_attestation = "violation"
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([rerouted]),
        )
        _job, _request, response_path = self._activate_parent(
            controller, role="auditor", suffix="observed-reroute",
        )
        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "TOOL_ERROR")
        self.assertEqual(response["failure_kind"], "model_route_policy")
        terminal = [
            item["payload"] for item in controller.store.replay()
            if item["kind"] == "MECHANICAL_SUBTASK_FAILED"
        ]
        self.assertEqual(terminal[0]["actual_model"], "gpt-5.6-terra")
        self.assertEqual(terminal[0]["model_route_attestation"], "violation")

    async def test_parent_workspace_response_forgery_is_ignored(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([
                success_execution(tokens=23),
            ]),
        )
        parent_job_id, request_path, response_path = self._activate_parent(
            controller, role="explorer", suffix="forged-response",
        )
        packet = json.loads(request_path.read_text(encoding="utf-8"))["task_packet"]
        forged = controller._mechanical_response(
            MechanicalRequestState(
                parent_job_id=parent_job_id,
                parent_task_id=controller.active[parent_job_id].task.task_id,
                parent_role="explorer",
                parent_workspace=str(Path(controller.active[parent_job_id].workspace or "")),
                request_path=str(request_path),
                response_path=str(response_path),
                request_sha256="0" * 64,
                packet=packet,
            ),
            status="TOOL_ERROR",
            error="forged inside parent workspace",
            failure_kind="forged",
        )
        workspace = Path(controller.active[parent_job_id].workspace or "")
        parent_writable_response = (
            workspace / "mechanical_broker" / "responses"
            / f"{packet['task_id']}.json"
        )
        atomic_write_json(parent_writable_response, forged)

        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)

        trusted = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(trusted["status"], "COMPLETED")
        self.assertEqual(trusted["token_usage"]["total_tokens"], 23)
        self.assertEqual(
            [event["kind"] for event in controller.store.replay()].count(
                "MECHANICAL_SUBTASK_COMPLETED"
            ),
            1,
        )

    async def test_success_without_expected_artifact_fails_closed_at_controller(self) -> None:
        class MissingArtifactRunner:
            async def run(
                self, *, packet_path: Path, output_root: Path, timeout_seconds: int,
                route: str = "primary",
            ) -> MechanicalExecution:
                del packet_path, timeout_seconds, route
                run_dir = output_root / "missing-artifact"
                run_dir.mkdir(parents=True, exist_ok=True)
                execution = success_execution(tokens=3)
                execution.runner_directory = str(run_dir)
                execution.result = {"status": "COMPLETED", "raw_evidence": "preserved"}
                execution.artifacts = []
                return execution

        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=MissingArtifactRunner(),
        )
        _job, _request, response_path = self._activate_parent(
            controller, role="prover", suffix="artifact-fail",
        )
        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)

        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "TOOL_ERROR")
        self.assertEqual(response["failure_kind"], "artifact_validation")
        self.assertEqual(response["result"]["raw_evidence"], "preserved")
        terminal = [
            event for event in controller.store.replay()
            if event["kind"] == "MECHANICAL_SUBTASK_FAILED"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["payload"]["runner_reported_status"], "COMPLETED")

    async def test_transient_retry_stays_on_spark_and_permanent_denial_falls_back_once(self) -> None:
        transient = MechanicalExecution(
            status="TOOL_ERROR", result={},
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort="high",
            token_usage=TokenUsage(total_tokens=3),
            token_telemetry="unknown",
            error="temporary transport failure",
            failure_kind="transport_transient",
            retryable=True,
        )
        runner = SequenceMechanicalRunner([transient, success_execution(tokens=7)])
        controller = AutonomousController(
            self.config, backend=MockCodexBackend(), mock=True, mechanical_runner=runner,
        )
        _job, _request, response_path = self._activate_parent(
            controller, role="prover", suffix="transient",
        )
        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)
        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual(response["token_usage"]["total_tokens"], 10)
        self.assertEqual(response["token_telemetry"], "partial")
        kinds = [item["kind"] for item in controller.store.replay()]
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_RETRY_QUEUED"), 1)
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_FALLBACK"), 0)
        self.assertEqual(len(runner.calls), 2)

        quota = MechanicalExecution(
            status="TOOL_ERROR",
            result={},
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort="high",
            token_usage=TokenUsage(total_tokens=4),
            token_telemetry="observed",
            error="provider quota exhausted",
            failure_kind="provider_quota_exhausted",
            retryable=False,
            provider_reset_at="Aug 23rd, 2026 2:54 PM",
        )
        quota_runner = SequenceMechanicalRunner([quota])
        quota_controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=quota_runner,
        )
        _job, _request, quota_response_path = self._activate_parent(
            quota_controller, role="prover", suffix="quota",
        )
        await quota_controller._poll_mechanical_requests()
        await self._drain_mechanical(quota_controller)
        quota_response = json.loads(quota_response_path.read_text(encoding="utf-8"))
        self.assertEqual(quota_response["failure_kind"], "provider_quota_exhausted")
        self.assertFalse(quota_response["retryable"])
        self.assertEqual(len(quota_runner.calls), 1)
        self.assertEqual(
            quota_controller.scheduler_stop_reason,
            (
                "campaign paused: mechanical provider quota exhausted until "
                "Aug 23rd, 2026 2:54 PM"
            ),
        )
        self.assertIs(quota_controller.lifecycle.phase, LifecyclePhase.DRAINING_EPOCH)
        quota_events = [
            event for event in quota_controller.store.replay()
            if event["kind"] == "MECHANICAL_PROVIDER_QUOTA_EXHAUSTED"
        ]
        self.assertEqual(len(quota_events), 1)
        self.assertFalse(quota_events[0]["payload"]["mathematical_failure"])
        self.assertEqual(quota_events[0]["payload"]["stagnation_effect"], "none")

        permanent = MechanicalExecution(
            status="TOOL_ERROR", result={},
            model=PRIMARY_MECHANICAL_ROUTE["model"], reasoning_effort="high",
            token_usage=TokenUsage(total_tokens=2), token_telemetry="observed",
            fallback={
                "from_model": PRIMARY_MECHANICAL_ROUTE["model"],
                "to_model": FALLBACK_MECHANICAL_ROUTE["model"],
                "reason": "permanent access denied",
                "continuation_required": True,
            },
            error="access denied", failure_kind="model_unavailable", retryable=False,
            unavailable_routes=[{
                "model": PRIMARY_MECHANICAL_ROUTE["model"],
                "reasoning_effort": "high",
                "service_tier": None,
                "failed_at_utc": "2026-08-18T00:00:00+00:00",
                "error": "access denied",
                "run_directory": "fake-primary",
            }],
        )
        luna_success = MechanicalExecution(
            status="COMPLETED", result={},
            model=FALLBACK_MECHANICAL_ROUTE["model"], reasoning_effort="medium",
            token_usage=TokenUsage(total_tokens=5), token_telemetry="observed",
        )
        fallback_runner = SequenceMechanicalRunner([transient, permanent, luna_success])
        fallback_controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=fallback_runner,
        )
        _job, _request, fallback_response_path = self._activate_parent(
            fallback_controller, role="auditor", suffix="fallback",
        )
        await fallback_controller._poll_mechanical_requests()
        await self._drain_mechanical(fallback_controller)
        fallback_response = json.loads(fallback_response_path.read_text(encoding="utf-8"))
        self.assertEqual(fallback_response["model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertEqual(fallback_response["reasoning_effort"], "medium")
        self.assertEqual(fallback_response["token_usage"]["total_tokens"], 10)
        fallback_kinds = [item["kind"] for item in fallback_controller.store.replay()]
        self.assertEqual(fallback_kinds.count("MECHANICAL_SUBTASK_FALLBACK"), 1)
        self.assertEqual(fallback_kinds.count("MECHANICAL_ROUTE_UNAVAILABLE"), 1)
        self.assertEqual(len(fallback_runner.calls), 3)

        luna_transient = MechanicalExecution(
            status="TOOL_ERROR", result={},
            model=FALLBACK_MECHANICAL_ROUTE["model"], reasoning_effort="medium",
            token_usage=TokenUsage(total_tokens=3), token_telemetry="unknown",
            error="temporary Luna transport failure",
            failure_kind="transport_transient", retryable=True,
        )
        continuation_runner = SequenceMechanicalRunner([
            permanent, luna_transient, luna_success,
        ])
        continuation_controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=continuation_runner,
        )
        _job, _request, continuation_response_path = self._activate_parent(
            continuation_controller, role="auditor", suffix="fallback-retry-budget",
        )
        await continuation_controller._poll_mechanical_requests()
        await self._drain_mechanical(continuation_controller)
        continuation_response = json.loads(
            continuation_response_path.read_text(encoding="utf-8")
        )
        self.assertEqual(continuation_response["status"], "COMPLETED")
        self.assertEqual(continuation_response["model"], FALLBACK_MECHANICAL_ROUTE["model"])
        continuation_kinds = [
            item["kind"] for item in continuation_controller.store.replay()
        ]
        self.assertEqual(
            continuation_kinds.count("MECHANICAL_SUBTASK_FALLBACK_CONTINUATION_QUEUED"),
            1,
        )
        self.assertEqual(continuation_kinds.count("MECHANICAL_SUBTASK_RETRY_QUEUED"), 1)
        self.assertEqual(len(continuation_runner.calls), 3)

    async def test_spark_transport_failure_continues_once_on_luna_medium(self) -> None:
        spark_failure = MechanicalExecution(
            status="TOOL_ERROR",
            result={},
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort="high",
            token_usage=TokenUsage(total_tokens=3),
            token_telemetry="unknown",
            fallback={
                "from_model": PRIMARY_MECHANICAL_ROUTE["model"],
                "to_model": FALLBACK_MECHANICAL_ROUTE["model"],
                "reason": "primary provider transport failed",
                "continuation_required": True,
            },
            error="temporary transport failure",
            failure_kind="transport_transient",
            retryable=True,
        )
        luna_success = success_execution(tokens=7)
        luna_success.model = FALLBACK_MECHANICAL_ROUTE["model"]
        luna_success.reasoning_effort = "medium"
        runner = SequenceMechanicalRunner([spark_failure, luna_success])
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=runner,
        )
        _job, _request, response_path = self._activate_parent(
            controller, role="prover", suffix="spark-to-luna",
        )

        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)

        response = json.loads(response_path.read_text(encoding="utf-8"))
        self.assertEqual(response["status"], "COMPLETED")
        self.assertEqual(response["model"], FALLBACK_MECHANICAL_ROUTE["model"])
        self.assertEqual([call["route"] for call in runner.calls], ["primary", "fallback"])
        kinds = [event["kind"] for event in controller.store.replay()]
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_FALLBACK"), 1)
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_RETRY_QUEUED"), 0)

    async def test_ineligible_task_is_rejected_before_runner_for_mock_and_real_modes(self) -> None:
        for mock in (True, False):
            runner = SequenceMechanicalRunner([])
            controller = AutonomousController(
                self.config,
                backend=MockCodexBackend(),
                mock=mock,
                mechanical_runner=runner,
            )
            bad = valid_packet(task_id=f"bad-{mock}")
            bad["requires_mathematical_judgment"] = True
            _job, _request, response_path = self._activate_parent(
                controller, role="director", suffix=f"bad-{mock}", packet=bad,
            )
            await controller._poll_mechanical_requests()
            response = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertEqual(response["status"], "REJECTED")
            self.assertEqual(response["failure_kind"], "ineligible_mechanical_task")
            self.assertEqual(runner.calls, [])

    async def test_exact_result_cache_reuses_parent_task_subtask_without_new_worker(self) -> None:
        runner = SequenceMechanicalRunner([success_execution(tokens=9)])
        controller = AutonomousController(
            self.config, backend=MockCodexBackend(), mock=True, mechanical_runner=runner,
        )
        shared_parent_task = "stable-parent-task"
        packet = valid_packet(task_id="stable-subtask")
        self._activate_parent(
            controller,
            role="explorer",
            suffix="cache-first",
            packet=packet,
            parent_task_id=shared_parent_task,
        )
        await controller._poll_mechanical_requests()
        await self._drain_mechanical(controller)
        self._activate_parent(
            controller,
            role="explorer",
            suffix="cache-second",
            packet=packet,
            parent_task_id=shared_parent_task,
        )
        await controller._poll_mechanical_requests()
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(controller.completed_mechanical_jobs), 2)
        self.assertTrue(controller.completed_mechanical_jobs[-1]["cache_reused"])
        self.assertEqual(controller.mechanical_governor.by_role["mechanical_subworker"], 9)

    async def test_recovery_restores_attempt_watermark_tokens_and_does_not_over_retry(self) -> None:
        runner = SequenceMechanicalRunner([])
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=runner,
        )
        parent_job_id, request_path, _response_path = self._activate_parent(
            controller, role="falsifier", suffix="recover",
        )
        raw_request = json.loads(request_path.read_text(encoding="utf-8"))
        packet = raw_request["task_packet"]
        controller.store.append("MECHANICAL_SUBTASK_REQUESTED", {
            "parent_job_id": parent_job_id,
            "parent_task_id": "parent-recover",
            "parent_role": "falsifier",
            "subtask_id": packet["task_id"],
            "task_kind": packet["task_kind"],
            "request_path": str(request_path),
            "request_sha256": raw_request["request_sha256"],
            "packet_sha256": stable_hash(packet),
            "task_packet": packet,
            "valid": True,
        })
        for attempt, tokens in ((1, 4), (2, 6)):
            controller.store.append("MECHANICAL_SUBTASK_STARTED", {
                "mechanical_job_id": f"old-a{attempt}",
                "parent_job_id": parent_job_id,
                "parent_task_id": "parent-recover",
                "parent_role": "falsifier",
                "subtask_id": packet["task_id"],
                "attempt": attempt,
            })
            controller.store.append("MECHANICAL_SUBTASK_ATTEMPT_FINISHED", {
                "mechanical_job_id": f"old-a{attempt}",
                "parent_job_id": parent_job_id,
                "parent_task_id": "parent-recover",
                "parent_role": "falsifier",
                "subtask_id": packet["task_id"],
                "attempt": attempt,
                "status": "TOOL_ERROR",
                "model": PRIMARY_MECHANICAL_ROUTE["model"],
                "reasoning_effort": "high",
                "actual_model": PRIMARY_MECHANICAL_ROUTE["model"],
                "actual_reasoning_effort": "high",
                "model_route_attestation": "matched",
                "service_tier": None,
                "token_usage": TokenUsage(total_tokens=tokens).to_dict(),
                "token_telemetry": "observed",
                "result": {},
                "artifacts": [],
                "runner_directory": None,
                "fallback": None,
                "error": "transient",
                "failure_kind": "transport_transient",
                "retryable": True,
            })
        for future in self.parent_futures:
            future.cancel()
        controller.active.clear()

        resumed = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            run_id=controller.run_id,
            mock=True,
            resume=True,
            mechanical_runner=runner,
        )
        resumed.recover()
        self.assertEqual(runner.calls, [])
        self.assertEqual(resumed.pending_mechanical, [])
        self.assertEqual(len(resumed.completed_mechanical_jobs), 1)
        terminal = resumed.completed_mechanical_jobs[0]
        self.assertEqual(terminal["status"], "TOOL_ERROR")
        self.assertEqual(terminal["token_usage"]["total_tokens"], 10)
        self.assertEqual(terminal["actual_model"], PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(terminal["model_route_attestation"], "matched")
        self.assertEqual(resumed.mechanical_governor.by_role["mechanical_subworker"], 10)
        self.assertTrue(Path(terminal["runner_directory"]).is_dir() if terminal["runner_directory"] else True)

    async def test_recovery_reattaches_live_lease_without_duplicate_dispatch(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        parent_job_id, request_path, response_path = self._activate_parent(
            controller, role="explorer", suffix="live-lease",
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        packet = request["task_packet"]
        workspace = request_path.resolve().parents[2]
        packet_path = (
            workspace / "mechanical_subtasks/packets"
            / f"{packet['task_id']}.attempt-1.json"
        )
        atomic_write_json(packet_path, packet)
        output_root = workspace / "mechanical_subtasks/runs"
        receipt_path = SubprocessMechanicalRunner.receipt_path(packet_path, output_root)
        controller.store.append("MECHANICAL_SUBTASK_REQUESTED", {
            "parent_job_id": parent_job_id,
            "parent_task_id": "parent-live-lease",
            "parent_role": "explorer",
            "subtask_id": packet["task_id"],
            "task_kind": packet["task_kind"],
            "request_path": str(request_path),
            "request_sha256": request["request_sha256"],
            "packet_sha256": stable_hash(packet),
            "task_packet": packet,
            "valid": True,
        })
        controller.store.append("MECHANICAL_SUBTASK_STARTED", {
            "mechanical_job_id": "old-live-lease-a1",
            "parent_job_id": parent_job_id,
            "parent_task_id": "parent-live-lease",
            "parent_role": "explorer",
            "subtask_id": packet["task_id"],
            "attempt": 1,
            "estimated_token_reservation": 60_000,
            "packet_path": str(packet_path),
            "output_root": str(output_root),
            "receipt_path": str(receipt_path),
            "recovery_deadline_epoch": time.time() + 60,
            "started_at": "2026-08-18T00:00:00Z",
        })
        controller.active.clear()
        runner = LeaseRecoveryRunner()
        resumed = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            run_id=controller.run_id,
            mock=True,
            resume=True,
            mechanical_runner=runner,
        )
        resumed.recover()
        self.assertEqual(len(resumed.active_mechanical), 1)
        self.assertEqual(resumed.pending_mechanical, [])
        self.assertEqual(runner.calls, [])
        await self._drain_mechanical(resumed)
        self.assertEqual(len(runner.recover_calls), 1)
        self.assertEqual(runner.calls, [])
        self.assertTrue(response_path.is_file())
        self.assertEqual(
            json.loads(response_path.read_text(encoding="utf-8"))["status"],
            "COMPLETED",
        )
        events = resumed.store.replay()
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_STARTED"), 1)
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_LEASE_REATTACHED"), 1)
        self.assertEqual(kinds.count("MECHANICAL_SUBTASK_ATTEMPT_FINISHED"), 1)
        self.assertEqual(resumed.mechanical_governor.by_role["mechanical_subworker"], 7)
        self.assertEqual(resumed.recent_changes[-1]["kind"], "MECHANICAL_SUBTASK_COMPLETED")
        self.assertTrue(resumed.recent_changes[-1]["mechanical_evidence_only"])

    async def test_subprocess_recovery_reads_matching_completed_receipt(self) -> None:
        packet_path = self.root / "lease-packet.json"
        atomic_write_json(packet_path, valid_packet(task_id="lease-read"))
        output_root = self.root / "lease-output/runs"
        run_dir = output_root / "completed-run"
        artifact = run_dir / "artifacts/result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}\n", encoding="utf-8", newline="\n")
        atomic_write_json(run_dir / "result.json", {
            "status": "COMPLETED",
            "artifacts": ["artifacts/result.json"],
        })
        atomic_write_json(run_dir / "runner.json", {
            "selected_model": PRIMARY_MECHANICAL_ROUTE["model"],
            "selected_reasoning_effort": PRIMARY_MECHANICAL_ROUTE["reasoning_effort"],
            "selected_service_tier": None,
            "worker_started": True,
            "route_attempts": [{
                "model": PRIMARY_MECHANICAL_ROUTE["model"],
                "worker_usage": {"total_tokens": 5},
            }],
            "fallback": None,
            "failure": None,
        })
        runner = SubprocessMechanicalRunner(self.root)
        receipt_path = runner.receipt_path(packet_path, output_root)
        now = "2026-08-18T00:00:00+00:00"
        atomic_write_json(receipt_path, {
            "schema_version": 1,
            "status": "exited",
            "pid": 12345,
            "packet_path": str(packet_path.resolve()),
            "packet_sha256": runner._digest(packet_path),
            "output_root": str(output_root.resolve()),
            "run_directory": str(run_dir.resolve()),
            "started_at": now,
            "heartbeat_at": now,
            "timeout_seconds": 30,
            "finished_at": now,
        })
        execution = await runner.recover(
            packet_path=packet_path,
            output_root=output_root,
            receipt_path=receipt_path,
            timeout_seconds=1,
        )
        self.assertEqual(execution.status, "COMPLETED")
        self.assertEqual(execution.model, PRIMARY_MECHANICAL_ROUTE["model"])
        self.assertEqual(execution.reasoning_effort, "high")
        self.assertEqual(execution.token_usage.total_tokens, 5)

    async def test_subprocess_recovery_retries_only_after_safely_dead_pid(self) -> None:
        packet_path = self.root / "dead-lease-packet.json"
        atomic_write_json(packet_path, valid_packet(task_id="dead-lease"))
        output_root = self.root / "dead-lease-output/runs"
        runner = SubprocessMechanicalRunner(self.root)
        receipt_path = runner.receipt_path(packet_path, output_root)
        atomic_write_json(receipt_path, {
            "schema_version": 1,
            "status": "running",
            "pid": 54321,
            "packet_path": str(packet_path.resolve()),
            "packet_sha256": runner._digest(packet_path),
            "output_root": str(output_root.resolve()),
            "run_directory": None,
            "started_at": "2020-01-01T00:00:00+00:00",
            "heartbeat_at": "2020-01-01T00:00:00+00:00",
            "timeout_seconds": 30,
            "finished_at": None,
        })
        with patch.object(runner, "_pid_is_alive", return_value=False):
            execution = await runner.recover(
                packet_path=packet_path,
                output_root=output_root,
                receipt_path=receipt_path,
                timeout_seconds=30,
            )
        self.assertEqual(execution.failure_kind, "mechanical_crash_unknown")
        self.assertTrue(execution.retryable)

    async def test_subprocess_recovery_does_not_duplicate_stale_live_or_missing_lease(self) -> None:
        packet_path = self.root / "uncertain-lease-packet.json"
        atomic_write_json(packet_path, valid_packet(task_id="uncertain-lease"))
        output_root = self.root / "uncertain-lease-output/runs"
        runner = SubprocessMechanicalRunner(self.root)
        receipt_path = runner.receipt_path(packet_path, output_root)
        atomic_write_json(receipt_path, {
            "schema_version": 1,
            "status": "running",
            "pid": 54322,
            "packet_path": str(packet_path.resolve()),
            "packet_sha256": runner._digest(packet_path),
            "output_root": str(output_root.resolve()),
            "run_directory": None,
            "started_at": "2020-01-01T00:00:00+00:00",
            "heartbeat_at": "2020-01-01T00:00:00+00:00",
            "timeout_seconds": 30,
            "finished_at": None,
        })
        with (
            patch.object(runner, "_pid_is_alive", return_value=True),
            patch.object(
                runner,
                "_terminate_recovered_process_tree",
                side_effect=AssertionError("stale lease PID must not be terminated"),
            ),
        ):
            stale = await runner.recover(
                packet_path=packet_path,
                output_root=output_root,
                receipt_path=receipt_path,
                timeout_seconds=1,
            )
        self.assertEqual(stale.failure_kind, "mechanical_lease_uncertain")
        self.assertFalse(stale.retryable)

        receipt_path.unlink()
        missing = await runner.recover(
            packet_path=packet_path,
            output_root=output_root,
            receipt_path=receipt_path,
            timeout_seconds=1,
        )
        self.assertEqual(missing.failure_kind, "mechanical_lease_uncertain")
        self.assertFalse(missing.retryable)

    async def test_top_level_direct_codex_or_recursive_spawn_attempt_fails_closed(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        await controller._trace_notification({
            "method": "item/started",
            "params": {
                "threadId": "thread-x",
                "turnId": "turn-x",
                "item": {"type": "collabToolCall", "command": "codex exec ..."},
            },
        })
        self.assertTrue(controller._internal_failure)
        self.assertIn("outside the controller mechanical broker", controller.stop_for_review or "")
        self.assertEqual(
            [item["kind"] for item in controller.store.replay()].count(
                "UNAUTHORIZED_DELEGATION_ATTEMPT"
            ),
            1,
        )

        second = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        await second._trace_notification({
            "method": "item/started",
            "params": {
                "threadId": "thread-y",
                "turnId": "turn-y",
                "item": {
                    "type": "commandExecution",
                    "command": "codex exec --model anything -",
                },
            },
        })
        self.assertTrue(second._internal_failure)
        self.assertEqual(
            [item["kind"] for item in second.store.replay()].count(
                "UNAUTHORIZED_DELEGATION_ATTEMPT"
            ),
            1,
        )

        for allowed_command in (
            'python "helpers/emit_event.py" '
            '--project "neutral-project" --file candidate_event.json',
            'python "runtime/runs/r/jobs/j/delegate_mechanical_task.py" task_packet.json',
            'powershell.exe -Command \'Get-Process python,codex '
            '-ErrorAction SilentlyContinue | Format-Table -AutoSize\'',
            'powershell.exe -Command \'Get-Process | Where-Object { '
            '$_.ProcessName -match "python|codex" }\'',
            'rg -n "codex|spawn_agent|create_thread" src tests',
            'Get-ChildItem mechanical_broker; Get-Process codex',
        ):
            allowed = AutonomousController(
                self.config,
                backend=MockCodexBackend(),
                mock=True,
                mechanical_runner=SequenceMechanicalRunner([]),
            )
            await allowed._trace_notification({
                "method": "item/started",
                "params": {
                    "threadId": "thread-allowed",
                    "turnId": "turn-allowed",
                    "item": {
                        "type": "commandExecution",
                        "command": allowed_command,
                    },
                },
            })
            self.assertFalse(allowed._internal_failure, allowed_command)
            self.assertNotIn(
                "UNAUTHORIZED_DELEGATION_ATTEMPT",
                [item["kind"] for item in allowed.store.replay()],
            )

        bypass = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        await bypass._trace_notification({
            "method": "item/started",
            "params": {
                "threadId": "thread-bypass",
                "turnId": "turn-bypass",
                "item": {
                    "type": "commandExecution",
                    "command": "npx @openai/codex exec --model anything -",
                },
            },
        })
        self.assertTrue(bypass._internal_failure)

        for forbidden_command in (
            'powershell.exe -Command \'codex exec --model anything -\'',
            'powershell.exe -Command \'Get-Date; & "C:\\\\tools\\\\codex.exe" exec\'',
            'cmd.exe /d /c "codex exec --model anything -"',
            "bash -lc 'codex exec --model anything -'",
            'Start-Process -FilePath codex -ArgumentList "exec"',
            'python -m codex exec --model anything -',
        ):
            rejected = AutonomousController(
                self.config,
                backend=MockCodexBackend(),
                mock=True,
                mechanical_runner=SequenceMechanicalRunner([]),
            )
            await rejected._trace_notification({
                "method": "item/started",
                "params": {
                    "threadId": "thread-rejected",
                    "turnId": "turn-rejected",
                    "item": {
                        "type": "commandExecution",
                        "command": forbidden_command,
                    },
                },
            })
            self.assertTrue(rejected._internal_failure, forbidden_command)

    async def test_broker_client_tamper_fails_closed_before_request_acceptance(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        parent_job_id, _request_path, response_path = self._activate_parent(
            controller, role="prover", suffix="broker-tamper",
        )
        workspace = Path(controller.active[parent_job_id].workspace or "")
        (workspace / "delegate_mechanical_task.py").write_text(
            "raise SystemExit('tampered')\n", encoding="utf-8", newline="\n",
        )
        await controller._poll_mechanical_requests()
        self.assertTrue(controller._internal_failure)
        self.assertIn("mechanical broker integrity failure", controller.scheduler_stop_reason or "")
        self.assertFalse(response_path.exists())
        failures = [
            event for event in controller.store.replay()
            if event["kind"] == "MECHANICAL_BROKER_INTEGRITY_FAILURE"
        ]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["payload"]["parent_job_id"], parent_job_id)
        status = build_status(controller.run_dir, controller.store.replay())
        self.assertIn(
            "MECHANICAL_BROKER_INTEGRITY_FAILURE",
            {problem["kind"] for problem in status["problems"]},
        )

    async def test_duplicate_task_job_instances_keep_broker_configs_isolated(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        controller.lifecycle.transition(
            LifecyclePhase.RUNNING, reason="unit test duplicate task isolation",
        )
        task_id = "stable-duplicate-task"
        workspaces: list[Path] = []
        sealed_config_hashes: list[str] = []
        for suffix in ("first", "second"):
            job_id = f"job-{suffix}"
            workspace, _writable, metadata = (
                controller.workspace.create_job_workspace(
                    task_id, job_id=job_id,
                )
            )
            future = asyncio.create_task(asyncio.Event().wait())
            self.parent_futures.append(future)
            controller.active[job_id] = ActiveJob(
                job_id,
                parent_task(task_id, "prover"),
                future,
                time.monotonic(),
                300,
                "research",
                str(workspace),
                "2026-08-20T00:00:00Z",
                metadata,
            )
            controller.workspace.install_mechanical_broker_client(
                workspace,
                parent_job_id=job_id,
                parent_task_id=task_id,
                parent_role="prover",
                parent_timeout_seconds=300,
                enabled=True,
                broker_client_source=PACKAGE_SOURCE / "delegate_mechanical_task.py",
                broker_client_sha256=file_digest(
                    PACKAGE_SOURCE / "delegate_mechanical_task.py"
                ),
            )
            active = controller.active[job_id]
            active.broker_client_sha256 = file_digest(
                workspace / "delegate_mechanical_task.py"
            )
            active.broker_config_sha256 = file_digest(
                controller.workspace.mechanical_broker_config_path(workspace)
            )
            workspaces.append(workspace)
            sealed_config_hashes.append(active.broker_config_sha256)

        self.assertNotEqual(workspaces[0], workspaces[1])
        self.assertNotEqual(
            controller.workspace.mechanical_broker_config_path(workspaces[0]),
            controller.workspace.mechanical_broker_config_path(workspaces[1]),
        )
        self.assertEqual(
            file_digest(controller.workspace.mechanical_broker_config_path(workspaces[0])),
            sealed_config_hashes[0],
        )
        self.assertEqual(
            file_digest(controller.workspace.mechanical_broker_config_path(workspaces[1])),
            sealed_config_hashes[1],
        )

        await controller._poll_mechanical_requests()

        self.assertFalse(controller._internal_failure)
        self.assertNotIn(
            "MECHANICAL_BROKER_INTEGRITY_FAILURE",
            [item["kind"] for item in controller.store.replay()],
        )

    async def test_start_job_rejects_an_already_active_task_id(self) -> None:
        controller = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        existing_job_id, _request_path, _response_path = self._activate_parent(
            controller,
            role="prover",
            suffix="duplicate-start-existing",
            parent_task_id="stable-active-task",
        )
        active = controller.active[existing_job_id]
        original_config_hash = active.broker_config_sha256
        new_job_id = "job-duplicate-start-new"
        workspace, writable, _metadata = controller.workspace.create_job_workspace(
            "stable-active-task", job_id=new_job_id,
        )

        with self.assertRaisesRegex(RuntimeError, "task_id .* already active"):
            controller._start_job(
                parent_task("stable-active-task", "prover"),
                "bounded prompt",
                controller._schema("worker_result.schema.json"),
                workspace,
                writable,
                "research",
                estimated_tokens=1,
                job_id=new_job_id,
            )

        self.assertEqual(
            file_digest(controller.workspace.mechanical_broker_config_path(
                Path(active.workspace or "")
            )),
            original_config_hash,
        )


class MechanicalConfigurationTests(TempProjectMixin, unittest.TestCase):
    def test_top_level_roles_keep_their_strong_model_routes(self) -> None:
        for role in sorted(MECHANICAL_PARENT_ROLES):
            route = self.config.raw["models"][role]
            self.assertEqual(route["model"], "gpt-5.6-sol")
            self.assertIsNone(route["service_tier"])

    def test_enabled_policy_accepts_unbounded_cap_after_v7_migration(self) -> None:
        raw = json.loads(self.config.config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 7
        raw["scheduler"].pop("max_mechanical_subworkers")
        raw["policy"]["one_shot_compute_worker"]["enabled"] = True
        bad = self.project / "autonomous/missing-mechanical-cap.json"
        bad.write_text(json.dumps(raw), encoding="utf-8")
        migrated = load_config(self.project, bad)
        self.assertIsNone(migrated.max_mechanical_subworkers)
        self.assertEqual(
            migrated.migrations_applied, ("7->8", "8->9", "9->10", "10->11"),
        )

    def test_subprocess_runner_executes_run_local_pinned_sources(self) -> None:
        manifest_path = self.project / "autonomous/runs/pin-test/policy/MANIFEST.json"
        manifest, _status = pin_policy_manifest(self.config, manifest_path)
        runner = SubprocessMechanicalRunner(self.root)
        runner.configure_pinned_policy(
            manifest["one_shot_compute_worker"], manifest_path,
        )
        policy_root = manifest_path.parent.resolve()
        self.assertTrue(runner.script.is_relative_to(policy_root))
        self.assertTrue(runner.task_schema.is_relative_to(policy_root))
        self.assertTrue(runner.result_schema.is_relative_to(policy_root))
        self.assertTrue(runner.schema_validator.is_relative_to(policy_root))
        self.assertTrue(runner.contract_definitions.is_relative_to(policy_root))
        self.assertEqual(runner._digest(runner.script), runner.expected_hashes[runner.script])
        helper_entry = manifest["one_shot_compute_worker"]["broker_client"]
        helper = (policy_root / helper_entry["snapshot_path"]).resolve()
        self.assertTrue(helper.is_relative_to(policy_root))
        self.assertEqual(file_digest(helper), helper_entry["sha256"])

        spec = importlib.util.spec_from_file_location("pinned_runner_loader_test", runner.script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader if spec else None)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        validate_value, validate_compatibility = module._load_pinned_schema_validator(
            runner.schema_validator, runner.contract_definitions,
        )
        strict_empty = {
            "type": "object", "properties": {}, "required": [],
            "additionalProperties": False,
        }
        validate_compatibility(strict_empty, schema_path="pinned-test")
        validate_value({}, strict_empty)

    def test_broker_model_cache_is_attempt_local_and_seeded_without_global_write(self) -> None:
        runner = SubprocessMechanicalRunner(self.root)
        runner.remember_unavailable(
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort="high",
            service_tier=None,
            error="structured access denied",
            run_directory="prior-parent-job",
        )
        packet_path = self.project / "autonomous/jobs/p/mechanical_subtasks/packets/x.attempt-1.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(packet_path, valid_packet(task_id="x"))
        output_root = self.project / "autonomous/jobs/p/mechanical_subtasks/runs"
        status_path = runner.model_status_path(packet_path, output_root)
        runner._write_attempt_status_seed(status_path)
        self.assertTrue(status_path.is_relative_to(packet_path.parents[2]))
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(len(status["unavailable"]), 1)
        self.assertEqual(
            (
                status["unavailable"][0]["model"],
                status["unavailable"][0]["reasoning_effort"],
                status["unavailable"][0]["service_tier"],
            ),
            (PRIMARY_MECHANICAL_ROUTE["model"], "high", None),
        )
        self.assertFalse((self.root / ".tooling/math-worker-model-status.json").exists())
        runner.persist_unavailable(
            model=PRIMARY_MECHANICAL_ROUTE["model"],
            reasoning_effort="high",
            service_tier=None,
            error="structured permanent access denial",
            run_directory="parent-job-event",
        )
        circuit_breaker = self.root / ".tooling/math-worker-model-status.json"
        self.assertTrue(circuit_breaker.is_file())
        restarted = SubprocessMechanicalRunner(self.root)
        self.assertIn(
            (PRIMARY_MECHANICAL_ROUTE["model"], "high", None),
            restarted._unavailable_records,
        )

    def test_mechanical_pool_is_independent_from_top_level_caps(self) -> None:
        raw = json.loads(self.config.config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 7
        raw["scheduler"]["max_mechanical_subworkers"] = 2
        raw["policy"]["one_shot_compute_worker"]["enabled"] = True
        path = self.project / "autonomous/explicit-mechanical-cap.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        config = load_config(self.project, path)
        controller = AutonomousController(
            config,
            backend=MockCodexBackend(),
            mock=True,
            mechanical_runner=SequenceMechanicalRunner([]),
        )
        self.assertEqual(
            (controller.max_director, controller.max_research_workers, controller.max_audit),
            (1, 8, 8),
        )
        self.assertEqual(controller.max_mechanical_subworkers, 2)


if __name__ == "__main__":
    unittest.main()
