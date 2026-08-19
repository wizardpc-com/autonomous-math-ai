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
    global_cost_budget: float | None = None
    soft_fraction: float = 0.75
    hard_fraction: float = 0.95
    rate_reduce_percent: float = 75.0
    rate_drain_percent: float = 90.0
    rate_stop_percent: float = 98.0
    role_budgets: dict[str, int] = field(default_factory=dict)
    role_cost_budgets: dict[str, float] = field(default_factory=dict)
    total: TokenUsage = field(default_factory=TokenUsage)
    total_cost_usd: float = 0.0
    by_role: dict[str, int] = field(default_factory=dict)
    cost_by_role: dict[str, float] = field(default_factory=dict)
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

    @property
    def reserved_cost_usd(self) -> float:
        return sum(float(item.get("estimated_cost_usd") or 0.0) for item in self.reservations.values())

    def reserved_cost_for_role(self, role: str) -> float:
        return sum(
            float(item.get("estimated_cost_usd") or 0.0)
            for item in self.reservations.values()
            if item.get("role") == role
        )

    def reserve(
        self,
        job_id: str,
        role: str,
        estimated_tokens: int,
        estimated_cost_usd: float | None = None,
    ) -> bool:
        if job_id in self.reservations:
            raise ValueError(f"duplicate token reservation: {job_id}")
        if not self.may_start(role, estimated_tokens, estimated_cost_usd):
            return False
        self.reservations[job_id] = {
            "role": role,
            "estimated_tokens": max(0, int(estimated_tokens)),
            "estimated_cost_usd": max(0.0, float(estimated_cost_usd or 0.0)),
        }
        return True

    def restore_reservation(
        self,
        job_id: str,
        role: str,
        estimated_tokens: int,
        estimated_cost_usd: float | None = None,
    ) -> None:
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
            "estimated_cost_usd": max(0.0, float(estimated_cost_usd or 0.0)),
        }

    def release(self, job_id: str) -> None:
        self.reservations.pop(job_id, None)

    def record(
        self,
        job_id: str,
        role: str,
        usage: TokenUsage,
        useful: bool | None = None,
        cost_usd: float | None = None,
    ) -> None:
        previous = self.by_job.get(job_id, {}).get("total_tokens", 0)
        delta = max(0, usage.total_tokens - int(previous))
        self.total.total_tokens += delta
        self.total.input_tokens += max(0, usage.input_tokens - int(self.by_job.get(job_id, {}).get("input_tokens", 0)))
        self.total.cached_input_tokens += max(0, usage.cached_input_tokens - int(self.by_job.get(job_id, {}).get("cached_input_tokens", 0)))
        self.total.cache_write_input_tokens += max(0, usage.cache_write_input_tokens - int(self.by_job.get(job_id, {}).get("cache_write_input_tokens", 0)))
        self.total.output_tokens += max(0, usage.output_tokens - int(self.by_job.get(job_id, {}).get("output_tokens", 0)))
        self.total.reasoning_output_tokens += max(0, usage.reasoning_output_tokens - int(self.by_job.get(job_id, {}).get("reasoning_output_tokens", 0)))
        self.by_role[role] = self.by_role.get(role, 0) + delta
        previous_cost = float(self.by_job.get(job_id, {}).get("cost_usd") or 0.0)
        observed_cost = (
            previous_cost
            if cost_usd is None else max(previous_cost, float(cost_usd))
        )
        cost_delta = max(0.0, observed_cost - previous_cost)
        self.total_cost_usd += cost_delta
        self.cost_by_role[role] = self.cost_by_role.get(role, 0.0) + cost_delta
        self.by_job[job_id] = {
            **usage.to_dict(), "role": role, "useful": useful,
            "cost_usd": (
                observed_cost
                if cost_usd is not None or "cost_usd" in self.by_job.get(job_id, {})
                else None
            ),
        }

    def may_start(
        self,
        role: str,
        estimated_tokens: int = 0,
        estimated_cost_usd: float | None = None,
    ) -> bool:
        estimated_tokens = max(0, int(estimated_tokens))
        committed_total = self.total.total_tokens + self.reserved_tokens
        if self.global_budget is not None and committed_total + estimated_tokens > self.global_budget:
            return False
        estimated_cost = max(0.0, float(estimated_cost_usd or 0.0))
        if (
            self.global_cost_budget is not None
            and self.total_cost_usd + self.reserved_cost_usd + estimated_cost
            > self.global_cost_budget
        ):
            return False
        role_budget = self.role_budgets.get(role)
        committed_role = self.by_role.get(role, 0) + self.reserved_tokens_for_role(role)
        if role_budget is not None and committed_role + estimated_tokens > role_budget:
            return False
        role_cost_budget = self.role_cost_budgets.get(role)
        committed_role_cost = self.cost_by_role.get(role, 0.0) + self.reserved_cost_for_role(role)
        return (
            role_cost_budget is None
            or committed_role_cost + estimated_cost <= role_cost_budget
        )

    def decide(self, rate_limits: dict[str, Any] | None = None) -> GovernorDecision:
        token_fraction = (
            0.0 if not self.global_budget else self.total.total_tokens / self.global_budget
        )
        cost_fraction = (
            0.0 if not self.global_cost_budget
            else self.total_cost_usd / self.global_cost_budget
        )
        used_fraction = max(token_fraction, cost_fraction)
        budget_kind = "token" if token_fraction >= cost_fraction else "cost"
        rate_percent = _highest_rate_percent(rate_limits)
        if rate_percent >= self.rate_stop_percent:
            return GovernorDecision("STOP", "hard rate limit reached", 0, False, False)
        if used_fraction >= 1:
            return GovernorDecision(
                "DRAIN_TO_STOP",
                f"global {budget_kind} budget reached; waiting for active jobs to finish",
                0,
                False,
                False,
            )
        if rate_percent >= self.rate_drain_percent or used_fraction >= self.hard_fraction:
            return GovernorDecision("DRAIN", "near token, cost, or rate limit", 1, False, False)
        if rate_percent >= self.rate_reduce_percent or used_fraction >= self.soft_fraction:
            return GovernorDecision("DEGRADE", "soft budget threshold", max(1, self.configured_max_research // 2), False, False)
        return GovernorDecision("RUN", "within budget", self.configured_max_research, True, False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "global_budget": self.global_budget,
            "global_cost_budget_usd": self.global_cost_budget,
            "total": self.total.to_dict(),
            "total_cost_usd": self.total_cost_usd,
            "by_role": dict(self.by_role),
            "cost_by_role": dict(self.cost_by_role),
            "reservations": {
                job_id: {
                    **reservation,
                    "remaining_tokens": self._reservation_remaining(job_id, reservation),
                }
                for job_id, reservation in self.reservations.items()
            },
            "reserved_tokens": self.reserved_tokens,
            "reserved_cost_usd": self.reserved_cost_usd,
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
