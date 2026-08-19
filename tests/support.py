from __future__ import annotations

from pathlib import Path
import shutil
from uuid import uuid4

from autonomous_math_research.config import load_config
from autonomous_math_research.initializer import initialize_project


REPO = Path(__file__).resolve().parents[1]


class TempProjectMixin:
    """Create one neutral project without relying on an external checkout."""

    def setUp(self) -> None:
        super().setUp()
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"amr-test-{uuid4().hex}"
        self.project = self.root / "neutral-project"
        initialize_project(self.project)
        for directory in ("claims", "proofs", "state", "artifacts", "experiments"):
            target = self.project / directory
            target.mkdir(parents=True, exist_ok=True)
            (target / "protected.txt").write_text(
                f"{directory}\n", encoding="utf-8", newline="\n",
            )
        self.config = load_config(self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)
        super().tearDown()
