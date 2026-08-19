from __future__ import annotations

import asyncio
import contextlib
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.cli import main as cli_main
from autonomous_math_research.catalog import rebuild_catalog
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import (
    AutonomousController,
    build_mock_full_cycle_backend,
)
from autonomous_math_research.lifecycle.campaign import CampaignStore
from autonomous_math_research.lifecycle.state import LifecyclePhase, MonotoneLifecycle
from autonomous_math_research.resources import policy_resource, schema_resource
from autonomous_math_research.monitor import resolve_run
from autonomous_math_research.storage import ProjectLayout, file_digest
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
        config_path = self.project / "autonomous" / "config.json"
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
        self.assertEqual(protocol_package.OUTPUT_PROTOCOL_VERSION, 2)
        self.assertIs(legacy_storage.ArtifactStore, storage_package.ArtifactStore)

    def test_standalone_source_contains_no_conjecture_fixture(self) -> None:
        package_root = Path(__file__).resolve().parents[1]
        roots = [
            package_root / "src",
            package_root / "docs",
            package_root / "templates",
        ]
        standalone_files = [
            package_root / "README.md",
            package_root / "pyproject.toml",
            package_root / "MANIFEST.in",
            package_root / "LICENSE",
        ]
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in [
                *standalone_files,
                *(item for root in roots for item in root.rglob("*")),
            ]
            if (
                path.is_file()
                and "__pycache__" not in path.parts
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
        )
        for marker in forbidden:
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
