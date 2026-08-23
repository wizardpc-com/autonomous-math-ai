from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


DIRECTOR_PROMPT_TARGET_BYTES = 4 * 1024
DIRECTOR_PROMPT_HARD_LIMIT_BYTES = 10 * 1024
COMPACT_SNAPSHOT_HARD_LIMIT_BYTES = 128 * 1024

_CHECKPOINT_SECTIONS = frozenset({
    "candidate_audit_frontier",
    "pending_research",
    "deferred_research_continuation_ids",
    "research_continuation_checkpoints",
    "pending_audits",
})
_EXTERNAL_ONLY_KEYS = frozenset({
    "compact_snapshot",
    "full_transcript",
    "prompt",
    "raw_output",
    "transcript",
})


class DirectorPromptTooLarge(ValueError):
    def __init__(self, size_bytes: int, hard_limit_bytes: int):
        self.size_bytes = int(size_bytes)
        self.hard_limit_bytes = int(hard_limit_bytes)
        super().__init__(
            "director prompt rejected before thread creation: "
            f"UTF-8 size {self.size_bytes} bytes must be < "
            f"{self.hard_limit_bytes} bytes"
        )


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def enforce_director_prompt_limit(prompt: str) -> int:
    size = utf8_size(prompt)
    if size >= DIRECTOR_PROMPT_HARD_LIMIT_BYTES:
        raise DirectorPromptTooLarge(size, DIRECTOR_PROMPT_HARD_LIMIT_BYTES)
    return size


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _externalized(value: Any) -> dict[str, Any]:
    payload = _json_bytes(value)
    return {
        "externalized": True,
        "bytes": len(payload),
        "sha256": sha256(payload).hexdigest(),
    }


def _bounded_json(
    value: Any,
    *,
    depth: int = 0,
    max_depth: int = 4,
    max_items: int = 12,
    max_text: int = 320,
) -> Any:
    """Return a deterministic current-state projection, never a history carrier."""
    if depth >= max_depth:
        return _externalized(value)
    if isinstance(value, str):
        if len(value.encode("utf-8")) <= max_text:
            return value
        return {
            "preview": value[: max_text // 2],
            "characters": len(value),
            "bytes": len(value.encode("utf-8")),
            "sha256": sha256(value.encode("utf-8")).hexdigest(),
            "truncated": True,
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        keys = sorted(value, key=str)
        for raw_key in keys[:max_items]:
            key = str(raw_key)
            item = value[raw_key]
            if key.casefold() in _EXTERNAL_ONLY_KEYS:
                result[key] = _externalized(item)
            else:
                result[key] = _bounded_json(
                    item,
                    depth=depth + 1,
                    max_depth=max_depth,
                    max_items=max_items,
                    max_text=max_text,
                )
        if len(keys) > max_items:
            result["_omitted_keys"] = len(keys) - max_items
        return result
    if isinstance(value, (list, tuple)):
        projected = [
            _bounded_json(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_items=max_items,
                max_text=max_text,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            projected.append({"_omitted_items": len(value) - max_items})
        return projected
    return value


def _canonical_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    inputs: list[dict[str, Any]] = []
    for raw in value.get("inputs") or []:
        if not isinstance(raw, dict):
            continue
        content = raw.get("content")
        row = {
            key: raw.get(key) for key in ("path", "sha256", "snapshot")
        }
        if isinstance(content, str):
            row["content_bytes"] = len(content.encode("utf-8"))
        inputs.append(_bounded_json(row, max_items=8, max_text=320))
    graph = value.get("claim_graph")
    claims = graph.get("claims") if isinstance(graph, dict) else None
    return _bounded_json({
        key: value.get(key)
        for key in (
            "authority",
            "canonical_state_sha256",
            "planning_context_sha256",
            "git_revision",
            "planning_mirror",
            "claim_graph_sha256",
            "trusted_state_sha256",
            "canonical_transition_id",
            "input_semantics",
        )
    } | {
        "inputs": inputs[:32],
        "input_count": len(inputs),
        "claim_count": len(claims) if isinstance(claims, list) else None,
        "full_content": "externalized in full_context_archive",
    }, max_items=16, max_text=320)


def _checkpoint_projection(value: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, list):
        return _bounded_json(value), {
            "complete": True,
            "total_items": 0,
            "included_items": 0,
        }
    payload = _json_bytes(value)
    complete = (
        len(value) <= 24
        and len(payload) <= 16 * 1024
        and not _contains_external_only_key(value)
    )
    projected = value if complete else _bounded_json(value, max_items=8)
    included = len(value) if complete else min(len(value), 8)
    return projected, {
        "complete": complete,
        "total_items": len(value),
        "included_items": included,
        "full_bytes": len(payload),
    }


def _contains_external_only_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in _EXTERNAL_ONLY_KEYS
            or _contains_external_only_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_external_only_key(item) for item in value)
    return False


def _exact_or_bounded(value: Any, *, max_bytes: int = 16 * 1024) -> Any:
    return value if len(_json_bytes(value)) <= max_bytes else _bounded_json(value)


def build_compact_snapshot(
    full_snapshot: dict[str, Any],
    *,
    full_context_reference: dict[str, Any],
    history_archive: dict[str, Any],
) -> dict[str, Any]:
    """Build a bounded, non-recursive Director view of current state."""
    compact: dict[str, Any] = {
        "schema_version": 2,
        "kind": "bounded_current_state_summary",
        "full_context_archive": dict(full_context_reference),
        "history_archive": dict(history_archive),
        "canonical_state": _canonical_summary(full_snapshot.get("canonical_state")),
        "section_manifest": {},
    }
    domain = full_snapshot.get("domain")
    negative_key = (
        "strictly_negative" if domain and domain != "math-research"
        else "strictly_refuted"
    )
    if domain and domain != "math-research":
        compact["domain"] = str(domain)
    for key in (
        "strictly_trusted",
        negative_key,
        "open_frontier",
        "active_tasks",
        "recent_changes",
        "budget",
        "claim_state_provenance",
        "mechanical_token_governor",
        "research_target",
        "controller_watermark",
        "mechanical_subworkers",
        "research_policy",
        "director_constraints",
    ):
        compact[key] = _bounded_json(full_snapshot.get(key))
    compact["representation_compatibility"] = _exact_or_bounded(
        full_snapshot.get("representation_compatibility")
    )
    compact["snapshot_provenance"] = _exact_or_bounded(
        full_snapshot.get("snapshot_provenance")
    )
    compact["route_state"] = _exact_or_bounded(full_snapshot.get("route_state"))
    overlay = full_snapshot.get("director_overlay")
    compact["director_overlay"] = (
        {
            "present": True,
            "sha256": overlay.get("sha256"),
            "text": _externalized(overlay.get("text")),
        }
        if isinstance(overlay, dict) else {"present": False}
    )
    for key in sorted(_CHECKPOINT_SECTIONS):
        compact[key], compact["section_manifest"][key] = _checkpoint_projection(
            full_snapshot.get(key)
        )
    compact["summary_counts"] = {
        key: len(full_snapshot.get(key) or [])
        for key in (
            "strictly_trusted",
            negative_key,
            "open_frontier",
            "active_tasks",
            "recent_changes",
            "candidate_audit_frontier",
            "pending_research",
            "pending_audits",
            "route_state",
        )
    }
    payload = _json_bytes(compact)
    if len(payload) >= COMPACT_SNAPSHOT_HARD_LIMIT_BYTES:
        # This fail-safe intentionally keeps only the recovery pointer and the
        # highest-value scheduling counters. The full state remains available
        # through the verified archive reference.
        compact = {
            "schema_version": 2,
            "kind": "bounded_current_state_summary",
            "reduced_to_hard_bound": True,
            "full_context_archive": dict(full_context_reference),
            "history_archive": dict(history_archive),
            "canonical_state": _canonical_summary(
                full_snapshot.get("canonical_state")
            ),
            "controller_watermark": _bounded_json(
                full_snapshot.get("controller_watermark")
            ),
            "snapshot_provenance": _bounded_json(
                full_snapshot.get("snapshot_provenance")
            ),
            "research_target": _bounded_json(full_snapshot.get("research_target")),
            "summary_counts": {
                key: len(full_snapshot.get(key) or [])
                for key in (
                    "strictly_trusted",
                    negative_key,
                    "open_frontier",
                    "active_tasks",
                    "recent_changes",
                    "candidate_audit_frontier",
                    "pending_research",
                    "pending_audits",
                    "route_state",
                )
            },
            "section_manifest": {
                key: {
                    "complete": False,
                    "total_items": len(full_snapshot.get(key) or []),
                    "included_items": 0,
                }
                for key in sorted(_CHECKPOINT_SECTIONS)
            },
        }
        if domain and domain != "math-research":
            compact["domain"] = str(domain)
        payload = _json_bytes(compact)
    if len(payload) >= COMPACT_SNAPSHOT_HARD_LIMIT_BYTES:
        raise ValueError(
            "bounded compact snapshot exceeds its hard limit: "
            f"{len(payload)} >= {COMPACT_SNAPSHOT_HARD_LIMIT_BYTES} bytes"
        )
    return compact


def load_full_context_archive(
    compact_path: Path,
    compact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load and authenticate a v2 external context, or accept a v1 snapshot."""
    value = compact
    if value is None:
        value = json.loads(compact_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("compact snapshot is invalid")
    reference = value.get("full_context_archive")
    if not isinstance(reference, dict):
        return value
    relative = reference.get("relative_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("full context archive reference is invalid")
    state_root = compact_path.resolve().parent
    archive_path = (state_root / relative).resolve()
    if not archive_path.is_relative_to(state_root):
        raise ValueError("full context archive escapes the checkpoint state directory")
    if not archive_path.is_file():
        raise ValueError("full context archive is missing")
    observed_bytes = archive_path.stat().st_size
    if observed_bytes != int(reference.get("bytes", -1)):
        raise ValueError("full context archive byte count changed")
    observed_digest = sha256(archive_path.read_bytes()).hexdigest()
    if observed_digest != str(reference.get("sha256") or ""):
        raise ValueError("full context archive sha256 changed")
    full = json.loads(archive_path.read_text(encoding="utf-8"))
    if not isinstance(full, dict):
        raise ValueError("full context archive is invalid")

    # Small checkpoint sections remain exact in the compact file. Keeping them
    # authoritative preserves tamper detection and legacy recovery validation;
    # large sections are recovered from the authenticated archive instead.
    manifest = value.get("section_manifest") or {}
    for key in _CHECKPOINT_SECTIONS:
        section = manifest.get(key) if isinstance(manifest, dict) else None
        if isinstance(section, dict) and section.get("complete") is True:
            full[key] = value.get(key)
    compact_provenance = value.get("snapshot_provenance")
    if not isinstance(compact_provenance, dict):
        raise ValueError("compact snapshot canonical provenance is invalid")
    full["snapshot_provenance"] = compact_provenance
    compact_canonical = value.get("canonical_state")
    full_canonical = full.get("canonical_state")
    if not isinstance(compact_canonical, dict) or not isinstance(
        full_canonical, dict
    ):
        raise ValueError("compact snapshot canonical state is invalid")
    if compact_canonical.get("canonical_state_sha256") != full_canonical.get(
        "canonical_state_sha256"
    ):
        raise ValueError("compact snapshot canonical provenance changed")
    # These fields describe the checkpoint that the compact snapshot actually
    # exposes. Preserve that exact binding for deterministic transition and
    # legacy-v1 validation instead of silently replacing it with archive data.
    for key in (
        "planning_context_sha256",
        "claim_graph_sha256",
        "trusted_state_sha256",
        "canonical_transition_id",
    ):
        if key in compact_canonical:
            full_canonical[key] = compact_canonical[key]
        else:
            full_canonical.pop(key, None)
    return full
