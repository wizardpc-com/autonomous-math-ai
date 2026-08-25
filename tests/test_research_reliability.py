from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.app_server import (
    AppServerClient, AppServerError, AppServerRequestError, AppServerRequestTimeout,
    AppServerTransportClosed, AppServerTurnTimeout,
    AppServerTurnTransportLost, TurnOwnershipRegistry,
    _configured_mcp_server_names, app_server_command, app_server_environment,
)
from autonomous_math_research.backend import (
    AppServerBackend, TurnDirective, _classify_failure,
)
from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import ActiveJob, AutonomousController
from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import (
    CandidateEvent,
    Claim,
    EvidenceLevel,
    ExecutionStatus,
    JobOutcome,
    MathStatus,
    ResearchTask,
    TokenUsage,
    TrustStatus,
)
from autonomous_math_research.reasoning_health import ReasoningHealthMonitor
from autonomous_math_research.research_job import ResearchTurnPolicy
from autonomous_math_research.stagnation import StagnationTracker


TEST_RUNTIME = Path(__file__).resolve().parent / "_runtime"


def worker_result(
    result_type: str,
    *,
    status: str = "INCOMPLETE",
    finding: str = "bounded partial result",
) -> dict[str, object]:
    return {
        "result_type": result_type,
        "main_finding": finding,
        "status": status,
        "artifact_paths": [],
        "next_suggested_question": "continue the next open obligation",
        "evidence_level": "E0_SPECULATIVE",
    }


def research_task() -> ResearchTask:
    return ResearchTask(
        task_id="proof-task",
        role="prover",
        target_claim="C_ROOT",
        exact_objective="Resolve the stated claim or preserve the exact remaining gap.",
        why_now="reliability regression",
        dependencies=[],
        expected_information_gain="HIGH",
        mathematical_impact="HIGH",
        estimated_cost_tier="LOW",
        required_files=[],
        stop_conditions=["submit auditable evidence or report a verified blocker"],
    )


def open_claim() -> Claim:
    return Claim(
        claim_id="C_ROOT",
        statement="Every admissible object has property P.",
        assumptions=["the object is admissible"],
        math_status=MathStatus.OPEN,
        trust_status=TrustStatus.CANONICAL_TRUSTED,
        dependencies=[],
        downstream_dependents=[],
        evidence_paths=[],
        known_counterexamples=[],
        current_gaps=["prove the universal step"],
        active_tasks=[],
        last_meaningful_progress=None,
        priority={"score": 1.0},
    )


def event(event_type: str, *, evidence: str = EvidenceLevel.E0_SPECULATIVE) -> CandidateEvent:
    return CandidateEvent.from_dict({
        "event_id": f"event-{event_type.lower()}",
        "producer_thread_id": "thread-proof",
        "producer_task_id": "proof-task",
        "claim_id": "C_ROOT",
        "type": event_type,
        "impact": "CRITICAL",
        "concise_summary": "neutral candidate",
        "exact_statement": "Every admissible object has property P.",
        "artifact_paths": [],
        "reproduction_commands": [],
        "dependency_impact": [],
        "assumptions": ["the object is admissible"],
        "dependencies": [],
        "proposed_evidence_level": evidence,
    })


class SequenceAppServerClient:
    def __init__(self, results: list[tuple[dict[str, object], int]]):
        self.results = list(results)
        self.start_thread_calls = 0
        self.start_thread_kwargs: list[dict[str, object]] = []
        self.goal_calls = 0
        self.turn_calls: list[dict[str, object]] = []

    async def start_thread(self, **kwargs):  # type: ignore[no-untyped-def]
        self.start_thread_calls += 1
        self.start_thread_kwargs.append(dict(kwargs))
        return {
            "thread": {"id": "thread-proof"},
            "model": kwargs["model"],
            "serviceTier": kwargs.get("service_tier"),
        }

    async def set_goal(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        self.goal_calls += 1
        return {}

    async def start_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        self.turn_calls.append(dict(kwargs))
        index = len(self.turn_calls)
        callback = kwargs.get("on_started")
        if callback:
            callback(f"turn-{index}")
        result, reasoning_tokens = self.results.pop(0)
        return (
            {
                "threadId": kwargs["thread_id"],
                "turn": {
                    "id": f"turn-{index}",
                    "status": "completed",
                    "model": kwargs["model"],
                },
            },
            json.dumps(result),
            TokenUsage(
                input_tokens=100,
                output_tokens=100,
                reasoning_output_tokens=reasoning_tokens,
                total_tokens=200 + reasoning_tokens,
            ),
            "observed",
        )


class ThreadLifecycleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = TEST_RUNTIME / f"amr-reliability-{uuid4().hex}"
        self.root.mkdir()
        self.project = initialize_project(self.root / "neutral-project")
        self.backend = AppServerBackend(load_config(self.project))

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    async def test_active_goal_is_not_armed_before_controller_owned_turn(self) -> None:
        client = SequenceAppServerClient([(worker_result("BLOCKED", status="BLOCKED"), 900)])
        self.backend.client = client  # type: ignore[assignment]

        async def stop(_outcome: JobOutcome, _turn_index: int) -> TurnDirective:
            return TurnDirective.stop("verified blocker")

        outcome = await self.backend.run_job(
            job_id="job-goal-race",
            task=research_task(),
            prompt="start",
            output_schema=json.loads(
                (Path(__file__).resolve().parents[1]
                 / "src/autonomous_math_research/resources/schemas/worker_result.schema.json")
                .read_text(encoding="utf-8")
            ),
            workspace=self.project,
            writable_roots=[self.project],
            timeout=1,
            token_budget=10_000,
            candidate_sink=lambda _event: None,  # type: ignore[arg-type]
            turn_controller=stop,
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(client.goal_calls, 0)
        self.assertEqual(len(client.turn_calls), 1)

    async def test_role_receives_a_bounded_local_tool_contract(self) -> None:
        client = SequenceAppServerClient([
            (worker_result("BLOCKED", status="BLOCKED"), 900),
        ])
        self.backend.client = client  # type: ignore[assignment]

        async def stop(_outcome: JobOutcome, _turn_index: int) -> TurnDirective:
            return TurnDirective.stop("verified blocker")

        await self.backend.run_job(
            job_id="job-tool-contract",
            task=research_task(),
            prompt="start",
            output_schema=json.loads(
                (Path(__file__).resolve().parents[1]
                 / "src/autonomous_math_research/resources/schemas/worker_result.schema.json")
                .read_text(encoding="utf-8")
            ),
            workspace=self.project,
            writable_roots=[self.project],
            timeout=1,
            token_budget=10_000,
            candidate_sink=lambda _event: None,  # type: ignore[arg-type]
            turn_controller=stop,
        )

        developer = str(client.start_thread_kwargs[0]["developer_instructions"])
        self.assertIn("Never inspect global Codex memories", developer)
        self.assertIn("AMR Python runtime is available as python", developer)
        self.assertIn("Do not assume optional commands such as rg", developer)
        if os.name == "nt":
            self.assertIn("Get-Content -Raw -Encoding UTF8", developer)
            self.assertIn("cannot be piped directly", developer)

    async def test_same_thread_multi_turn_is_explicitly_controller_owned(self) -> None:
        client = SequenceAppServerClient([
            (worker_result("NO_PROGRESS"), 900),
            (worker_result("BLOCKED", status="BLOCKED"), 1000),
        ])
        self.backend.client = client  # type: ignore[assignment]

        async def decide(_outcome: JobOutcome, turn_index: int) -> TurnDirective:
            if turn_index == 1:
                return TurnDirective.continue_with("continue the same obligation")
            return TurnDirective.stop("verified blocker")

        outcome = await self.backend.run_job(
            job_id="job-multiturn",
            task=research_task(),
            prompt="start",
            output_schema=json.loads(
                (Path(__file__).resolve().parents[1]
                 / "src/autonomous_math_research/resources/schemas/worker_result.schema.json")
                .read_text(encoding="utf-8")
            ),
            workspace=self.project,
            writable_roots=[self.project],
            timeout=1,
            token_budget=None,
            candidate_sink=lambda _event: None,  # type: ignore[arg-type]
            turn_controller=decide,
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(client.start_thread_calls, 1)
        self.assertEqual(len(client.turn_calls), 2)
        self.assertEqual({call["thread_id"] for call in client.turn_calls}, {"thread-proof"})
        self.assertEqual([item["turn_index"] for item in outcome.turn_history], [1, 2])
        self.assertEqual(
            set(outcome.turn_history[-1]["token_usage"]),
            set(TokenUsage().to_dict()),
        )

    async def test_controller_repairs_first_blocker_before_scheduling_terminal(self) -> None:
        controller = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="blocker-repair", campaign_id="blocker-repair",
        )
        controller._pin_run_inputs(0.01, True)
        task = research_task()
        canonical_before = controller._canonical_progress_marker(task.target_claim)

        first = JobOutcome(
            job_id="job-blocker", task_id=task.task_id, role=task.role,
            claim_id=task.target_claim, status="completed",
            result=worker_result("BLOCKED", status="BLOCKED"),
            thread_id="thread-blocker", turn_id="turn-1",
        )
        first_directive = await controller._control_research_turn(
            job_id=first.job_id, task=task, canonical_before=canonical_before,
            outcome=first, turn_index=1,
        )
        self.assertTrue(first_directive.continue_same_thread)

        second = JobOutcome(
            job_id="job-blocker", task_id=task.task_id, role=task.role,
            claim_id=task.target_claim, status="completed",
            result=worker_result("BLOCKED", status="BLOCKED"),
            thread_id="thread-blocker", turn_id="turn-2",
        )
        second_directive = await controller._control_research_turn(
            job_id=second.job_id, task=task, canonical_before=canonical_before,
            outcome=second, turn_index=2,
        )
        self.assertFalse(second_directive.continue_same_thread)
        self.assertEqual(
            second_directive.reason, "controller-verified execution blocker",
        )
        turn_events = [
            item["payload"] for item in controller.store.replay()
            if item["kind"] == "RESEARCH_TURN_COMPLETED"
        ]
        self.assertFalse(turn_events[0]["blocker_controller_verified"])
        self.assertTrue(turn_events[1]["blocker_controller_verified"])
        self.assertEqual(
            turn_events[1]["blocker_verification_scope"],
            "execution scheduling only; no mathematical or trust effect",
        )

    async def test_health_escalation_changes_only_the_next_owned_turn(self) -> None:
        client = SequenceAppServerClient([
            (worker_result("PROOF", finding="short plausible sketch"), 516),
            (worker_result("BLOCKED", status="BLOCKED"), 900),
        ])
        self.backend.client = client  # type: ignore[assignment]
        monitor = ReasoningHealthMonitor(
            short_reasoning_tokens=600,
            repeated_token_tolerance=2,
            retry_limit=1,
        )

        async def decide(outcome: JobOutcome, turn_index: int) -> TurnDirective:
            signal = monitor.observe(
                job_id=outcome.job_id,
                turn_index=turn_index,
                effort=str(outcome.reasoning_effort),
                usage=outcome.token_usage,
                telemetry=outcome.token_telemetry,
                max_effort_supported=True,
            )
            if turn_index == 1:
                return TurnDirective.continue_with(
                    "retry the same obligation",
                    reason=signal.diagnostic,
                    effort_override=signal.recommended_effort,
                )
            return TurnDirective.stop("bounded blocker")

        await self.backend.run_job(
            job_id="job-health-escalation",
            task=research_task(),
            prompt="start",
            output_schema=json.loads(
                (Path(__file__).resolve().parents[1]
                 / "src/autonomous_math_research/resources/schemas/worker_result.schema.json")
                .read_text(encoding="utf-8")
            ),
            workspace=self.project,
            writable_roots=[self.project],
            timeout=1,
            token_budget=None,
            candidate_sink=lambda _event: None,  # type: ignore[arg-type]
            turn_controller=decide,
        )
        self.assertEqual(
            [call["effort"] for call in client.turn_calls],
            ["xhigh", "max"],
        )

    async def test_backend_enforces_budget_even_if_callback_requests_more(self) -> None:
        client = SequenceAppServerClient([(worker_result("NO_PROGRESS"), 900)])
        self.backend.client = client  # type: ignore[assignment]

        async def keep_going(_outcome: JobOutcome, _turn_index: int) -> TurnDirective:
            return TurnDirective.continue_with("continue")

        outcome = await self.backend.run_job(
            job_id="job-budget-stop",
            task=research_task(),
            prompt="start",
            output_schema=json.loads(
                (Path(__file__).resolve().parents[1]
                 / "src/autonomous_math_research/resources/schemas/worker_result.schema.json")
                .read_text(encoding="utf-8")
            ),
            workspace=self.project,
            writable_roots=[self.project],
            timeout=1,
            token_budget=1_000,
            candidate_sink=lambda _event: None,  # type: ignore[arg-type]
            turn_controller=keep_going,
        )
        self.assertEqual(len(client.turn_calls), 1)
        self.assertEqual(outcome.logical_stop_reason, "controller token budget reached")

    async def test_shutdown_reaps_cancelled_job_before_backend_close(self) -> None:
        order: list[str] = []

        class ShutdownOrderBackend(MockCodexBackend):
            async def cancel(self, job_id: str) -> bool:
                order.append(f"remote-cancel:{job_id}")
                return True

            async def close(self) -> None:
                order.append("backend-close")

        backend = ShutdownOrderBackend()
        controller = AutonomousController(
            load_config(self.project), backend=backend, mock=True,
            run_id="shutdown-order", campaign_id="shutdown-order",
        )
        never = asyncio.Event()

        async def running_job() -> JobOutcome:
            try:
                await never.wait()
            finally:
                await asyncio.sleep(0)
                order.append("job-cleaned")

        future = asyncio.create_task(running_job())
        await asyncio.sleep(0)
        controller.active["job-1"] = ActiveJob(
            logical_job_id="job-1",
            task=research_task(),
            future=future,
            started_monotonic=0.0,
            timeout=60.0,
            kind="research",
        )

        await controller._cancel_active_jobs_before_backend_close("internal failure")
        await backend.close()

        self.assertEqual(
            order,
            ["remote-cancel:job-1", "job-cleaned", "backend-close"],
        )
        self.assertTrue(future.done())
        self.assertEqual(controller.active, {})


class TurnOwnershipTests(unittest.TestCase):
    def test_native_continuation_after_first_turn_is_unmanaged(self) -> None:
        registry = TurnOwnershipRegistry()
        registry.begin_controller_turn("thread-1")
        self.assertTrue(registry.observe_started("thread-1", "turn-1"))
        registry.bind_response("thread-1", "turn-1")
        self.assertTrue(registry.observe_completed("thread-1", "turn-1"))
        registry.finish_controller_turn("thread-1")

        self.assertFalse(registry.observe_started("thread-1", "native-turn-2"))
        self.assertEqual(
            registry.unmanaged_continuations,
            [{"thread_id": "thread-1", "turn_id": "native-turn-2"}],
        )

    def test_duplicate_started_notification_for_owned_turn_is_idempotent(self) -> None:
        registry = TurnOwnershipRegistry()
        registry.begin_controller_turn("thread-1")

        self.assertTrue(registry.observe_started("thread-1", "turn-1"))
        self.assertTrue(registry.observe_started("thread-1", "turn-1"))
        self.assertEqual(registry.unmanaged_continuations, [])

    def test_second_distinct_started_notification_is_unmanaged(self) -> None:
        registry = TurnOwnershipRegistry()
        registry.begin_controller_turn("thread-1")

        self.assertTrue(registry.observe_started("thread-1", "turn-1"))
        self.assertFalse(registry.observe_started("thread-1", "turn-2"))
        self.assertEqual(
            registry.unmanaged_continuations,
            [{"thread_id": "thread-1", "turn_id": "turn-2"}],
        )

    def test_unknown_completion_cannot_rebind_started_owned_turn(self) -> None:
        registry = TurnOwnershipRegistry()
        registry.begin_controller_turn("thread-1")
        self.assertTrue(registry.observe_started("thread-1", "turn-1"))

        self.assertFalse(registry.observe_completed("thread-1", "turn-2"))


class _CorrelatedTurnClient(AppServerClient):
    def __init__(self, completion_orders: list[str], *, mismatched_response_ids: bool = False):
        super().__init__(codex_executable="unused")
        self.completion_orders = list(completion_orders)
        self.mismatched_response_ids = mismatched_response_ids
        self.turn_number = 0
        self.turn_params: list[dict[str, object]] = []
        self.traced: list[dict[str, object]] = []
        self.notification_handler = self.traced.append

    def _emit_completed_turn(self, thread_id: str, turn_id: str, number: int) -> None:
        self._handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "inProgress"},
            },
        })
        self._handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "id": f"message-{number}",
                    "type": "agentMessage",
                    "text": json.dumps({"turn": number}),
                },
            },
        })
        self._handle_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "tokenUsage": {"total": {"totalTokens": number * 100}},
            },
        })
        self._handle_notification({
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed"},
            },
        })

    async def request(  # type: ignore[override]
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 60,
    ) -> object:
        del timeout
        if method != "turn/start":
            raise AssertionError(f"unexpected request: {method}")
        assert params is not None
        self.turn_params.append(dict(params))
        self.turn_number += 1
        number = self.turn_number
        thread_id = str(params["threadId"])
        turn_id = f"turn-{number}"
        response_turn_id = f"response-{number}" if self.mismatched_response_ids else turn_id
        order = self.completion_orders.pop(0)
        if order == "before_response":
            self._emit_completed_turn(thread_id, turn_id, number)
        elif order == "after_response":
            asyncio.get_running_loop().call_later(
                0.01, self._emit_completed_turn, thread_id, turn_id, number,
            )
        elif order == "late_previous_before_response":
            if number < 2:
                raise AssertionError("late previous completion requires a prior turn")
            previous_turn_id = f"turn-{number - 1}"
            self._handle_notification({
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": previous_turn_id, "status": "inProgress"},
                },
            })
            self._handle_notification({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": previous_turn_id,
                    "item": {
                        "id": "late-message",
                        "type": "agentMessage",
                        "text": '{"turn": 999}',
                    },
                },
            })
            self._handle_notification({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": previous_turn_id,
                    "tokenUsage": {"total": {"totalTokens": 99900}},
                },
            })
            self._handle_notification({
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": previous_turn_id, "status": "completed"},
                },
            })
            asyncio.get_running_loop().call_later(
                0.01, self._emit_completed_turn, thread_id, turn_id, number,
            )
        else:
            raise AssertionError(f"unknown completion order: {order}")
        return {"turn": {"id": response_turn_id, "status": "inProgress"}}


class _FailedStartClient(AppServerClient):
    def __init__(self):
        super().__init__(codex_executable="unused")
        self.interrupt_calls: list[tuple[str, str]] = []

    async def request(  # type: ignore[override]
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 60,
    ) -> object:
        del timeout
        if method != "turn/start":
            raise AssertionError(f"unexpected request: {method}")
        assert params is not None
        thread_id = str(params["threadId"])
        self._handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": thread_id,
                "turn": {"id": "turn-started-before-error", "status": "inProgress"},
            },
        })
        raise AppServerRequestError({"code": "request_failed", "message": "rejected"})

    async def interrupt(self, thread_id: str, turn_id: str) -> object:
        self.interrupt_calls.append((thread_id, turn_id))
        return {}


class _HangingTurnClient(AppServerClient):
    def __init__(self):
        super().__init__(codex_executable="unused")
        self.started = asyncio.Event()
        self.interrupt_calls: list[tuple[str, str]] = []

    async def request(  # type: ignore[override]
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 60,
    ) -> object:
        del timeout
        if method != "turn/start":
            raise AssertionError(f"unexpected request: {method}")
        assert params is not None
        thread_id = str(params["threadId"])
        self._handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": thread_id,
                "turn": {"id": "turn-hanging", "status": "inProgress"},
            },
        })
        self.started.set()
        return {"turn": {"id": "turn-hanging", "status": "inProgress"}}

    async def interrupt(self, thread_id: str, turn_id: str) -> object:
        self.interrupt_calls.append((thread_id, turn_id))
        return {}


class _DelegationContainmentClient(AppServerClient):
    def __init__(self):
        super().__init__(codex_executable="unused")
        self.traced: list[dict[str, object]] = []
        self.notification_handler = self.traced.append
        self.interrupt_calls: list[tuple[str, str]] = []

    @property
    def transport_available(self) -> bool:
        return True

    async def interrupt(self, thread_id: str, turn_id: str) -> object:
        self.interrupt_calls.append((thread_id, turn_id))
        return {}


class _TransportLostStartClient(AppServerClient):
    def __init__(self):
        super().__init__(codex_executable="unused")
        self._transport_alive = True
        self.interrupt_calls: list[tuple[str, str]] = []

    @property
    def transport_available(self) -> bool:
        return self._transport_alive

    async def request(  # type: ignore[override]
        self,
        method: str,
        params: dict[str, object] | None = None,
        timeout: float = 60,
    ) -> object:
        del timeout
        if method != "turn/start":
            raise AssertionError(f"unexpected request: {method}")
        assert params is not None
        thread_id = str(params["threadId"])
        self._handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": thread_id,
                "turn": {"id": "turn-lost-during-start", "status": "inProgress"},
            },
        })
        self._handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": "turn-lost-during-start",
                "item": {"type": "agentMessage", "text": '{"turn": 1}'},
            },
        })
        self._transport_alive = False
        raise AppServerTransportClosed("app-server stdout closed")

    async def interrupt(self, thread_id: str, turn_id: str) -> object:
        self.interrupt_calls.append((thread_id, turn_id))
        raise AppServerTransportClosed("app-server stdout closed")


class _BlockingInterruptClient(_HangingTurnClient):
    def __init__(self):
        super().__init__()
        self.interrupt_started = asyncio.Event()
        self.release_interrupt = asyncio.Event()

    async def interrupt(self, thread_id: str, turn_id: str) -> object:
        self.interrupt_calls.append((thread_id, turn_id))
        self.interrupt_started.set()
        await self.release_interrupt.wait()
        return {}


class _RequestTimeoutClient(AppServerClient):
    def __init__(self):
        super().__init__(codex_executable="unused")
        self._transport_generation = 1
        self._transport_alive = True

    @property
    def transport_available(self) -> bool:
        return self._transport_alive

    async def _send(self, message: dict[str, object]) -> None:
        del message


class AppServerLaunchIsolationTests(unittest.TestCase):
    def test_launch_disables_ambient_agent_features(self) -> None:
        project = Path.cwd().resolve()
        command = app_server_command(
            "codex",
            project_root=project,
            permission_profile="amr-role-test",
            mcp_server_names=("plain", "server_two"),
            model_shell_path=str(Path("C:/runtime")),
            runtime_read_roots=(Path("C:/runtime"),),
            blocked_executable=Path("C:/runtime/codex.exe"),
            blocked_read_paths=(Path("C:/codex-home/auth.json"),),
        )

        self.assertEqual(command[:3], ["codex", "app-server", "--strict-config"])
        self.assertIn(["-c", "project_doc_max_bytes=0"], [
            command[index:index + 2] for index in range(len(command) - 1)
        ])
        self.assertEqual(command[-1], "--stdio")
        for feature in (
            "apps", "browser_use", "computer_use", "goals", "hooks",
            "image_generation", "memories", "multi_agent", "plugins",
            "skill_search", "workspace_dependencies",
        ):
            self.assertIn(["--disable", feature], [
                command[index:index + 2] for index in range(len(command) - 1)
            ])
        overrides = [
            command[index + 1]
            for index, item in enumerate(command[:-1])
            if item == "-c"
        ]
        self.assertIn("allow_login_shell=false", overrides)
        self.assertIn("web_search=\"disabled\"", overrides)
        self.assertIn("tools.view_image=false", overrides)
        self.assertIn("shell_environment_policy.inherit=\"core\"", overrides)
        self.assertIn("shell_environment_policy.ignore_default_excludes=false", overrides)
        filesystem_override = next(
            item for item in overrides
            if item.startswith("permissions.amr-role-test.filesystem={")
        )
        self.assertIn("\":root\" = \"deny\"", filesystem_override)
        self.assertIn("\":minimal\" = \"read\"", filesystem_override)
        self.assertNotIn(
            f"{json.dumps(str(project))} = \"read\"",
            filesystem_override,
        )
        self.assertIn(
            f"{json.dumps(str(Path('C:/runtime').resolve()))} = \"read\"",
            filesystem_override,
        )
        self.assertIn(
            f"{json.dumps(str(Path('C:/runtime/codex.exe').resolve()))} = \"deny\"",
            filesystem_override,
        )
        self.assertIn(
            f"{json.dumps(str(Path('C:/codex-home/auth.json').resolve()))} = \"deny\"",
            filesystem_override,
        )
        self.assertIn(
            "\":workspace_roots\" = { \".\" = \"write\" }",
            filesystem_override,
        )
        self.assertIn(
            "mcp_servers.plain.enabled=false", overrides,
        )
        self.assertIn(
            "mcp_servers.server_two.enabled=false", overrides,
        )
        self.assertFalse(any(item == "--ignore-user-config" for item in command))

    def test_mcp_inventory_is_parsed_without_exposing_configuration(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([
                {"name": "zeta", "transport": {"url": "secret"}},
                {"name": "alpha", "auth_status": "oAuth"},
            ]),
            stderr="",
        )
        with patch(
            "autonomous_math_research.app_server.subprocess.run",
            return_value=completed,
        ) as run:
            names = _configured_mcp_server_names(
                "codex", project_root=Path.cwd(), environment={"PATH": "safe"},
            )

        self.assertEqual(names, ("alpha", "zeta"))
        self.assertEqual(run.call_args.kwargs["env"], {"PATH": "safe"})

    def test_mcp_inventory_failure_is_fail_closed(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not-json", stderr="",
        )
        with patch(
            "autonomous_math_research.app_server.subprocess.run",
            return_value=completed,
        ), self.assertRaisesRegex(AppServerError, "invalid JSON"):
            _configured_mcp_server_names(
                "codex", project_root=Path.cwd(), environment={},
            )

    def test_environment_exposes_runtime_python_without_forwarding_secrets(self) -> None:
        with patch.dict(os.environ, {
            "PATH": str(Path("C:/system-tools")),
            "CODEX_HOME": str(Path("C:/codex-home")),
            "UNIT_TEST_API_KEY": "must-not-leak",
        }, clear=True):
            environment = app_server_environment()

        entries = environment["PATH"].split(os.pathsep)
        self.assertEqual(
            os.path.normcase(entries[0]),
            os.path.normcase(str(Path(sys.executable).resolve().parent)),
        )
        self.assertEqual(environment["CODEX_HOME"], str(Path("C:/codex-home")))
        self.assertNotIn("UNIT_TEST_API_KEY", environment)

    def test_environment_removes_the_codex_entrypoint_from_role_path(self) -> None:
        codex = Path("C:/codex-bin/codex.exe")
        with patch.dict(os.environ, {
            "PATH": os.pathsep.join((str(codex.parent), str(Path("C:/tools")))),
        }, clear=True):
            environment = app_server_environment(blocked_executable=codex)

        entries = {
            os.path.normcase(str(Path(item)))
            for item in environment["PATH"].split(os.pathsep)
        }
        self.assertNotIn(os.path.normcase(str(codex.parent)), entries)

    def test_environment_preserves_colocated_runtime_for_exact_executable_deny(self) -> None:
        codex = Path(sys.executable).resolve().with_name("codex")
        with patch.dict(os.environ, {
            "PATH": str(codex.parent),
        }, clear=True):
            environment = app_server_environment(blocked_executable=codex)

        entries = {
            os.path.normcase(str(Path(item).resolve()))
            for item in environment["PATH"].split(os.pathsep)
        }
        self.assertIn(os.path.normcase(str(codex.parent.resolve())), entries)


class AppServerThreadPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_accepts_only_empty_disabled_mcp_inventory(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del timeout
            self.assertEqual(method, "mcpServerStatus/list")
            self.assertEqual(params["detail"], "full")
            return {
                "data": [{
                    "name": "configured-but-disabled",
                    "authStatus": "unsupported",
                    "tools": {},
                    "resources": [],
                    "resourceTemplates": [],
                    "serverInfo": None,
                }],
                "nextCursor": None,
            }

        client.request = request  # type: ignore[method-assign]
        await client._attest_no_mcp_servers()

    async def test_startup_rejects_exposed_mcp_tools(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del method, params, timeout
            return {
                "data": [{
                    "name": "ambient",
                    "authStatus": "unsupported",
                    "tools": {"unexpected": {"name": "unexpected"}},
                    "resources": [],
                    "resourceTemplates": [],
                    "serverInfo": None,
                }],
                "nextCursor": None,
            }

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "exposed an inherited MCP"):
            await client._attest_no_mcp_servers()

    async def test_startup_rejects_malformed_mcp_inventory(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del method, params, timeout
            return {"data": [{}], "nextCursor": None}

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "entry is invalid"):
            await client._attest_no_mcp_servers()

    async def test_startup_rejects_repeated_mcp_inventory_cursor(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )
        calls = 0

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            nonlocal calls
            del method, params, timeout
            calls += 1
            return {"data": [], "nextCursor": "repeated"}

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "cursor repeated"):
            await client._attest_no_mcp_servers()
        self.assertEqual(calls, 2)

    async def test_startup_rejects_repeated_permission_profile_cursor(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )
        calls = 0

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            nonlocal calls
            del method, params, timeout
            calls += 1
            return {"data": [], "nextCursor": "repeated"}

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "cursor repeated"):
            await client._attest_permission_profile_available()
        self.assertEqual(calls, 2)

    async def test_startup_rejects_disallowed_permission_profile(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del method, params, timeout
            return {
                "data": [{
                    "id": client.permission_profile,
                    "allowed": False,
                }],
                "nextCursor": None,
            }

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "is not allowed"):
            await client._attest_permission_profile_available()

    async def test_thread_uses_and_attests_controller_permission_profile(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )
        captured: dict[str, object] = {}

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del timeout
            self.assertEqual(method, "thread/start")
            captured.update(params)
            return {
                "thread": {"id": "thread-isolated"},
                "activePermissionProfile": {"id": client.permission_profile},
            }

        client.request = request  # type: ignore[method-assign]
        await client.start_thread(
            model="gpt-5.6-sol",
            cwd=Path.cwd(),
            writable_roots=[Path.cwd()],
        )

        self.assertEqual(captured["permissions"], client.permission_profile)
        self.assertEqual(
            captured["runtimeWorkspaceRoots"], [str(Path.cwd().resolve())],
        )
        self.assertIn("disabled", captured["multiAgentMode"]["custom"].lower())
        self.assertNotIn("sandbox", captured)

    async def test_thread_permission_profile_mismatch_fails_closed(self) -> None:
        client = AppServerClient(
            codex_executable="unused", project_root=Path.cwd(),
        )

        async def request(
            method: str, params: dict[str, object], timeout: float = 60,
        ) -> dict[str, object]:
            del method, params, timeout
            return {
                "thread": {"id": "thread-isolated"},
                "activePermissionProfile": {"id": ":workspace"},
            }

        client.request = request  # type: ignore[method-assign]
        with self.assertRaisesRegex(AppServerError, "did not attest"):
            await client.start_thread(
                model="gpt-5.6-sol",
                cwd=Path.cwd(),
                writable_roots=[Path.cwd()],
            )


class AppServerTurnCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def _start_turn(
        self, client: AppServerClient,
    ) -> tuple[dict[str, object], str, TokenUsage, str]:
        return await client.start_turn(
            thread_id="thread-1",
            prompt="continue",
            cwd=Path.cwd(),
            model="gpt-5.6-sol",
            effort="high",
            output_schema={
                "type": "object",
                "properties": {"turn": {"type": "integer"}},
                "required": ["turn"],
                "additionalProperties": False,
            },
            writable_roots=[Path.cwd()],
            timeout=1,
        )

    async def test_normal_completion_is_not_reused_by_next_turn(self) -> None:
        client = _CorrelatedTurnClient(["after_response", "after_response"])

        first = await self._start_turn(client)
        second = await self._start_turn(client)

        self.assertEqual(first[0]["turn"]["id"], "turn-1")
        self.assertEqual(first[1], '{"turn": 1}')
        self.assertEqual(second[0]["turn"]["id"], "turn-2")
        self.assertEqual(second[1], '{"turn": 2}')
        self.assertEqual(second[2].total_tokens, 200)
        self.assertNotIn("turn-1", client._messages)
        self.assertEqual(client.turn_ownership.unmanaged_continuations, [])
        self.assertEqual(client._completed_thread_turns, {})
        self.assertEqual(client._completed_turns, {})

    async def test_completion_before_response_clears_both_cache_indexes(self) -> None:
        client = _CorrelatedTurnClient(["before_response", "after_response"])

        first = await self._start_turn(client)
        second = await self._start_turn(client)

        self.assertEqual(first[0]["turn"]["id"], "turn-1")
        self.assertEqual(second[0]["turn"]["id"], "turn-2")
        self.assertEqual(second[1], '{"turn": 2}')
        self.assertEqual(second[2].total_tokens, 200)
        self.assertNotIn("turn-1", client._messages)
        self.assertEqual(client.turn_ownership.unmanaged_continuations, [])
        self.assertEqual(client._completed_thread_turns, {})
        self.assertEqual(client._completed_turns, {})

    async def test_turn_uses_controller_permission_profile_and_exact_roots(self) -> None:
        client = _CorrelatedTurnClient(["after_response"])

        await self._start_turn(client)

        params = client.turn_params[0]
        self.assertEqual(params["permissions"], client.permission_profile)
        self.assertEqual(
            params["runtimeWorkspaceRoots"], [str(Path.cwd().resolve())],
        )
        self.assertIn("disabled", params["multiAgentMode"]["custom"].lower())
        self.assertNotIn("sandboxPolicy", params)

    async def test_current_delegation_events_interrupt_parent_and_fail_closed(self) -> None:
        for item in (
            {
                "id": "collab-1",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "receiverThreadIds": ["child-collab"],
            },
            {
                "id": "activity-1",
                "type": "subAgentActivity",
                "kind": "started",
                "agentThreadId": "child-activity",
            },
        ):
            with self.subTest(item_type=item["type"]):
                client = _DelegationContainmentClient()
                client._handle_notification({
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": item,
                    },
                })
                await asyncio.sleep(0)

                self.assertEqual(client.interrupt_calls, [("thread-1", "turn-1")])
                self.assertEqual(len(client.traced), 1)
                self.assertEqual(
                    client.traced[0]["method"], "amr/unauthorizedDelegation",
                )
                self.assertEqual(
                    client.traced[0]["params"]["itemType"], item["type"],
                )

    async def test_mismatched_response_and_stream_ids_remain_correlated(self) -> None:
        client = _CorrelatedTurnClient(
            ["before_response", "after_response"],
            mismatched_response_ids=True,
        )

        first = await self._start_turn(client)
        second = await self._start_turn(client)

        self.assertEqual(first[0]["turn"]["id"], "turn-1")
        self.assertEqual(first[1], '{"turn": 1}')
        self.assertEqual(second[0]["turn"]["id"], "turn-2")
        self.assertEqual(second[1], '{"turn": 2}')
        self.assertEqual(client.turn_ownership.unmanaged_continuations, [])

    async def test_late_previous_notifications_cannot_bind_next_turn(self) -> None:
        client = _CorrelatedTurnClient(
            ["after_response", "late_previous_before_response"],
        )

        first = await self._start_turn(client)
        second = await self._start_turn(client)

        self.assertEqual(first[0]["turn"]["id"], "turn-1")
        self.assertEqual(second[0]["turn"]["id"], "turn-2")
        self.assertEqual(second[1], '{"turn": 2}')
        self.assertEqual(second[2].total_tokens, 200)
        self.assertNotIn("turn-1", client._messages)
        self.assertEqual(client.turn_ownership.unmanaged_continuations, [])

    async def test_started_turn_is_interrupted_when_start_request_fails(self) -> None:
        client = _FailedStartClient()

        with self.assertRaises(AppServerRequestError):
            await self._start_turn(client)

        self.assertEqual(
            client.interrupt_calls,
            [("thread-1", "turn-started-before-error")],
        )
        self.assertNotIn("thread-1", client.turn_ownership._open_threads)

    async def test_transport_loss_during_turn_start_is_not_misclassified_as_unmanaged(self) -> None:
        client = _TransportLostStartClient()

        with self.assertRaises(AppServerTurnTransportLost) as raised:
            await self._start_turn(client)

        self.assertEqual(raised.exception.turn_id, "turn-lost-during-start")
        self.assertEqual(raised.exception.raw_output, '{"turn": 1}')
        self.assertEqual(client.interrupt_calls, [])
        self.assertNotIn("thread-1", client.turn_ownership._open_threads)

    async def test_cancelled_wait_interrupts_remote_turn_and_closes_ownership(self) -> None:
        client = _HangingTurnClient()
        task = asyncio.create_task(self._start_turn(client))
        await client.started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.interrupt_calls, [("thread-1", "turn-hanging")])
        self.assertNotIn("thread-1", client.turn_ownership._open_threads)

    async def test_stdout_eof_during_cancel_retrieves_abandoned_waiter_exception(self) -> None:
        client = _HangingTurnClient()
        task = asyncio.create_task(self._start_turn(client))
        await client.started.wait()
        await asyncio.sleep(0)
        waiter = client._thread_turn_waiters["thread-1"]

        task.cancel()
        client._fail_pending()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(waiter.done())
        self.assertFalse(
            waiter._log_traceback,  # type: ignore[attr-defined]
            "the abandoned shielded waiter would emit 'Future exception was never retrieved'",
        )

    async def test_stale_reader_eof_cannot_fail_new_transport_futures(self) -> None:
        client = AppServerClient(codex_executable="unused")
        client._transport_generation = 2
        future = asyncio.get_running_loop().create_future()
        client._pending[1] = future

        client._fail_pending(1)
        self.assertFalse(future.done())

        client._fail_pending(2)
        with self.assertRaises(AppServerTransportClosed):
            await future

    async def test_one_request_timeout_fails_shared_waiters_and_closes_dispatch(self) -> None:
        client = _RequestTimeoutClient()
        co_tenant = asyncio.get_running_loop().create_future()
        client._turn_waiters["turn-co-tenant"] = co_tenant

        with self.assertRaises(AppServerRequestTimeout):
            await client.request("thread/start", {}, timeout=0.001)

        self.assertFalse(client.transport_available)
        with self.assertRaises(AppServerTransportClosed):
            await co_tenant

    async def test_turn_transport_loss_preserves_buffered_output_and_usage(self) -> None:
        client = _HangingTurnClient()
        task = asyncio.create_task(self._start_turn(client))
        await client.started.wait()
        await asyncio.sleep(0)
        client._handle_notification({
            "method": "item/completed",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-hanging",
                "item": {
                    "type": "agentMessage",
                    "text": '{"turn": 1}',
                },
            },
        })
        client._handle_notification({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-hanging",
                "tokenUsage": {"total": {"totalTokens": 321}},
            },
        })
        client._fail_pending()

        with self.assertRaises(AppServerTurnTransportLost) as raised:
            await task

        self.assertEqual(raised.exception.raw_output, '{"turn": 1}')
        self.assertEqual(raised.exception.token_usage.total_tokens, 321)
        self.assertEqual(raised.exception.token_telemetry, "observed")

    async def test_repeated_cancellation_does_not_orphan_interrupt_task(self) -> None:
        client = _BlockingInterruptClient()
        baseline_tasks = set(asyncio.all_tasks())
        task = asyncio.create_task(self._start_turn(client))
        await client.started.wait()
        task.cancel()
        await client.interrupt_started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        leaked = [
            pending for pending in asyncio.all_tasks()
            if pending not in baseline_tasks and not pending.done()
        ]
        try:
            self.assertEqual(leaked, [])
        finally:
            client.release_interrupt.set()
            for pending in leaked:
                pending.cancel()
            if leaked:
                await asyncio.gather(*leaked, return_exceptions=True)
        self.assertNotIn("thread-1", client.turn_ownership._open_threads)

    async def test_unknown_completion_of_open_turn_fails_closed_immediately(self) -> None:
        traced: list[dict[str, object]] = []
        client = AppServerClient(codex_executable="unused", notification_handler=traced.append)
        client.turn_ownership.begin_controller_turn("thread-1")
        client._handle_notification({
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-owned", "status": "inProgress"},
            },
        })
        client._handle_notification({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-other", "status": "completed"},
            },
        })

        self.assertIn(
            "amr/unmanagedContinuation",
            [str(item.get("method")) for item in traced],
        )
        self.assertEqual(
            client.turn_ownership.unmanaged_continuations,
            [{"thread_id": "thread-1", "turn_id": "turn-other"}],
        )


class ReasoningHealthTests(unittest.TestCase):
    def test_516_like_short_reasoning_is_diagnostic_not_math_verdict(self) -> None:
        monitor = ReasoningHealthMonitor(
            short_reasoning_tokens=600,
            repeated_token_tolerance=2,
            retry_limit=2,
        )
        signal = monitor.observe(
            job_id="job-1",
            turn_index=1,
            effort="xhigh",
            usage=TokenUsage(reasoning_output_tokens=516, total_tokens=800),
            telemetry="observed",
            max_effort_supported=True,
        )
        self.assertEqual(signal.diagnostic, "SHORT_REASONING")
        self.assertEqual(signal.action, "ESCALATE")
        self.assertEqual(signal.recommended_effort, "max")
        self.assertIsNone(signal.mathematical_status)
        self.assertIsNone(signal.trust_status)

    def test_repeated_reasoning_count_retries_without_claiming_incorrectness(self) -> None:
        monitor = ReasoningHealthMonitor(
            short_reasoning_tokens=100,
            repeated_token_tolerance=2,
            retry_limit=2,
        )
        monitor.observe(
            job_id="job-repeat", turn_index=1, effort="max",
            usage=TokenUsage(reasoning_output_tokens=700, total_tokens=900),
            telemetry="observed", max_effort_supported=True,
        )
        signal = monitor.observe(
            job_id="job-repeat", turn_index=2, effort="max",
            usage=TokenUsage(reasoning_output_tokens=700, total_tokens=900),
            telemetry="observed", max_effort_supported=True,
        )
        self.assertEqual(signal.diagnostic, "REPEATED_REASONING_TOKENS")
        self.assertEqual(signal.action, "RETRY")
        self.assertIsNone(signal.mathematical_status)


class ResearchTerminationAndStagnationTests(unittest.TestCase):
    def test_usage_limit_is_provider_quota_not_rate_or_math_failure(self) -> None:
        kind, retryable, details = _classify_failure(AppServerRequestError({
            "code": "usage_limit_reached",
            "message": "You've hit your usage limit",
            "reset_at": "2026-08-22T00:00:00Z",
            "http_status": 429,
        }))
        self.assertEqual(kind, "provider_quota_exhausted")
        self.assertFalse(retryable)
        self.assertEqual(details["provider_reset_at"], "2026-08-22T00:00:00Z")

    def test_uncontained_turn_timeout_is_provider_transport_loss(self) -> None:
        kind, retryable, details = _classify_failure(AppServerTurnTimeout(
            thread_id="thread-timeout",
            turn_id="turn-timeout",
            timeout=1800,
            turn=None,
            raw_output="bounded partial output",
            token_usage=TokenUsage(total_tokens=100),
            token_telemetry="observed",
            did_not_stop=True,
        ))
        self.assertEqual(kind, "provider_transport_lost")
        self.assertFalse(retryable)
        self.assertTrue(details["did_not_stop_after_interrupt"])

    def test_provider_transport_loss_pauses_epoch_and_requeues_exact_task(self) -> None:
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        root = TEST_RUNTIME / f"amr-transport-loss-{uuid4().hex}"
        root.mkdir()
        try:
            project = initialize_project(root / "neutral-project")
            controller = AutonomousController(
                load_config(project), backend=MockCodexBackend(), mock=True,
                run_id="transport-epoch", campaign_id="transport-campaign",
            )
            controller._pin_run_inputs(0.01, True)
            task = research_task()
            outcome = JobOutcome(
                job_id="job-transport", task_id=task.task_id, role=task.role,
                claim_id=task.target_claim, status="ERROR", result={},
                failure_kind="provider_transport_lost", retryable=False,
                error="app-server stdout closed",
                server_error={"code": "app_server_stdout_closed"},
            )

            controller._accept_research_result(outcome, task)

            self.assertFalse(controller._internal_failure)
            self.assertEqual(
                [item.task_id for item in controller.pending_research],
                [task.task_id],
            )
            self.assertIn(
                "provider transport lost", controller.scheduler_stop_reason or "",
            )
            self.assertTrue(controller._provider_transport_lost)
            self.assertEqual(controller.stagnation.attempts, {})
        finally:
            shutil.rmtree(root)

    def test_provider_quota_pauses_campaign_and_requeues_exact_task(self) -> None:
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        root = TEST_RUNTIME / f"amr-quota-{uuid4().hex}"
        root.mkdir()
        try:
            project = initialize_project(root / "neutral-project")
            controller = AutonomousController(
                load_config(project), backend=MockCodexBackend(), mock=True,
                run_id="quota-epoch", campaign_id="quota-campaign",
            )
            controller._pin_run_inputs(0.01, True)
            task = research_task()
            outcome = JobOutcome(
                job_id="quota-job",
                task_id=task.task_id,
                role=task.role,
                claim_id=task.target_claim,
                status="ERROR",
                result={},
                failure_kind="provider_quota_exhausted",
                retryable=False,
                error="provider usage quota exhausted",
                server_error={
                    "provider_reset_at": "2026-08-22T00:00:00Z",
                },
            )

            controller._accept_research_result(outcome, task)

            self.assertEqual(controller.pending_research, [task])
            self.assertIn("provider quota exhausted", controller.scheduler_stop_reason)
            self.assertIn("2026-08-22T00:00:00Z", controller.scheduler_stop_reason)
            self.assertFalse(controller._internal_failure)
            self.assertEqual(controller.stagnation.attempts, {})
            self.assertFalse(any(
                item.get("status") == "FAILED"
                for item in controller.route_ledger.records()
            ))
            events = [item["kind"] for item in controller.store.replay()]
            self.assertIn("PROVIDER_QUOTA_EXHAUSTED", events)
            self.assertIn("TASK_REQUEUED_AFTER_PROVIDER_QUOTA", events)
        finally:
            shutil.rmtree(root)

    def test_blocked_requires_one_repair_turn_and_controller_verification(self) -> None:
        policy = ResearchTurnPolicy(
            max_turns={"prover": 12, "falsifier": 8, "explorer": 6},
        )
        first = policy.decide(
            result=worker_result("BLOCKED", status="BLOCKED"),
            role="prover",
            turn_index=1,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
            blocker_repair_attempted=False,
            blocker_verified=False,
        )
        self.assertTrue(first.continue_same_thread)
        self.assertIn("repair", first.reason)

        unverified = policy.decide(
            result=worker_result("BLOCKED", status="BLOCKED"),
            role="prover",
            turn_index=2,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
            blocker_repair_attempted=True,
            blocker_verified=False,
        )
        self.assertTrue(unverified.continue_same_thread)

        verified = policy.decide(
            result=worker_result("BLOCKED", status="BLOCKED"),
            role="prover",
            turn_index=2,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
            blocker_repair_attempted=True,
            blocker_verified=True,
        )
        self.assertFalse(verified.continue_same_thread)
        self.assertEqual(verified.reason, "controller-verified execution blocker")

    def test_turn_limits_are_selected_per_research_role(self) -> None:
        policy = ResearchTurnPolicy(
            max_turns={"prover": 12, "falsifier": 8, "explorer": 6},
        )
        self.assertEqual(policy.max_turns_for("prover"), 12)
        self.assertEqual(policy.max_turns_for("falsifier"), 8)
        self.assertEqual(policy.max_turns_for("explorer"), 6)
        at_limit = policy.decide(
            result=worker_result("NO_PROGRESS"),
            role="explorer",
            turn_index=6,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
        )
        self.assertFalse(at_limit.continue_same_thread)
        self.assertEqual(
            at_limit.reason, "bounded same-thread turn limit reached",
        )

    def test_incomplete_plausible_proof_does_not_end_logical_job(self) -> None:
        policy = ResearchTurnPolicy(max_turns=3)
        directive = policy.decide(
            result=worker_result("PROOF", status="COMPLETED", finding="plausible sketch"),
            turn_index=1,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
        )
        self.assertTrue(directive.continue_same_thread)
        self.assertIn("untrusted", directive.reason)

    def test_only_controller_verified_progress_resets_stagnation(self) -> None:
        tracker = StagnationTracker(threshold=2)
        self.assertFalse(tracker.record("C_ROOT", "NO_PROGRESS", canonical_progress=False))
        self.assertTrue(tracker.record("C_ROOT", "PROOF", canonical_progress=False))
        self.assertFalse(tracker.record("C_ROOT", "PROOF", canonical_progress=True))
        self.assertEqual(tracker.attempts["C_ROOT"], [])

    def test_unmetered_turn_cannot_continue_past_controller_budget_gate(self) -> None:
        policy = ResearchTurnPolicy(max_turns=3)
        directive = policy.decide(
            result=worker_result("NO_PROGRESS"),
            turn_index=1,
            candidate_accepted=False,
            canonical_progress=False,
            health_signal=None,
            budget_stop_reason=(
                "token telemetry unavailable; bounded continuation stopped fail-closed"
            ),
        )
        self.assertFalse(directive.continue_same_thread)
        self.assertIn("fail-closed", directive.reason)

    def test_execution_status_is_distinct_from_math_and_trust_status(self) -> None:
        self.assertEqual(ExecutionStatus.FAILED, "FAILED")
        self.assertEqual(MathStatus.REFUTED, "FAILED")
        self.assertNotEqual(TrustStatus.REJECTED, MathStatus.REFUTED)


class ProofObligationReliabilityTests(unittest.TestCase):
    def test_obligation_frontier_is_canonical_stable_and_crash_recoverable(self) -> None:
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        root = TEST_RUNTIME / f"amr-obligation-{uuid4().hex}"
        root.mkdir()
        try:
            path = root / "claim_graph.json"
            graph = ClaimGraph({"C_ROOT": open_claim()}, path)
            first = graph.proof_frontier("C_ROOT")
            graph.save()
            recovered = ClaimGraph.load(path)
            second = recovered.proof_frontier("C_ROOT")
        finally:
            shutil.rmtree(root)
        self.assertEqual(first, second)
        self.assertEqual(len(first["remaining_obligation_ids"]), 1)
        self.assertEqual(first["next_obligation_id"], first["remaining_obligation_ids"][0])

    def test_unreviewed_proof_and_finite_check_do_not_close_obligation(self) -> None:
        graph = ClaimGraph({"C_ROOT": open_claim()})
        before = graph.proof_frontier("C_ROOT")
        proof = event("THEOREM_CANDIDATE")
        graph.mark_candidate(proof)
        self.assertEqual(graph.proof_frontier("C_ROOT"), before)

        finite = event("COMPUTATIONAL_PATTERN", evidence=EvidenceLevel.E2_EXACT_TESTED)
        graph.mark_candidate(finite)
        graph.apply_audit_pass(finite, 1, 1, EvidenceLevel.E2_EXACT_TESTED)
        self.assertNotEqual(graph.claims["C_ROOT"].math_status, MathStatus.PROVED)
        self.assertEqual(graph.proof_frontier("C_ROOT"), before)

    def test_false_conjecture_closes_only_after_audited_counterexample(self) -> None:
        graph = ClaimGraph({"C_ROOT": open_claim()})
        counterexample = event("COUNTEREXAMPLE", evidence=EvidenceLevel.E2_EXACT_TESTED)
        graph.mark_candidate(counterexample)
        self.assertEqual(graph.claims["C_ROOT"].math_status, MathStatus.OPEN)
        graph.apply_audit_pass(counterexample, 1, 1, EvidenceLevel.E2_EXACT_TESTED)
        self.assertEqual(graph.claims["C_ROOT"].math_status, MathStatus.REFUTED)
        self.assertEqual(graph.proof_frontier("C_ROOT")["remaining_obligation_ids"], [])


class CrashRecoveryFaultInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = TEST_RUNTIME / f"amr-crash-recovery-{uuid4().hex}"
        self.project = initialize_project(self.root / "neutral-project")
        self.config = load_config(self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_crash_after_first_turn_requeues_task_and_interrupts_stale_turn(self) -> None:
        controller = AutonomousController(
            self.config, backend=MockCodexBackend(), mock=True,
        )
        task = research_task()
        controller.store.append("TASK_ACCEPTED", {
            "task_id": task.task_id,
            "fingerprint": task.fingerprint,
            "task": task.to_dict(),
        })
        controller.store.append("JOB_STARTED", {
            "job_id": "job-before-crash",
            "task_id": task.task_id,
            "role": task.role,
            "claim_id": task.target_claim,
        })
        controller.store.append("JOB_BOUND", {
            "job_id": "job-before-crash",
            "thread_id": "thread-before-crash",
            "turn_id": "turn-1",
        })
        controller.store.append("RESEARCH_TURN_COMPLETED", {
            "job_id": "job-before-crash",
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "thread_id": "thread-before-crash",
            "turn_id": "turn-1",
            "turn_index": 1,
            "execution_status": "COMPLETED",
            "controller_directive": "CONTINUE",
            "canonical_progress": False,
        })

        resumed = AutonomousController(
            self.config,
            backend=MockCodexBackend(),
            run_id=controller.run_id,
            mock=True,
            resume=True,
        )
        resumed.recover()

        self.assertEqual([item.task_id for item in resumed.pending_research], [task.task_id])
        self.assertEqual(
            resumed.stale_remote_turns,
            [("job-before-crash", "thread-before-crash", "turn-1")],
        )
        self.assertEqual(resumed.stagnation.attempts, {})
        self.assertIn(
            "JOB_RETRY_QUEUED",
            [item["kind"] for item in resumed.store.replay()],
        )


if __name__ == "__main__":
    unittest.main()
