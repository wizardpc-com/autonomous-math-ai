from __future__ import annotations

from dataclasses import dataclass

from ..models import LifecyclePhase


_ALLOWED = {
    LifecyclePhase.BOOTSTRAP: {LifecyclePhase.RUNNING, LifecyclePhase.DRAINING_FAILURE},
    LifecyclePhase.RUNNING: {
        LifecyclePhase.DRAINING_FAILURE,
        LifecyclePhase.DRAINING_BUDGET,
        LifecyclePhase.DRAINING_EPOCH,
        LifecyclePhase.FINALIZING,
    },
    LifecyclePhase.DRAINING_FAILURE: {LifecyclePhase.SEALED},
    LifecyclePhase.DRAINING_BUDGET: {LifecyclePhase.SEALED},
    LifecyclePhase.DRAINING_EPOCH: {LifecyclePhase.SEALED},
    LifecyclePhase.FINALIZING: {LifecyclePhase.COMPLETED, LifecyclePhase.DRAINING_FAILURE},
    LifecyclePhase.SEALED: set(),
    LifecyclePhase.COMPLETED: set(),
}


@dataclass(slots=True)
class MonotoneLifecycle:
    phase: LifecyclePhase = LifecyclePhase.BOOTSTRAP
    reason: str | None = None

    @property
    def can_dispatch(self) -> bool:
        return self.phase is LifecyclePhase.RUNNING

    @property
    def draining(self) -> bool:
        return self.phase in {
            LifecyclePhase.DRAINING_FAILURE,
            LifecyclePhase.DRAINING_BUDGET,
            LifecyclePhase.DRAINING_EPOCH,
            LifecyclePhase.FINALIZING,
        }

    def transition(self, target: LifecyclePhase, *, reason: str) -> bool:
        if target is self.phase:
            if self.reason is None:
                self.reason = reason
            return False
        if target not in _ALLOWED[self.phase]:
            raise ValueError(f"illegal lifecycle transition {self.phase} -> {target}")
        self.phase = target
        self.reason = reason
        return True
