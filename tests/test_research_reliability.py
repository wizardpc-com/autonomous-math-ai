from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.app_server import (
    AppServerClient, AppServerRequestError, TurnOwnershipRegistry,
)
from autonomous_math_research.backend import AppServerBackend, TurnDirective
from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import AutonomousController
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
        self.goal_calls = 0
        self.turn_calls: list[dict[str, object]] = []

    async def start_thread(self, **kwargs):  # type: ignore[no-untyped-def]
        self.start_thread_calls += 1
        return {
            "thread": {
                "id": "thread-proof",
                "model": kwargs["model"],
                "serviceTier": None,
            }
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
                    "serviceTier": None,
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

    async def test_cancelled_wait_interrupts_remote_turn_and_closes_ownership(self) -> None:
        client = _HangingTurnClient()
        task = asyncio.create_task(self._start_turn(client))
        await client.started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(client.interrupt_calls, [("thread-1", "turn-hanging")])
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
