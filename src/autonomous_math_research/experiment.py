from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Protocol

from .models import utc_now
from .storage import ProjectLayout, atomic_write_json, file_digest


EXPERIMENT_MANIFEST_SCHEMA_VERSION = 1
EXPERIMENT_RUN_SCHEMA_VERSION = 2
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = frozenset({
    "schema_version", "experiment_id", "protocol_version", "adapter",
    "timeout_seconds", "inputs", "config", "versions",
    "resource_metadata", "cost_metadata", "cases",
})
_ADAPTER_KEYS = frozenset({"kind", "config"})
_INPUT_KEYS = frozenset({"path", "sha256"})
_CASE_KEYS = frozenset({"case_id", "argv", "cwd"})
_LEDGER_KEYS = frozenset({
    "schema_version", "sequence", "run_id", "experiment_fingerprint",
    "case_id", "case_run_id", "case_fingerprint", "result_path",
    "result_sha256", "stdout_path", "stdout_sha256", "stderr_path",
    "stderr_sha256",
})
_RESULT_KEYS = frozenset({
    "schema_version", "experiment_id", "run_id", "experiment_fingerprint",
    "case_id", "case_run_id", "case_fingerprint", "started_at",
    "finished_at", "command", "execution", "research_result", "provenance",
    "resource", "cost", "artifacts",
})
_TERMINATIONS = frozenset({
    "EXITED", "TIMED_OUT", "LAUNCH_FAILED", "ADAPTER_FAILED",
    "INPUT_MUTATED",
})


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _loads_json(payload: str, label: str) -> Any:
    try:
        return json.loads(
            payload,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON in {label}: {exc}") from exc


def _canonical_json_bytes(value: Any, *, pretty: bool = False) -> bytes:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if pretty:
        options["indent"] = 2
        return (json.dumps(value, **options) + "\n").encode("utf-8")
    options["separators"] = (",", ":")
    return json.dumps(value, **options).encode("utf-8")


def _json_hash(value: Any) -> str:
    return sha256(_canonical_json_bytes(value)).hexdigest()


def _copy_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        encoded = _canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain only finite JSON values") from exc
    copied = _loads_json(encoded.decode("utf-8"), label)
    if not isinstance(copied, dict):
        raise ValueError(f"{label} must be an object")
    return copied


def _require_exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a portable identifier")
    return value


def _portable_relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty project-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError(f"{label} must be a normalized project-relative POSIX path")
    if path.parts and path.parts[0].endswith(":"):
        raise ValueError(f"{label} must not contain a drive prefix")
    return value


def _exclusive_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if os.path.exists(path):
            os.unlink(path)
        raise


def _exclusive_write_json(path: Path, value: Any) -> None:
    _exclusive_write(path, _canonical_json_bytes(value, pretty=True))


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(
        value, ensure_ascii=False, sort_keys=True
    ) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class ExperimentInput:
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ExperimentCase:
    case_id: str
    argv: tuple[str, ...]
    cwd: str

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "argv": list(self.argv), "cwd": self.cwd}


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    project_root: Path
    path: Path
    experiment_id: str
    protocol_version: str
    adapter_kind: str
    adapter_config: dict[str, Any]
    timeout_seconds: float
    inputs: tuple[ExperimentInput, ...]
    config: dict[str, Any]
    versions: dict[str, str]
    resource_metadata: dict[str, Any]
    cost_metadata: dict[str, Any]
    cases: tuple[ExperimentCase, ...]

    @classmethod
    def load(cls, project_root: Path, path: Path) -> "ExperimentManifest":
        root = project_root.resolve()
        raw_path = path if path.is_absolute() else root / path
        source = raw_path.resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ValueError("experiment manifest must be a file inside the project")
        raw = _loads_json(source.read_text(encoding="utf-8"), str(source))
        raw = _require_exact_keys(raw, _MANIFEST_KEYS, "experiment manifest")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise ValueError("unsupported experiment manifest schema_version")

        experiment_id = _identifier(raw["experiment_id"], "experiment_id")
        protocol_version = raw["protocol_version"]
        if not isinstance(protocol_version, str) or not protocol_version.strip():
            raise ValueError("protocol_version must be a non-empty string")

        adapter = _require_exact_keys(raw["adapter"], _ADAPTER_KEYS, "adapter")
        adapter_kind = str(adapter["kind"])
        if adapter_kind not in {"subprocess", "docker"}:
            raise ValueError(f"unsupported experiment adapter: {adapter_kind}")
        adapter_config = _copy_json_object(adapter["config"], "adapter.config")
        if adapter_kind == "subprocess" and adapter_config:
            raise ValueError("subprocess adapter config must be empty")

        timeout = raw["timeout_seconds"]
        if type(timeout) not in {int, float} or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout_seconds must be a positive finite number")

        raw_inputs = raw["inputs"]
        if not isinstance(raw_inputs, list):
            raise ValueError("inputs must be an array")
        inputs: list[ExperimentInput] = []
        for index, item in enumerate(raw_inputs):
            item = _require_exact_keys(item, _INPUT_KEYS, f"inputs[{index}]")
            relative = _portable_relative(item["path"], f"inputs[{index}].path")
            digest = item["sha256"]
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise ValueError(f"inputs[{index}].sha256 must be a lowercase SHA-256")
            inputs.append(ExperimentInput(relative, digest))
        if len({item.path for item in inputs}) != len(inputs):
            raise ValueError("inputs contains duplicate paths")

        versions_raw = raw["versions"]
        if not isinstance(versions_raw, dict) or not versions_raw:
            raise ValueError("versions must be a non-empty object")
        versions: dict[str, str] = {}
        for key, value in versions_raw.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("version names must be non-empty strings")
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"versions.{key} must be a non-empty string")
            versions[key] = value

        raw_cases = raw["cases"]
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("cases must be a non-empty array")
        cases: list[ExperimentCase] = []
        for index, item in enumerate(raw_cases):
            item = _require_exact_keys(item, _CASE_KEYS, f"cases[{index}]")
            case_id = _identifier(item["case_id"], f"cases[{index}].case_id")
            argv = item["argv"]
            if not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv
            ):
                raise ValueError(f"cases[{index}].argv must contain non-empty strings")
            cwd = _portable_relative(item["cwd"], f"cases[{index}].cwd")
            cases.append(ExperimentCase(case_id, tuple(argv), cwd))
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("cases contains duplicate case_id values")

        value = cls(
            project_root=root,
            path=source,
            experiment_id=experiment_id,
            protocol_version=protocol_version,
            adapter_kind=adapter_kind,
            adapter_config=adapter_config,
            timeout_seconds=float(timeout),
            inputs=tuple(inputs),
            config=_copy_json_object(raw["config"], "config"),
            versions=dict(sorted(versions.items())),
            resource_metadata=_copy_json_object(
                raw["resource_metadata"], "resource_metadata"
            ),
            cost_metadata=_copy_json_object(raw["cost_metadata"], "cost_metadata"),
            cases=tuple(cases),
        )
        value.input_provenance()
        for case in value.cases:
            value.resolve_project_path(case.cwd, directory=True)
        return value

    def resolve_project_path(self, relative: str, *, directory: bool = False) -> Path:
        normalized = _portable_relative(relative, "experiment path")
        unresolved = self.project_root / Path(*PurePosixPath(normalized).parts)
        target = unresolved.resolve()
        if not target.is_relative_to(self.project_root):
            raise ValueError(f"experiment path escapes project: {relative}")
        if directory:
            if not target.is_dir():
                raise ValueError(f"experiment working directory is unavailable: {relative}")
        elif not target.is_file() or unresolved.is_symlink():
            raise ValueError(f"experiment input is unavailable or symbolic: {relative}")
        return target

    def input_provenance(self) -> list[dict[str, Any]]:
        provenance: list[dict[str, Any]] = []
        for item in self.inputs:
            target = self.resolve_project_path(item.path)
            observed = file_digest(target)
            if observed != item.sha256:
                raise ValueError(f"experiment input digest mismatch: {item.path}")
            provenance.append({
                "path": item.path,
                "sha256": observed,
                "size_bytes": target.stat().st_size,
            })
        return provenance

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPERIMENT_MANIFEST_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "protocol_version": self.protocol_version,
            "adapter": {
                "kind": self.adapter_kind,
                "config": self.adapter_config,
            },
            "timeout_seconds": self.timeout_seconds,
            "inputs": [item.to_dict() for item in self.inputs],
            "config": self.config,
            "versions": self.versions,
            "resource_metadata": self.resource_metadata,
            "cost_metadata": self.cost_metadata,
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class ExperimentExecutionRequest:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float
    stdout_path: Path
    stderr_path: Path
    adapter_config: dict[str, Any]
    environment: dict[str, str]


@dataclass(frozen=True, slots=True)
class ExperimentExecution:
    termination: str
    exit_code: int | None
    wall_seconds: float
    infrastructure_failure: dict[str, str] | None


class ExperimentAdapter(Protocol):
    kind: str
    deterministic: bool
    uses_llm: bool

    def execute(self, request: ExperimentExecutionRequest) -> ExperimentExecution:
        ...


class SubprocessExperimentAdapter:
    kind = "subprocess"
    deterministic = True
    uses_llm = False

    def execute(self, request: ExperimentExecutionRequest) -> ExperimentExecution:
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        termination = "LAUNCH_FAILED"
        exit_code: int | None = None
        failure: dict[str, str] | None = None
        with request.stdout_path.open("xb") as stdout, request.stderr_path.open("xb") as stderr:
            try:
                process = subprocess.Popen(
                    list(request.argv),
                    cwd=request.cwd,
                    env=request.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    shell=False,
                )
                try:
                    exit_code = process.wait(timeout=request.timeout_seconds)
                    termination = "EXITED"
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait()
                    termination = "TIMED_OUT"
                    failure = {
                        "kind": "timeout",
                        "message": f"command exceeded {request.timeout_seconds:g} seconds",
                    }
            except OSError as exc:
                failure = {"kind": type(exc).__name__, "message": str(exc)}
            finally:
                stdout.flush()
                stderr.flush()
                os.fsync(stdout.fileno())
                os.fsync(stderr.fileno())
        return ExperimentExecution(
            termination=termination,
            exit_code=exit_code,
            wall_seconds=round(max(0.0, time.monotonic() - started), 9),
            infrastructure_failure=failure,
        )


@dataclass(frozen=True, slots=True)
class ExperimentRunSummary:
    experiment_id: str
    run_id: str
    experiment_fingerprint: str
    root: Path
    executed_case_ids: tuple[str, ...]
    resumed_case_ids: tuple[str, ...]
    recovered_case_ids: tuple[str, ...]
    infrastructure_failure_case_ids: tuple[str, ...]


class ExperimentRunner:
    """Execute frozen, non-LLM experiment batches into append-only raw storage."""

    def __init__(
        self,
        project_root: Path,
        *,
        docker_adapter: ExperimentAdapter | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.layout = ProjectLayout(self.project_root)
        self._adapters: dict[str, ExperimentAdapter] = {
            "subprocess": SubprocessExperimentAdapter(),
        }
        if docker_adapter is not None:
            if getattr(docker_adapter, "kind", None) != "docker":
                raise ValueError("docker_adapter.kind must be docker")
            self._adapters["docker"] = docker_adapter
        for adapter in self._adapters.values():
            if getattr(adapter, "deterministic", None) is not True:
                raise ValueError(f"experiment adapter is not deterministic: {adapter.kind}")
            if getattr(adapter, "uses_llm", None) is not False:
                raise ValueError(f"experiment adapter may not use an LLM: {adapter.kind}")

    def run(
        self,
        manifest: ExperimentManifest | Path,
        *,
        resume: bool = False,
    ) -> ExperimentRunSummary:
        value = (
            manifest
            if isinstance(manifest, ExperimentManifest)
            else ExperimentManifest.load(self.project_root, manifest)
        )
        if value.project_root != self.project_root:
            raise ValueError("experiment manifest belongs to a different project")
        adapter = self._adapters.get(value.adapter_kind)
        if adapter is None:
            raise ValueError(f"no deterministic adapter registered for {value.adapter_kind}")

        run_manifest = self._build_run_manifest(value)
        run_id = str(run_manifest["run_id"])
        fingerprint = str(run_manifest["experiment_fingerprint"])
        root = self.layout.experiments_root / run_id
        snapshot_path = root / "RUN_MANIFEST.json"
        ledger_path = root / "RAW_RESULTS.jsonl"
        checkpoint_path = root / "CHECKPOINT.json"
        if root.exists():
            if not resume:
                raise FileExistsError(
                    f"deterministic experiment run already exists; use resume: {run_id}"
                )
            if not root.is_dir():
                raise ValueError("experiment run path is not a directory")
            observed = self._read_canonical_json(snapshot_path)
            if observed != run_manifest:
                raise ValueError("experiment run manifest does not match frozen inputs")
        else:
            root.mkdir(parents=True, exist_ok=False)
            _exclusive_write_json(snapshot_path, run_manifest)

        cases_root = root / "cases"
        cases_root.mkdir(exist_ok=True)
        records = self._read_and_verify_ledger(
            ledger_path, root, value, run_manifest
        )
        self._repair_checkpoint(
            checkpoint_path, ledger_path, records, run_manifest
        )
        resumed_ids = [str(record["case_id"]) for record in records]
        executed_ids: list[str] = []
        recovered_ids: list[str] = []

        for index, case in enumerate(value.cases):
            if index < len(records):
                continue
            self._verify_run_snapshot(snapshot_path, run_manifest)
            value.input_provenance()
            case_plan = self._case_plan(case, fingerprint)
            case_root = cases_root / str(case_plan["case_run_id"])
            result_path = case_root / "RESULT.json"
            if case_root.exists():
                if not case_root.is_dir() or not result_path.is_file():
                    raise ValueError(f"incomplete raw experiment case: {case.case_id}")
                record = self._read_and_verify_result(
                    result_path, root, value, case, run_manifest, case_plan
                )
                recovered_ids.append(case.case_id)
            else:
                case_root.mkdir(exist_ok=False)
                record = self._execute_case(
                    adapter, root, case_root, value, case, run_manifest, case_plan
                )
                executed_ids.append(case.case_id)

            entry = self._ledger_entry(
                len(records) + 1, root, result_path, record
            )
            _append_jsonl(ledger_path, entry)
            records.append(record)
            self._repair_checkpoint(
                checkpoint_path, ledger_path, records, run_manifest
            )
            self._verify_run_snapshot(snapshot_path, run_manifest)
            if record["execution"]["termination"] == "INPUT_MUTATED":
                raise RuntimeError("experiment input changed during command execution")

        expected_case_roots = {
            str(self._case_plan(case, fingerprint)["case_run_id"])
            for case in value.cases
        }
        actual_case_roots = {path.name for path in cases_root.iterdir()}
        if actual_case_roots != expected_case_roots:
            raise ValueError("experiment raw case directory set is inconsistent")
        records = self._read_and_verify_ledger(
            ledger_path, root, value, run_manifest
        )
        self._repair_checkpoint(
            checkpoint_path, ledger_path, records, run_manifest
        )
        failures = tuple(
            str(record["case_id"])
            for record in records
            if record["execution"]["infrastructure_failure"] is not None
        )
        return ExperimentRunSummary(
            experiment_id=value.experiment_id,
            run_id=run_id,
            experiment_fingerprint=fingerprint,
            root=root,
            executed_case_ids=tuple(executed_ids),
            resumed_case_ids=tuple(resumed_ids),
            recovered_case_ids=tuple(recovered_ids),
            infrastructure_failure_case_ids=failures,
        )

    def verify_receipt(
        self,
        manifest: ExperimentManifest | Path,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Verify one completed run without executing or repairing it."""
        value = (
            manifest
            if isinstance(manifest, ExperimentManifest)
            else ExperimentManifest.load(self.project_root, manifest)
        )
        if value.project_root != self.project_root:
            raise ValueError("experiment manifest belongs to a different project")
        run_manifest = self._build_run_manifest(value)
        expected_run_id = str(run_manifest["run_id"])
        if run_id != expected_run_id:
            raise ValueError("experiment receipt run_id does not match the frozen manifest")
        root = self.layout.experiments_root / expected_run_id
        if not root.is_dir():
            raise ValueError("experiment receipt run is unavailable")
        snapshot_path = root / "RUN_MANIFEST.json"
        if self._read_canonical_json(snapshot_path) != run_manifest:
            raise ValueError("experiment receipt run manifest is invalid")
        value.input_provenance()
        ledger_path = root / "RAW_RESULTS.jsonl"
        records = self._read_and_verify_ledger(
            ledger_path, root, value, run_manifest,
        )
        if len(records) != len(value.cases):
            raise ValueError("experiment receipt run is incomplete")
        expected_case_roots = {
            str(self._case_plan(case, str(run_manifest["experiment_fingerprint"]))["case_run_id"])
            for case in value.cases
        }
        cases_root = root / "cases"
        if not cases_root.is_dir() or {
            path.name for path in cases_root.iterdir()
        } != expected_case_roots:
            raise ValueError("experiment receipt case directory set is inconsistent")

        artifact_paths = [
            value.path.relative_to(self.project_root).as_posix(),
            *[item.path for item in value.inputs],
            snapshot_path.relative_to(self.project_root).as_posix(),
            ledger_path.relative_to(self.project_root).as_posix(),
        ]
        case_summaries: list[dict[str, Any]] = []
        for record in records:
            result_path = root / "cases" / str(record["case_run_id"]) / "RESULT.json"
            artifact_paths.extend([
                result_path.relative_to(self.project_root).as_posix(),
                str(record["artifacts"]["stdout"]["path"]),
                str(record["artifacts"]["stderr"]["path"]),
            ])
            case_summaries.append({
                "case_id": record["case_id"],
                "case_run_id": record["case_run_id"],
                "case_fingerprint": record["case_fingerprint"],
                "termination": record["execution"]["termination"],
                "exit_code": record["execution"]["exit_code"],
                "infrastructure_failure": record["execution"]["infrastructure_failure"],
                "result_sha256": file_digest(result_path),
                "stdout_sha256": record["artifacts"]["stdout"]["sha256"],
                "stderr_sha256": record["artifacts"]["stderr"]["sha256"],
            })
        # stdout/stderr paths in a result are run-relative, unlike the other
        # paths above. Normalize the complete set to project-relative paths.
        normalized_artifacts: list[str] = []
        for relative in artifact_paths:
            candidate = Path(relative)
            if candidate.parts and candidate.parts[0] == "cases":
                candidate = root.relative_to(self.project_root) / candidate
            normalized = candidate.as_posix()
            if normalized not in normalized_artifacts:
                normalized_artifacts.append(normalized)
        receipt = {
            "schema_version": 1,
            "manifest_path": value.path.relative_to(self.project_root).as_posix(),
            "manifest_sha256": file_digest(value.path),
            "run_id": expected_run_id,
            "experiment_fingerprint": run_manifest["experiment_fingerprint"],
            "protocol_version": value.protocol_version,
            "raw_ledger_sha256": file_digest(ledger_path),
            "input_provenance": value.input_provenance(),
            "cases": case_summaries,
            "artifact_paths": normalized_artifacts,
        }
        receipt["receipt_fingerprint"] = _json_hash(receipt)
        return receipt

    @staticmethod
    def _build_run_manifest(value: ExperimentManifest) -> dict[str, Any]:
        normalized = value.to_dict()
        manifest_sha256 = _json_hash(normalized)
        provenance = {
            "runner_contract_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "manifest_sha256": manifest_sha256,
            "config_sha256": _json_hash(value.config),
            "protocol_version": value.protocol_version,
            "versions": value.versions,
            "inputs": value.input_provenance(),
            "execution_environment": ExperimentRunner._execution_environment_provenance(
                value
            ),
        }
        fingerprint = _json_hash({
            "manifest": normalized,
            "provenance": provenance,
        })
        return {
            "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "run_id": f"run-{fingerprint}",
            "experiment_id": value.experiment_id,
            "experiment_fingerprint": fingerprint,
            "llm_execution_allowed": False,
            "manifest": normalized,
            "provenance": provenance,
        }

    @staticmethod
    def _subprocess_environment(*, scratch_root: Path | None = None) -> dict[str, str]:
        inherited: dict[str, str] = {}
        for name in (
            "COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR",
        ):
            value = os.environ.get(name)
            if value:
                inherited[name] = value
        inherited.update({
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "TZ": "UTC",
        })
        if scratch_root is not None:
            temporary = scratch_root / "tmp"
            temporary.mkdir(parents=True, exist_ok=True)
            inherited["TEMP"] = str(temporary)
            inherited["TMP"] = str(temporary)
            inherited["TMPDIR"] = str(temporary)
        return inherited

    @staticmethod
    def _resolve_case_executable(
        manifest: ExperimentManifest,
        case: ExperimentCase,
        environment: dict[str, str],
    ) -> Path | None:
        requested = case.argv[0]
        candidate = Path(requested)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        elif candidate.parent != Path("."):
            working = manifest.resolve_project_path(case.cwd, directory=True)
            resolved = (working / candidate).resolve()
        else:
            found = shutil.which(requested, path=environment.get("PATH"))
            if found is None:
                return None
            resolved = Path(found).resolve()
        return resolved if resolved.is_file() else None

    @staticmethod
    def _execution_environment_provenance(
        manifest: ExperimentManifest,
    ) -> dict[str, Any]:
        if manifest.adapter_kind != "subprocess":
            return {
                "adapter": manifest.adapter_kind,
                "adapter_config_sha256": _json_hash(manifest.adapter_config),
                "isolation": "adapter-owned",
            }
        environment = ExperimentRunner._subprocess_environment()
        executables: list[dict[str, Any]] = []
        for case in manifest.cases:
            resolved = ExperimentRunner._resolve_case_executable(
                manifest, case, environment
            )
            executables.append({
                "case_id": case.case_id,
                "requested": case.argv[0],
                "resolved_path": str(resolved) if resolved is not None else None,
                "sha256": file_digest(resolved) if resolved is not None else None,
                "size_bytes": resolved.stat().st_size if resolved is not None else None,
            })
        return {
            "adapter": "subprocess",
            "environment_contract_version": 1,
            "environment_sha256": _json_hash(environment),
            "inherited_environment_keys": sorted(
                key for key in environment
                if key not in {
                    "LANG", "LC_ALL", "PYTHONHASHSEED", "PYTHONNOUSERSITE",
                    "PYTHONUTF8", "TZ",
                }
            ),
            "deterministic_environment": {
                key: environment[key]
                for key in (
                    "LANG", "LC_ALL", "PYTHONHASHSEED", "PYTHONNOUSERSITE",
                    "PYTHONUTF8", "TZ",
                )
            },
            "platform": {
                "implementation": platform.python_implementation(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "release": platform.release(),
                "system": platform.system(),
            },
            "isolation": "materialized-declared-inputs",
            "network_isolated": False,
            "executables": executables,
        }

    @staticmethod
    def _case_plan(case: ExperimentCase, fingerprint: str) -> dict[str, str]:
        case_fingerprint = _json_hash({
            "experiment_fingerprint": fingerprint,
            "case": case.to_dict(),
        })
        return {
            "case_fingerprint": case_fingerprint,
            "case_run_id": f"case-{case_fingerprint}",
        }

    def _execute_case(
        self,
        adapter: ExperimentAdapter,
        root: Path,
        case_root: Path,
        manifest: ExperimentManifest,
        case: ExperimentCase,
        run_manifest: dict[str, Any],
        case_plan: dict[str, str],
    ) -> dict[str, Any]:
        stdout_path = case_root / "stdout.bin"
        stderr_path = case_root / "stderr.bin"
        started_at = utc_now()
        materialized_input_error: ValueError | None = None
        try:
            if manifest.adapter_kind == "subprocess":
                with tempfile.TemporaryDirectory(prefix="amr-experiment-") as temporary:
                    scratch_root = Path(temporary).resolve()
                    self._materialize_experiment_inputs(manifest, scratch_root)
                    scratch_cwd = scratch_root / Path(
                        *PurePosixPath(case.cwd).parts
                    )
                    scratch_cwd.mkdir(parents=True, exist_ok=True)
                    request = ExperimentExecutionRequest(
                        argv=self._materialized_argv(manifest, case, scratch_root),
                        cwd=scratch_cwd,
                        timeout_seconds=manifest.timeout_seconds,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                        adapter_config=manifest.adapter_config,
                        environment=self._subprocess_environment(
                            scratch_root=scratch_root
                        ),
                    )
                    outcome = adapter.execute(request)
                    self._validate_execution(outcome)
                    try:
                        self._verify_materialized_inputs(manifest, scratch_root)
                    except ValueError as exc:
                        materialized_input_error = exc
            else:
                request = ExperimentExecutionRequest(
                    argv=case.argv,
                    cwd=manifest.resolve_project_path(case.cwd, directory=True),
                    timeout_seconds=manifest.timeout_seconds,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    adapter_config=manifest.adapter_config,
                    environment={},
                )
                outcome = adapter.execute(request)
                self._validate_execution(outcome)
        except Exception as exc:
            outcome = ExperimentExecution(
                termination="ADAPTER_FAILED",
                exit_code=None,
                wall_seconds=0.0,
                infrastructure_failure={
                    "kind": type(exc).__name__,
                    "message": str(exc),
                },
            )
        self._ensure_raw_artifact(stdout_path)
        self._ensure_raw_artifact(stderr_path)
        try:
            if materialized_input_error is not None:
                raise materialized_input_error
            observed_inputs = manifest.input_provenance()
            if observed_inputs != run_manifest["provenance"]["inputs"]:
                raise ValueError("experiment inputs changed")
        except ValueError as exc:
            outcome = ExperimentExecution(
                termination="INPUT_MUTATED",
                exit_code=outcome.exit_code,
                wall_seconds=outcome.wall_seconds,
                infrastructure_failure={
                    "kind": "input_mutation",
                    "message": str(exc),
                },
            )
        finished_at = utc_now()
        record = {
            "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "experiment_id": manifest.experiment_id,
            "run_id": run_manifest["run_id"],
            "experiment_fingerprint": run_manifest["experiment_fingerprint"],
            "case_id": case.case_id,
            "case_run_id": case_plan["case_run_id"],
            "case_fingerprint": case_plan["case_fingerprint"],
            "started_at": started_at,
            "finished_at": finished_at,
            "command": {
                "adapter": manifest.adapter_kind,
                "adapter_config": manifest.adapter_config,
                "argv": list(case.argv),
                "cwd": case.cwd,
                "shell": False,
            },
            "execution": {
                "termination": outcome.termination,
                "exit_code": outcome.exit_code,
                "timeout_seconds": manifest.timeout_seconds,
                "wall_seconds": outcome.wall_seconds,
                "infrastructure_failure": outcome.infrastructure_failure,
            },
            "research_result": {"status": "UNINTERPRETED"},
            "provenance": {
                **run_manifest["provenance"],
                "argv_sha256": _json_hash(list(case.argv)),
            },
            "resource": {
                "declared": manifest.resource_metadata,
                "observed_wall_seconds": outcome.wall_seconds,
            },
            "cost": {
                "declared": manifest.cost_metadata,
                "llm_calls": 0,
            },
            "artifacts": {
                "stdout": self._artifact_record(root, stdout_path),
                "stderr": self._artifact_record(root, stderr_path),
            },
        }
        result_path = case_root / "RESULT.json"
        _exclusive_write_json(result_path, record)
        return self._read_and_verify_result(
            result_path, root, manifest, case, run_manifest, case_plan
        )

    @staticmethod
    def _materialize_experiment_inputs(
        manifest: ExperimentManifest,
        scratch_root: Path,
    ) -> None:
        for item in manifest.inputs:
            source = manifest.resolve_project_path(item.path)
            target = scratch_root / Path(*PurePosixPath(item.path).parts)
            resolved_target = target.resolve()
            if not resolved_target.is_relative_to(scratch_root):
                raise ValueError("materialized experiment input escapes scratch root")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
            if file_digest(target) != item.sha256:
                raise ValueError(
                    f"materialized experiment input digest mismatch: {item.path}"
                )

    @staticmethod
    def _verify_materialized_inputs(
        manifest: ExperimentManifest,
        scratch_root: Path,
    ) -> None:
        for item in manifest.inputs:
            target = scratch_root / Path(*PurePosixPath(item.path).parts)
            if (
                not target.is_file()
                or target.is_symlink()
                or file_digest(target) != item.sha256
            ):
                raise ValueError(
                    f"materialized experiment input changed: {item.path}"
                )

    @staticmethod
    def _materialized_argv(
        manifest: ExperimentManifest,
        case: ExperimentCase,
        scratch_root: Path,
    ) -> tuple[str, ...]:
        declared = {item.path for item in manifest.inputs}
        mapped: list[str] = []
        for index, argument in enumerate(case.argv):
            candidate = Path(argument)
            if not candidate.is_absolute():
                mapped.append(argument)
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(manifest.project_root):
                mapped.append(argument)
                continue
            relative = resolved.relative_to(manifest.project_root).as_posix()
            if relative not in declared:
                if index == 0 and not resolved.exists():
                    mapped.append(argument)
                    continue
                if index == 0:
                    raise ValueError(
                        "project-local experiment executable must be a declared input"
                    )
                raise ValueError(
                    "absolute project-local experiment argument must be a declared input"
                )
            mapped.append(str(scratch_root / Path(*PurePosixPath(relative).parts)))
        return tuple(mapped)

    @staticmethod
    def _validate_execution(
        outcome: Any, *, allow_input_mutated: bool = False
    ) -> None:
        if not isinstance(outcome, ExperimentExecution):
            raise TypeError("experiment adapter returned an invalid execution result")
        allowed = _TERMINATIONS if allow_input_mutated else _TERMINATIONS - {"INPUT_MUTATED"}
        if outcome.termination not in allowed:
            raise ValueError("experiment adapter returned an invalid termination")
        if outcome.exit_code is not None and type(outcome.exit_code) is not int:
            raise ValueError("experiment adapter exit_code must be an integer or null")
        if (
            type(outcome.wall_seconds) not in {int, float}
            or not math.isfinite(float(outcome.wall_seconds))
            or outcome.wall_seconds < 0
        ):
            raise ValueError("experiment adapter wall_seconds must be finite and non-negative")
        failure = outcome.infrastructure_failure
        if failure is not None and (
            not isinstance(failure, dict)
            or set(failure) != {"kind", "message"}
            or any(not isinstance(value, str) for value in failure.values())
        ):
            raise ValueError("invalid infrastructure_failure")
        if outcome.termination == "EXITED" and failure is not None:
            raise ValueError("an exited process cannot report infrastructure failure")
        if outcome.termination != "EXITED" and failure is None:
            raise ValueError("non-exit termination requires infrastructure failure")

    @staticmethod
    def _ensure_raw_artifact(path: Path) -> None:
        if not path.exists():
            _exclusive_write(path, b"")
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"raw experiment artifact is not a regular file: {path.name}")

    @staticmethod
    def _artifact_record(root: Path, path: Path) -> dict[str, Any]:
        return {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_digest(path),
            "size_bytes": path.stat().st_size,
        }

    def _read_and_verify_result(
        self,
        result_path: Path,
        root: Path,
        manifest: ExperimentManifest,
        case: ExperimentCase,
        run_manifest: dict[str, Any],
        case_plan: dict[str, str],
    ) -> dict[str, Any]:
        record = self._read_canonical_json(result_path)
        _require_exact_keys(record, _RESULT_KEYS, "raw experiment result")
        fixed = {
            "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "experiment_id": manifest.experiment_id,
            "run_id": run_manifest["run_id"],
            "experiment_fingerprint": run_manifest["experiment_fingerprint"],
            "case_id": case.case_id,
            "case_run_id": case_plan["case_run_id"],
            "case_fingerprint": case_plan["case_fingerprint"],
        }
        for key, expected in fixed.items():
            if record.get(key) != expected:
                raise ValueError(f"raw experiment result has invalid {key}")
        if not isinstance(record["started_at"], str) or not isinstance(record["finished_at"], str):
            raise ValueError("raw experiment result timestamps are invalid")
        expected_command = {
            "adapter": manifest.adapter_kind,
            "adapter_config": manifest.adapter_config,
            "argv": list(case.argv),
            "cwd": case.cwd,
            "shell": False,
        }
        if record["command"] != expected_command:
            raise ValueError("raw experiment command provenance is invalid")
        execution = _require_exact_keys(
            record["execution"],
            frozenset({
                "termination", "exit_code", "timeout_seconds", "wall_seconds",
                "infrastructure_failure",
            }),
            "raw experiment execution",
        )
        self._validate_execution(
            ExperimentExecution(
                termination=execution["termination"],
                exit_code=execution["exit_code"],
                wall_seconds=execution["wall_seconds"],
                infrastructure_failure=execution["infrastructure_failure"],
            ),
            allow_input_mutated=True,
        )
        if execution["timeout_seconds"] != manifest.timeout_seconds:
            raise ValueError("raw experiment timeout provenance is invalid")
        if record["research_result"] != {"status": "UNINTERPRETED"}:
            raise ValueError("raw experiment evidence cannot contain an interpreted result")
        expected_provenance = {
            **run_manifest["provenance"],
            "argv_sha256": _json_hash(list(case.argv)),
        }
        if record["provenance"] != expected_provenance:
            raise ValueError("raw experiment provenance is invalid")
        if record["resource"] != {
            "declared": manifest.resource_metadata,
            "observed_wall_seconds": execution["wall_seconds"],
        }:
            raise ValueError("raw experiment resource metadata is invalid")
        if record["cost"] != {
            "declared": manifest.cost_metadata,
            "llm_calls": 0,
        }:
            raise ValueError("raw experiment cost metadata is invalid")
        artifacts = _require_exact_keys(
            record["artifacts"], frozenset({"stdout", "stderr"}),
            "raw experiment artifacts",
        )
        for name in ("stdout", "stderr"):
            observed = _require_exact_keys(
                artifacts[name], frozenset({"path", "sha256", "size_bytes"}),
                f"raw experiment {name} artifact",
            )
            path = result_path.parent / f"{name}.bin"
            expected = self._artifact_record(root, path)
            if observed != expected:
                raise ValueError(f"raw experiment {name} artifact digest mismatch")
        return record

    def _read_and_verify_ledger(
        self,
        ledger_path: Path,
        root: Path,
        manifest: ExperimentManifest,
        run_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not ledger_path.exists():
            return []
        if not ledger_path.is_file() or ledger_path.is_symlink():
            raise ValueError("raw experiment ledger is not a regular file")
        payload = ledger_path.read_bytes()
        if payload and not payload.endswith(b"\n"):
            raise ValueError("raw experiment ledger has a partial final record")
        try:
            lines = payload.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ValueError("raw experiment ledger is not UTF-8") from exc
        records: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                raise ValueError("raw experiment ledger contains a blank record")
            entry = _loads_json(line, f"{ledger_path}:{index + 1}")
            entry = _require_exact_keys(entry, _LEDGER_KEYS, "raw experiment ledger entry")
            expected_line = json.dumps(
                entry, ensure_ascii=False, sort_keys=True
            ) + "\n"
            if line != expected_line:
                raise ValueError("raw experiment ledger encoding is non-canonical")
            if index >= len(manifest.cases):
                raise ValueError("raw experiment ledger contains an unknown case")
            case = manifest.cases[index]
            case_plan = self._case_plan(
                case, str(run_manifest["experiment_fingerprint"])
            )
            result_path = root / "cases" / case_plan["case_run_id"] / "RESULT.json"
            record = self._read_and_verify_result(
                result_path, root, manifest, case, run_manifest, case_plan
            )
            expected_entry = self._ledger_entry(index + 1, root, result_path, record)
            if entry != expected_entry:
                raise ValueError("raw experiment ledger entry or artifact digest mismatch")
            records.append(record)
        return records

    @staticmethod
    def _ledger_entry(
        sequence: int,
        root: Path,
        result_path: Path,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "sequence": sequence,
            "run_id": record["run_id"],
            "experiment_fingerprint": record["experiment_fingerprint"],
            "case_id": record["case_id"],
            "case_run_id": record["case_run_id"],
            "case_fingerprint": record["case_fingerprint"],
            "result_path": result_path.relative_to(root).as_posix(),
            "result_sha256": file_digest(result_path),
            "stdout_path": record["artifacts"]["stdout"]["path"],
            "stdout_sha256": record["artifacts"]["stdout"]["sha256"],
            "stderr_path": record["artifacts"]["stderr"]["path"],
            "stderr_sha256": record["artifacts"]["stderr"]["sha256"],
        }

    @staticmethod
    def _checkpoint(
        ledger_path: Path,
        records: list[dict[str, Any]],
        run_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        ledger_sha256 = (
            file_digest(ledger_path)
            if ledger_path.is_file()
            else sha256(b"").hexdigest()
        )
        return {
            "schema_version": EXPERIMENT_RUN_SCHEMA_VERSION,
            "run_id": run_manifest["run_id"],
            "run_fingerprint": run_manifest["experiment_fingerprint"],
            "raw_ledger_sha256": ledger_sha256,
            "verified_terminal_case_count": len(records),
            "verified_terminal_case_fingerprints": [
                record["case_fingerprint"] for record in records
            ],
        }

    def _repair_checkpoint(
        self,
        checkpoint_path: Path,
        ledger_path: Path,
        records: list[dict[str, Any]],
        run_manifest: dict[str, Any],
    ) -> None:
        expected = self._checkpoint(ledger_path, records, run_manifest)
        observed: dict[str, Any] | None = None
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            if checkpoint_path.is_dir() and not checkpoint_path.is_symlink():
                raise ValueError("derived experiment checkpoint is a directory")
            try:
                observed = self._read_canonical_json(checkpoint_path)
            except ValueError:
                observed = None
        if observed != expected:
            atomic_write_json(checkpoint_path, expected)
        if self._read_canonical_json(checkpoint_path) != expected:
            raise ValueError("derived experiment checkpoint could not be rebuilt")

    @staticmethod
    def _read_canonical_json(path: Path) -> dict[str, Any]:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"required experiment record is unavailable: {path.name}")
        payload = path.read_bytes()
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"experiment record is not UTF-8: {path.name}") from exc
        value = _loads_json(text, str(path))
        if not isinstance(value, dict):
            raise ValueError(f"experiment record must be an object: {path.name}")
        if payload != _canonical_json_bytes(value, pretty=True):
            raise ValueError(f"experiment record encoding is non-canonical: {path.name}")
        return value

    def _verify_run_snapshot(
        self, path: Path, expected: dict[str, Any]
    ) -> None:
        if self._read_canonical_json(path) != expected:
            raise ValueError("experiment run manifest was modified")
