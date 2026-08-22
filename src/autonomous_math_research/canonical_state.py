from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any

from .models import stable_hash, utc_now
from .project import ProjectManifest
from .storage import atomic_write_bytes, atomic_write_json, file_digest


CANONICAL_STATE_SCHEMA_VERSION = 1


def _project_uri(root: Path, path: Path) -> str:
    return "project://" + path.resolve().relative_to(root.resolve()).as_posix()


def _epoch_uri(epoch_id: str, run_dir: Path, path: Path) -> str:
    return f"epoch://{epoch_id}/" + path.resolve().relative_to(run_dir.resolve()).as_posix()


def _snapshot_source(
    *,
    source: Path,
    target: Path,
    project_root: Path,
    run_dir: Path,
    epoch_id: str,
) -> dict[str, str]:
    payload = source.read_bytes()
    digest = sha256(payload).hexdigest()
    if file_digest(source) != digest:
        raise ValueError(f"canonical source changed while being frozen: {source}")
    atomic_write_bytes(target, payload)
    if file_digest(target) != digest:
        raise ValueError(f"canonical source snapshot failed digest verification: {source}")
    return {
        "path": _project_uri(project_root, source),
        "sha256": digest,
        "snapshot": _epoch_uri(epoch_id, run_dir, target),
    }


def _git_revision(workspace_root: Path) -> dict[str, str] | None:
    root = workspace_root.resolve()
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            "cannot freeze the Git revision for canonical startup state"
        ) from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError("cannot freeze the Git revision for canonical startup state")
    return {"repository_root": str(root), "head": result.stdout.strip()}


def _fingerprint_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": state["project_id"],
        "final_claim_id": state["final_claim_id"],
        "project_manifest": {
            "path": state["project_manifest"]["path"],
            "sha256": state["project_manifest"]["sha256"],
        },
        "canonical_inputs": [
            {
                "path": item["path"],
                "sha256": item["sha256"],
                "roles": item["roles"],
            }
            for item in state["canonical_inputs"]
        ],
    }


def _planning_fingerprint_payload(state: dict[str, Any]) -> dict[str, Any]:
    overlay = state.get("director_overlay")
    return {
        "canonical_state_sha256": state["canonical_state_sha256"],
        "claim_graph_sha256": state["claim_graph"]["sha256"],
        "trusted_state_sha256": state["trusted_state"]["sha256"],
        "director_overlay_sha256": overlay["sha256"] if overlay else None,
    }


def capture_canonical_state(
    *,
    manifest: ProjectManifest,
    run_dir: Path,
    epoch_id: str,
    workspace_root: Path,
    previous_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project_root = manifest.project_root
    snapshot_root = run_dir / "state" / "canonical_inputs"
    sources: dict[str, dict[str, Any]] = {}
    ordered_paths: list[str] = []
    for role in ("director", "research", "audit"):
        for relative in manifest.canonical_inputs[role]:
            if relative not in sources:
                sources[relative] = {"roles": []}
                ordered_paths.append(relative)
            sources[relative]["roles"].append(role)

    canonical_inputs: list[dict[str, Any]] = []
    for index, relative in enumerate(ordered_paths):
        source = manifest.resolve(relative, must_exist=True)
        target = snapshot_root / f"{index:03d}-{source.name}"
        canonical_inputs.append({
            **_snapshot_source(
                source=source, target=target, project_root=project_root,
                run_dir=run_dir, epoch_id=epoch_id,
            ),
            "roles": list(sources[relative]["roles"]),
        })

    manifest_entry = _snapshot_source(
        source=manifest.path,
        target=snapshot_root / "project.json",
        project_root=project_root,
        run_dir=run_dir,
        epoch_id=epoch_id,
    )
    claim_graph = _snapshot_source(
        source=manifest.resolve(manifest.claim_graph, must_exist=True),
        target=snapshot_root / "claim_graph.json",
        project_root=project_root,
        run_dir=run_dir,
        epoch_id=epoch_id,
    )
    trusted_state = _snapshot_source(
        source=manifest.resolve(manifest.trusted_state, must_exist=True),
        target=snapshot_root / "trusted_state.json",
        project_root=project_root,
        run_dir=run_dir,
        epoch_id=epoch_id,
    )
    overlay_path = manifest.resolve(manifest.prompt_root) / "director.md"
    director_overlay = (
        _snapshot_source(
            source=overlay_path,
            target=snapshot_root / "director_overlay.md",
            project_root=project_root,
            run_dir=run_dir,
            epoch_id=epoch_id,
        )
        if overlay_path.is_file() else None
    )
    state: dict[str, Any] = {
        "schema_version": CANONICAL_STATE_SCHEMA_VERSION,
        "epoch_id": epoch_id,
        "generated_at": utc_now(),
        "project_id": manifest.project_id,
        "final_claim_id": manifest.final_claim_id,
        "project_manifest": manifest_entry,
        "canonical_inputs": canonical_inputs,
        "claim_graph": claim_graph,
        "trusted_state": trusted_state,
        "director_overlay": director_overlay,
        "git_revision": _git_revision(workspace_root),
    }
    state["canonical_state_sha256"] = stable_hash(_fingerprint_payload(state))
    state["planning_context_sha256"] = stable_hash(
        _planning_fingerprint_payload(state)
    )
    prior_planning = (
        str(previous_state.get("planning_context_sha256") or "")
        if previous_state else ""
    )
    state["planning_mirror"] = {
        "previous_state_available": previous_state is not None,
        "previous_planning_context_sha256": prior_planning or None,
        "drift_detected": bool(
            previous_state
            and prior_planning != state["planning_context_sha256"]
        ),
        "disposition": (
            "discard_stale_planning_and_rebuild"
            if previous_state and prior_planning != state["planning_context_sha256"]
            else "reuse_only_after_integrity_checks"
            if previous_state else "no_previous_mirror"
        ),
    }
    atomic_write_json(run_dir / "state" / "canonical_state.json", state)
    return state


def _snapshot_path(state: dict[str, Any], run_dir: Path, uri: str) -> Path:
    prefix = f"epoch://{state['epoch_id']}/" if "epoch_id" in state else None
    if prefix is None or not uri.startswith(prefix):
        raise ValueError(f"canonical state contains an invalid snapshot URI: {uri}")
    path = (run_dir / uri[len(prefix):]).resolve()
    if not path.is_relative_to(run_dir.resolve()):
        raise ValueError("canonical state snapshot escapes the run directory")
    return path


def validate_canonical_state(state: dict[str, Any], *, run_dir: Path) -> None:
    if int(state.get("schema_version", 0)) != CANONICAL_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported canonical state schema")
    if stable_hash(_fingerprint_payload(state)) != state.get("canonical_state_sha256"):
        raise ValueError("canonical state fingerprint is invalid")
    if stable_hash(_planning_fingerprint_payload(state)) != state.get(
        "planning_context_sha256"
    ):
        raise ValueError("canonical planning-context fingerprint is invalid")
    for entry in [
        state["project_manifest"], *state["canonical_inputs"],
        state["claim_graph"], state["trusted_state"],
        *([state["director_overlay"]] if state.get("director_overlay") else []),
    ]:
        snapshot = _snapshot_path(state, run_dir, str(entry["snapshot"]))
        if not snapshot.is_file() or file_digest(snapshot) != str(entry["sha256"]):
            raise ValueError(f"canonical state snapshot is missing or changed: {entry['path']}")


def verify_live_startup_sources(
    state: dict[str, Any], *, project_root: Path, run_dir: Path,
) -> list[str]:
    validate_canonical_state(state, run_dir=run_dir)
    changed: list[str] = []
    guarded = [state["project_manifest"], *state["canonical_inputs"]]
    if state.get("director_overlay"):
        guarded.append(state["director_overlay"])
    for entry in guarded:
        relative = str(entry["path"])
        if not relative.startswith("project://"):
            raise ValueError("canonical source path is not project-relative")
        source = (project_root / relative.removeprefix("project://")).resolve()
        if (
            not source.is_relative_to(project_root.resolve())
            or not source.is_file()
            or file_digest(source) != str(entry["sha256"])
        ):
            changed.append(relative)
    return sorted(changed)


def director_canonical_view(state: dict[str, Any], *, run_dir: Path) -> dict[str, Any]:
    validate_canonical_state(state, run_dir=run_dir)
    inputs: list[dict[str, Any]] = []
    for entry in state["canonical_inputs"]:
        if "director" not in entry["roles"]:
            continue
        snapshot = _snapshot_path(state, run_dir, str(entry["snapshot"]))
        try:
            content = snapshot.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Director canonical input is not UTF-8 text: {entry['path']}"
            ) from exc
        inputs.append({
            "path": entry["path"],
            "sha256": entry["sha256"],
            "snapshot": entry["snapshot"],
            "content": content,
        })
    return {
        "authority": "startup_frozen_canonical_inputs",
        "precedence": (
            "These startup-frozen inputs override project Director overlay text and "
            "all prior derived planning mirrors for frontier, claim-status, and "
            "recent-progress descriptions. Claim/trust transitions remain controller-owned."
        ),
        "canonical_state_sha256": state["canonical_state_sha256"],
        "planning_context_sha256": state["planning_context_sha256"],
        "git_revision": state["git_revision"],
        "planning_mirror": state["planning_mirror"],
        "inputs": inputs,
        "claim_graph_startup": state["claim_graph"],
        "trusted_state_startup": state["trusted_state"],
    }


def director_overlay_text(state: dict[str, Any], *, run_dir: Path) -> str | None:
    validate_canonical_state(state, run_dir=run_dir)
    entry = state.get("director_overlay")
    if not entry:
        return None
    snapshot = _snapshot_path(state, run_dir, str(entry["snapshot"]))
    return snapshot.read_text(encoding="utf-8")


def load_canonical_state(path: Path, *, epoch_id: str) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("canonical state must be a JSON object")
    if raw.get("epoch_id") != epoch_id:
        raise ValueError("canonical state belongs to another epoch")
    return raw
