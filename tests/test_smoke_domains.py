from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.config import load_config
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import TokenUsage
from autonomous_math_research.smoke import run_real_smoke


class _ScriptedSmokeClient:
    instances: list["_ScriptedSmokeClient"] = []

    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = dict(kwargs)
        self.stderr_lines: list[str] = []
        self.prompts: list[str] = []
        self.counter = 0
        self.instances.append(self)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def probe_capabilities(self, project_root: Path) -> dict[str, object]:
        return {"project_root": str(project_root), "scripted": True}

    async def start_thread(self, **kwargs):  # type: ignore[no-untyped-def]
        self.counter += 1
        return {
            "thread": {"id": f"smoke-thread-{self.counter}"},
            "model": kwargs["model"],
        }

    async def start_turn(self, **kwargs):  # type: ignore[no-untyped-def]
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        required = set(kwargs["output_schema"]["required"])
        if "spawn" in required:
            payload = {
                "assessment": "bounded domain smoke plan",
                "spawn": [],
                "audit_priorities": [],
                "route_updates": [],
                "short_rationale": "exercise the pinned domain lifecycle",
            }
        elif "result_type" in required:
            challenger = "adversarial" in prompt.lower()
            if challenger:
                result_type = "NO_PROGRESS"
                evidence = "E0_SPECULATIVE"
            elif "certified-computational-research" in prompt:
                result_type = "CERTIFICATE"
                evidence = "E4_CERTIFIED"
            elif "empirical-research" in prompt:
                result_type = "EMPIRICAL_FINDING"
                evidence = "E3_REDUNDANT_EXACT"
            else:
                result_type = "PROOF"
                evidence = "E0_SPECULATIVE"
            payload = {
                "result_type": result_type,
                "main_finding": "the bounded evidence matches the exact toy claim",
                "status": "COMPLETED",
                "artifact_paths": [],
                "next_suggested_question": "none for this smoke",
                "evidence_level": evidence,
            }
        else:
            if "empirical-research" in prompt:
                evidence = "E3_REDUNDANT_EXACT"
            elif "certified-computational-research" in prompt:
                evidence = "E4_CERTIFIED"
            else:
                evidence = "E0_SPECULATIVE"
            payload = {
                "verdict": "PASS",
                "checks": [{
                    "name": "independent bounded check",
                    "passed": True,
                    "detail": "the exact claim and controller receipt agree",
                }],
                "gaps": [],
                "notes": [],
                "verified_evidence_level": evidence,
            }
        turn_id = f"turn-{len(self.prompts)}"
        return (
            {"turn": {
                "id": turn_id,
                "status": "completed",
                "model": kwargs["model"],
            }},
            json.dumps(payload, ensure_ascii=False),
            TokenUsage(total_tokens=10, input_tokens=6, output_tokens=4),
            "provider",
        )


class DomainAwareSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"smoke-domains-{uuid4().hex}"
        self.root.mkdir()
        _ScriptedSmokeClient.instances.clear()

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    @patch(
        "autonomous_math_research.smoke.inspect_generated_schema",
        return_value={"scripted": True},
    )
    def test_real_provider_smoke_uses_each_pinned_domain_semantics(
        self, _schema_probe,
    ) -> None:
        expected = {
            "math-research": ("PROVED", 0),
            "certified-computational-research": ("CERTIFIED", 1),
            "empirical-research": ("CONFIRMED", 2),
        }
        for domain, (status, receipt_count) in expected.items():
            with self.subTest(domain=domain):
                project = self.root / domain
                initialize_project(project, domain=domain)
                report = asyncio.run(run_real_smoke(
                    load_config(project),
                    token_budget=10_000,
                    client_factory=_ScriptedSmokeClient,
                ))
                summary = json.loads(
                    (report.parent / "SMOKE_SUMMARY.json").read_text(encoding="utf-8")
                )
                self.assertEqual(summary["domain"], domain)
                self.assertEqual(summary["research_status"], status)
                self.assertEqual(
                    len(summary["deterministic_evidence_receipts"]), receipt_count,
                )
                self.assertFalse(summary["internal_failure"])
                client = _ScriptedSmokeClient.instances[-1]
                if domain == "empirical-research":
                    self.assertTrue(any(
                        "Never call empirical evidence PROVED" in prompt
                        for prompt in client.prompts
                    ))


if __name__ == "__main__":
    unittest.main()
