from __future__ import annotations

from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sys
import unittest

from autonomous_math_research.experiment import (
    ExperimentExecution,
    ExperimentExecutionRequest,
    ExperimentManifest,
    ExperimentRunner,
)
from autonomous_math_research.cli import main as cli_main
from autonomous_math_research.storage import ProjectLayout, file_digest

from support import TempProjectMixin


class _FakeDockerAdapter:
    kind = "docker"
    deterministic = True
    uses_llm = False

    def execute(self, request: ExperimentExecutionRequest) -> ExperimentExecution:
        request.stdout_path.write_bytes(b"docker seam\n")
        request.stderr_path.write_bytes(b"")
        return ExperimentExecution(
            termination="EXITED",
            exit_code=0,
            wall_seconds=0.0,
            infrastructure_failure=None,
        )


class ExperimentRunnerTests(TempProjectMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.protocol = self.project / "experiments" / "frozen-protocol.txt"
        self.protocol.write_text("protocol-v1\n", encoding="utf-8", newline="\n")

    def _manifest_data(
        self,
        cases: list[dict[str, object]],
        *,
        experiment_id: str = "toy-experiment",
        adapter: dict[str, object] | None = None,
        timeout_seconds: float = 5.0,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "protocol_version": "frozen-v1",
            "adapter": adapter or {"kind": "subprocess", "config": {}},
            "timeout_seconds": timeout_seconds,
            "inputs": [{
                "path": "experiments/frozen-protocol.txt",
                "sha256": file_digest(self.protocol),
            }],
            "config": {"seed": 7, "benchmark_order": "declared"},
            "versions": {"python": sys.version.split()[0], "toy": "1.0"},
            "resource_metadata": {"worker_slots": 1},
            "cost_metadata": {"billing": "none", "llm_budget": 0},
            "cases": cases,
        }

    def _write_manifest(self, data: dict[str, object], name: str = "manifest.json") -> Path:
        path = self.project / "experiments" / name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return path

    @staticmethod
    def _case(case_id: str, code: str) -> dict[str, object]:
        return {
            "case_id": case_id,
            "argv": [sys.executable, "-c", code],
            "cwd": ".",
        }

    @staticmethod
    def _ledger(path: Path) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_batch_outputs_are_raw_uninterpreted_and_content_addressed(self) -> None:
        manifest_path = self._write_manifest(self._manifest_data([
            self._case(
                "alpha",
                "import sys; print('alpha-out'); print('alpha-err', file=sys.stderr)",
            ),
            self._case("exit-seven", "import sys; print('seven'); sys.exit(7)"),
        ]))
        manifest = ExperimentManifest.load(self.project, manifest_path)
        runner = ExperimentRunner(self.project)
        summary = runner.run(manifest)

        runtime_root = ProjectLayout(self.project).experiments_root.resolve()
        self.assertTrue(summary.root.resolve().is_relative_to(runtime_root))
        self.assertFalse(
            summary.root.resolve().is_relative_to((self.project / "experiments").resolve())
        )
        self.assertEqual(summary.executed_case_ids, ("alpha", "exit-seven"))
        self.assertEqual(summary.infrastructure_failure_case_ids, ())
        self.assertEqual(summary.run_id, f"run-{summary.experiment_fingerprint}")

        run_manifest = json.loads(
            (summary.root / "RUN_MANIFEST.json").read_text(encoding="utf-8")
        )
        checkpoint = json.loads(
            (summary.root / "CHECKPOINT.json").read_text(encoding="utf-8")
        )
        self.assertFalse(run_manifest["llm_execution_allowed"])
        self.assertEqual(checkpoint["run_fingerprint"], summary.experiment_fingerprint)
        self.assertEqual(checkpoint["verified_terminal_case_count"], 2)
        self.assertEqual(
            run_manifest["provenance"]["inputs"][0]["sha256"],
            file_digest(self.protocol),
        )
        expected_config_hash = sha256(json.dumps(
            {"benchmark_order": "declared", "seed": 7},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.assertEqual(run_manifest["provenance"]["config_sha256"], expected_config_hash)

        ledger = self._ledger(summary.root / "RAW_RESULTS.jsonl")
        self.assertEqual([item["sequence"] for item in ledger], [1, 2])
        records = [
            json.loads((summary.root / str(item["result_path"])).read_text(encoding="utf-8"))
            for item in ledger
        ]
        self.assertEqual(
            [record["research_result"] for record in records],
            [{"status": "UNINTERPRETED"}, {"status": "UNINTERPRETED"}],
        )
        self.assertEqual(records[0]["execution"]["exit_code"], 0)
        self.assertEqual(records[1]["execution"]["exit_code"], 7)
        self.assertIsNone(records[1]["execution"]["infrastructure_failure"])
        self.assertFalse(records[0]["command"]["shell"])
        self.assertEqual(records[0]["cost"]["llm_calls"], 0)
        stdout = summary.root / str(ledger[0]["stdout_path"])
        stderr = summary.root / str(ledger[0]["stderr_path"])
        expected_stdout = (
            b"alpha-out\r\n" if sys.platform == "win32" else b"alpha-out\n"
        )
        self.assertEqual(stdout.read_bytes(), expected_stdout)
        self.assertIn(b"alpha-err", stderr.read_bytes())

    def test_resume_never_reruns_or_overwrites_completed_raw_cases(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case("one", "print('one')"),
            self._case("two", "print('two')"),
        ]))
        runner = ExperimentRunner(self.project)
        first = runner.run(path)
        ledger_path = first.root / "RAW_RESULTS.jsonl"
        before_ledger = ledger_path.read_bytes()
        before = {
            item["stdout_path"]: file_digest(first.root / str(item["stdout_path"]))
            for item in self._ledger(ledger_path)
        }

        resumed = runner.run(path, resume=True)

        self.assertEqual(resumed.run_id, first.run_id)
        self.assertEqual(resumed.executed_case_ids, ())
        self.assertEqual(resumed.resumed_case_ids, ("one", "two"))
        self.assertEqual(resumed.recovered_case_ids, ())
        self.assertEqual(ledger_path.read_bytes(), before_ledger)
        self.assertEqual({
            item["stdout_path"]: file_digest(first.root / str(item["stdout_path"]))
            for item in self._ledger(ledger_path)
        }, before)
        with self.assertRaises(FileExistsError):
            runner.run(path)

    def test_resume_recovers_a_durable_case_record_missing_only_ledger_tail(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case("one", "print('one')"),
            self._case("two", "print('two')"),
        ]))
        runner = ExperimentRunner(self.project)
        first = runner.run(path)
        ledger_path = first.root / "RAW_RESULTS.jsonl"
        lines = ledger_path.read_text(encoding="utf-8").splitlines(keepends=True)
        second = json.loads(lines[1])
        second_stdout = first.root / str(second["stdout_path"])
        digest = file_digest(second_stdout)
        ledger_path.write_text(lines[0], encoding="utf-8", newline="")

        resumed = runner.run(path, resume=True)

        self.assertEqual(resumed.executed_case_ids, ())
        self.assertEqual(resumed.resumed_case_ids, ("one",))
        self.assertEqual(resumed.recovered_case_ids, ("two",))
        self.assertEqual(file_digest(second_stdout), digest)
        self.assertEqual(len(self._ledger(ledger_path)), 2)
        checkpoint = json.loads(
            (first.root / "CHECKPOINT.json").read_text(encoding="utf-8")
        )
        self.assertEqual(checkpoint["verified_terminal_case_count"], 2)
        self.assertEqual(
            checkpoint["raw_ledger_sha256"], file_digest(ledger_path)
        )

    def test_resume_rebuilds_deleted_or_stale_derived_checkpoint_only(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case("one", "print('one')"),
        ]))
        runner = ExperimentRunner(self.project)
        first = runner.run(path)
        checkpoint_path = first.root / "CHECKPOINT.json"
        ledger_path = first.root / "RAW_RESULTS.jsonl"
        entry = self._ledger(ledger_path)[0]
        stdout_path = first.root / str(entry["stdout_path"])
        raw_before = {
            "ledger": file_digest(ledger_path),
            "stdout": file_digest(stdout_path),
            "result": file_digest(first.root / str(entry["result_path"])),
        }

        checkpoint_path.unlink()
        runner.run(path, resume=True)
        rebuilt = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt["verified_terminal_case_count"], 1)
        self.assertEqual(rebuilt["raw_ledger_sha256"], raw_before["ledger"])

        checkpoint_path.write_text(
            json.dumps({"schema_version": 1, "verified_terminal_case_count": 0}),
            encoding="utf-8",
        )
        runner.run(path, resume=True)
        repaired = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired, rebuilt)
        self.assertEqual({
            "ledger": file_digest(ledger_path),
            "stdout": file_digest(stdout_path),
            "result": file_digest(first.root / str(entry["result_path"])),
        }, raw_before)

    def test_resume_rejects_modified_raw_artifact(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case("one", "print('original')"),
        ]))
        runner = ExperimentRunner(self.project)
        first = runner.run(path)
        entry = self._ledger(first.root / "RAW_RESULTS.jsonl")[0]
        artifact = first.root / str(entry["stdout_path"])
        artifact.write_bytes(b"analysis overwrote raw evidence\n")

        with self.assertRaisesRegex(ValueError, "artifact digest mismatch"):
            runner.run(path, resume=True)

    def test_timeout_and_launch_failure_are_infrastructure_not_research_results(self) -> None:
        missing = str((self.project / "does-not-exist-command").resolve())
        path = self._write_manifest(self._manifest_data([
            {"case_id": "missing", "argv": [missing], "cwd": "."},
            self._case("timeout", "import time; time.sleep(2)"),
            self._case("after-failures", "print('still-ran')"),
        ], timeout_seconds=0.05))

        summary = ExperimentRunner(self.project).run(path)

        self.assertEqual(
            summary.infrastructure_failure_case_ids, ("missing", "timeout")
        )
        ledger = self._ledger(summary.root / "RAW_RESULTS.jsonl")
        records = {
            str(item["case_id"]): json.loads(
                (summary.root / str(item["result_path"])).read_text(encoding="utf-8")
            )
            for item in ledger
        }
        self.assertEqual(records["missing"]["execution"]["termination"], "LAUNCH_FAILED")
        self.assertEqual(records["timeout"]["execution"]["termination"], "TIMED_OUT")
        self.assertEqual(records["after-failures"]["execution"]["termination"], "EXITED")
        for record in records.values():
            self.assertEqual(record["research_result"]["status"], "UNINTERPRETED")

    def test_frozen_input_mutation_is_recorded_then_stops_the_batch(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case(
                "mutator",
                "from pathlib import Path; "
                "Path('experiments/frozen-protocol.txt').write_text('changed')",
            ),
            self._case("must-not-run", "print('should not run')"),
        ]))
        manifest = ExperimentManifest.load(self.project, path)
        run_manifest = ExperimentRunner._build_run_manifest(manifest)
        root = ProjectLayout(self.project).experiments_root / str(run_manifest["run_id"])

        with self.assertRaisesRegex(RuntimeError, "input changed"):
            ExperimentRunner(self.project).run(manifest)

        ledger = self._ledger(root / "RAW_RESULTS.jsonl")
        self.assertEqual([item["case_id"] for item in ledger], ["mutator"])
        record = json.loads(
            (root / str(ledger[0]["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(record["execution"]["termination"], "INPUT_MUTATED")
        self.assertEqual(record["research_result"]["status"], "UNINTERPRETED")

    def test_strict_manifest_rejects_unknown_fields_bad_hash_and_duplicate_json_key(self) -> None:
        data = self._manifest_data([self._case("one", "print('one')")])
        data["unexpected"] = True
        path = self._write_manifest(data)
        with self.assertRaisesRegex(ValueError, "fields differ"):
            ExperimentManifest.load(self.project, path)

        data.pop("unexpected")
        data["inputs"][0]["sha256"] = "0" * 64  # type: ignore[index]
        path = self._write_manifest(data)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            ExperimentManifest.load(self.project, path)

        path.write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            ExperimentManifest.load(self.project, path)

    def test_docker_is_an_injected_non_llm_adapter_seam(self) -> None:
        adapter = {
            "kind": "docker",
            "config": {"image": "toy@sha256:" + "a" * 64},
        }
        path = self._write_manifest(self._manifest_data([
            {"case_id": "container-case", "argv": ["toy-check"], "cwd": "."},
        ], adapter=adapter))
        with self.assertRaisesRegex(ValueError, "no deterministic adapter"):
            ExperimentRunner(self.project).run(path)

        summary = ExperimentRunner(
            self.project, docker_adapter=_FakeDockerAdapter()
        ).run(path)
        ledger = self._ledger(summary.root / "RAW_RESULTS.jsonl")
        self.assertEqual(
            (summary.root / str(ledger[0]["stdout_path"])).read_bytes(),
            b"docker seam\n",
        )

    def test_cli_validates_runs_and_resumes_the_same_frozen_batch(self) -> None:
        path = self._write_manifest(self._manifest_data([
            self._case("cli-case", "print('cli raw output')"),
        ]))
        manifest = path.relative_to(self.project).as_posix()

        output = StringIO()
        with redirect_stdout(output):
            code = cli_main([
                "experiment", "validate", "--project", str(self.project),
                "--manifest", manifest,
            ])
        self.assertEqual(code, 0)
        validated = json.loads(output.getvalue())
        self.assertTrue(validated["valid"])
        self.assertFalse(validated["llm_execution_allowed"])

        output = StringIO()
        with redirect_stdout(output):
            code = cli_main([
                "experiment", "run", "--project", str(self.project),
                "--manifest", manifest,
            ])
        self.assertEqual(code, 0)
        executed = json.loads(output.getvalue())
        self.assertEqual(executed["executed_case_ids"], ["cli-case"])
        self.assertFalse(executed["research_result_interpreted"])

        output = StringIO()
        with redirect_stdout(output):
            code = cli_main([
                "experiment", "run", "--project", str(self.project),
                "--manifest", manifest, "--resume",
            ])
        self.assertEqual(code, 0)
        resumed = json.loads(output.getvalue())
        self.assertEqual(resumed["run_id"], executed["run_id"])
        self.assertEqual(resumed["executed_case_ids"], [])
        self.assertEqual(resumed["resumed_case_ids"], ["cli-case"])


if __name__ == "__main__":
    unittest.main()
