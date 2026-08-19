from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .models import TokenUsage


@dataclass(slots=True)
class GovernorDecision:
    action: str
    reason: str
    max_research: int | None = None
    allow_exploration: bool = True
    use_economy_routes: bool = False


@dataclass(slots=True)
class TokenGovernor:
    global_budget: int | None
    configured_max_research: int
    soft_fraction: float = 0.75
    hard_fraction: float = 0.95
    rate_reduce_percent: float = 75.0
    rate_drain_percent: float = 90.0
    rate_stop_percent: float = 98.0
    role_budgets: dict[str, int] = field(default_factory=dict)
    total: TokenUsage = field(default_factory=TokenUsage)
    by_role: dict[str, int] = field(default_factory=dict)
    by_job: dict[str, dict[str, Any]] = field(default_factory=dict)
    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _reservation_remaining(self, job_id: str, reservation: dict[str, Any]) -> int:
        observed = int(self.by_job.get(job_id, {}).get("total_tokens", 0) or 0)
        return max(0, int(reservation.get("estimated_tokens", 0)) - observed)

    @property
    def reserved_tokens(self) -> int:
        return sum(
            self._reservation_remaining(job_id, reservation)
            for job_id, reservation in self.reservations.items()
        )

    def reserved_tokens_for_role(self, role: str) -> int:
        return sum(
            self._reservation_remaining(job_id, reservation)
            for job_id, reservation in self.reservations.items()
            if reservation.get("role") == role
        )

    def reserve(self, job_id: str, role: str, estimated_tokens: int) -> bool:
        if job_id in self.reservations:
            raise ValueError(f"duplicate token reservation: {job_id}")
        if not self.may_start(role, estimated_tokens):
            return False
        self.reservations[job_id] = {
            "role": role, "estimated_tokens": max(0, int(estimated_tokens)),
        }
        return True

    def restore_reservation(self, job_id: str, role: str, estimated_tokens: int) -> None:
        """Restore an already-started job without treating it as new dispatch.

        Crash recovery must continue observing in-flight work even when the
        remaining budget would forbid launching another job. The restored
        reservation is still included in snapshots and is released/accounted
        through the ordinary terminal path.
        """
        if job_id in self.reservations:
            raise ValueError(f"duplicate token reservation: {job_id}")
        self.reservations[job_id] = {
            "role": role,
            "estimated_tokens": max(0, int(estimated_tokens)),
        }

    def release(self, job_id: str) -> None:
        self.reservations.pop(job_id, None)

    def record(self, job_id: str, role: str, usage: TokenUsage, useful: bool | None = None) -> None:
        previous = self.by_job.get(job_id, {}).get("total_tokens", 0)
        delta = max(0, usage.total_tokens - int(previous))
        self.total.total_tokens += delta
        self.total.input_tokens += max(0, usage.input_tokens - int(self.by_job.get(job_id, {}).get("input_tokens", 0)))
        self.total.cached_input_tokens += max(0, usage.cached_input_tokens - int(self.by_job.get(job_id, {}).get("cached_input_tokens", 0)))
        self.total.cache_write_input_tokens += max(0, usage.cache_write_input_tokens - int(self.by_job.get(job_id, {}).get("cache_write_input_tokens", 0)))
        self.total.output_tokens += max(0, usage.output_tokens - int(self.by_job.get(job_id, {}).get("output_tokens", 0)))
        self.total.reasoning_output_tokens += max(0, usage.reasoning_output_tokens - int(self.by_job.get(job_id, {}).get("reasoning_output_tokens", 0)))
        self.by_role[role] = self.by_role.get(role, 0) + delta
        self.by_job[job_id] = {**usage.to_dict(), "role": role, "useful": useful}

    def may_start(self, role: str, estimated_tokens: int = 0) -> bool:
        estimated_tokens = max(0, int(estimated_tokens))
        committed_total = self.total.total_tokens + self.reserved_tokens
        if self.global_budget is not None and committed_total + estimated_tokens > self.global_budget:
            return False
        role_budget = self.role_budgets.get(role)
        committed_role = self.by_role.get(role, 0) + self.reserved_tokens_for_role(role)
        return role_budget is None or committed_role + estimated_tokens <= role_budget

    def decide(self, rate_limits: dict[str, Any] | None = None) -> GovernorDecision:
        used_fraction = 0.0 if not self.global_budget else self.total.total_tokens / self.global_budget
        rate_percent = _highest_rate_percent(rate_limits)
        if rate_percent >= self.rate_stop_percent:
            return GovernorDecision("STOP", "hard rate limit reached", 0, False, False)
        if used_fraction >= 1:
            return GovernorDecision(
                "DRAIN_TO_STOP",
                "global token budget reached; waiting for active jobs to finish",
                0,
                False,
                False,
            )
        if rate_percent >= self.rate_drain_percent or used_fraction >= self.hard_fraction:
            return GovernorDecision("DRAIN", "near token or rate limit", 1, False, False)
        if rate_percent >= self.rate_reduce_percent or used_fraction >= self.soft_fraction:
            return GovernorDecision("DEGRADE", "soft budget threshold", max(1, self.configured_max_research // 2), False, False)
        return GovernorDecision("RUN", "within budget", self.configured_max_research, True, False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "global_budget": self.global_budget,
            "total": self.total.to_dict(),
            "by_role": dict(self.by_role),
            "reservations": {
                job_id: {
                    **reservation,
                    "remaining_tokens": self._reservation_remaining(job_id, reservation),
                }
                for job_id, reservation in self.reservations.items()
            },
            "reserved_tokens": self.reserved_tokens,
            "decision": asdict(self.decide()),
        }


def _highest_rate_percent(data: dict[str, Any] | None) -> float:
    if not data:
        return 0.0
    snapshots = []
    if isinstance(data.get("rateLimits"), dict):
        snapshots.append(data["rateLimits"])
    if isinstance(data.get("rateLimitsByLimitId"), dict):
        snapshots.extend(data["rateLimitsByLimitId"].values())
    values: list[float] = []
    for snapshot in snapshots:
        if snapshot.get("rateLimitReachedType") or snapshot.get("spendControlReached") is True:
            return 100.0
        for key in ("primary", "secondary"):
            if isinstance(snapshot.get(key), dict) and snapshot[key].get("usedPercent") is not None:
                values.append(float(snapshot[key]["usedPercent"]))
    return max(values, default=0.0)
