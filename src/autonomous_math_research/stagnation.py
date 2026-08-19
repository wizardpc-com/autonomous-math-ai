from __future__ import annotations

from dataclasses import dataclass, field


MEANINGFUL = {
    "PROOF", "COUNTEREXAMPLE", "STRICT_REDUCTION", "NEW_OBSTRUCTION",
    "NEW_DETECTOR", "STRONGER_COMPUTATION", "DEPENDENCY_CHANGE",
}


@dataclass(slots=True)
class StagnationTracker:
    threshold: int
    attempts: dict[str, list[str]] = field(default_factory=dict)

    def record(self, claim_id: str, outcome: str) -> bool:
        history = self.attempts.setdefault(claim_id, [])
        history.append(outcome)
        if outcome in MEANINGFUL:
            history.clear()
            return False
        if len(history) > self.threshold:
            del history[:-self.threshold]
        return len(history) >= self.threshold and all(item not in MEANINGFUL for item in history[-self.threshold:])

    def diversification_constraint(self, claim_id: str, dominant_route: str) -> dict[str, str]:
        return {
            "claim_id": claim_id,
            "action": "DIVERSIFY",
            "forbidden_route": dominant_route,
            "instruction": (
                "Do not use the dominant conjectural lemma or its equivalent reformulation. "
                "Choose a different representation, evaluator, or open-frontier dependency."
            ),
        }
