from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import TokenUsage


@dataclass(frozen=True, slots=True)
class ReasoningHealthSignal:
    diagnostic: str
    action: str
    observed_reasoning_tokens: int | None
    recommended_effort: str | None = None
    mathematical_status: None = None
    trust_status: None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ReasoningHealthMonitor:
    """Diagnose suspicious turn shape without judging mathematical content."""

    def __init__(
        self,
        *,
        short_reasoning_tokens: int,
        repeated_token_tolerance: int,
        retry_limit: int,
    ):
        if short_reasoning_tokens < 0:
            raise ValueError("short_reasoning_tokens must be non-negative")
        if repeated_token_tolerance < 2:
            raise ValueError("repeated_token_tolerance must be at least 2")
        if retry_limit < 0:
            raise ValueError("retry_limit must be non-negative")
        self.short_reasoning_tokens = int(short_reasoning_tokens)
        self.repeated_token_tolerance = int(repeated_token_tolerance)
        self.retry_limit = int(retry_limit)
        self._reasoning_history: dict[str, list[int]] = {}
        self._actions_used: dict[str, int] = {}

    def observe(
        self,
        *,
        job_id: str,
        turn_index: int,
        effort: str,
        usage: TokenUsage,
        telemetry: str,
        max_effort_supported: bool,
    ) -> ReasoningHealthSignal:
        del turn_index
        if telemetry != "observed" or usage.reasoning_output_tokens <= 0:
            return ReasoningHealthSignal(
                diagnostic="UNKNOWN_TELEMETRY",
                action="DIAGNOSE_ONLY",
                observed_reasoning_tokens=None,
            )
        observed = int(usage.reasoning_output_tokens)
        history = self._reasoning_history.setdefault(job_id, [])
        history.append(observed)
        if len(history) > self.repeated_token_tolerance:
            del history[:-self.repeated_token_tolerance]
        repeated = (
            len(history) >= self.repeated_token_tolerance
            and len(set(history[-self.repeated_token_tolerance:])) == 1
        )
        if repeated:
            diagnostic = "REPEATED_REASONING_TOKENS"
        elif observed <= self.short_reasoning_tokens:
            diagnostic = "SHORT_REASONING"
        else:
            return ReasoningHealthSignal(
                diagnostic="HEALTHY",
                action="NONE",
                observed_reasoning_tokens=observed,
            )
        actions_used = self._actions_used.get(job_id, 0)
        if actions_used >= self.retry_limit:
            return ReasoningHealthSignal(
                diagnostic=diagnostic,
                action="DIAGNOSE_ONLY",
                observed_reasoning_tokens=observed,
            )
        self._actions_used[job_id] = actions_used + 1
        if effort == "xhigh" and max_effort_supported:
            return ReasoningHealthSignal(
                diagnostic=diagnostic,
                action="ESCALATE",
                observed_reasoning_tokens=observed,
                recommended_effort="max",
            )
        return ReasoningHealthSignal(
            diagnostic=diagnostic,
            action="RETRY",
            observed_reasoning_tokens=observed,
        )
