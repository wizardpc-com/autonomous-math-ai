from __future__ import annotations

from typing import Any

from .backend import TurnDirective
from .reasoning_health import ReasoningHealthSignal


class ResearchTurnPolicy:
    """Controller policy for deciding whether one logical research job is done."""

    def __init__(
        self,
        *,
        max_turns: int | dict[str, int],
        domain: str = "math-research",
    ):
        if isinstance(max_turns, int) and not isinstance(max_turns, bool):
            if max_turns < 1:
                raise ValueError("max_turns must be positive")
            self.max_turns = {
                role: int(max_turns)
                for role in ("prover", "falsifier", "explorer")
            }
        elif isinstance(max_turns, dict):
            expected = {"prover", "falsifier", "explorer"}
            if set(max_turns) != expected or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in max_turns.values()
            ):
                raise ValueError(
                    "max_turns must define positive prover, falsifier, explorer limits"
                )
            self.max_turns = dict(max_turns)
        else:
            raise ValueError("max_turns must be an integer or per-role mapping")
        self.domain = domain

    def max_turns_for(self, role: str) -> int:
        if role not in self.max_turns:
            raise ValueError(f"unsupported multi-turn research role: {role}")
        return self.max_turns[role]

    def decide(
        self,
        *,
        result: dict[str, Any],
        role: str = "prover",
        turn_index: int,
        candidate_accepted: bool,
        canonical_progress: bool,
        health_signal: ReasoningHealthSignal | None,
        budget_stop_reason: str | None = None,
        blocker_repair_attempted: bool = False,
        blocker_verified: bool = False,
    ) -> TurnDirective:
        if canonical_progress:
            return TurnDirective.stop("controller-verified canonical progress")
        if candidate_accepted:
            return TurnDirective.stop("validated candidate entered the audit frontier")
        if budget_stop_reason:
            return TurnDirective.stop(budget_stop_reason)
        result_type = str(result.get("result_type") or "NO_PROGRESS")
        if result_type == "BLOCKED":
            if blocker_repair_attempted and blocker_verified:
                return TurnDirective.stop("controller-verified execution blocker")
            if not blocker_repair_attempted:
                return TurnDirective.continue_with(
                    self._continuation_prompt(result, "an unverified blocker report"),
                    reason="controller-required blocker repair turn",
                )
            if turn_index >= self.max_turns_for(role):
                return TurnDirective.stop("bounded same-thread turn limit reached")
            return TurnDirective.continue_with(
                self._continuation_prompt(result, "the blocker remains unverified"),
                reason="unverified blocker requires another bounded repair turn",
            )
        if turn_index >= self.max_turns_for(role):
            return TurnDirective.stop("bounded same-thread turn limit reached")
        if health_signal and health_signal.action in {"RETRY", "ESCALATE"}:
            return TurnDirective.continue_with(
                self._continuation_prompt(result, health_signal.diagnostic),
                reason=f"reasoning health diagnostic: {health_signal.diagnostic}",
                effort_override=health_signal.recommended_effort,
            )
        if result_type == "TOOL_ERROR":
            return TurnDirective.stop(f"explicit execution terminal: {result_type}")
        # A model's claimed result is not controller-verified
        # progress and cannot terminate the job without a validated candidate.
        return TurnDirective.continue_with(
            self._continuation_prompt(result, "untrusted role result"),
            reason="untrusted role result did not meet a strict stop condition",
        )

    def _continuation_prompt(self, result: dict[str, Any], reason: str) -> str:
        next_question = str(result.get("next_suggested_question") or "").strip()
        if self.domain == "math-research":
            instruction = (
                "Continue the same controller-owned proof task in this thread. "
                f"The prior turn ended because {reason}. "
                "Do not treat a self-reported PROOF, a finite search, or a plausible sketch as "
                "canonical progress. Resolve the next open proof obligation, submit any auditable "
                "candidate through the installed helper, or report a concrete verified blocker. "
            )
        else:
            instruction = (
                "Continue the same controller-owned research task in this thread. "
                f"The prior turn ended because {reason}. "
                "Do not treat a self-reported result or finite experiment as trusted progress. "
                "Resolve the next domain obligation, submit auditable evidence through the "
                "installed helper, or report a concrete verified blocker. "
            )
        return (
            instruction
            + (f"Next recorded question: {next_question}" if next_question else "")
        )
