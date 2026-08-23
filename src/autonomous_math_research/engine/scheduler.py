from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..models import ResearchTask


_GAIN = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0}
_IMPACT = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0, "CRITICAL": 8.0}
_COST = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0}


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    audit_backlog_pressure: float
    target_research: int
    task_scores: dict[str, float]


class DynamicScheduler:
    """Admission-only scheduler; it never cancels healthy active jobs."""

    def __init__(self, *, max_research: int, max_audit: int):
        self.max_research = int(max_research)
        self.max_audit = int(max_audit)

    @staticmethod
    def task_score(task: ResearchTask, route_counts: dict[str, int]) -> float:
        novelty = 1.0 / (1.0 + route_counts.get(task.route_family, 0))
        return (
            _GAIN.get(task.expected_information_gain, 2.0)
            * _IMPACT[task.research_impact]
            * novelty
            / _COST[task.estimated_cost_tier]
        )

    def decide(
        self,
        *,
        pending_audits: int,
        active_audits: int,
        tasks: Iterable[ResearchTask],
        route_counts: dict[str, int],
    ) -> SchedulerDecision:
        pressure = (pending_audits + active_audits) / max(1, self.max_audit)
        if pressure < 1:
            target = self.max_research
        elif pressure < 2:
            target = min(self.max_research, 4)
        else:
            target = min(self.max_research, 2)
        scores = {
            task.task_id: self.task_score(task, route_counts) for task in tasks
        }
        return SchedulerDecision(pressure, target, scores)

    @staticmethod
    def eligible_under_pressure(
        task: ResearchTask,
        *,
        pressure: float,
        known_representation_ids: set[str],
    ) -> bool:
        if pressure < 2:
            return True
        return (
            task.expected_information_gain == "HIGH"
            or task.representation_id not in known_representation_ids
        )
