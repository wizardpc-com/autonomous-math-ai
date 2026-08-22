from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .models import stable_hash, utc_now
from .storage import atomic_write_bytes, append_jsonl, read_jsonl


TRANSITION_SCHEMA_VERSION = 1


def json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def bytes_sha256(payload: bytes) -> str:
    return sha256(payload).hexdigest()


class CanonicalTransitionStore:
    """Crash-recoverable, append-only transactions for canonical claim state."""

    def __init__(self, *, project_root: Path, runtime_root: Path):
        self.project_root = project_root.resolve()
        self.root = runtime_root.resolve() / "state" / "canonical_transitions"
        self.ledger_path = self.root / "TRANSITIONS.jsonl"

    def _project_path(self, uri: str) -> Path:
        if not uri.startswith("project://"):
            raise ValueError("canonical transition target is not project-relative")
        path = (self.project_root / uri.removeprefix("project://")).resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("canonical transition target escapes the project")
        return path

    def _project_uri(self, path: Path) -> str:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("canonical transition target escapes the project")
        return "project://" + resolved.relative_to(self.project_root).as_posix()

    def _bundle_file(self, transition_id: str, relative: str) -> Path:
        bundle = (self.root / transition_id).resolve()
        path = (bundle / relative).resolve()
        if not path.is_relative_to(bundle):
            raise ValueError("canonical transition bundle path escapes its bundle")
        return path

    def records(self) -> list[dict[str, Any]]:
        return read_jsonl(self.ledger_path)

    def target_paths(self, transition_id: str) -> tuple[Path, ...]:
        prepared = next(
            (
                item for item in reversed(self.records())
                if item.get("kind") == "PREPARED"
                and item.get("transition_id") == transition_id
            ),
            None,
        )
        if prepared is None:
            raise ValueError("canonical transition lacks a prepared record")
        return tuple(
            self._project_path(str(target["path"]))
            for target in prepared["targets"]
        )

    def recover(self) -> list[str]:
        records = self.records()
        latest: dict[str, dict[str, Any]] = {}
        ordered: list[str] = []
        for record in records:
            transition_id = str(record.get("transition_id") or "")
            if not transition_id:
                raise ValueError("canonical transition ledger entry lacks an id")
            if transition_id not in latest:
                ordered.append(transition_id)
            latest[transition_id] = record

        recovered: list[str] = []
        for transition_id in ordered:
            record = latest[transition_id]
            if record.get("kind") != "PREPARED":
                continue
            targets = record.get("targets")
            if not isinstance(targets, list) or not targets:
                raise ValueError("prepared canonical transition has no targets")
            for target in targets:
                live = self._project_path(str(target["path"]))
                current = live.read_bytes() if live.is_file() else b""
                current_hash = bytes_sha256(current)
                if current_hash not in {
                    str(target["before_sha256"]), str(target["after_sha256"]),
                }:
                    raise ValueError(
                        "canonical transition recovery found an unknown target state: "
                        f"{target['path']}"
                    )
            for target in targets:
                live = self._project_path(str(target["path"]))
                staged = self._bundle_file(
                    transition_id, str(target["after_snapshot"]),
                )
                payload = staged.read_bytes()
                if bytes_sha256(payload) != str(target["after_sha256"]):
                    raise ValueError("canonical transition after-snapshot is invalid")
                if not live.is_file() or bytes_sha256(live.read_bytes()) != str(
                    target["after_sha256"]
                ):
                    atomic_write_bytes(live, payload)
            self._append_terminal(
                transition_id, "COMMITTED", recovered=True,
                authorization=dict(record.get("authorization") or {}),
            )
            recovered.append(transition_id)
        self.verify_current()
        return recovered

    def verify_current(self) -> None:
        records = self.records()
        committed = [item for item in records if item.get("kind") == "COMMITTED"]
        if not committed:
            return
        transition_id = str(committed[-1]["transition_id"])
        prepared = next(
            (
                item for item in reversed(records)
                if item.get("kind") == "PREPARED"
                and item.get("transition_id") == transition_id
            ),
            None,
        )
        if prepared is None:
            raise ValueError("committed canonical transition lacks a prepared record")
        for target in prepared["targets"]:
            live = self._project_path(str(target["path"]))
            if not live.is_file() or bytes_sha256(live.read_bytes()) != str(
                target["after_sha256"]
            ):
                raise ValueError(
                    "canonical state differs from the latest committed transition: "
                    f"{target['path']}"
                )

    def commit(
        self,
        *,
        targets: dict[Path, bytes],
        authorization: dict[str, Any],
        trusted_state_path: Path,
        claim_graph_sha256: str,
    ) -> str:
        if not targets:
            raise ValueError("canonical transition requires at least one target")
        self.recover()
        trusted_state_path = trusted_state_path.resolve()
        if trusted_state_path not in {path.resolve() for path in targets}:
            raise ValueError("canonical transition lacks its trusted-state target")
        before_hashes = {
            self._project_uri(path): bytes_sha256(
                path.read_bytes() if path.is_file() else b""
            )
            for path in targets
        }
        seed = {
            "authorization": authorization,
            "before": before_hashes,
            "after": {
                self._project_uri(path): bytes_sha256(payload)
                for path, payload in targets.items()
                if path.resolve() != trusted_state_path.resolve()
            },
            "claim_graph_sha256": claim_graph_sha256,
        }
        transition_id = f"transition-{stable_hash(seed)[:24]}"

        trusted_payload = json.loads(
            next(
                payload for path, payload in targets.items()
                if path.resolve() == trusted_state_path
            ).decode("utf-8")
        )
        if not isinstance(trusted_payload, dict):
            raise ValueError("canonical trusted state must be a JSON object")
        trusted_payload["claim_graph_sha256"] = claim_graph_sha256
        trusted_payload["last_transition_id"] = transition_id
        targets = dict(targets)
        trusted_target = next(
            path for path in targets if path.resolve() == trusted_state_path
        )
        targets[trusted_target] = json_bytes(trusted_payload)

        existing = self.records()
        if any(
            item.get("transition_id") == transition_id
            and item.get("kind") == "COMMITTED"
            for item in existing
        ):
            self.verify_current()
            return transition_id

        target_records: list[dict[str, str]] = []
        for index, (path, after) in enumerate(
            sorted(targets.items(), key=lambda item: self._project_uri(item[0]))
        ):
            before = path.read_bytes() if path.is_file() else b""
            before_name = f"before/{index:03d}-{path.name}"
            after_name = f"after/{index:03d}-{path.name}"
            atomic_write_bytes(
                self._bundle_file(transition_id, before_name), before,
            )
            atomic_write_bytes(
                self._bundle_file(transition_id, after_name), after,
            )
            target_records.append({
                "path": self._project_uri(path),
                "before_sha256": bytes_sha256(before),
                "after_sha256": bytes_sha256(after),
                "before_snapshot": before_name,
                "after_snapshot": after_name,
            })
        append_jsonl(self.ledger_path, {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "kind": "PREPARED",
            "transition_id": transition_id,
            "timestamp": utc_now(),
            "authorization": authorization,
            "targets": target_records,
        })
        self.recover()
        return transition_id

    def _append_terminal(
        self,
        transition_id: str,
        kind: str,
        *,
        recovered: bool,
        authorization: dict[str, Any],
    ) -> None:
        append_jsonl(self.ledger_path, {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "kind": kind,
            "transition_id": transition_id,
            "timestamp": utc_now(),
            "recovered": bool(recovered),
            "authorization": authorization,
        })
