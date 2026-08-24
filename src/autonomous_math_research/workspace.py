from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from .storage import atomic_write_json, file_digest


class WorkspaceManager:
    def __init__(self, repository_root: Path, run_dir: Path):
        self.repository_root = repository_root.resolve()
        self.run_dir = run_dir.resolve()

    def create_job_workspace(
        self,
        task_id: str,
        modifies_code: bool = False,
        *,
        job_id: str | None = None,
    ) -> tuple[Path, list[Path], dict[str, Any]]:
        safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
        safe_job = (
            re.sub(r"[^A-Za-z0-9_.-]", "_", job_id)
            if job_id is not None else None
        )
        safe = f"{safe_task}--{safe_job}" if safe_job else safe_task
        if modifies_code:
            path = self.run_dir / "worktrees" / safe
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(path), "HEAD"],
                    cwd=self.repository_root, check=True, capture_output=True, text=True,
                )
            metadata = {"kind": "git_worktree", "path": str(path), "commit": self._git_head(path)}
        else:
            path = self.run_dir / "jobs" / safe
            path.mkdir(parents=True, exist_ok=True)
            metadata = {"kind": "isolated_output", "path": str(path), "commit": self._git_head(self.repository_root)}
        if job_id is not None:
            metadata.update({"task_id": task_id, "job_id": job_id})
        (path / "artifacts").mkdir(parents=True, exist_ok=True)
        atomic_write_json(path / "workspace.json", metadata)
        return path, [path], metadata

    def write_task_packet(self, workspace: Path, packet: dict[str, Any]) -> Path:
        target = workspace / "task_packet.json"
        atomic_write_json(target, packet)
        return target

    def materialize_input(self, workspace: Path, source: Path) -> Path:
        """Copy one controller-selected input into a job-local readable root."""
        root = workspace.resolve()
        resolved = source.resolve()
        if not resolved.is_file():
            raise ValueError(f"job input is unavailable: {source}")
        digest = file_digest(resolved)
        target = root / "inputs" / digest / (resolved.name or "input")
        if not target.resolve().is_relative_to(root):
            raise ValueError("job input target escapes its workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or file_digest(target) != digest:
                raise ValueError("job input copy has an invalid digest")
        else:
            shutil.copy2(resolved, target)
        if file_digest(target) != digest:
            raise ValueError("job input copy failed digest verification")
        return target

    def materialize_required_file_access(
        self,
        workspace: Path,
        access: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        materialized: list[dict[str, str]] = []
        for item in access:
            copied = self.materialize_input(workspace, Path(item["path"]))
            materialized.append({
                "reference": item["reference"],
                "path": str(copied),
                "sha256": file_digest(copied),
            })
        return materialized

    def mechanical_broker_control_dir(self, workspace: Path) -> Path:
        """Return a controller-owned sibling area not writable by the parent job."""
        resolved = workspace.resolve()
        workspace_key = hashlib.sha256(
            str(resolved).encode("utf-8")
        ).hexdigest()
        return self.run_dir / "mechanical_broker_control" / workspace_key

    def mechanical_broker_config_path(self, workspace: Path) -> Path:
        return self.mechanical_broker_control_dir(workspace) / "mechanical_broker.json"

    def mechanical_broker_response_root(self, workspace: Path) -> Path:
        return self.mechanical_broker_control_dir(workspace) / "responses"

    def install_mechanical_broker_client(
        self,
        workspace: Path,
        *,
        parent_job_id: str,
        parent_task_id: str,
        parent_role: str,
        parent_timeout_seconds: float,
        enabled: bool,
        broker_client_source: Path | None = None,
        broker_client_sha256: str | None = None,
    ) -> str:
        """Install the queue-only client; it cannot launch a worker itself."""
        if not enabled:
            return "DISABLED_BY_PINNED_POLICY"
        if broker_client_source is None or not broker_client_sha256:
            raise ValueError("enabled mechanical broker requires a pinned client snapshot")
        source = broker_client_source.resolve()
        if (
            not source.is_file()
            or file_digest(source) != str(broker_client_sha256)
        ):
            raise ValueError("pinned mechanical broker client snapshot is missing or modified")
        target = workspace / "delegate_mechanical_task.py"
        shutil.copy2(source, target)
        if file_digest(target) != str(broker_client_sha256):
            raise ValueError("installed mechanical broker client failed digest verification")
        queue_root = workspace / "mechanical_broker"
        requests = queue_root / "requests"
        control_root = self.mechanical_broker_control_dir(workspace)
        responses = control_root / "responses"
        requests.mkdir(parents=True, exist_ok=True)
        responses.mkdir(parents=True, exist_ok=True)
        atomic_write_json(control_root / "mechanical_broker.json", {
            "schema_version": 1,
            "enabled": bool(enabled),
            "parent_job_id": parent_job_id,
            "parent_task_id": parent_task_id,
            "parent_role": parent_role,
            "client_sha256": str(broker_client_sha256),
            "workspace_sha256": hashlib.sha256(
                str(workspace.resolve()).encode("utf-8")
            ).hexdigest(),
            "requests_dir": str(requests.resolve()),
            "responses_dir": str(responses.resolve()),
            "deadline_epoch": time.time() + max(1.0, float(parent_timeout_seconds)),
            "poll_interval_seconds": 0.2,
        })
        return subprocess.list2cmdline([
            sys.executable, str(target.resolve()), "PATH_TO_MECHANICAL_TASK_PACKET.json",
        ])

    @staticmethod
    def _git_head(path: Path) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None
