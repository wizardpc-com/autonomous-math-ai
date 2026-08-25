from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .models import stable_hash, utc_now
from .storage import atomic_write_bytes, append_jsonl, read_jsonl


LEGACY_TRANSITION_SCHEMA_VERSION = 1
TRANSITION_SCHEMA_VERSION = 2
_SUPPORTED_TRANSITION_SCHEMA_VERSIONS = frozenset({
    LEGACY_TRANSITION_SCHEMA_VERSION,
    TRANSITION_SCHEMA_VERSION,
})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREPARED_KEYS = frozenset({
    "schema_version", "kind", "transition_id", "timestamp", "authorization",
    "preconditions", "targets",
})
_LEGACY_PREPARED_KEYS = _PREPARED_KEYS - {"preconditions"}
_COMMITTED_KEYS = frozenset({
    "schema_version", "kind", "transition_id", "timestamp", "recovered",
    "authorization",
})
_PRECONDITION_KEYS = frozenset({"path", "sha256"})
_TARGET_KEYS = frozenset({
    "path", "before_sha256", "after_sha256", "before_snapshot",
    "after_snapshot",
})


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

    @staticmethod
    def _require_exact_keys(
        value: Any, keys: frozenset[str], label: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError(f"{label} fields are invalid")
        return value

    @staticmethod
    def _require_sha256(value: Any, label: str) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{label} is not a SHA256 digest")
        return value

    def _validate_prepared_record(
        self, record: dict[str, Any], transition_id: str,
    ) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise ValueError("prepared canonical transition fields are invalid")
        schema_version = record.get("schema_version")
        legacy = (
            schema_version == LEGACY_TRANSITION_SCHEMA_VERSION
            and set(record) == _LEGACY_PREPARED_KEYS
        )
        if not legacy:
            self._require_exact_keys(
                record, _PREPARED_KEYS, "prepared canonical transition",
            )
        if schema_version not in _SUPPORTED_TRANSITION_SCHEMA_VERSIONS:
            raise ValueError("prepared canonical transition schema is unsupported")
        prepared = record
        if prepared["kind"] != "PREPARED" or prepared["transition_id"] != transition_id:
            raise ValueError("prepared canonical transition identity is invalid")
        if not isinstance(prepared["timestamp"], str) or not prepared["timestamp"]:
            raise ValueError("prepared canonical transition timestamp is invalid")
        authorization = prepared["authorization"]
        if not isinstance(authorization, dict):
            raise ValueError("prepared canonical transition authorization is invalid")

        raw_preconditions = [] if legacy else prepared["preconditions"]
        if not isinstance(raw_preconditions, list):
            raise ValueError("canonical transition preconditions are invalid")
        preconditions: dict[str, str] = {}
        for raw in raw_preconditions:
            item = self._require_exact_keys(
                raw, _PRECONDITION_KEYS, "canonical transition precondition",
            )
            uri = str(item["path"])
            self._project_path(uri)
            if uri in preconditions:
                raise ValueError("canonical transition preconditions contain duplicate paths")
            preconditions[uri] = self._require_sha256(
                item["sha256"], "canonical transition precondition",
            )

        raw_targets = prepared["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError("prepared canonical transition has no targets")
        targets: dict[str, dict[str, Any]] = {}
        trusted_targets: list[tuple[str, dict[str, Any]]] = []
        for raw in raw_targets:
            target = self._require_exact_keys(
                raw, _TARGET_KEYS, "canonical transition target",
            )
            uri = str(target["path"])
            self._project_path(uri)
            if uri in targets:
                raise ValueError("canonical transition targets contain duplicate paths")
            before_sha = self._require_sha256(
                target["before_sha256"], "canonical transition before digest",
            )
            after_sha = self._require_sha256(
                target["after_sha256"], "canonical transition after digest",
            )
            before = self._bundle_file(transition_id, str(target["before_snapshot"]))
            after = self._bundle_file(transition_id, str(target["after_snapshot"]))
            if not before.is_file() or bytes_sha256(before.read_bytes()) != before_sha:
                raise ValueError("canonical transition before-snapshot is invalid")
            if not after.is_file() or bytes_sha256(after.read_bytes()) != after_sha:
                raise ValueError("canonical transition after-snapshot is invalid")
            normalized = dict(target)
            targets[uri] = normalized
            try:
                payload = json.loads(after.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("last_transition_id") == transition_id
            ):
                trusted_targets.append((uri, payload))
        if len(trusted_targets) != 1:
            raise ValueError(
                "canonical transition must identify exactly one trusted-state target"
            )
        trusted_uri, trusted_payload = trusted_targets[0]
        if legacy:
            trusted_before_path = self._bundle_file(
                transition_id, str(targets[trusted_uri]["before_snapshot"]),
            )
            try:
                trusted_before = json.loads(
                    trusted_before_path.read_text(encoding="utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "legacy canonical trusted-state before-snapshot is invalid"
                ) from exc
            if not isinstance(trusted_before, dict) or (
                "semantic_alignment" in trusted_before
                or "semantic_alignment" in trusted_payload
                or str(authorization.get("kind", "")).startswith("SEMANTIC_")
            ):
                raise ValueError(
                    "legacy canonical transition cannot authorize semantic trust state"
                )
        claim_graph_sha256 = self._require_sha256(
            trusted_payload.get("claim_graph_sha256"),
            "canonical trusted-state claim graph digest",
        )
        claim_graph_uri = trusted_payload.get("claim_graph")
        if claim_graph_uri is None:
            matching_graph_targets = [
                uri for uri, target in targets.items()
                if uri != trusted_uri and target["after_sha256"] == claim_graph_sha256
            ]
            claim_graph_uri = (
                matching_graph_targets[0] if len(matching_graph_targets) == 1 else None
            )
        if not isinstance(claim_graph_uri, str) or (
            claim_graph_uri not in targets
            or targets[claim_graph_uri]["after_sha256"] != claim_graph_sha256
        ):
            raise ValueError("canonical trusted state does not bind its claim graph target")
        seed: dict[str, Any] = {
            "authorization": authorization,
            "before": {
                uri: target["before_sha256"] for uri, target in targets.items()
            },
            "after": {
                uri: target["after_sha256"]
                for uri, target in targets.items() if uri != trusted_uri
            },
            "claim_graph_sha256": claim_graph_sha256,
        }
        if not legacy:
            seed["preconditions"] = dict(sorted(preconditions.items()))
        expected_id = f"transition-{stable_hash(seed)[:24]}"
        if expected_id != transition_id:
            raise ValueError("canonical transition identity does not match its transaction digest")
        if not legacy:
            return prepared
        normalized = dict(prepared)
        normalized["preconditions"] = []
        return normalized

    def _validated_ledger(
        self, records: list[dict[str, Any]], *, verify_current: bool,
    ) -> tuple[list[str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        ordered: list[str] = []
        prepared_by_id: dict[str, dict[str, Any]] = {}
        committed_by_id: dict[str, dict[str, Any]] = {}
        committed: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("canonical transition ledger entry is invalid")
            transition_id = record.get("transition_id")
            if not isinstance(transition_id, str) or not transition_id:
                raise ValueError("canonical transition ledger entry lacks an id")
            kind = record.get("kind")
            if kind == "PREPARED":
                if transition_id in prepared_by_id:
                    raise ValueError("canonical transition has duplicate PREPARED records")
                prepared_by_id[transition_id] = self._validate_prepared_record(
                    record, transition_id,
                )
                ordered.append(transition_id)
                continue
            if kind != "COMMITTED":
                raise ValueError("canonical transition ledger contains an unsupported record")
            committed_record = self._require_exact_keys(
                record, _COMMITTED_KEYS, "committed canonical transition",
            )
            if committed_record["schema_version"] not in (
                _SUPPORTED_TRANSITION_SCHEMA_VERSIONS
            ):
                raise ValueError("committed canonical transition schema is unsupported")
            if not isinstance(committed_record["timestamp"], str) or not committed_record[
                "timestamp"
            ]:
                raise ValueError("committed canonical transition timestamp is invalid")
            if type(committed_record["recovered"]) is not bool:
                raise ValueError("committed canonical transition recovery marker is invalid")
            if transition_id in committed_by_id:
                raise ValueError("canonical transition has duplicate COMMITTED records")
            prepared = prepared_by_id.get(transition_id)
            if prepared is None:
                raise ValueError("committed canonical transition lacks a prepared record")
            if committed_record["schema_version"] != prepared["schema_version"]:
                raise ValueError(
                    "committed canonical transition schema disagrees with PREPARED"
                )
            if committed_record["authorization"] != prepared["authorization"]:
                raise ValueError(
                    "committed canonical transition authorization disagrees with PREPARED"
                )
            committed_by_id[transition_id] = committed_record
            committed.append(committed_record)
        if verify_current and committed:
            transition_id = str(committed[-1]["transition_id"])
            prepared = prepared_by_id[transition_id]
            for target in prepared["targets"]:
                live = self._project_path(str(target["path"]))
                if not live.is_file() or bytes_sha256(live.read_bytes()) != str(
                    target["after_sha256"]
                ):
                    raise ValueError(
                        "canonical state differs from the latest committed transition: "
                        f"{target['path']}"
                    )
        return ordered, prepared_by_id, committed

    def verified_committed_records(self, *, recover: bool = True) -> list[dict[str, Any]]:
        """Return only COMMITTED records proven to belong to canonical transactions."""
        if recover:
            self.recover()
        _, _, committed = self._validated_ledger(
            self.records(), verify_current=True,
        )
        return [dict(item) for item in committed]

    def verified_committed_authorizations(
        self, *, recover: bool = True,
    ) -> list[dict[str, Any]]:
        return [
            dict(item["authorization"])
            for item in self.verified_committed_records(recover=recover)
        ]

    def verified_committed_transactions(
        self, *, recover: bool = True,
    ) -> list[dict[str, Any]]:
        """Return verified authorizations with their exact before/after snapshots."""
        if recover:
            self.recover()
        _, prepared_by_id, committed = self._validated_ledger(
            self.records(), verify_current=True,
        )
        transactions: list[dict[str, Any]] = []
        for record in committed:
            transition_id = str(record["transition_id"])
            prepared = prepared_by_id[transition_id]
            targets: list[dict[str, Any]] = []
            for target in prepared["targets"]:
                before = self._bundle_file(
                    transition_id, str(target["before_snapshot"]),
                ).read_bytes()
                after = self._bundle_file(
                    transition_id, str(target["after_snapshot"]),
                ).read_bytes()
                if bytes_sha256(before) != str(target["before_sha256"]) or (
                    bytes_sha256(after) != str(target["after_sha256"])
                ):
                    raise ValueError("canonical transition snapshot is invalid")
                targets.append({
                    "path": str(target["path"]),
                    "before": before,
                    "after": after,
                })
            transactions.append({
                "transition_id": transition_id,
                "authorization": dict(record["authorization"]),
                "targets": targets,
            })
        return transactions

    def verified_prepared_record(
        self, transition_id: str, *, recover: bool = True,
    ) -> dict[str, Any]:
        committed = self.verified_committed_records(recover=recover)
        if transition_id not in {str(item["transition_id"]) for item in committed}:
            raise ValueError("canonical transition is not a verified committed transaction")
        _, prepared_by_id, _ = self._validated_ledger(
            self.records(), verify_current=True,
        )
        return dict(prepared_by_id[transition_id])

    def target_paths(self, transition_id: str) -> tuple[Path, ...]:
        prepared = self.verified_prepared_record(transition_id)
        return tuple(
            self._project_path(str(target["path"]))
            for target in prepared["targets"]
        )

    def recover(self) -> list[str]:
        records = self.records()
        ordered, prepared_by_id, committed = self._validated_ledger(
            records, verify_current=False,
        )
        committed_ids = {str(item["transition_id"]) for item in committed}

        recovered: list[str] = []
        for transition_id in ordered:
            if transition_id in committed_ids:
                continue
            record = prepared_by_id[transition_id]
            preconditions = record.get("preconditions") or []
            if not isinstance(preconditions, list):
                raise ValueError("canonical transition preconditions are invalid")
            for precondition in preconditions:
                live = self._project_path(str(precondition["path"]))
                if not live.is_file() or bytes_sha256(live.read_bytes()) != str(
                    precondition["sha256"]
                ):
                    raise ValueError(
                        "canonical transition precondition changed before commit: "
                        f"{precondition['path']}"
                    )
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
                schema_version=int(record["schema_version"]),
            )
            recovered.append(transition_id)
        self.verify_current()
        return recovered

    def verify_current(self) -> None:
        self._validated_ledger(self.records(), verify_current=True)

    def current_target_digests(self) -> dict[str, str]:
        """Return the exact live digests authorized by the latest transition."""
        committed = self.verified_committed_records()
        if not committed:
            return {}
        transition_id = str(committed[-1]["transition_id"])
        prepared = self.verified_prepared_record(transition_id, recover=False)
        return {
            str(target["path"]): str(target["after_sha256"])
            for target in prepared["targets"]
        }

    def commit(
        self,
        *,
        targets: dict[Path, bytes],
        authorization: dict[str, Any],
        trusted_state_path: Path,
        claim_graph_sha256: str,
        preconditions: dict[Path, str] | None = None,
    ) -> str:
        if not targets:
            raise ValueError("canonical transition requires at least one target")
        self.recover()
        checked_preconditions: dict[Path, str] = {}
        for path, expected in (preconditions or {}).items():
            resolved = path.resolve()
            if not resolved.is_relative_to(self.project_root):
                raise ValueError("canonical transition precondition escapes the project")
            if not resolved.is_file() or bytes_sha256(resolved.read_bytes()) != expected:
                raise ValueError(
                    "canonical transition precondition changed before prepare: "
                    f"{self._project_uri(resolved)}"
                )
            checked_preconditions[resolved] = expected
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
            "preconditions": {
                self._project_uri(path): digest
                for path, digest in sorted(
                    checked_preconditions.items(), key=lambda item: self._project_uri(item[0])
                )
            },
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
            "preconditions": [
                {"path": self._project_uri(path), "sha256": digest}
                for path, digest in sorted(
                    checked_preconditions.items(), key=lambda item: self._project_uri(item[0])
                )
            ],
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
        schema_version: int = TRANSITION_SCHEMA_VERSION,
    ) -> None:
        append_jsonl(self.ledger_path, {
            "schema_version": schema_version,
            "kind": kind,
            "transition_id": transition_id,
            "timestamp": utc_now(),
            "recovered": bool(recovered),
            "authorization": authorization,
        })
