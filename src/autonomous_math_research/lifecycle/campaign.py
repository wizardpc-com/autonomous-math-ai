from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ..models import utc_now
from ..storage import append_jsonl, atomic_write_json, read_jsonl, validate_storage_id


CAMPAIGN_SCHEMA_VERSION = 1
DEFAULT_CAMPAIGN_HOURS = 12.0
DEFAULT_EPOCH_HOURS = 2.0
CAMPAIGN_STATUSES = frozenset({"ACTIVE", "PAUSED", "STOPPED", "COMPLETED"})


@dataclass(frozen=True, slots=True)
class CampaignCheckpoint:
    campaign_id: str
    project_id: str
    created_at: str
    campaign_hours: float
    epoch_hours: float
    status: str
    epochs: tuple[str, ...]
    elapsed_epoch_seconds: float

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.campaign_hours * 3600.0 - self.elapsed_epoch_seconds)


class CampaignStore:
    """Append-only campaign/epoch index outside immutable epoch run records."""

    def __init__(self, runtime_root: Path, campaign_id: str):
        self.runtime_root = runtime_root.resolve()
        self.campaign_id = validate_storage_id(campaign_id, "campaign_id")
        self.root = self.runtime_root / "campaigns" / campaign_id
        self.manifest_path = self.root / "CAMPAIGN.json"
        self.epochs_path = self.root / "EPOCHS.jsonl"
        self.applied_inputs_path = self.root / "APPLIED_INPUTS.jsonl"

    def create(
        self,
        *,
        project_id: str,
        campaign_hours: float = DEFAULT_CAMPAIGN_HOURS,
        epoch_hours: float = DEFAULT_EPOCH_HOURS,
    ) -> CampaignCheckpoint:
        if campaign_hours <= 0 or epoch_hours <= 0:
            raise ValueError("campaign_hours and epoch_hours must be positive")
        if self.manifest_path.exists():
            return self.load()
        payload = {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "project_id": project_id,
            "created_at": utc_now(),
            "campaign_hours": float(campaign_hours),
            "epoch_hours": float(epoch_hours),
            "status": "ACTIVE",
        }
        atomic_write_json(self.manifest_path, payload)
        return self.load()

    def load(self) -> CampaignCheckpoint:
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version", "campaign_id", "project_id", "created_at",
            "campaign_hours", "epoch_hours", "status",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("campaign manifest fields are invalid")
        if raw["schema_version"] != CAMPAIGN_SCHEMA_VERSION:
            raise ValueError("unsupported campaign schema")
        if raw["campaign_id"] != self.campaign_id:
            raise ValueError("campaign id does not match its storage directory")
        if raw["status"] not in CAMPAIGN_STATUSES:
            raise ValueError("campaign status is invalid")
        records = read_jsonl(self.epochs_path)
        epochs: list[str] = []
        elapsed = 0.0
        for record in records:
            if record.get("campaign_id") != self.campaign_id:
                raise ValueError("epoch record belongs to a different campaign")
            if record.get("kind") == "EPOCH_STARTED":
                epoch_id = str(record.get("epoch_id") or "")
                if not epoch_id or epoch_id in epochs:
                    raise ValueError("campaign epoch start is invalid or duplicated")
                epochs.append(epoch_id)
            elif record.get("kind") == "EPOCH_SEALED":
                elapsed += max(0.0, float(record.get("elapsed_seconds", 0.0)))
        return CampaignCheckpoint(
            campaign_id=self.campaign_id,
            project_id=str(raw["project_id"]),
            created_at=str(raw["created_at"]),
            campaign_hours=float(raw["campaign_hours"]),
            epoch_hours=float(raw["epoch_hours"]),
            status=str(raw["status"]),
            epochs=tuple(epochs),
            elapsed_epoch_seconds=elapsed,
        )

    def append_epoch_started(
        self, *, epoch_id: str, previous_epoch_id: str | None, mode: str
    ) -> None:
        validate_storage_id(epoch_id, "epoch_id")
        if previous_epoch_id is not None:
            validate_storage_id(previous_epoch_id, "previous_epoch_id")
        current = self.load()
        if current.status not in {"ACTIVE", "PAUSED"}:
            raise ValueError(f"campaign is not continuable: {current.status}")
        if epoch_id in current.epochs:
            return
        append_jsonl(self.epochs_path, {
            "schema_version": 1,
            "kind": "EPOCH_STARTED",
            "campaign_id": self.campaign_id,
            "epoch_id": epoch_id,
            "previous_epoch_id": previous_epoch_id,
            "mode": mode,
            "timestamp": utc_now(),
        })
        if current.status == "PAUSED":
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "ACTIVE"
            atomic_write_json(self.manifest_path, manifest)

    def append_epoch_sealed(
        self,
        *,
        epoch_id: str,
        elapsed_seconds: float,
        status: str,
        stopped_reason: str,
        checkpoint_uri: str,
    ) -> None:
        validate_storage_id(epoch_id, "epoch_id")
        if status not in {"PAUSED", "STOPPED", "COMPLETED"}:
            raise ValueError(f"invalid sealed campaign status: {status}")
        records = read_jsonl(self.epochs_path)
        if any(
            item.get("kind") == "EPOCH_SEALED" and item.get("epoch_id") == epoch_id
            for item in records
        ):
            return
        append_jsonl(self.epochs_path, {
            "schema_version": 1,
            "kind": "EPOCH_SEALED",
            "campaign_id": self.campaign_id,
            "epoch_id": epoch_id,
            "elapsed_seconds": max(0.0, float(elapsed_seconds)),
            "status": status,
            "stopped_reason": stopped_reason,
            "checkpoint_uri": checkpoint_uri,
            "timestamp": utc_now(),
        })
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = status
        atomic_write_json(self.manifest_path, manifest)

    def events(self) -> list[dict[str, Any]]:
        return read_jsonl(self.epochs_path)

    def applied_inputs(self) -> set[str]:
        keys: set[str] = set()
        for record in read_jsonl(self.applied_inputs_path):
            key = record.get("input_key")
            if not isinstance(key, str) or not key:
                raise ValueError("campaign applied-input record is invalid")
            keys.add(key)
        return keys

    def mark_input_applied(self, input_key: str, *, epoch_id: str) -> None:
        if not input_key or input_key in self.applied_inputs():
            return
        append_jsonl(self.applied_inputs_path, {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "epoch_id": epoch_id,
            "input_key": input_key,
            "timestamp": utc_now(),
        })
