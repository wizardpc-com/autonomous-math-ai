from __future__ import annotations

import asyncio
import contextlib
from io import StringIO
import json
from pathlib import Path
import re
import shutil
import tomllib
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.cli import build_parser, main as cli_main
from autonomous_math_research.catalog import rebuild_catalog
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import (
    AutonomousController,
    build_mock_full_cycle_backend,
)
from autonomous_math_research.lifecycle.campaign import CampaignStore
from autonomous_math_research.lifecycle.state import LifecyclePhase, MonotoneLifecycle
from autonomous_math_research.resources import policy_resource, schema_resource
from autonomous_math_research.monitor import (
    _TerminalMouseInput, build_status, format_live_event, resolve_run, watch_run,
)
from autonomous_math_research.storage import EventStore, ProjectLayout, file_digest
from autonomous_math_research.storage_layer.steering import append_steering, ingest_asset


class StandalonePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"standalone-{uuid4().hex}"
        self.root.mkdir()
        self.project = self.root / "neutral-project"

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _cli(self, args: list[str]) -> tuple[int, dict]:
        output = StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_main(args)
        return code, json.loads(output.getvalue())

    def _init(self) -> None:
        code, payload = self._cli(["init", str(self.project)])
        self.assertEqual(code, 0, payload)

    def test_global_version_option(self) -> None:
        output = StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            build_parser().parse_args(["--version"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "amr 0.2.16")

    def test_init_and_validate_arbitrary_external_project(self) -> None:
        self._init()
        code, result = self._cli(["validate", "--project", str(self.project)])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["valid"])
        self.assertEqual(result["project_id"], "neutral-project")
        self.assertEqual(result["model_turns_started"], 0)

    def test_dry_run_starts_no_model_turn(self) -> None:
        self._init()
        code, result = self._cli([
            "run", "--project", str(self.project), "--mock", "--dry-run",
        ])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["jobs_started"], 0)
        self.assertFalse(result["internal_failure"])

    def test_monitor_persists_attempt_failure_reason_and_paths(self) -> None:
        run_dir = self.root / "monitor-attempt-failure"
        run_dir.mkdir()
        store = EventStore(run_dir / "EVENTS.jsonl", run_dir.name)
        store.append("ATTEMPT_STARTED", {
            "attempt_id": "attempt-1", "campaign_id": "campaign-1",
            "epoch_id": run_dir.name, "mode": "mock",
        })
        store.append("ATTEMPT_FAILED", {
            "attempt_id": "attempt-1", "internal_failure": True,
            "reason": "resume manifest mismatch",
            "report": "E:/reports/NIGHTLY_REPORT.md",
            "outcome": "E:/outcomes/OUTCOME.md",
        })
        output = StringIO()

        code = watch_run(
            run_dir, chat=True, output=output, ui_mode="plain",
            color_mode="never", hold_on_error=False,
        )

        rendered = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("内部失败", rendered)
        self.assertIn("resume manifest mismatch", rendered)
        self.assertIn("E:/reports/NIGHTLY_REPORT.md", rendered)
        self.assertIn("E:/outcomes/OUTCOME.md", rendered)

    def test_monitor_stays_finalizing_until_terminal_artifacts_are_ready(self) -> None:
        run_dir = self.root / "monitor-artifact-finalization"
        run_dir.mkdir()
        store = EventStore(run_dir / "EVENTS.jsonl", run_dir.name)
        store.append("ATTEMPT_STARTED", {
            "attempt_id": "attempt-1", "campaign_id": "campaign-1",
            "epoch_id": run_dir.name, "mode": "mock",
        })
        store.append("RUN_ARTIFACT_FINALIZATION_STARTED", {
            "attempt_id": "attempt-1", "report": "report.md",
            "outcome": "outcome.md",
        })

        self.assertEqual(build_status(run_dir)["state"], "FINALIZING")

        store.append("RUN_ARTIFACT_FINALIZATION_COMPLETED", {
            "attempt_id": "attempt-1", "artifacts_finalized": True,
        })
        store.append("ATTEMPT_COMPLETED", {
            "attempt_id": "attempt-1", "artifacts_finalized": True,
        })
        store.append("RUN_STOPPED", {
            "reason": "epoch time limit reached", "internal_failure": False,
            "artifacts_finalized": True, "report": "report.md",
            "outcome": "outcome.md",
        })
        output = StringIO()
        code = watch_run(
            run_dir, chat=True, output=output, ui_mode="plain",
            color_mode="never", hold_on_error=False,
        )

        self.assertEqual(code, 0)
        self.assertIn("成果归档：完成", output.getvalue())

    def test_new_attempt_does_not_reuse_previous_terminal_artifact_paths(self) -> None:
        run_dir = self.root / "monitor-retried-attempt"
        run_dir.mkdir()
        store = EventStore(run_dir / "EVENTS.jsonl", run_dir.name)
        store.append("RUN_STOPPED", {
            "reason": "old failure", "internal_failure": True,
            "report": "old-report.md", "outcome": "old-outcome.md",
        })
        store.append("ATTEMPT_STARTED", {
            "attempt_id": "attempt-2", "campaign_id": "campaign-1",
            "epoch_id": run_dir.name, "mode": "mock",
        })
        store.append("ATTEMPT_FAILED", {
            "attempt_id": "attempt-2", "internal_failure": True,
            "reason": "new pre-recovery failure", "artifacts_finalized": False,
        })

        status = build_status(run_dir)

        self.assertIsNone(status["report"])
        self.assertIsNone(status["outcome"])
        self.assertFalse(status["artifacts_finalized"])

    def test_terminal_mouse_close_discards_buffered_sgr_input(self) -> None:
        mouse = _TerminalMouseInput()
        mouse.enabled = True
        mouse.parser.pending = "\x1b[<35;141;7M"
        output = StringIO()

        mouse.close(output)

        self.assertFalse(mouse.enabled)
        self.assertEqual(mouse.parser.pending, "")
        self.assertIn("\x1b[?1003l\x1b[?1006l", output.getvalue())

    def test_monitor_distinguishes_command_exit_from_tool_call_failure(self) -> None:
        command_event = {
            "kind": "AGENT_ITEM_COMPLETED",
            "timestamp": "2026-08-24T00:00:00Z",
            "payload": {
                "role": "director", "item_type": "commandExecution",
                "command": "rg missing-file", "status": "failed", "exit_code": 1,
            },
        }
        tool_event = {
            "kind": "AGENT_ITEM_COMPLETED",
            "timestamp": "2026-08-24T00:00:01Z",
            "payload": {
                "role": "director", "item_type": "dynamicToolCall",
                "tool": "example_tool", "status": "failed", "success": False,
            },
        }

        command_line = str(format_live_event(command_event))
        tool_line = str(format_live_event(tool_event))
        self.assertIn("命令未成功", command_line)
        self.assertIn("当前 Agent turn 可继续", command_line)
        self.assertNotIn("工具调用失败", command_line)
        self.assertIn("工具调用失败", tool_line)
        self.assertIn("example_tool 调用失败", tool_line)

    def test_run_keyboard_interrupt_returns_structured_exit_130(self) -> None:
        self._init()
        output = StringIO()
        with (
            patch(
                "autonomous_math_research.cli._run_command",
                side_effect=KeyboardInterrupt,
            ),
            contextlib.redirect_stdout(output),
        ):
            code = cli_main(["run", "--project", str(self.project), "--mock"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 130)
        self.assertTrue(payload["interrupted"])
        self.assertEqual(payload["error_type"], "KeyboardInterrupt")

    def test_campaign_continue_reports_exact_resume_command_for_unsealed_epoch(self) -> None:
        self._init()
        store = CampaignStore(
            ProjectLayout(self.project).autonomous_root, "unsealed-campaign",
        )
        store.create(project_id="neutral-project")
        store.append_epoch_started(
            epoch_id="unsealed-epoch", previous_epoch_id=None, mode="mock",
        )

        code, payload = self._cli([
            "campaign", "continue", "--project", str(self.project),
            "--campaign", "unsealed-campaign",
        ])

        self.assertEqual(code, 2)
        self.assertIn("--resume \"unsealed-epoch\"", payload["error"])

    def test_campaign_continue_rejects_superseded_legacy_ghost(self) -> None:
        self._init()
        store = CampaignStore(
            ProjectLayout(self.project).autonomous_root, "ghost-campaign",
        )
        store.create(project_id="neutral-project")
        store.mark_superseded(
            by_campaign_id="original-campaign",
            epoch_id="ghost-epoch",
            reason="legacy metadata reconciliation",
        )

        code, payload = self._cli([
            "campaign", "continue", "--project", str(self.project),
            "--campaign", "ghost-campaign",
        ])

        self.assertEqual(code, 2)
        self.assertIn("superseded campaign cannot be continued", payload["error"])

    def test_campaign_continue_forwards_a_complete_run_namespace(self) -> None:
        self._init()
        code, first = self._cli([
            "run", "--project", str(self.project), "--dry-run",
        ])
        self.assertEqual(code, 0, first)

        code, continued = self._cli([
            "campaign", "continue", "--project", str(self.project),
            "--campaign", first["campaign_id"], "--dry-run",
        ])

        self.assertEqual(code, 0, continued)
        self.assertEqual(continued["campaign_id"], first["campaign_id"])
        self.assertNotEqual(continued["run_id"], first["run_id"])
        self.assertEqual(continued["jobs_started"], 0)
        manifest = json.loads(
            (
                ProjectLayout(self.project).run_dir(continued["run_id"])
                / "RUN_MANIFEST.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["campaign"]["previous_epoch_id"], first["epoch_id"],
        )

    def test_campaign_continue_forwards_auto_epochs(self) -> None:
        self._init()
        code, first = self._cli([
            "run", "--project", str(self.project), "--dry-run",
        ])
        self.assertEqual(code, 0, first)

        with patch(
            "autonomous_math_research.cli._run_command", return_value=0,
        ) as run_command:
            code = cli_main([
                "campaign", "continue", "--project", str(self.project),
                "--campaign", first["campaign_id"],
                "--run-id", "launcher-reserved-epoch", "--auto-epochs",
            ])

        self.assertEqual(code, 0)
        self.assertTrue(run_command.await_args.args[0].auto_epochs)
        self.assertEqual(
            run_command.await_args.args[0].run_id, "launcher-reserved-epoch",
        )

    def test_run_uses_project_campaign_defaults_and_cli_overrides_them(self) -> None:
        self._init()
        config_path = self.project / "autonomous" / "config.yaml"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["campaign"] = {"hours": 3.0, "epoch_hours": 0.5}
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        code, result = self._cli([
            "run", "--project", str(self.project), "--mock", "--dry-run",
        ])
        self.assertEqual(code, 0, result)
        manifest = json.loads(
            (ProjectLayout(self.project).run_dir(result["run_id"]) / "RUN_MANIFEST.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["campaign"]["campaign_hours"], 3.0)
        self.assertEqual(manifest["campaign"]["epoch_hours"], 0.5)
        self.assertEqual(manifest["execution"]["limits"]["duration_seconds"], 1800.0)

        code, result = self._cli([
            "run", "--project", str(self.project), "--mock", "--dry-run",
            "--hours", "4", "--epoch-hours", "1.5",
        ])
        self.assertEqual(code, 0, result)
        manifest = json.loads(
            (ProjectLayout(self.project).run_dir(result["run_id"]) / "RUN_MANIFEST.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["campaign"]["campaign_hours"], 4.0)
        self.assertEqual(manifest["campaign"]["epoch_hours"], 1.5)
        self.assertEqual(manifest["execution"]["limits"]["duration_seconds"], 5400.0)

    def test_manifest_identity_and_nondefault_runtime_are_authoritative(self) -> None:
        self._init()
        manifest_path = self.project / "autonomous" / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["project_id"] = "declared-project"
        manifest["runtime_root"] = "runtime-data"
        (self.project / "runtime-data").mkdir()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        config_path = self.project / "autonomous" / "config.yaml"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["project"]["name"] = "declared-project"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        code, result = self._cli([
            "run", "--project", str(self.project), "--mock", "--dry-run",
        ])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["project"], "declared-project")
        layout = ProjectLayout(self.project)
        run_dir = layout.run_dir(result["run_id"])
        self.assertTrue(run_dir.is_dir())
        self.assertEqual(resolve_run(self.project, result["run_id"]), run_dir)
        outcome_dir = layout.outcomes_root / result["run_id"]
        intermediate = json.loads(
            (outcome_dir / "INTERMEDIATE_INDEX.json").read_text(encoding="utf-8")
        )
        semantic = json.loads(
            (outcome_dir / "SEMANTIC_INDEX.json").read_text(encoding="utf-8")
        )
        self.assertEqual(intermediate["project"], "declared-project")
        self.assertEqual(semantic["project"], "declared-project")
        catalog = rebuild_catalog(self.project)
        self.assertEqual(catalog["project"], "declared-project")
        self.assertTrue((self.project / "runtime-data" / "catalog").is_dir())

    def test_generic_mock_full_cycle(self) -> None:
        self._init()
        config = load_config(self.project)
        controller = AutonomousController(
            config,
            backend=build_mock_full_cycle_backend(),
            mock=True,
        )
        result = asyncio.run(controller.run(0.01))
        self.assertFalse(result.internal_failure)
        self.assertTrue(controller.final_conjecture_proved)
        self.assertEqual(controller.graph.claims["C_ROOT"].math_status, "PROVED")

    def test_lifecycle_cannot_return_to_running_after_drain(self) -> None:
        lifecycle = MonotoneLifecycle()
        lifecycle.transition(LifecyclePhase.RUNNING, reason="start")
        lifecycle.transition(LifecyclePhase.DRAINING_EPOCH, reason="seal")
        with self.assertRaises(ValueError):
            lifecycle.transition(LifecyclePhase.RUNNING, reason="illegal continuation")

    def test_campaign_ids_cannot_escape_runtime_root(self) -> None:
        runtime = self.root / "runtime"
        with self.assertRaises(ValueError):
            CampaignStore(runtime, "../escape")

    def test_steering_and_asset_ingest_are_append_only_inputs(self) -> None:
        self._init()
        runtime = self.project / "autonomous"
        campaign = CampaignStore(runtime, "campaign-1")
        campaign.create(project_id="neutral-project")
        steering = append_steering(
            runtime, "campaign-1", kind="NOTE", note="Check a finite boundary.",
        )
        source = self.root / "asset.txt"
        source.write_text("bounded input\n", encoding="utf-8")
        asset = ingest_asset(
            runtime, "campaign-1", source, description="local bounded input",
        )
        self.assertEqual(steering["kind"], "NOTE")
        self.assertTrue(asset["uri"].startswith("campaign://campaign-1/"))
        self.assertNotIn(str(source.resolve()), json.dumps(asset))
        with self.assertRaises(ValueError):
            append_steering(
                runtime, "campaign-1", kind="SET_TRUST", note="not allowed",
            )

    def test_bundled_schema_and_policy_resources_have_stable_hashes(self) -> None:
        hashes: list[str] = []
        for name in (
            "director_plan.schema.json",
            "worker_result.schema.json",
            "audit_result.schema.json",
        ):
            with schema_resource(name) as path:
                hashes.append(file_digest(path))
        with policy_resource("scripts/run_worker.py") as path:
            hashes.append(file_digest(path))
        self.assertEqual(len(hashes), len(set(hashes)))
        self.assertTrue(all(len(value) == 64 for value in hashes))

    def test_release_namespaces_are_packages_and_legacy_storage_is_a_wrapper(self) -> None:
        import autonomous_math_research.cli as cli_package
        import autonomous_math_research.mechanical as mechanical_package
        import autonomous_math_research.protocol as protocol_package
        import autonomous_math_research.storage as storage_package
        import autonomous_math_research.storage_layer as legacy_storage

        self.assertEqual(Path(cli_package.__file__).parent.name, "cli")
        self.assertEqual(Path(mechanical_package.__file__).parent.name, "mechanical")
        self.assertEqual(Path(protocol_package.__file__).parent.name, "protocol")
        self.assertEqual(Path(storage_package.__file__).parent.name, "storage")
        self.assertEqual(protocol_package.OUTPUT_PROTOCOL_VERSION, 3)
        self.assertIs(legacy_storage.ArtifactStore, storage_package.ArtifactStore)

    def test_public_distribution_metadata_and_cli_are_stable(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (package_root / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(metadata["name"], "autonomous-math-ai")
        self.assertEqual(metadata["scripts"]["amr"], "autonomous_math_research.cli:main")
        self.assertEqual(metadata["license"], "MIT")
        self.assertEqual(metadata["requires-python"], ">=3.11")
        self.assertEqual(
            metadata["urls"]["Repository"],
            "https://github.com/wizardpc-com/autonomous-math-ai",
        )

    def test_detect_tools_cli_uses_an_explicit_project_root(self) -> None:
        args = build_parser().parse_args([
            "detect-tools", "--project-root", str(self.project),
        ])
        self.assertEqual(args.command, "detect-tools")
        self.assertEqual(args.project_root, self.project)

    def test_mechanical_runner_has_no_source_checkout_import_fallback(self) -> None:
        with policy_resource("scripts/run_worker.py") as runner:
            text = runner.read_text(encoding="utf-8")
        self.assertNotIn("tools" + ".autonomous_math_research", text)
        self.assertNotIn("sys.path.insert(0, str(REPO_ROOT))", text)

    def test_public_markdown_relative_links_resolve(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        documents = [
            package_root / "README.md",
            package_root / "README.zh-CN.md",
            package_root / "CONTRIBUTING.md",
            package_root / "SECURITY.md",
            *(package_root / "docs").glob("*.md"),
        ]
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                path_text = target.split("#", 1)[0]
                resolved = (document.parent / path_text).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"broken relative link in {document.name}: {target}",
                )

    def test_standalone_source_contains_no_conjecture_fixture(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        roots = [
            package_root / "src",
            package_root / "docs",
            package_root / "templates",
            package_root / ".github",
            package_root / "scripts",
            package_root / "tests",
        ]
        standalone_files = [
            package_root / "README.md",
            package_root / "README.zh-CN.md",
            package_root / "pyproject.toml",
            package_root / "MANIFEST.in",
            package_root / "LICENSE",
            package_root / ".gitignore",
            package_root / "CHANGELOG.md",
            package_root / "CONTRIBUTING.md",
            package_root / "CODE_OF_CONDUCT.md",
            package_root / "SECURITY.md",
            package_root / "CITATION.cff",
            package_root / "amr-launcher.cmd",
        ]
        release_paths = [
            *standalone_files,
            *(item for root in roots for item in root.rglob("*")),
        ]
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in release_paths
            if (
                path.is_file()
                and "__pycache__" not in path.parts
                and "_runtime" not in path.parts
                and path.suffix not in {".pyc", ".whl", ".gz"}
            )
        )
        forbidden = (
            "Tree-" + "CSF",
            "tree-" + "chromatic",
            "Casas" + "-Alvero",
            "R" + "23",
            "S" + "3",
            "S" + "4",
            "S" + "6",
            "S" + "7",
            "E:" + "\\math-ai-research",
            "projects" + "/",
            "tools" + ".autonomous_math_research",
        )
        for marker in forbidden:
            self.assertNotIn(marker, text)
        path_text = "\n".join(path.as_posix() for path in release_paths)
        for marker in forbidden[:-2]:
            self.assertNotIn(marker, path_text)

        # A repository-local discovery entry is allowed, but the installable
        # implementation must never import or resolve runtime resources from it.
        source_text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (package_root / "src").rglob("*")
            if path.is_file() and path.suffix == ".py"
        )
        self.assertNotIn("." + "agents", source_text)

    def test_windows_launcher_is_single_file_and_safe_by_default(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        launcher = package_root / "amr-launcher.cmd"
        text = launcher.read_text(encoding="utf-8")
        manifest = (package_root / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("include amr-launcher.cmd", manifest)
        self.assertNotIn("AMR_PROJECT_ROOT=", text)
        self.assertNotIn("AMR_PROFILE=", text)
        self.assertIn('set "AMR_HARNESS_ROOT=%~dp0."', text)
        self.assertIn('if not defined AMR_REFRESH_HARNESS set "AMR_REFRESH_HARNESS=0"', text)
        self.assertNotIn('if not defined AMR_REFRESH_HARNESS set "AMR_REFRESH_HARNESS=1"', text)
        self.assertIn("call :ensure_venv_idle", text)
        self.assertIn("Get-Process -ErrorAction SilentlyContinue", text)
        self.assertIn("Refusing to install or upgrade", text)
        self.assertIn('"%AMR_EXE%" launcher %*', text)
        self.assertNotRegex(text, r"sk-[A-Za-z0-9_-]{16,}")
        self.assertNotIn("OPENAI_API_KEY=", text)


if __name__ == "__main__":
    unittest.main()
