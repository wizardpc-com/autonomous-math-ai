from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import CandidateEvent, stable_hash
from .schema import load_schema, validate
from .resources import schema_resource
from .storage import (
    ProjectLayout, append_jsonl, atomic_write_json, file_digest, read_jsonl,
)
from .storage.artifacts import PORTABLE_SCHEMES, resolve_portable_uri


class CandidateInbox:
    def __init__(
        self,
        layout: ProjectLayout,
        *,
        inbox_root: Path | None = None,
        event_log: Path | None = None,
        candidate_root: Path | None = None,
    ):
        self.layout = layout
        # Package resources can change during a long-running local upgrade.
        # Pin the protocol schema in memory when the controller-owned inbox is
        # created so later submit/poll operations do not depend on mutable
        # site-packages bytes.
        with schema_resource("candidate_event.schema.json") as schema_path:
            self.candidate_schema = load_schema(schema_path)
        self.inbox_root = inbox_root or layout.inbox_root
        self.event_log = event_log or layout.event_log
        self.candidate_root = candidate_root or (layout.autonomous_root / "candidates")
        self.processed: set[str] = set()
        self.persisted: set[str] = set()
        self.processed_root = self.inbox_root.parent / "processed"
        self.quarantine_root = self.inbox_root.parent / "quarantine"
        self.processed_root.mkdir(parents=True, exist_ok=True)
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        self.sources: dict[str, Path] = {}
        self.poll_errors: list[dict[str, str]] = []
        self.accepted: set[str] = {
            str(item.get("fingerprint"))
            for item in read_jsonl(self.event_log)
            if (
                item.get("kind") == "CANDIDATE_ACCEPTED"
                and item.get("fingerprint")
                and (self.candidate_root / f"{item['fingerprint']}.json").is_file()
            )
        }

    def submit(
        self,
        event: CandidateEvent,
        schema_path: Path | None = None,
        *,
        target_root: Path | None = None,
    ) -> Path:
        """Write only to the worker-facing inbox.

        Workers receive write access to this directory and nothing else in the
        shared autonomous state.  The controller later validates and persists
        the event in the append-only ledger.
        """
        self._assign_current_evidence_attempt(event)
        payload = event.to_dict()
        payload.pop("fingerprint", None)
        schema = load_schema(schema_path) if schema_path else self.candidate_schema
        validate(payload, schema)
        self._validate_paths(event)
        root = (target_root or self.inbox_root).resolve()
        if not root.is_relative_to(self.inbox_root.resolve()):
            raise ValueError("candidate target inbox escapes the controller inbox root")
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{event.event_id}.json"
        if target.exists():
            existing = CandidateEvent.from_dict(json.loads(target.read_text(encoding="utf-8")))
            if existing.fingerprint != event.fingerprint:
                raise ValueError(f"event id collision: {event.event_id}")
            self.sources[event.event_id] = target.resolve()
            return target
        atomic_write_json(target, payload)
        self.sources[event.event_id] = target.resolve()
        return target

    def persist(self, event: CandidateEvent) -> Path:
        """Controller-owned durable candidate and append-only ledger write."""
        # Submission validates the producer-visible attempt. The controller may
        # subsequently add verified receipt artifacts before sealing; those
        # derived files are already bound by the receipt run ids in the attempt.
        payload = event.to_dict()
        payload.pop("fingerprint", None)
        durable = self.candidate_root / f"{event.fingerprint}.json"
        if event.fingerprint in self.persisted or durable.exists():
            self.persisted.add(event.fingerprint)
            return durable
        append_jsonl(self.event_log, {"kind": "CANDIDATE_EMITTED", **event.to_dict()})
        atomic_write_json(durable, payload)
        self.persisted.add(event.fingerprint)
        return durable

    def emit(self, event: CandidateEvent, schema_path: Path | None = None) -> Path:
        target = self.submit(event, schema_path)
        self.persist(event)
        return target

    def poll(self) -> list[CandidateEvent]:
        found: list[CandidateEvent] = []
        self.poll_errors = []
        for path in sorted(self.inbox_root.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                validate(payload, self.candidate_schema)
                event = CandidateEvent.from_dict(payload)
                self._validate_paths(event)
                self._validate_evidence_attempt(event)
            except Exception as exc:
                relative = path.relative_to(self.inbox_root).as_posix().replace("/", "__")
                target = self.quarantine_root / f"{relative}.invalid"
                if target.exists():
                    target = self.quarantine_root / f"{relative}.{len(self.poll_errors)}.invalid"
                path.replace(target)
                self.poll_errors.append({"source": str(path), "quarantine": str(target), "error": str(exc)})
                continue
            # Return every inbox file.  A duplicate can have a fresh event_id
            # but a fingerprint that was processed earlier; filtering it here
            # would leave that file stranded forever.  The controller owns the
            # durable fingerprint decision and archives duplicates explicitly.
            self.sources[event.event_id] = path.resolve()
            found.append(event)
        return found

    def mark_processed(
        self, event: CandidateEvent, run_id: str | None = None, *, accepted: bool = False,
    ) -> None:
        self.processed.add(event.fingerprint)
        if accepted and event.fingerprint not in self.accepted:
            append_jsonl(self.event_log, {
                "kind": "CANDIDATE_ACCEPTED", "fingerprint": event.fingerprint,
                "event_id": event.event_id, "run_id": run_id,
            })
            self.accepted.add(event.fingerprint)
        sources = []
        remembered = self.sources.pop(event.event_id, None)
        if remembered is not None:
            sources.append(remembered)
        sources.extend(
            path.resolve() for path in self.inbox_root.rglob(f"{event.event_id}.json")
            if path.resolve() not in sources
        )
        for source in sources:
            if not source.exists():
                continue
            target = self.processed_root / f"{event.event_id}.{event.fingerprint[:12]}.json"
            if target.exists():
                source.unlink()
            else:
                source.replace(target)

    def _validate_paths(self, event: CandidateEvent) -> None:
        for raw in event.artifact_paths:
            self._artifact_path(raw)

    def _artifact_path(self, raw: str) -> Path:
        project = self.layout.project_root.resolve()
        if raw.startswith(PORTABLE_SCHEMES):
            return resolve_portable_uri(project, self.layout.autonomous_root, raw)
        path = Path(raw)
        resolved = (project / path).resolve() if not path.is_absolute() else path.resolve()
        if not resolved.is_relative_to(project):
            raise ValueError(f"candidate artifact escapes project: {raw}")
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError(f"candidate artifact does not exist or is symbolic: {raw}")
        return resolved

    def _evidence_attempt_id(self, event: CandidateEvent) -> str:
        payload = {
            "artifact_sha256": sorted(
                file_digest(self._artifact_path(raw))
                for raw in event.artifact_paths
            ),
            "evidence_receipts": sorted(
                event.evidence_receipts,
                key=lambda item: (item["kind"], item["manifest_path"], item["run_id"]),
            ),
            "proposed_evidence_level": event.proposed_evidence_level,
            "reproduction_commands": list(event.reproduction_commands),
        }
        return "attempt-" + stable_hash(payload)

    def _assign_current_evidence_attempt(self, event: CandidateEvent) -> None:
        self._validate_paths(event)
        event.fingerprint_version = 2
        event.evidence_attempt_id = self._evidence_attempt_id(event)

    def _validate_evidence_attempt(self, event: CandidateEvent) -> None:
        if event.fingerprint_version == 1:
            return
        if event.evidence_attempt_id != self._evidence_attempt_id(event):
            raise ValueError("candidate evidence_attempt_id does not match its evidence")


def event_from_cli(data: dict[str, Any]) -> CandidateEvent:
    return CandidateEvent.from_dict(data)
