from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.config import load_config
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.launcher import (
    COMMON_OVERRIDE_PATHS,
    LauncherProject,
    find_unfinished_campaigns,
    _monitor_command,
    _open_monitor_window,
    format_config_summary,
    load_launcher_state,
    override_path_allowed,
    parse_override_assignment,
    run_launcher,
    save_launcher_state,
    scan_workspace,
    temporary_profile,
)
from autonomous_math_research.lifecycle.campaign import CampaignStore


RUNTIME = Path(__file__).resolve().parent / "_runtime"


class UnifiedLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME / f"launcher-{uuid4().hex}"
        self.project = self.root / "projects" / "alpha"
        self.other = self.root / "projects" / "beta"
        initialize_project(self.project, project_id="alpha", final_claim_id="A_FINAL")
        initialize_project(self.other, project_id="beta", final_claim_id="B_FINAL")
        self.state = self.root / "user-state" / "launcher.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    @staticmethod
    def _inputs(values: list[str]):
        iterator = iter(values)
        return lambda _prompt="": next(iterator)

    def _campaign(
        self,
        campaign_id: str,
        *,
        epoch_id: str,
        mode: str,
        sealed: bool,
        created_at: str,
    ) -> CampaignStore:
        runtime = self.project / "autonomous"
        store = CampaignStore(runtime, campaign_id)
        store.create(
            project_id="alpha", campaign_hours=2.0, epoch_hours=1.0,
        )
        manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = created_at
        store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        store.append_epoch_started(
            epoch_id=epoch_id, previous_epoch_id=None, mode=mode,
        )
        if sealed:
            checkpoint = runtime / "runs" / epoch_id / "state" / "compact_snapshot.json"
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text("{}\n", encoding="utf-8")
            store.append_epoch_sealed(
                epoch_id=epoch_id,
                elapsed_seconds=60.0,
                status="PAUSED",
                stopped_reason="epoch time limit reached",
                checkpoint_uri=f"epoch://{epoch_id}/state/compact_snapshot.json",
            )
        return store

    def test_fallback_scan_excludes_nested_repo_runtime_and_reports_invalid(self) -> None:
        nested = self.root / "tools" / "nested-harness"
        (nested / ".git").mkdir(parents=True)
        initialize_project(
            nested / "templates" / "minimal-project",
            project_id="template-project", final_claim_id="T_FINAL",
        )
        initialize_project(
            self.root / "runs" / "runtime-copy",
            project_id="runtime-copy", final_claim_id="R_FINAL",
        )
        invalid = self.root / "broken" / "autonomous"
        invalid.mkdir(parents=True)
        (invalid / "project.json").write_text("{}\n", encoding="utf-8")

        with patch(
            "autonomous_math_research.launcher._git_manifest_paths",
            return_value=None,
        ):
            projects, issues = scan_workspace(self.root)

        self.assertEqual([item.project_id for item in projects], ["alpha", "beta"])
        self.assertTrue(any("broken" in item for item in issues))

    def test_duplicate_project_ids_are_rejected(self) -> None:
        duplicate = self.root / "projects" / "duplicate"
        initialize_project(duplicate, project_id="alpha", final_claim_id="D_FINAL")
        with patch(
            "autonomous_math_research.launcher._git_manifest_paths",
            return_value=None,
        ):
            projects, issues = scan_workspace(self.root)
        self.assertEqual([item.project_id for item in projects], ["beta"])
        self.assertTrue(any("duplicate project_id 'alpha'" in item for item in issues))

    def test_launcher_state_contains_only_workspace_and_last_project(self) -> None:
        save_launcher_state(self.root, "alpha", self.state)
        raw = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(
            set(raw), {"schema_version", "workspace_root", "last_project_id"},
        )
        self.assertNotIn("provider", json.dumps(raw))
        self.assertEqual(load_launcher_state(self.state)["last_project_id"], "alpha")

    def test_override_parser_accepts_simple_fields_and_blocks_trust_fields(self) -> None:
        path, value = parse_override_assignment(
            'models.prover.effort="xhigh"'
        )
        self.assertEqual(path, "models.prover.effort")
        self.assertEqual(value, "xhigh")
        path, value = parse_override_assignment(
            "scheduler.max_mechanical_subworkers=null"
        )
        self.assertIsNone(value)
        path, value = parse_override_assignment(
            "engine.research_max_turns.prover=16"
        )
        self.assertEqual((path, value), ("engine.research_max_turns.prover", 16))
        self.assertFalse(override_path_allowed("workspace.protected_paths"))
        self.assertFalse(override_path_allowed("providers.codex.credential.reference"))
        with self.assertRaisesRegex(ValueError, "not eligible"):
            parse_override_assignment("audit.critical_double_audit=false")
        with self.assertRaisesRegex(ValueError, "JSON syntax"):
            parse_override_assignment("models.prover.effort=xhigh")

        common_paths = {path for path, _label in COMMON_OVERRIDE_PATHS}
        self.assertIn("models.prover.retries.transport", common_paths)
        self.assertIn("models.auditor.cost_limit_usd", common_paths)
        self.assertIn("engine.research_max_turns.prover", common_paths)
        self.assertIn(
            "policy.one_shot_compute_worker.primary_route.model", common_paths,
        )
        self.assertIn(
            "policy.one_shot_compute_worker.selection_policy.mode", common_paths,
        )

    def test_temporary_profile_validates_and_is_always_removed(self) -> None:
        override = {"campaign": {"hours": 24}}
        with temporary_profile(override) as profile:
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertTrue(profile.is_file())
            config = load_config(self.project, profile_path=profile)
            self.assertEqual(config.campaign_hours, 24.0)
            profile_path = profile
        self.assertFalse(profile_path.exists())

    def test_real_confirmation_requires_project_id_and_starts_nothing_on_mismatch(self) -> None:
        commands: list[list[str]] = []
        result = run_launcher(
            project_root=self.project,
            action="real",
            state_path=self.state,
            input_fn=self._inputs(["", "RUN wrong-project"]),
            output=lambda _text: None,
            command_runner=lambda args: commands.append(list(args)) or 0,
        )
        self.assertEqual(result, 0)
        self.assertEqual(commands, [])

    def test_one_time_override_reaches_run_then_temp_profile_is_removed(self) -> None:
        profiles: list[Path] = []

        def runner(arguments):  # type: ignore[no-untyped-def]
            args = list(arguments)
            self.assertIn("--dry-run", args)
            self.assertIn("--run-id", args)
            run_id = args[args.index("--run-id") + 1]
            self.assertRegex(run_id, r"^\d{8}T\d{6}\.\d{6}Z$")
            profile = Path(args[args.index("--profile") + 1])
            self.assertTrue(profile.is_file())
            self.assertEqual(
                load_config(self.project, profile_path=profile).campaign_hours,
                24.0,
            )
            profiles.append(profile)
            return 0

        result = run_launcher(
            project_root=self.project,
            action="dry-run",
            state_path=self.state,
            input_fn=self._inputs(["o", "campaign.hours=24", ""]),
            output=lambda _text: None,
            command_runner=runner,
        )
        self.assertEqual(result, 0)
        self.assertEqual(len(profiles), 1)
        self.assertFalse(profiles[0].exists())

    def test_monitor_command_waits_for_the_exact_launcher_run(self) -> None:
        command = _monitor_command(self.project, "20260820T010203.123456Z")
        self.assertIn("watch", command)
        self.assertEqual(command[command.index("--run") + 1], "20260820T010203.123456Z")
        self.assertEqual(command[command.index("--wait-seconds") + 1], "60")
        self.assertIn("--chat", command)

    def test_windows_monitor_opens_a_separate_console_without_blocking_run(self) -> None:
        messages: list[str] = []
        with (
            patch("autonomous_math_research.launcher.os.name", "nt"),
            patch("autonomous_math_research.launcher.subprocess.Popen") as popen,
        ):
            opened = _open_monitor_window(
                self.project, "20260820T010203.123456Z", messages.append,
            )
        self.assertTrue(opened)
        command = popen.call_args.args[0]
        self.assertIn("/k", command)
        self.assertIn("20260820T010203.123456Z", command)
        self.assertTrue(popen.call_args.kwargs["creationflags"])
        self.assertTrue(any("监视窗口已启动" in item for item in messages))

    def test_workspace_selection_routes_validate_without_model(self) -> None:
        commands: list[list[str]] = []
        with patch(
            "autonomous_math_research.launcher._git_manifest_paths",
            return_value=None,
        ):
            result = run_launcher(
                workspace_root=self.root,
                action="validate",
                state_path=self.state,
                input_fn=self._inputs(["1"]),
                output=lambda _text: None,
                command_runner=lambda args: commands.append(list(args)) or 0,
            )
        self.assertEqual(result, 0)
        self.assertEqual(commands[0][:2], ["validate", "--project"])
        self.assertEqual(load_launcher_state(self.state)["last_project_id"], "alpha")

    def test_unfinished_campaigns_are_newest_first_and_distinguish_recovery(self) -> None:
        older = self._campaign(
            "older-sealed", epoch_id="older-epoch", mode="real", sealed=True,
            created_at="2026-08-25T00:00:00Z",
        )
        newer = self._campaign(
            "newer-active", epoch_id="newer-epoch", mode="mock", sealed=False,
            created_at="2026-08-26T00:00:00Z",
        )
        completed = self._campaign(
            "newest-completed", epoch_id="completed-epoch", mode="real", sealed=True,
            created_at="2026-08-27T00:00:00Z",
        )
        self._campaign(
            "newest-dry-run", epoch_id="dry-run-epoch", mode="dry-run", sealed=True,
            created_at="2026-08-28T00:00:00Z",
        )
        completed_manifest = json.loads(
            completed.manifest_path.read_text(encoding="utf-8")
        )
        completed_manifest["status"] = "COMPLETED"
        completed.manifest_path.write_text(
            json.dumps(completed_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        project = LauncherProject(
            "alpha", "A_FINAL", self.project,
            self.project / "autonomous" / "config.yaml",
        )
        campaigns, issues = find_unfinished_campaigns(project)

        self.assertEqual(issues, [])
        self.assertEqual(
            [item.campaign_id for item in campaigns],
            [newer.campaign_id, older.campaign_id],
        )
        self.assertEqual(campaigns[0].continuation_kind, "resume")
        self.assertEqual(campaigns[0].epoch_id, "newer-epoch")
        self.assertEqual(campaigns[1].continuation_kind, "continue")
        self.assertEqual(campaigns[1].epoch_id, "older-epoch")

    def test_interactive_launcher_prompts_and_resumes_latest_unsealed_campaign(self) -> None:
        self._campaign(
            "active-mock", epoch_id="active-epoch", mode="mock", sealed=False,
            created_at="2026-08-26T00:00:00Z",
        )
        commands: list[list[str]] = []
        messages: list[str] = []

        result = run_launcher(
            project_root=self.project,
            state_path=self.state,
            input_fn=self._inputs(["8", "", "0"]),
            output=messages.append,
            command_runner=lambda args: commands.append(list(args)) or 0,
        )

        self.assertEqual(result, 0)
        self.assertTrue(any("检测到上一轮未完成 campaign" in item for item in messages))
        self.assertEqual(commands[0][:3], ["run", "--project", str(self.project)])
        self.assertEqual(
            commands[0][commands[0].index("--resume") + 1], "active-epoch",
        )
        self.assertIn("--auto-epochs", commands[0])
        self.assertIn("--mock", commands[0])

    def test_direct_continue_action_confirms_real_sealed_campaign(self) -> None:
        self._campaign(
            "paused-real", epoch_id="sealed-epoch", mode="real", sealed=True,
            created_at="2026-08-26T00:00:00Z",
        )
        commands: list[list[str]] = []

        result = run_launcher(
            project_root=self.project,
            action="continue",
            state_path=self.state,
            input_fn=self._inputs(["CONTINUE paused-real"]),
            output=lambda _text: None,
            command_runner=lambda args: commands.append(list(args)) or 0,
        )

        self.assertEqual(result, 0)
        self.assertEqual(commands[0][:2], ["campaign", "continue"])
        self.assertEqual(
            commands[0][commands[0].index("--campaign") + 1], "paused-real",
        )
        self.assertRegex(
            commands[0][commands[0].index("--run-id") + 1],
            r"^\d{8}T\d{6}\.\d{6}Z$",
        )
        self.assertIn("--auto-epochs", commands[0])
        self.assertNotIn("--mock", commands[0])

    def test_summary_contains_routes_but_no_secret_values(self) -> None:
        summary = format_config_summary(load_config(self.project))
        self.assertIn("alpha", summary)
        self.assertIn("prover", summary)
        self.assertIn("gpt-5.6-sol", summary)
        self.assertNotIn("sk-", summary)


if __name__ == "__main__":
    unittest.main()
