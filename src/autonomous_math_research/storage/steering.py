from __future__ import annotations

from hashlib import sha256
import json
import mimetypes
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from ..models import utc_now
from . import append_jsonl, validate_storage_id


STEERING_KINDS = frozenset({
    "NOTE", "PRIORITIZE_CLAIM", "PAUSE_ROUTE", "RESUME_ROUTE",
    "REQUEST_AUDIT", "STOP_AFTER_EPOCH",
})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def _clean_id(value: str | None, label: str, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"{label} is required")
        return None
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{label} is not a portable identifier")
    return value


def append_steering(
    runtime_root: Path,
    campaign_id: str,
    *,
    kind: str,
    note: str,
    claim_id: str | None = None,
    route_id: str | None = None,
    audit_kind: str | None = None,
) -> dict[str, Any]:
    campaign_id = validate_storage_id(campaign_id, "campaign_id")
    normalized = kind.upper()
    if normalized not in STEERING_KINDS:
        raise ValueError(f"unsupported steering kind: {kind}")
    if not note.strip() or len(note) > 8000:
        raise ValueError("steering note must contain 1..8000 characters")
    claim_id = _clean_id(
        claim_id, "claim_id",
        required=normalized in {"PRIORITIZE_CLAIM", "REQUEST_AUDIT"},
    )
    route_id = _clean_id(
        route_id, "route_id",
        required=normalized in {"PAUSE_ROUTE", "RESUME_ROUTE"},
    )
    audit_kind = _clean_id(
        audit_kind, "audit_kind", required=normalized == "REQUEST_AUDIT",
    )
    record = {
        "schema_version": 1,
        "steering_id": f"steer-{uuid4().hex}",
        "campaign_id": campaign_id,
        "kind": normalized,
        "note": note.strip(),
        "claim_id": claim_id,
        "route_id": route_id,
        "audit_kind": audit_kind,
        "timestamp": utc_now(),
    }
    target = runtime_root.resolve() / "campaigns" / campaign_id / "STEERING.jsonl"
    append_jsonl(target, record)
    return record


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ingest_asset(
    runtime_root: Path,
    campaign_id: str,
    source: Path,
    *,
    description: str,
) -> dict[str, Any]:
    campaign_id = validate_storage_id(campaign_id, "campaign_id")
    raw_source = source
    if raw_source.is_symlink():
        raise ValueError("asset ingest rejects symbolic links")
    source = raw_source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("asset source must be one regular local file")
    if not description.strip() or len(description) > 4000:
        raise ValueError("asset description must contain 1..4000 characters")
    digest = _digest(source)
    name = source.name
    campaign_root = runtime_root.resolve() / "campaigns" / campaign_id
    destination = campaign_root / "assets" / digest / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _digest(destination) != digest:
            raise ValueError("content-addressed asset destination is inconsistent")
    else:
        shutil.copy2(source, destination)
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    record = {
        "schema_version": 1,
        "asset_id": f"asset-{digest[:24]}",
        "campaign_id": campaign_id,
        "uri": f"campaign://{campaign_id}/assets/{digest}/{name}",
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "media_type": media_type,
        "source_description": description.strip(),
        "timestamp": utc_now(),
    }
    index = campaign_root / "ASSETS.jsonl"
    existing = [
        json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if index.exists() else []
    if not any(item.get("asset_id") == record["asset_id"] for item in existing):
        append_jsonl(index, record)
    return record
