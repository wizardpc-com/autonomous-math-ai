from __future__ import annotations

from typing import Any

from .backend import TurnDirective
from .reasoning_health import ReasoningHealthSignal


class ResearchTurnPolicy:
    """Controller policy for deciding whether one logical research job is done."""

    def __init__(self, *, max_turns: int):
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        self.max_turns = int(max_turns)

    def decide(
        self,
        *,
        result: dict[str, Any],
        turn_index: int,
        candidate_accepted: bool,
        canonical_progress: bool,
        health_signal: ReasoningHealthSignal | None,
        budget_stop_reason: str | None = None,
    ) -> TurnDirective:
        if canonical_progress:
            return TurnDirective.stop("controller-verified canonical progress")
        if candidate_accepted:
            return TurnDirective.stop("validated candidate entered the audit frontier")
        if budget_stop_reason:
            return TurnDirective.stop(budget_stop_reason)
        if turn_index >= self.max_turns:
            return TurnDirective.stop("bounded same-thread turn limit reached")
        if health_signal and health_signal.action in {"RETRY", "ESCALATE"}:
            return TurnDirective.continue_with(
                self._continuation_prompt(result, health_signal.diagnostic),
                reason=f"reasoning health diagnostic: {health_signal.diagnostic}",
                effort_override=health_signal.recommended_effort,
            )
        result_type = str(result.get("result_type") or "NO_PROGRESS")
        if result_type in {"BLOCKED", "TOOL_ERROR"}:
            return TurnDirective.stop(f"explicit execution terminal: {result_type}")
        # A model's PROOF/COUNTEREXAMPLE label is not controller-verified
        # progress and cannot terminate the job without a validated candidate.
        return TurnDirective.continue_with(
            self._continuation_prompt(result, "untrusted role result"),
            reason="untrusted role result did not meet a strict stop condition",
        )

    @staticmethod
    def _continuation_prompt(result: dict[str, Any], reason: str) -> str:
        next_question = str(result.get("next_suggested_question") or "").strip()
        return (
            "Continue the same controller-owned proof task in this thread. "
            f"The prior turn ended because {reason}. "
            "Do not treat a self-reported PROOF, a finite search, or a plausible sketch as "
            "canonical progress. Resolve the next open proof obligation, submit any auditable "
            "candidate through the installed helper, or report a concrete verified blocker. "
            + (f"Next recorded question: {next_question}" if next_question else "")
        )
