#!/usr/bin/env python3
"""Run one bounded mathematics execution task in an ephemeral Codex process."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# A resumed autonomous run executes this file from its immutable policy
# snapshot. The controller supplies the target workspace root only for
# validated inputs/output placement; route configuration cannot be overridden.
_REPOSITORY_ROOT_OVERRIDE = os.environ.get("MATH_WORKER_REPOSITORY_ROOT", "").strip()
REPO_ROOT = (
    Path(_REPOSITORY_ROOT_OVERRIDE).resolve()
    if _REPOSITORY_ROOT_OVERRIDE
    else Path.cwd().resolve()
)
SKILL_ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA_SOURCE = SKILL_ROOT / "references" / "worker-result.schema.json"
TASK_SCHEMA_SOURCE = SKILL_ROOT / "references" / "worker-task.schema.json"
_SCHEMA_VALIDATOR_OVERRIDE = os.environ.get(
    "MATH_WORKER_SCHEMA_VALIDATOR_PATH", "",
).strip()
_CONTRACT_DEFINITIONS_OVERRIDE = os.environ.get(
    "MATH_WORKER_CONTRACT_DEFINITIONS_PATH", "",
).strip()


def _load_pinned_schema_validator(
    schema_path: Path, contracts_path: Path,
) -> tuple[Any, Any]:
    """Load the controller-pinned validator without importing live repo code."""
    schema_path = schema_path.resolve()
    contracts_path = contracts_path.resolve()
    if not schema_path.is_file() or not contracts_path.is_file():
        raise RuntimeError("pinned mechanical schema validator snapshot is missing")
    package_name = "_math_worker_pinned_protocol"
    package = types.ModuleType(package_name)
    package.__path__ = [str(schema_path.parent)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    sys.modules[package_name] = package

    contracts_spec = importlib.util.spec_from_file_location(
        f"{package_name}.contracts", contracts_path,
    )
    if contracts_spec is None or contracts_spec.loader is None:
        raise RuntimeError("cannot load pinned mechanical contract definitions")
    contracts_module = importlib.util.module_from_spec(contracts_spec)
    sys.modules[contracts_spec.name] = contracts_module
    contracts_spec.loader.exec_module(contracts_module)

    schema_spec = importlib.util.spec_from_file_location(
        f"{package_name}.schema", schema_path,
    )
    if schema_spec is None or schema_spec.loader is None:
        raise RuntimeError("cannot load pinned mechanical schema validator")
    schema_module = importlib.util.module_from_spec(schema_spec)
    sys.modules[schema_spec.name] = schema_module
    schema_spec.loader.exec_module(schema_module)
    return (
        getattr(schema_module, "validate"),
        getattr(schema_module, "validate_output_schema_compatibility"),
    )


if bool(_SCHEMA_VALIDATOR_OVERRIDE) != bool(_CONTRACT_DEFINITIONS_OVERRIDE):
    raise RuntimeError(
        "mechanical schema validator and contract snapshots must be supplied together"
    )
if _SCHEMA_VALIDATOR_OVERRIDE:
    validate, validate_output_schema_compatibility = _load_pinned_schema_validator(
        Path(_SCHEMA_VALIDATOR_OVERRIDE), Path(_CONTRACT_DEFINITIONS_OVERRIDE),
    )
else:
    _BUNDLED_PACKAGE_ROOT = Path(__file__).resolve().parents[4]
    _BUNDLED_SCHEMA = _BUNDLED_PACKAGE_ROOT / "schema.py"
    _BUNDLED_CONTRACTS = _BUNDLED_PACKAGE_ROOT / "contracts.py"
    if _BUNDLED_SCHEMA.is_file() and _BUNDLED_CONTRACTS.is_file():
        validate, validate_output_schema_compatibility = _load_pinned_schema_validator(
            _BUNDLED_SCHEMA, _BUNDLED_CONTRACTS,
        )
    else:
        def _validator_snapshot_required(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                "controller-pinned mechanical validator snapshot is required"
            )

        validate = _validator_snapshot_required
        validate_output_schema_compatibility = _validator_snapshot_required
DEFAULT_WORKER_MODEL = "gpt-5.3-codex-spark"
DEFAULT_WORKER_REASONING_EFFORT = "high"
FALLBACK_WORKER_MODEL = "gpt-5.6-luna"
FALLBACK_WORKER_REASONING_EFFORT = "medium"
FIXED_WORKER_ROUTES = (
    {
        "model": DEFAULT_WORKER_MODEL,
        "reasoning_effort": DEFAULT_WORKER_REASONING_EFFORT,
        "service_tier": None,
    },
    {
        "model": FALLBACK_WORKER_MODEL,
        "reasoning_effort": FALLBACK_WORKER_REASONING_EFFORT,
        "service_tier": None,
    },
)
ALLOWED_WORKER_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}
WORKER_ISOLATION_OVERRIDES = {
    "approval_policy": "never",
    "shell_environment_policy.inherit": "core",
    "shell_environment_policy.ignore_default_excludes": False,
    "shell_environment_policy.experimental_use_profile": False,
    "shell_environment_policy.exclude": [
        "CODEX_HOME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "SSH_*", "GIT_ASKPASS", "SSH_ASKPASS",
        "*TOKEN*", "*SECRET*", "*PASSWORD*", "*PASSWD*", "*API_KEY*",
        "*APIKEY*", "*ACCESS_KEY*", "*CREDENTIAL*", "*AUTHORIZATION*",
        "*COOKIE*",
    ],
    "web_search": "disabled",
    "features.memories": False,
    "features.multi_agent": False,
    "features.plugins": False,
    "features.apps": False,
}


def load_worker_routes(path: Path | None, *, broker_managed: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None:
        return (
            {"provider": "codex", "profile": None, **FIXED_WORKER_ROUTES[0]},
            {"provider": "codex", "profile": None, **FIXED_WORKER_ROUTES[1]},
        )
    resolved = path.resolve()
    if broker_managed and not resolved.is_relative_to(REPO_ROOT):
        raise ContractError("broker route configuration must stay inside the project")
    value = read_json(resolved)
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "primary_route", "fallback_route",
    }:
        raise ContractError("mechanical route configuration fields are invalid")
    if value["schema_version"] != 1:
        raise ContractError("unsupported mechanical route configuration version")
    expected = {
        "provider", "model", "reasoning_effort", "service_tier", "profile",
    }
    routes: list[dict[str, Any]] = []
    for name in ("primary_route", "fallback_route"):
        route = value[name]
        if not isinstance(route, dict) or set(route) != expected:
            raise ContractError(f"{name} fields are invalid")
        if not isinstance(route["provider"], str) or not route["provider"]:
            raise ContractError(f"{name}.provider must be non-empty")
        if not isinstance(route["model"], str) or not route["model"]:
            raise ContractError(f"{name}.model must be non-empty")
        if route["reasoning_effort"] not in ALLOWED_WORKER_REASONING_EFFORTS:
            raise ContractError(f"{name}.reasoning_effort is unsupported by the Codex runner")
        if route["service_tier"] is not None:
            raise ContractError(f"{name}.service_tier must be null")
        if route["profile"] is not None and not isinstance(route["profile"], str):
            raise ContractError(f"{name}.profile must be a string or null")
        routes.append(dict(route))
    return routes[0], routes[1]
MODEL_STATUS_PATH = REPO_ROOT / ".tooling" / "math-worker-model-status.json"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
REQUIRED_TASK_FIELDS = (
    "task_id",
    "objective",
    "mathematical_statement",
    "input_files",
    "allowed_tools",
    "bounds",
    "expected_artifacts",
    "success_condition",
    "falsification_condition",
    "stop_condition",
)
OPTIONAL_TASK_FIELDS = (
    "project_id", "notes", "schema_version", "task_kind", "timeout_seconds",
    "verification_steps", "requires_mathematical_judgment",
)
STATUSES = {
    "COMPLETED",
    "FALSIFIED",
    "NO_COUNTEREXAMPLE_WITHIN_SCOPE",
    "FORMAL_CHECK_PASSED",
    "BLOCKED",
    "TOOL_ERROR",
}
EVIDENCE_LEVELS = {
    "E0_SPECULATIVE",
    "E1_NUMERIC",
    "E2_EXACT_TESTED",
    "E3_REDUNDANT_EXACT",
    "E4_CERTIFIED",
    "E5_FORMAL",
}
WORKER_PERMISSION_PROFILE = "mechanical-one-shot"
_SENSITIVE_OUTPUT_PATTERNS = (
    (
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED_OPENAI_KEY]"),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|"
            r"secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"id[_-]?token|auth[_-]?token|session[_-]?token|client[_-]?secret|"
            r"password|passwd|credential|authorization|cookie|private[_-]?key|"
            r"secret)[\"']?\s*[=:]\s*[\"']?)[^\"'\s,;}]+"
        ),
        r"\1[REDACTED]",
    ),
)
FORBIDDEN_TOOL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:codex|subagents?|agents?|workers?|web(?:_search)?|"
    r"network|browsers?|plugins?|apps?|memory|multi[-_ ]?agent)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
JUDGMENT_MARKERS = (
    "choose a proof", "choose proof", "proof strategy", "select a lemma",
    "choose a lemma", "invent a lemma", "find an invariant", "choose an invariant",
    "research direction", "prioritize", "decide the truth", "judge whether",
    "derive a new lemma", "interpret the result", "规划证明", "证明策略",
    "选择引理", "寻找不变量", "选择不变量", "研究方向", "决定下一步",
    "判断命题", "解释数学意义",
)
PROOF_DIRECTIVE_MARKERS = (
    "prove the", "prove that", "disprove", "construct a proof",
    "证明该", "证明此", "证明命题", "给出证明", "构造证明", "证伪命题",
)
RESULT_FIELDS = (
    "task_id",
    "status",
    "evidence_level",
    "objective",
    "completed_scope",
    "key_findings",
    "counterexample",
    "artifacts",
    "commands",
    "toolchain",
    "interpretation",
    "limitations",
    "blocked_on",
    "observations",
)
RESERVED_ARTIFACT_NAMES = {
    "AGENTS.md",
    "math-tools.snapshot.json",
    "report.md",
    "result.json",
    "result.raw.json",
    "result.schema.json",
    "runner.json",
    "task.schema.json",
    "task_packet.json",
    "worker-command.json",
    "worker.stderr.log",
    "worker.stdout.jsonl",
    "worker_prompt.md",
}
SENSITIVE_INPUT_DIRECTORIES = frozenset({".git", ".codex", ".ssh", ".gnupg"})
SENSITIVE_INPUT_NAMES = frozenset({
    "auth.json", "credentials", "credentials.json", "credential.json",
    "secrets.json", "secret.json", ".git-credentials", ".netrc", ".npmrc",
    ".pypirc", "id_rsa", "id_ed25519",
})


class ContractError(ValueError):
    """Raised when a task packet or worker result violates the contract."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ContractError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


class BrokerReceipt:
    """Crash-recovery lease owned by the one-shot runner process.

    The controller may disappear while this process and its Codex child are
    still alive. A heartbeat lets the resumed controller observe that exact
    attempt instead of launching a duplicate. The receipt contains no prompt,
    result content, environment, or authentication material.
    """

    def __init__(
        self,
        path: Path,
        *,
        packet_path: Path,
        output_root: Path,
        timeout_seconds: int,
        interval_seconds: float = 2.0,
    ) -> None:
        self.path = path.resolve()
        self.interval_seconds = max(0.5, float(interval_seconds))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        started = utc_now()
        self._payload: dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "pid": os.getpid(),
            "packet_path": str(packet_path.resolve()),
            "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            "output_root": str(output_root.resolve()),
            "run_directory": None,
            "started_at": started.isoformat(),
            "heartbeat_at": started.isoformat(),
            "timeout_seconds": int(timeout_seconds),
            "finished_at": None,
        }
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="mechanical-broker-receipt",
            daemon=True,
        )

    def _write_locked(self) -> None:
        write_json(self.path, self._payload)

    def start(self) -> None:
        with self._lock:
            self._write_locked()
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            with self._lock:
                self._payload["heartbeat_at"] = utc_now().isoformat()
                self._write_locked()

    def set_run_directory(self, run_dir: Path) -> None:
        with self._lock:
            self._payload["run_directory"] = str(run_dir.resolve())
            self._payload["heartbeat_at"] = utc_now().isoformat()
            self._write_locked()

    def finish(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.interval_seconds + 1.0)
        with self._lock:
            finished = utc_now().isoformat()
            self._payload["status"] = "exited"
            self._payload["heartbeat_at"] = finished
            self._payload["finished_at"] = finished
            self._write_locked()


def create_broker_receipt(args: argparse.Namespace) -> BrokerReceipt | None:
    raw_receipt = args.broker_receipt
    if not args.broker_managed:
        if raw_receipt is not None:
            raise ContractError("--broker-receipt requires --broker-managed")
        return None
    if args.output_root is None or raw_receipt is None:
        raise ContractError(
            "broker-managed execution requires --output-root and --broker-receipt"
        )
    output_root = args.output_root.resolve()
    receipt = raw_receipt.resolve()
    allowed_root = (output_root.parent / "receipts").resolve()
    if receipt.parent != allowed_root or receipt.suffix.lower() != ".json":
        raise ContractError(
            "broker receipt must be one JSON file in the sibling receipts directory"
        )
    if receipt.exists():
        raise ContractError("broker receipt already exists for this attempt")
    lease = BrokerReceipt(
        receipt,
        packet_path=args.task_packet.resolve(),
        output_root=output_root,
        timeout_seconds=args.timeout,
    )
    lease.start()
    return lease


def broker_model_status_path(args: argparse.Namespace) -> Path:
    if not args.broker_managed:
        if args.model_status_path is not None:
            raise ContractError("--model-status-path requires --broker-managed")
        return MODEL_STATUS_PATH
    if args.output_root is None or args.model_status_path is None:
        raise ContractError(
            "broker-managed execution requires an isolated --model-status-path"
        )
    output_root = args.output_root.resolve()
    status_path = args.model_status_path.resolve()
    allowed_root = (output_root.parent / "model-status").resolve()
    if status_path.parent != allowed_root or status_path.suffix.lower() != ".json":
        raise ContractError(
            "broker model status must be one JSON file in the parent job model-status directory"
        )
    return status_path


def read_jsonl_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def cli_service_tier_attestation(path: Path) -> dict[str, Any]:
    """Fail closed if Codex JSONL ever reports a non-null service tier."""
    observed: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).replace("_", "").casefold()
                if normalized == "servicetier":
                    observed.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for event in read_jsonl_events(path):
        visit(event)
    violations = [value for value in observed if value not in {None, ""}]
    return {
        "status": (
            "violation" if violations else "none" if observed else "unobservable"
        ),
        "observed": violations[:10] if violations else None,
    }


def cli_model_route_attestation(
    path: Path, *, requested_model: str, requested_reasoning_effort: str,
) -> dict[str, Any]:
    """Reject an observed CLI model reroute and report only runtime evidence.

    Codex exec does not promise that every JSONL stream contains resolved model
    metadata.  In that case the honest observation is ``unobservable`` rather
    than copying the requested route into an ``actual`` field.  Explicit model
    reroute events or a mismatching resolved route fail closed.
    """
    observed_models: list[str] = []
    observed_efforts: list[str] = []
    reroute_events: list[dict[str, Any]] = []
    model_keys = {
        "model", "modelid", "modelname", "resolvedmodel", "actualmodel",
        "selectedmodel",
    }
    effort_keys = {
        "reasoningeffort", "modelreasoningeffort", "resolvedreasoningeffort",
        "actualreasoningeffort", "selectedreasoningeffort",
    }

    def normalized_key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value).casefold())

    for event in read_jsonl_events(path):
        event_name = str(event.get("type") or event.get("method") or "")
        normalized_event = normalized_key(event_name)
        if normalized_event in {
            "modelrerouted", "modelroutechanged", "modelroutingchanged",
        }:
            reroute_events.append({
                "type": event_name,
                "detail": sanitize_sensitive_output(
                    json.dumps(event, ensure_ascii=False, sort_keys=True)
                )[:1000],
            })
        containers = [event]
        for key in ("thread", "turn", "route", "params"):
            child = event.get(key)
            if isinstance(child, dict):
                containers.append(child)
        for container in containers:
            for key, value in container.items():
                normalized = normalized_key(key)
                if normalized in model_keys and isinstance(value, str) and value.strip():
                    observed_models.append(value.strip())
                elif normalized in effort_keys and isinstance(value, str) and value.strip():
                    observed_efforts.append(value.strip())

    unique_models = list(dict.fromkeys(observed_models))
    unique_efforts = list(dict.fromkeys(observed_efforts))
    mismatched_models = [item for item in unique_models if item != requested_model]
    mismatched_efforts = [
        item for item in unique_efforts if item != requested_reasoning_effort
    ]
    violation = bool(reroute_events or mismatched_models or mismatched_efforts)
    if violation:
        status = "violation"
    elif unique_models and unique_efforts:
        status = "matched"
    elif unique_models or unique_efforts:
        status = "partial"
    else:
        status = "unobservable"
    return {
        "status": status,
        "actual_model": unique_models[0] if len(unique_models) == 1 else None,
        "actual_reasoning_effort": (
            unique_efforts[0] if len(unique_efforts) == 1 else None
        ),
        "observed_models": unique_models,
        "observed_reasoning_efforts": unique_efforts,
        "reroute_events": reroute_events,
        "mismatched_models": mismatched_models,
        "mismatched_reasoning_efforts": mismatched_efforts,
    }


def _allowed_tool_aliases(labels: list[str]) -> set[str]:
    aliases: set[str] = set()
    known = {
        "python": {"python", "python3", "py"},
        "sage": {"sage"},
        "gap": {"gap"},
        "pari": {"gp"},
        "singular": {"singular"},
        "macaulay": {"m2"},
        "lean": {"lean", "lake"},
        "z3": {"z3"},
        "julia": {"julia"},
        "wolfram": {"wolframscript", "math"},
        "mathematica": {"wolframscript", "math"},
        "node": {"node", "npm", "npx"},
        "rust": {"rustc", "cargo"},
        "powershell": {"powershell", "pwsh"},
        "apply_patch": {"apply_patch"},
    }
    for raw in labels:
        normalized = raw.casefold()
        words = re.findall(r"[a-z0-9_.+-]+", normalized)
        if words:
            aliases.add(words[0].removesuffix(".exe").removesuffix(".cmd"))
        for marker, values in known.items():
            if marker in normalized:
                aliases.update(values)
    return aliases


def _command_executable(command: str) -> str:
    rendered = command.strip()
    while rendered.startswith("&"):
        rendered = rendered[1:].lstrip()
    match = re.match(r'''^(?:"([^"]+)"|'([^']+)'|(\S+))''', rendered)
    token = next((value for value in match.groups() if value), "") if match else ""
    basename = re.split(r"[\\/]", token)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1"):
        basename = basename.removesuffix(suffix)
    return basename


def _command_scope_violation(command: str, run_dir: Path) -> str | None:
    normalized = command.replace("/", "\\").casefold()
    run_text = str(run_dir.resolve()).replace("/", "\\").casefold()
    repo_text = str(REPO_ROOT.resolve()).replace("/", "\\").casefold()
    without_run = normalized.replace(run_text, "<run-directory>")
    if repo_text in without_run:
        return "command references repository files outside the isolated worker run"
    if re.search(r"(?:^|[\s\"'=])\.\.[\\/]", normalized):
        return "command contains a parent-directory escape"
    if re.search(
        r"(?i)(?:\.codex[\\/]|auth\.json|credentials?(?:\.json)?|"
        r"get-childitem\s+env:|printenv(?:\s|$)|\$env:)",
        command,
    ):
        return "command attempts to inspect authentication or environment material"
    return None


def forbidden_worker_activity(
    path: Path,
    *,
    packet: dict[str, Any] | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Detect recursive, unapproved-tool, or out-of-scope worker activity."""
    forbidden_item_types = {
        "collabtoolcall", "mcp_tool_call", "mcptoolcall", "dynamictoolcall",
        "websearch", "web_search", "browser", "appcall", "plugincall",
    }
    forbidden_command = re.compile(
        r"(?i)(?:^|[\\/\s\"'])codex(?:\.cmd|\.exe)?(?:\s|$|[\"'])|"
        r"run_worker\.py|delegate_mechanical_task\.py|"
        r"tools[./\\]autonomous_math_research|spawn_agent|create_thread"
    )
    for event in read_jsonl_events(path):
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        normalized_type = item_type.replace("-", "").replace("_", "").casefold()
        if normalized_type in {
            value.replace("-", "").replace("_", "").casefold()
            for value in forbidden_item_types
        }:
            return {
                "item_type": item_type,
                "command": None,
                "reason": "forbidden recursive/network/plugin/app tool event",
            }
        raw_command = item.get("command")
        command = (
            raw_command
            if isinstance(raw_command, str)
            else " ".join(map(str, raw_command)) if isinstance(raw_command, list)
            else None
        )
        if isinstance(command, str) and forbidden_command.search(command):
            return {
                "item_type": item_type,
                "command": sanitize_sensitive_output(command)[:1000],
                "reason": "forbidden recursive worker or harness command",
            }
        if isinstance(command, str) and packet is not None:
            executable = _command_executable(command)
            aliases = _allowed_tool_aliases(list(packet.get("allowed_tools") or []))
            if not executable or executable not in aliases:
                return {
                    "item_type": item_type,
                    "command": sanitize_sensitive_output(command)[:1000],
                    "reason": (
                        f"command executable {executable!r} is not in the packet's "
                        "allowed_tools"
                    ),
                }
            if run_dir is not None:
                scope_reason = _command_scope_violation(command, run_dir)
                if scope_reason:
                    return {
                        "item_type": item_type,
                        "command": sanitize_sensitive_output(command)[:1000],
                        "reason": scope_reason,
                    }
    return None


def extract_turn_usage(path: Path) -> dict[str, int] | None:
    totals: dict[str, int] = {}
    for event in read_jsonl_events(path):
        if event.get("type") != "turn.completed" or not isinstance(
            event.get("usage"), dict
        ):
            continue
        for key, value in event["usage"].items():
            if isinstance(value, int) and value >= 0:
                totals[key] = totals.get(key, 0) + value
    return totals or None


def requested_configuration(
    *, model: str, reasoning_effort: str, service_tier: str | None
) -> dict[str, Any]:
    # Preserve the legacy keys while making their requested-only meaning explicit.
    return {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "requested_model": model,
        "requested_reasoning_effort": reasoning_effort,
        "requested_service_tier": service_tier,
        "actual_model": None,
        "actual_reasoning_effort": None,
        "model_route_attestation": "unobservable",
        "actual_configuration_observation": (
            "Resolved model and reasoning effort were not reported by available runtime metadata."
        ),
    }


def classify_probe_failure(stdout_path: Path, stderr_path: Path) -> dict[str, Any]:
    events = read_jsonl_events(stdout_path)
    service_failures = [
        event
        for event in events
        if event.get("type") == "turn.failed" and isinstance(event.get("error"), dict)
    ]
    service_fragments = [
        json.dumps(event["error"], ensure_ascii=False) for event in service_failures
    ]
    fragments = list(service_fragments)
    if stderr_path.is_file():
        try:
            fragments.append(stderr_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    detail = "\n".join(fragments).strip()[:4000]
    # Only a structured service-side turn failure may poison the persistent
    # route cache. Stderr is retained for diagnosis, but a local filesystem or
    # wrapper message containing "access denied" must never disable Spark.
    service_detail = "\n".join(service_fragments).lower()
    permanent_unavailable_markers = (
        "model_not_found",
        "access_denied",
        "access denied",
        "do not have access",
        "does not have access",
        "not available to your account",
        "not available to this account",
        "not available to your project",
        "not available to this project",
    )
    cache_as_unavailable = bool(service_failures) and any(
        marker in service_detail for marker in permanent_unavailable_markers
    )
    return {
        "failure_scope": "service" if service_failures else "local-or-transport",
        "failure_detail": detail or None,
        "cache_as_unavailable": cache_as_unavailable,
    }


def empty_model_status() -> dict[str, Any]:
    return {"schema_version": 1, "unavailable": []}


def load_model_status(path: Path = MODEL_STATUS_PATH) -> dict[str, Any]:
    if not path.is_file():
        return empty_model_status()
    value = read_json(path)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("unavailable"), list)
    ):
        raise ContractError(f"invalid worker model status file: {path}")
    for record in value["unavailable"]:
        if not isinstance(record, dict):
            raise ContractError(f"invalid worker model status record: {path}")
        for field in ("model", "failed_at_utc", "error", "run_directory"):
            if not isinstance(record.get(field), str):
                raise ContractError(f"invalid {field} in worker model status: {path}")
        if record.get("service_tier") is not None and not isinstance(
            record.get("service_tier"), str
        ):
            raise ContractError(f"invalid service_tier in worker model status: {path}")
        if record.get("reasoning_effort") is not None and not isinstance(
            record.get("reasoning_effort"), str
        ):
            raise ContractError(f"invalid reasoning_effort in worker model status: {path}")
    return value


def same_model_configuration(
    record: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str | None,
    service_tier: str | None,
) -> bool:
    return (
        record.get("model") == model
        and record.get("reasoning_effort") == reasoning_effort
        and record.get("service_tier") == service_tier
    )


def cached_model_unavailable(
    *,
    model: str,
    reasoning_effort: str | None,
    service_tier: str | None,
    path: Path = MODEL_STATUS_PATH,
) -> dict[str, Any] | None:
    status = load_model_status(path)
    for record in status["unavailable"]:
        if same_model_configuration(
            record,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
        ):
            return record
    return None


def record_model_unavailable(
    *,
    model: str,
    reasoning_effort: str | None,
    service_tier: str | None,
    error: str,
    run_dir: Path,
    path: Path = MODEL_STATUS_PATH,
) -> dict[str, Any]:
    status = load_model_status(path)
    status["unavailable"] = [
        record
        for record in status["unavailable"]
        if not same_model_configuration(
            record,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
        )
    ]
    record = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "service_tier": service_tier,
        "failed_at_utc": utc_now().isoformat(),
        "error": error,
        "run_directory": run_dir.relative_to(REPO_ROOT).as_posix(),
    }
    status["unavailable"].append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, status)
    return record


def clear_model_unavailable(
    *,
    model: str,
    reasoning_effort: str | None,
    service_tier: str | None,
    path: Path = MODEL_STATUS_PATH,
) -> None:
    if not path.is_file():
        return
    status = load_model_status(path)
    remaining = [
        record
        for record in status["unavailable"]
        if not same_model_configuration(
            record,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
        )
    ]
    if len(remaining) != len(status["unavailable"]):
        status["unavailable"] = remaining
        write_json(path, status)


def require_nonempty_string(mapping: dict[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value


def require_string_list(mapping: dict[str, Any], field: str, *, nonempty: bool) -> list[str]:
    value = mapping.get(field)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ContractError(f"{field} must be a list of non-empty strings")
    if nonempty and not value:
        raise ContractError(f"{field} must not be empty")
    return value


def relative_artifact_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ContractError(f"artifact path must stay inside the run directory: {raw}")
    if path.parts[0] in {".", ""}:
        raise ContractError(f"artifact path must be normalized: {raw}")
    return path


def validate_expected_artifact_path(raw: str) -> Path:
    path = relative_artifact_path(raw)
    if path.parts[0] in {"inputs", "model-probe"} or path.as_posix() in RESERVED_ARTIFACT_NAMES:
        raise ContractError(f"expected artifact uses a runner-reserved path: {raw}")
    return path


def repository_input_path(raw: str) -> Path:
    relative = Path(raw)
    parts = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    if (
        bool(set(parts) & SENSITIVE_INPUT_DIRECTORIES)
        or name in SENSITIVE_INPUT_NAMES
        or name == ".env"
        or name.startswith(".env.")
    ):
        raise ContractError(
            f"input file is authentication/VCS metadata and is forbidden: {raw}"
        )
    candidate = (REPO_ROOT / relative).resolve()
    try:
        candidate.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ContractError(f"input file is outside the repository: {raw}") from exc
    if not candidate.is_file():
        raise ContractError(f"input file does not exist or is not a file: {raw}")
    return candidate


def validate_task_packet(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("task packet must be a JSON object")
    missing = [field for field in REQUIRED_TASK_FIELDS if field not in value]
    if missing:
        raise ContractError(f"missing required task fields: {', '.join(missing)}")
    unknown = sorted(set(value) - set(REQUIRED_TASK_FIELDS) - set(OPTIONAL_TASK_FIELDS))
    if unknown:
        raise ContractError(f"unknown task fields: {', '.join(unknown)}")

    task_id = require_nonempty_string(value, "task_id")
    if not TASK_ID_RE.fullmatch(task_id):
        raise ContractError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,99}")
    for field in (
        "objective",
        "mathematical_statement",
        "success_condition",
        "falsification_condition",
        "stop_condition",
    ):
        require_nonempty_string(value, field)
    inputs = require_string_list(value, "input_files", nonempty=False)
    allowed_tools = require_string_list(value, "allowed_tools", nonempty=True)
    artifacts = require_string_list(value, "expected_artifacts", nonempty=True)
    if not isinstance(value.get("bounds"), dict) or not value["bounds"]:
        raise ContractError("bounds must be a non-empty JSON object")
    for raw in inputs:
        repository_input_path(raw)
    for raw in artifacts:
        validate_expected_artifact_path(raw)
    if len(set(artifacts)) != len(artifacts):
        raise ContractError("expected_artifacts contains duplicates")

    project_id = value.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ContractError("project_id must use lowercase letters, digits, and hyphens")
    notes = value.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ContractError("notes must be a string when provided")
    if "schema_version" in value and value["schema_version"] != 1:
        raise ContractError("unsupported task packet schema_version")
    if "task_kind" in value and (
        not isinstance(value["task_kind"], str) or not value["task_kind"].strip()
    ):
        raise ContractError("task_kind must be a non-empty string")
    if "timeout_seconds" in value and (
        type(value["timeout_seconds"]) is not int or value["timeout_seconds"] <= 0
    ):
        raise ContractError("timeout_seconds must be a positive integer")
    if "verification_steps" in value:
        require_string_list(value, "verification_steps", nonempty=True)
    if "requires_mathematical_judgment" in value and (
        value["requires_mathematical_judgment"] is not False
    ):
        raise ContractError("one-shot worker cannot accept mathematical judgment")
    directive_parts = [
        value.get("objective"), value.get("success_condition"),
        value.get("falsification_condition"), value.get("stop_condition"),
        value.get("notes"), json.dumps(value.get("bounds"), ensure_ascii=False),
        *(value.get("verification_steps") or []),
    ]
    directive_text = " ".join(str(item).casefold() for item in directive_parts)
    all_text = (
        f"{str(value.get('mathematical_statement')).casefold()} {directive_text}"
    )
    judgment_marker = next(
        (marker for marker in JUDGMENT_MARKERS if marker in all_text), None
    )
    if judgment_marker is None:
        judgment_marker = next(
            (
                marker
                for marker in PROOF_DIRECTIVE_MARKERS
                if marker in directive_text
            ),
            None,
        )
    if judgment_marker:
        raise ContractError(
            f"one-shot worker task crosses strategy/judgment boundary: {judgment_marker!r}"
        )
    for tool in allowed_tools:
        forbidden = FORBIDDEN_TOOL_RE.search(tool)
        if forbidden is not None:
            raise ContractError(
                "one-shot worker allowed_tools contains forbidden capability "
                f"{forbidden.group(0)!r}: {tool!r}"
            )
    return value


def default_output_root(packet: dict[str, Any]) -> Path:
    del packet
    return REPO_ROOT / ".amr" / "mechanical-worker-runs"


def checked_output_root(raw: Path, packet: dict[str, Any]) -> Path:
    root = raw if raw.is_absolute() else REPO_ROOT / raw
    root = root.resolve()
    try:
        root.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ContractError("output root must stay inside the repository") from exc
    return root / packet["task_id"]


def create_run_directory(root: Path) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def copy_inputs(packet: dict[str, Any], run_dir: Path) -> None:
    input_root = run_dir / "inputs"
    input_root.mkdir()
    for raw in packet["input_files"]:
        source = repository_input_path(raw)
        relative = source.relative_to(REPO_ROOT.resolve())
        target = input_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def immutable_snapshot(run_dir: Path) -> dict[str, str]:
    paths = [
        run_dir / "AGENTS.md",
        run_dir / "result.schema.json",
        run_dir / "task.schema.json",
        run_dir / "task_packet.json",
        run_dir / "worker_prompt.md",
    ]
    tooling = run_dir / "math-tools.snapshot.json"
    if tooling.is_file():
        paths.append(tooling)
    input_root = run_dir / "inputs"
    if input_root.is_dir():
        paths.extend(path for path in input_root.rglob("*") if path.is_file())
    return {
        path.relative_to(run_dir).as_posix(): file_digest(path)
        for path in sorted(paths, key=lambda item: item.as_posix())
    }


def verify_immutable_snapshot(run_dir: Path, before: dict[str, str]) -> None:
    after = immutable_snapshot(run_dir)
    if after != before:
        changed = sorted(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )
        raise ContractError(
            "worker modified runner-controlled files: " + ", ".join(changed)
        )


def find_codex() -> Path:
    # Prefer the npm wrapper on Windows. The Microsoft Store app alias may resolve
    # to a WindowsApps path that Python can see but cannot execute directly.
    candidates = ("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return Path(found).resolve()
    raise ContractError("Codex CLI not found on PATH")


def codex_prefix(codex: Path) -> list[str]:
    if os.name == "nt" and codex.suffix.lower() in {".cmd", ".bat"}:
        command_processor = (
            os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        )
        return [command_processor, "/d", "/s", "/c", str(codex)]
    return [str(codex)]


def run_capture(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContractError(f"failed to inspect Codex CLI: {exc}") from exc


def inspect_codex(codex: Path) -> str:
    version_run = run_capture([*codex_prefix(codex), "--version"])
    version = (version_run.stdout or version_run.stderr).strip()
    if version_run.returncode != 0 or not version:
        raise ContractError("Codex CLI version probe failed")
    help_run = run_capture([*codex_prefix(codex), "exec", "--help"])
    help_text = help_run.stdout + help_run.stderr
    required_flags = (
        "--ephemeral",
        "--output-schema",
        "--output-last-message",
        "--model",
        "--sandbox",
        "--json",
        "--ignore-user-config",
        "--strict-config",
    )
    missing = [flag for flag in required_flags if flag not in help_text]
    if help_run.returncode != 0 or missing:
        detail = ", ".join(missing) if missing else "exec help failed"
        raise ContractError(f"installed Codex CLI lacks required exec support: {detail}")
    return version


def codex_command(
    codex: Path,
    *,
    model: str,
    reasoning_effort: str,
    service_tier: str | None,
    cwd: Path,
    schema: Path,
    last_message: Path,
    permission_profile: str,
    workspace_access: str,
) -> list[str]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", permission_profile):
        raise ContractError("invalid mechanical permission profile name")
    if workspace_access not in {"read", "write"}:
        raise ContractError("mechanical workspace access must be read or write")
    command = [
        *codex_prefix(codex),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--strict-config",
        "--json",
        "--model",
        model,
        "-C",
        str(cwd),
        "--skip-git-repo-check",
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(last_message),
    ]
    command.extend(
        ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
    )
    # Explicit null is essential: omission could inherit a user's fast or
    # priority override, which this worker is never allowed to use.
    command.extend(["-c", f"service_tier={json.dumps(service_tier)}"])
    # Permission profiles apply only to model-started local commands.  Codex
    # itself still receives CODEX_HOME long enough to reuse the existing login,
    # while every child command is denied access to the rest of the filesystem
    # and can access only minimal runtime paths plus this isolated workspace.
    command.extend([
        "-c", f"default_permissions={json.dumps(permission_profile)}",
        "-c", f'permissions.{permission_profile}.filesystem.":root"="deny"',
        "-c", f'permissions.{permission_profile}.filesystem.":minimal"="read"',
        "-c", (
            f'permissions.{permission_profile}.filesystem.'
            f'":workspace_roots"."."={json.dumps(workspace_access)}'
        ),
        "-c", f"permissions.{permission_profile}.network.enabled=false",
    ])
    for key, value in WORKER_ISOLATION_OVERRIDES.items():
        command.extend(["-c", f"{key}={json.dumps(value)}"])
    command.append("-")
    return command


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _normalized_path_entry(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.strip().strip('"')))


def isolated_worker_environment(
    *, blocked_executable: Path | None = None,
) -> dict[str, str]:
    """Reuse Codex login state while hiding secrets and recursive CLI entrypoints."""
    environment = dict(os.environ)
    secret_markers = (
        "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
        "ACCESS_KEY", "CREDENTIAL", "AUTHORIZATION", "COOKIE",
    )
    credential_helpers = {
        "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS",
        "GOOGLE_APPLICATION_CREDENTIALS", "AWS_PROFILE", "AZURE_CONFIG_DIR",
        "DOCKER_CONFIG", "KUBECONFIG",
    }
    for key in tuple(environment):
        normalized = key.upper()
        if normalized in credential_helpers or any(
            marker in normalized for marker in secret_markers
        ):
            environment.pop(key, None)
    path_key = next((key for key in environment if key.upper() == "PATH"), None)
    if path_key is not None:
        blocked_directory = (
            _normalized_path_entry(str(blocked_executable.resolve().parent))
            if blocked_executable is not None
            else None
        )
        retained: list[str] = []
        for raw_entry in environment[path_key].split(os.pathsep):
            if not raw_entry.strip():
                continue
            normalized = _normalized_path_entry(raw_entry)
            if blocked_directory is not None and normalized == blocked_directory:
                continue
            entry_path = Path(raw_entry.strip().strip('"'))
            if any(
                (entry_path / name).is_file()
                for name in ("codex", "codex.exe", "codex.cmd", "codex.bat")
            ):
                continue
            retained.append(raw_entry)
        environment[path_key] = os.pathsep.join(retained)
    environment["MATH_MECHANICAL_ONE_SHOT"] = "1"
    # CODEX_HOME is intentionally retained. It is a location, not secret
    # material; the Codex CLI itself may use its existing login there.
    return environment


def public_codex_command(command: list[str], codex: Path) -> list[str]:
    """Return a reproducible descriptor without publishing a recursive CLI path."""
    codex_text = os.path.normcase(str(codex.resolve()))
    return [
        "<codex-cli>"
        if os.path.normcase(str(part).strip('"')) == codex_text
        else str(part)
        for part in command
    ]


def run_one_shot(
    command: list[str],
    *,
    prompt: str,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
    blocked_executable: Path | None = None,
) -> tuple[int | None, bool, float]:
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    stdout_text = ""
    stderr_text = ""
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            start_new_session=os.name != "nt",
            env=isolated_worker_environment(
                blocked_executable=blocked_executable,
            ),
        )
    except OSError as exc:
        stderr_text = f"failed to start Codex CLI: {exc}\n"
        stdout_path.write_text("", encoding="utf-8", newline="")
        stderr_path.write_text(
            sanitize_sensitive_output(stderr_text), encoding="utf-8", newline="",
        )
        return None, False, time.monotonic() - started
    timed_out = False
    try:
        stdout_text, stderr_text = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process(proc)
        try:
            stdout_text, stderr_text = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_text, stderr_text = proc.communicate()
    stdout_path.write_text(
        sanitize_sensitive_output(stdout_text), encoding="utf-8", newline="",
    )
    stderr_path.write_text(
        sanitize_sensitive_output(stderr_text), encoding="utf-8", newline="",
    )
    return proc.returncode, timed_out, time.monotonic() - started


def sanitize_sensitive_output(value: Any) -> str:
    text = str(value or "")
    text = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
    for pattern, replacement in _SENSITIVE_OUTPUT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_sensitive_file(path: Path) -> None:
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    path.write_text(
        sanitize_sensitive_output(text), encoding="utf-8", newline="",
    )


def validate_worker_result(value: Any, packet: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("worker result must be a JSON object")
    if set(value) != set(RESULT_FIELDS):
        missing = sorted(set(RESULT_FIELDS) - set(value))
        extra = sorted(set(value) - set(RESULT_FIELDS))
        raise ContractError(f"worker result fields differ; missing={missing}, extra={extra}")
    if value["task_id"] != packet["task_id"]:
        raise ContractError("worker changed task_id")
    if value["objective"] != packet["objective"]:
        raise ContractError("worker changed objective")
    if value["status"] not in STATUSES:
        raise ContractError("worker returned an invalid primary status")
    if value["evidence_level"] not in EVIDENCE_LEVELS:
        raise ContractError("worker returned an invalid evidence level")
    for field in (
        "completed_scope",
        "interpretation",
    ):
        if not isinstance(value[field], str):
            raise ContractError(f"worker result {field} must be a string")
    for field in (
        "key_findings",
        "artifacts",
        "commands",
        "toolchain",
        "limitations",
        "observations",
    ):
        require_string_list(value, field, nonempty=False)
    for field in ("counterexample", "blocked_on"):
        if value[field] is not None and not isinstance(value[field], str):
            raise ContractError(f"worker result {field} must be a string or null")
    if value["status"] == "BLOCKED" and not value["blocked_on"]:
        raise ContractError("BLOCKED requires the smallest unresolved decision in blocked_on")
    if value["status"] == "FALSIFIED" and not value["counterexample"]:
        raise ContractError("FALSIFIED requires an exact counterexample")
    if value["status"] == "NO_COUNTEREXAMPLE_WITHIN_SCOPE" and not value["completed_scope"].strip():
        raise ContractError("NO_COUNTEREXAMPLE_WITHIN_SCOPE requires completed_scope")

    allowed_aliases = _allowed_tool_aliases(packet["allowed_tools"])
    for command in value["commands"]:
        executable = _command_executable(command)
        if not executable or executable not in allowed_aliases:
            raise ContractError(
                f"replay command executable {executable!r} is not in allowed_tools"
            )
        scope_reason = _command_scope_violation(command, run_dir)
        if scope_reason:
            raise ContractError(f"replay command violates isolated file scope: {scope_reason}")

    successful = value["status"] in {
        "COMPLETED",
        "FALSIFIED",
        "NO_COUNTEREXAMPLE_WITHIN_SCOPE",
        "FORMAL_CHECK_PASSED",
    }
    declared = set(value["artifacts"])
    if successful:
        missing_declarations = set(packet["expected_artifacts"]) - declared
        if missing_declarations:
            raise ContractError(
                "successful result omitted expected artifacts: "
                + ", ".join(sorted(missing_declarations))
            )
    for raw in value["artifacts"]:
        relative = relative_artifact_path(raw)
        if not (run_dir / relative).is_file():
            raise ContractError(f"declared artifact does not exist: {raw}")
    if successful:
        for raw in packet["expected_artifacts"]:
            if not (run_dir / relative_artifact_path(raw)).is_file():
                raise ContractError(f"expected artifact does not exist: {raw}")
    return value


def tool_error_result(
    packet: dict[str, Any],
    message: str,
    *,
    artifacts: list[str] | None = None,
    commands: list[str] | None = None,
    toolchain: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": packet["task_id"],
        "status": "TOOL_ERROR",
        "evidence_level": "E0_SPECULATIVE",
        "objective": packet["objective"],
        "completed_scope": "",
        "key_findings": [message],
        "counterexample": None,
        "artifacts": artifacts or [],
        "commands": commands or [],
        "toolchain": toolchain or [],
        "interpretation": "The worker mechanism failed before producing contract-valid evidence.",
        "limitations": ["Inspect preserved stdout, stderr, command, and runner metadata."],
        "blocked_on": None,
        "observations": [],
    }


def markdown_list(values: list[str]) -> str:
    return "\n".join(f"- {item}" for item in values) if values else "- None recorded"


def write_report(run_dir: Path, packet: dict[str, Any], result: dict[str, Any]) -> None:
    report = f"""# One-shot worker report: {packet['task_id']}

## Primary status

`{result['status']}`

## Evidence level

`{result['evidence_level']}`

## Exact objective

{packet['objective']}

## Mathematical statement or computational question

{packet['mathematical_statement']}

## Completed scope

{result['completed_scope'] or '<none>'}

## Key findings

{markdown_list(result['key_findings'])}

## Counterexample

{result['counterexample'] or '<none>'}

## Commands

{markdown_list(result['commands'])}

## Toolchain

{markdown_list(result['toolchain'])}

## Interpretation

{result['interpretation'] or '<none>'}

## Artifacts

{markdown_list(result['artifacts'])}

## Limitations

{markdown_list(result['limitations'])}

## Blocked on

{result['blocked_on'] or '<not blocked>'}

## Observations returned to controller

{markdown_list(result['observations'])}

Observations were recorded only. This worker did not act on them or enqueue another task.
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")


def worker_prompt(packet: dict[str, Any]) -> str:
    return f"""You are a one-shot mathematical execution worker, not a research controller.

Execute exactly the single task in task_packet.json. Its normalized contents are below:

{json.dumps(packet, indent=2, ensure_ascii=False)}

Hard boundaries:
- Preserve the mathematical statement, objective, assumptions, bounds, and stop condition exactly.
- Use only the computational tools listed in allowed_tools. Built-in file editing is allowed only to create the expected artifacts.
- Read only task_packet.json and copied files under inputs/. Do not inspect unrelated repository or project material.
- Do not choose a new lemma, proof strategy, invariant, classification, research objective, or model.
- Do not invoke Codex, resume a session, spawn any agent or worker, enqueue work, or investigate an observation.
- Stop immediately at success, falsification, scope exhaustion, blockage, tool error, timeout risk, or the stated stop condition.
- If a new mathematical judgment is needed, return BLOCKED with all evidence obtained and the smallest decision required in blocked_on.
- For a counterexample search that exhausts its scope, return NO_COUNTEREXAMPLE_WITHIN_SCOPE rather than COMPLETED. It is never proof outside the completed scope.

Create every expected artifact under this run directory. Preserve source code, exact commands, parameters, finite ranges, seeds, versions, outputs, interpretation, and limitations. Record unexpected structure only in observations and do not act on it.

In commands, list only exact replay commands for computation or verification; do not include file-edit pseudo-commands or placeholders. Your final response must be only one JSON object matching result.schema.json. It must use exactly one primary status and must copy task_id and objective verbatim. After returning that JSON, terminate.
"""


def worker_agents_text() -> str:
    return """# One-shot execution worker

- Execute only `task_packet.json`; do not perform open-ended mathematical research.
- Do not alter the statement, objective, bounds, or stop condition.
- Do not invoke Codex, spawn workers or agents, resume, enqueue, or chain tasks.
- Write only the assigned artifacts and final structured result.
- Record observations without acting on them, then terminate.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_packet", type=Path)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--retry-unavailable-route",
        dest="retry_unavailable_route",
        action="store_true",
        help="Explicitly retry a worker model configuration previously marked unavailable.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the task packet without creating a run or starting Codex.",
    )
    parser.add_argument(
        "--broker-managed",
        action="store_true",
        help="Attest that this invocation came from the autonomous controller broker.",
    )
    parser.add_argument(
        "--broker-receipt",
        type=Path,
        default=None,
        help="Controller-owned crash-recovery lease path (broker-managed execution only).",
    )
    parser.add_argument(
        "--model-status-path",
        type=Path,
        default=None,
        help="Attempt-local model circuit-breaker snapshot (broker-managed execution only).",
    )
    parser.add_argument(
        "--route-config",
        type=Path,
        default=None,
        help="Controller-owned provider/model route configuration.",
    )
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be a positive integer")

    run_dir: Path | None = None
    packet: dict[str, Any] | None = None
    receipt: BrokerReceipt | None = None
    try:
        receipt = create_broker_receipt(args)
        model_status_path = broker_model_status_path(args)
        primary_route, fallback_route_config = load_worker_routes(
            args.route_config, broker_managed=bool(args.broker_managed),
        )
        fixed_routes = (primary_route, fallback_route_config)
        packet_path = args.task_packet.resolve()
        raw_packet = read_json(packet_path)
        task_schema = read_json(TASK_SCHEMA_SOURCE)
        validate_output_schema_compatibility(
            task_schema, schema_path=TASK_SCHEMA_SOURCE,
        )
        validate(raw_packet, task_schema)
        packet = validate_task_packet(raw_packet)
        validate_output_schema_compatibility(
            read_json(RESULT_SCHEMA_SOURCE), schema_path=RESULT_SCHEMA_SOURCE,
        )
        if args.broker_managed:
            packet_timeout = packet.get("timeout_seconds")
            if type(packet_timeout) is not int or packet_timeout != args.timeout:
                raise ContractError(
                    "broker-managed timeout must exactly match task_packet.timeout_seconds"
                )
        if args.validate_only:
            print(f"VALID {packet['task_id']}")
            return 0
        base_root = args.output_root or default_output_root(packet)
        run_root = checked_output_root(base_root, packet)
        run_dir = create_run_directory(run_root)
        if receipt is not None:
            receipt.set_run_directory(run_dir)
        write_json(run_dir / "task_packet.json", packet)
        copy_inputs(packet, run_dir)
        shutil.copy2(RESULT_SCHEMA_SOURCE, run_dir / "result.schema.json")
        shutil.copy2(TASK_SCHEMA_SOURCE, run_dir / "task.schema.json")
        (run_dir / "AGENTS.md").write_text(worker_agents_text(), encoding="utf-8")
        prompt = worker_prompt(packet)
        (run_dir / "worker_prompt.md").write_text(prompt, encoding="utf-8")
        tooling = REPO_ROOT / ".tooling" / "math-tools.json"
        if tooling.is_file():
            shutil.copy2(tooling, run_dir / "math-tools.snapshot.json")
        immutable_before = immutable_snapshot(run_dir)

        forbidden_overrides = {
            key: os.environ.get(key)
            for key in (
                "MATH_WORKER_MODEL", "MATH_WORKER_REASONING_EFFORT",
                "MATH_WORKER_SERVICE_TIER",
            )
            if os.environ.get(key, "").strip()
        }
        if forbidden_overrides:
            raise ContractError(
                "worker routes are controller-configured and environment overrides are forbidden; "
                f"environment overrides are forbidden: {sorted(forbidden_overrides)}"
            )

        started_at = utc_now()
        codex = find_codex()
        codex_version = inspect_codex(codex)
        route_attempts: list[dict[str, Any]] = []
        fallback: dict[str, Any] | None = None
        selected_route: dict[str, Any] | None = None
        failure: dict[str, Any] | None = None

        for route_index, route in enumerate(fixed_routes):
            model = str(route["model"])
            reasoning_effort = str(route["reasoning_effort"])
            service_tier = route["service_tier"]
            unavailable = cached_model_unavailable(
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                path=model_status_path,
            )
            if unavailable is not None and args.retry_unavailable_route:
                clear_model_unavailable(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    service_tier=service_tier,
                    path=model_status_path,
                )
                unavailable = None
            if unavailable is not None:
                route_attempts.append({
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "service_tier": None,
                    "cached_unavailable": unavailable,
                    "worker_usage": None,
                })
                if route_index == 0:
                    fallback = {
                        "from_provider": primary_route["provider"],
                        "from_model": primary_route["model"],
                        "to_provider": fallback_route_config["provider"],
                        "to_model": fallback_route_config["model"],
                        "reason": "primary exact configuration cached as permanently unavailable",
                        "primary_actual_attempted": False,
                    }
                    continue
                failure = {
                    "kind": "model_unavailable",
                    "retryable": False,
                    "message": (
                        "fallback configuration is cached as permanently unavailable; "
                        "no other model is permitted"
                    ),
                }
                break

            route_attempts.append({
                "model": model,
                "reasoning_effort": reasoning_effort,
                "service_tier": None,
                "availability_check": "first_actual_execution",
                "worker_usage": None,
            })
            selected_route = dict(route)
            break

        if selected_route is None:
            failure = failure or {
                "kind": "model_unavailable", "retryable": False,
                "message": "no permitted mechanical worker route is available",
            }
            preserved = []
            result = tool_error_result(
                packet,
                str(failure["message"]),
                artifacts=preserved,
                toolchain=[
                    f"Codex CLI {codex_version}",
                    f"primary={primary_route['model']}/{primary_route['reasoning_effort']}/null",
                    f"fallback={fallback_route_config['model']}/{fallback_route_config['reasoning_effort']}/null",
                ],
            )
            write_json(run_dir / "result.json", result)
            write_report(run_dir, packet, result)
            write_json(run_dir / "runner.json", {
                "codex_executable": codex.name,
                "codex_version": codex_version,
                "broker_managed": bool(args.broker_managed),
                "fixed_routes": list(fixed_routes),
                "requested_provider": primary_route["provider"],
                "selected_provider": None,
                "selected_provider_profile": None,
                "selected_model": None,
                "selected_reasoning_effort": None,
                "selected_service_tier": None,
                "isolation_overrides": WORKER_ISOLATION_OVERRIDES,
                "permission_profiles": {"worker": WORKER_PERMISSION_PROFILE},
                "route_attempts": route_attempts,
                "fallback": fallback,
                "failure": failure,
                "worker_started": False,
                "result_source": "runner",
                "started_at_utc": started_at.isoformat(),
                "finished_at_utc": utc_now().isoformat(),
            })
            print(run_dir)
            print("TOOL_ERROR: no permitted actual worker route is available")
            return 4

        model = str(selected_route["model"])
        reasoning_effort = str(selected_route["reasoning_effort"])
        provider = str(selected_route["provider"])
        provider_profile = selected_route.get("profile")
        service_tier = None

        last_message = run_dir / "result.raw.json"
        command = codex_command(
            codex,
            model=model,
            reasoning_effort=reasoning_effort,
            service_tier=service_tier,
            cwd=run_dir,
            schema=run_dir / "result.schema.json",
            last_message=last_message,
            permission_profile=WORKER_PERMISSION_PROFILE,
            workspace_access="write",
        )
        public_command = public_codex_command(command, codex)
        write_json(run_dir / "worker-command.json", public_command)
        exit_code, timed_out, runtime = run_one_shot(
            command,
            prompt=prompt,
            stdout_path=run_dir / "worker.stdout.jsonl",
            stderr_path=run_dir / "worker.stderr.log",
            timeout=args.timeout,
            blocked_executable=codex,
        )
        # --output-last-message bypasses run_one_shot's stdout/stderr capture;
        # redact it before any validation, preservation, or report path can read it.
        sanitize_sensitive_file(last_message)
        result_source = "worker"
        execution_failure: dict[str, Any] | None = None
        worker_tier_attestation = {
            "status": "unobservable", "observed": None,
        }
        worker_model_attestation = {
            "status": "unobservable",
            "actual_model": None,
            "actual_reasoning_effort": None,
            "observed_models": [],
            "observed_reasoning_efforts": [],
            "reroute_events": [],
            "mismatched_models": [],
            "mismatched_reasoning_efforts": [],
        }
        try:
            worker_tier_attestation = cli_service_tier_attestation(
                run_dir / "worker.stdout.jsonl"
            )
            worker_model_attestation = cli_model_route_attestation(
                run_dir / "worker.stdout.jsonl",
                requested_model=model,
                requested_reasoning_effort=reasoning_effort,
            )
            if worker_tier_attestation["status"] == "violation":
                execution_failure = {
                    "kind": "service_tier_policy", "retryable": False,
                    "message": "mechanical worker reported a forbidden non-null service tier",
                }
                raise ContractError(str(execution_failure["message"]))
            if worker_model_attestation["status"] == "violation":
                execution_failure = {
                    "kind": "model_route_policy", "retryable": False,
                    "message": "mechanical worker reported a forbidden model reroute or route mismatch",
                    "detail": worker_model_attestation,
                }
                raise ContractError(str(execution_failure["message"]))
            activity_violation = forbidden_worker_activity(
                run_dir / "worker.stdout.jsonl", packet=packet, run_dir=run_dir,
            )
            if activity_violation is not None:
                execution_failure = {
                    "kind": "worker_capability_policy", "retryable": False,
                    "message": "mechanical worker used a forbidden capability or file scope",
                    "detail": activity_violation,
                }
                raise ContractError(str(execution_failure["message"]))
            if timed_out:
                execution_failure = {
                    "kind": "timeout_transient", "retryable": True,
                    "message": f"worker exceeded timeout of {args.timeout} seconds",
                }
                raise ContractError(f"worker exceeded timeout of {args.timeout} seconds")
            if exit_code != 0:
                classified = classify_probe_failure(
                    run_dir / "worker.stdout.jsonl", run_dir / "worker.stderr.log",
                )
                if classified.get("cache_as_unavailable"):
                    record_model_unavailable(
                        model=model,
                        reasoning_effort=reasoning_effort,
                        service_tier=None,
                        error=f"worker execution rejected exact route: exit {exit_code}",
                        run_dir=run_dir,
                        path=model_status_path,
                    )
                    if model == str(primary_route["model"]):
                        fallback = {
                            "from_provider": primary_route["provider"],
                            "from_model": primary_route["model"],
                            "to_provider": fallback_route_config["provider"],
                            "to_model": fallback_route_config["model"],
                            "reason": (
                                "primary worker execution returned permanent unavailable/access denied; "
                                "controller must continue once with cached-primary Luna fallback"
                            ),
                            "primary_actual_attempted": True,
                            "continuation_required": True,
                        }
                execution_failure = {
                    "kind": (
                        "model_unavailable" if classified.get("cache_as_unavailable")
                        else "transport_transient"
                    ),
                    "retryable": not bool(classified.get("cache_as_unavailable")),
                    "message": f"worker exited with code {exit_code}",
                    "detail": classified.get("failure_detail"),
                }
                raise ContractError(f"worker exited with code {exit_code}")
            verify_immutable_snapshot(run_dir, immutable_before)
            result = validate_worker_result(read_json(last_message), packet, run_dir)
        except ContractError as exc:
            result_source = "runner"
            if execution_failure is None:
                execution_failure = {
                    "kind": "model_output_protocol", "retryable": True,
                    "message": str(exc),
                }
            preserved = ["worker.stdout.jsonl", "worker.stderr.log", "worker-command.json"]
            if last_message.is_file():
                preserved.append("result.raw.json")
            result = tool_error_result(
                packet,
                str(exc),
                artifacts=preserved,
                commands=[
                    shlex.join(public_command)
                    if os.name != "nt"
                    else subprocess.list2cmdline(public_command)
                ],
                toolchain=[f"Codex CLI {codex_version}", f"model={model}"],
            )
        worker_usage = extract_turn_usage(run_dir / "worker.stdout.jsonl")
        for attempt in reversed(route_attempts):
            if attempt.get("model") == model:
                attempt["worker_usage"] = worker_usage
                break
        if result.get("status") == "TOOL_ERROR" and execution_failure is None:
            execution_failure = {
                "kind": "mechanical_tool_error", "retryable": False,
                "message": "mechanical worker reported TOOL_ERROR",
            }
        write_json(run_dir / "result.json", result)
        write_report(run_dir, packet, result)
        write_json(
            run_dir / "runner.json",
            {
                "codex_executable": codex.name,
                "codex_version": codex_version,
                "broker_managed": bool(args.broker_managed),
                "fixed_routes": list(fixed_routes),
                "requested_provider": provider,
                "selected_provider": provider,
                "selected_provider_profile": provider_profile,
                **requested_configuration(
                    model=model,
                    reasoning_effort=reasoning_effort,
                    service_tier=None,
                ),
                "selected_model": model,
                "selected_reasoning_effort": reasoning_effort,
                "selected_service_tier": None,
                "actual_model": worker_model_attestation["actual_model"],
                "actual_reasoning_effort": worker_model_attestation[
                    "actual_reasoning_effort"
                ],
                "model_route_attestation": worker_model_attestation["status"],
                "model_route_evidence": worker_model_attestation,
                "actual_configuration_observation": (
                    "Codex JSONL resolved route matched the exact request."
                    if worker_model_attestation["status"] == "matched"
                    else "Codex JSONL did not expose a complete resolved route."
                    if worker_model_attestation["status"] in {"partial", "unobservable"}
                    else "Codex JSONL reported a forbidden model reroute or mismatch."
                ),
                "observed_service_tier": worker_tier_attestation["observed"],
                "service_tier_attestation": worker_tier_attestation["status"],
                "isolation_overrides": WORKER_ISOLATION_OVERRIDES,
                "permission_profiles": {"worker": WORKER_PERMISSION_PROFILE},
                "route_attempts": route_attempts,
                "fallback": fallback,
                "failure": execution_failure,
                "worker_started": True,
                "worker_exit_code": exit_code,
                "worker_timed_out": timed_out,
                "worker_runtime_seconds": runtime,
                "worker_usage": worker_usage,
                "result_source": result_source,
                "started_at_utc": started_at.isoformat(),
                "finished_at_utc": utc_now().isoformat(),
            },
        )
        print(run_dir)
        print(result["status"])
        if result["status"] == "BLOCKED":
            return 3
        if result["status"] == "TOOL_ERROR":
            return 4
        return 0
    except ContractError as exc:
        if run_dir is not None and packet is not None:
            result = tool_error_result(
                packet,
                f"runner preflight failed: {exc}",
                artifacts=[
                    name
                    for name in ("task_packet.json", "result.schema.json", "worker_prompt.md")
                    if (run_dir / name).is_file()
                ],
            )
            write_json(run_dir / "result.json", result)
            write_report(run_dir, packet, result)
            write_json(
                run_dir / "runner.json",
                {
                    "worker_started": False,
                    "result_source": "runner",
                    "preflight_error": str(exc),
                    "finished_at_utc": utc_now().isoformat(),
                },
            )
            print(run_dir)
            print("TOOL_ERROR: runner preflight failed")
            return 4
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if receipt is not None:
            receipt.finish()


if __name__ == "__main__":
    sys.exit(main())
