"""Filesystem client for a controller-owned mechanical subworker broker.

This client never starts Codex or another worker. It only submits one request
bound to the current parent job and waits for the controller's response.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any


SCHEMA_VERSION = 1
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
CONFIG_FIELDS = {
    "schema_version", "enabled", "parent_job_id", "parent_task_id", "parent_role",
    "client_sha256", "workspace_sha256", "requests_dir", "responses_dir", "deadline_epoch",
    "poll_interval_seconds",
}
REQUEST_FIELDS = {
    "schema_version", "parent_job_id", "parent_task_id", "parent_role",
    "submitted_at", "task_packet", "request_sha256",
}
RESPONSE_FIELDS = {
    "schema_version", "parent_job_id", "parent_task_id", "parent_role",
    "subtask_id", "status", "model", "reasoning_effort", "service_tier",
    "token_usage", "token_telemetry", "result", "artifacts",
    "runner_directory", "fallback", "error", "failure_kind", "retryable",
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    os.replace(temporary, path)


def _workspace_digest(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()


def _control_root(script: Path) -> Path:
    workspace = script.parent.resolve()
    run_dir = workspace.parent.parent.resolve()
    return run_dir / "mechanical_broker_control" / _workspace_digest(workspace)


def _load_config(script: Path) -> dict[str, Any]:
    workspace = script.parent.resolve()
    control_root = _control_root(script)
    path = control_root / "mechanical_broker.json"
    value = _read_json(path)
    if not isinstance(value, dict) or set(value) != CONFIG_FIELDS:
        raise ValueError("mechanical broker configuration is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["enabled"] is not True:
        raise ValueError("mechanical broker is not enabled for this parent job")
    if _file_hash(script) != str(value["client_sha256"]):
        raise ValueError("mechanical broker client does not match its pinned digest")
    if value["workspace_sha256"] != _workspace_digest(workspace):
        raise ValueError("mechanical broker configuration is bound to another workspace")
    requests = Path(value["requests_dir"]).resolve()
    responses = Path(value["responses_dir"]).resolve()
    if not requests.is_relative_to(workspace):
        raise ValueError("mechanical request queue escapes the parent workspace")
    if responses != (control_root / "responses").resolve():
        raise ValueError("mechanical response queue is not controller-owned")
    if responses.is_relative_to(workspace):
        raise ValueError("mechanical response queue must not be parent-writable")
    if float(value["deadline_epoch"]) <= time.time():
        raise ValueError("parent job deadline has already expired")
    return value


def _request_for_packet(
    config: dict[str, Any], packet: dict[str, Any], request_path: Path,
) -> dict[str, Any]:
    if request_path.exists():
        existing = _read_json(request_path)
        if not isinstance(existing, dict) or set(existing) != REQUEST_FIELDS:
            raise ValueError("existing mechanical request has an invalid contract")
        unsigned = dict(existing)
        reported_hash = str(unsigned.pop("request_sha256") or "")
        if reported_hash != _stable_hash(unsigned):
            raise ValueError("existing mechanical request hash is invalid")
        expected_bindings = {
            "schema_version": SCHEMA_VERSION,
            "parent_job_id": config["parent_job_id"],
            "parent_task_id": config["parent_task_id"],
            "parent_role": config["parent_role"],
            "task_packet": packet,
        }
        for key, expected in expected_bindings.items():
            if existing.get(key) != expected:
                raise ValueError(
                    "subtask_id was already used for a different request or parent binding"
                )
        return existing
    request = {
        "schema_version": SCHEMA_VERSION,
        "parent_job_id": config["parent_job_id"],
        "parent_task_id": config["parent_task_id"],
        "parent_role": config["parent_role"],
        "submitted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "task_packet": packet,
    }
    request["request_sha256"] = _stable_hash(request)
    _atomic_json(request_path, request)
    return request


def _validate_response(
    response: Any,
    *,
    config: dict[str, Any],
    subtask_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != RESPONSE_FIELDS:
        raise ValueError("controller returned an invalid mechanical response")
    expected_bindings = {
        "schema_version": SCHEMA_VERSION,
        "parent_job_id": config["parent_job_id"],
        "parent_task_id": config["parent_task_id"],
        "parent_role": config["parent_role"],
        "subtask_id": subtask_id,
    }
    for key, expected in expected_bindings.items():
        if response.get(key) != expected:
            raise ValueError(f"controller response {key} does not match broker binding")
    if response["service_tier"] is not None:
        raise ValueError("controller response violates the no-tier policy")
    route = (response.get("model"), response.get("reasoning_effort"))
    if route not in {
        ("gpt-5.3-codex-spark", "high"),
        ("gpt-5.6-luna", "medium"),
        (None, None),
    }:
        raise ValueError("controller response used an unapproved mechanical route")
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_packet", type=Path)
    args = parser.parse_args()
    try:
        script = Path(__file__).resolve()
        workspace = script.parent.resolve()
        config = _load_config(script)
        packet_path = args.task_packet.resolve()
        if not packet_path.is_relative_to(workspace):
            raise ValueError("task packet must be inside the parent job workspace")
        packet = _read_json(packet_path)
        if not isinstance(packet, dict):
            raise ValueError("task packet must be a JSON object")
        subtask_id = str(packet.get("task_id") or "")
        if not TASK_ID_RE.fullmatch(subtask_id):
            raise ValueError("task packet has an invalid task_id")
        requests = Path(config["requests_dir"]).resolve()
        responses = Path(config["responses_dir"]).resolve()
        if not requests.is_relative_to(workspace):
            raise ValueError("broker request queue escapes the parent job workspace")
        if responses != (_control_root(script) / "responses").resolve():
            raise ValueError("broker response queue is not controller-owned")
        request_path = requests / f"{subtask_id}.json"
        response_path = responses / f"{subtask_id}.json"
        _request_for_packet(config, packet, request_path)
        deadline = float(config["deadline_epoch"])
        interval = max(0.05, float(config["poll_interval_seconds"]))
        while time.time() < deadline:
            if response_path.is_file():
                response = _validate_response(
                    _read_json(response_path), config=config, subtask_id=subtask_id,
                )
                print(json.dumps(response, ensure_ascii=False, sort_keys=True))
                return 0 if response["status"] not in {"TOOL_ERROR", "REJECTED"} else 4
            time.sleep(interval)
        raise TimeoutError("controller did not return a mechanical response before parent deadline")
    except Exception as exc:
        print(f"MECHANICAL_BROKER_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
