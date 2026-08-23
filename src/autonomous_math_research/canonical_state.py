from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any

from .claim_graph import ClaimGraph
from .models import stable_hash, utc_now
from .project import ProjectManifest
from .storage import atomic_write_bytes, atomic_write_json, file_digest


CANONICAL_STATE_SCHEMA_VERSION = 1
MARKDOWN_STATE_BEGIN = "<!-- AMR-CANONICAL-STATE-BEGIN -->"
MARKDOWN_STATE_END = "<!-- AMR-CANONICAL-STATE-END -->"


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


def planning_context_fingerprint(
    state: dict[str, Any],
    *,
    claim_graph_sha256: str | None = None,
    trusted_state_sha256: str | None = None,
) -> str:
    """Bind planning context to the current audited claim-state revision."""
    payload = _planning_fingerprint_payload(state)
    if claim_graph_sha256 is not None:
        payload["claim_graph_sha256"] = claim_graph_sha256
    if trusted_state_sha256 is not None:
        payload["trusted_state_sha256"] = trusted_state_sha256
    return stable_hash(payload)


def _graph_state_view_payload(graph_payload: dict[str, Any], graph_digest: str) -> dict[str, Any]:
    domain = str(graph_payload.get("domain") or "math-research")
    status_field = "math_status" if domain == "math-research" else "research_status"
    payload = {
        "schema_version": 1,
        "authority": "claim_graph",
        "claim_graph_sha256": graph_digest,
        "claims": [
            {
                "claim_id": item["claim_id"],
                status_field: item[status_field],
                "trust_status": item["trust_status"],
                "evidence_level": item.get("evidence_level"),
                "current_gaps": list(item.get("current_gaps") or []),
                "last_meaningful_progress": item.get("last_meaningful_progress"),
            }
            for item in sorted(
                graph_payload.get("claims") or [], key=lambda item: str(item["claim_id"]),
            )
        ],
    }
    if domain != "math-research":
        payload["domain"] = domain
    return payload


def render_markdown_state_block(graph_payload: dict[str, Any], graph_digest: str) -> str:
    payload = json.dumps(
        _graph_state_view_payload(graph_payload, graph_digest),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (
        f"{MARKDOWN_STATE_BEGIN}\n"
        "This block is generated from the canonical ClaimGraph.\n\n"
        f"```json\n{payload}\n```\n"
        f"{MARKDOWN_STATE_END}"
    )


def replace_markdown_state_block(text: str, block: str) -> str:
    begin_count = text.count(MARKDOWN_STATE_BEGIN)
    end_count = text.count(MARKDOWN_STATE_END)
    if begin_count != end_count or begin_count > 1:
        raise ValueError("canonical Markdown state markers are malformed")
    if begin_count == 0:
        return text
    start = text.index(MARKDOWN_STATE_BEGIN)
    end = text.index(MARKDOWN_STATE_END, start) + len(MARKDOWN_STATE_END)
    return text[:start] + block + text[end:]


def canonical_markdown_state_views(manifest: ProjectManifest) -> tuple[Path, ...]:
    views: list[Path] = []
    seen: set[Path] = set()
    for role in ("director", "research", "audit"):
        for relative in manifest.canonical_inputs[role]:
            path = manifest.resolve(relative, must_exist=True)
            if path in seen or path.suffix.casefold() != ".md":
                continue
            seen.add(path)
            text = path.read_text(encoding="utf-8")
            begin = text.count(MARKDOWN_STATE_BEGIN)
            end = text.count(MARKDOWN_STATE_END)
            if begin != end or begin > 1:
                raise ValueError(
                    f"canonical Markdown state markers are malformed: {path}"
                )
            if begin == 1:
                views.append(path)
    return tuple(views)


def validate_canonical_research_state(manifest: ProjectManifest) -> tuple[Path, ...]:
    graph_path = manifest.resolve(manifest.claim_graph, must_exist=True)
    graph_bytes = graph_path.read_bytes()
    graph_digest = sha256(graph_bytes).hexdigest()
    try:
        graph_payload = json.loads(graph_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("canonical ClaimGraph is not valid JSON") from exc
    ClaimGraph.load(graph_path).validate()
    trusted_path = manifest.resolve(manifest.trusted_state, must_exist=True)
    trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
    if not isinstance(trusted, dict):
        raise ValueError("canonical trusted state must be a JSON object")
    bound_digest = trusted.get("claim_graph_sha256")
    if bound_digest is not None and str(bound_digest) != graph_digest:
        raise ValueError(
            "canonical trusted state is bound to a different ClaimGraph digest"
        )
    expected = render_markdown_state_block(graph_payload, graph_digest)
    views = canonical_markdown_state_views(manifest)
    for path in views:
        text = path.read_text(encoding="utf-8")
        start = text.index(MARKDOWN_STATE_BEGIN)
        end = text.index(MARKDOWN_STATE_END, start) + len(MARKDOWN_STATE_END)
        if text[start:end] != expected:
            raise ValueError(
                "canonical Markdown state conflicts with ClaimGraph: "
                f"{_project_uri(manifest.project_root, path)}"
            )
    return views


# Compatibility for callers and persisted diagnostics using the pre-domain name.
validate_canonical_mathematical_state = validate_canonical_research_state


def updated_markdown_state_views(
    manifest: ProjectManifest,
    *,
    graph_payload: dict[str, Any],
    graph_digest: str,
) -> dict[Path, bytes]:
    block = render_markdown_state_block(graph_payload, graph_digest)
    return {
        path: replace_markdown_state_block(
            path.read_text(encoding="utf-8"), block,
        ).encode("utf-8")
        for path in canonical_markdown_state_views(manifest)
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
    state_views = validate_canonical_research_state(manifest)
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
        "canonical_markdown_state_views": [
            _project_uri(project_root, path) for path in state_views
        ],
        "git_revision": _git_revision(workspace_root),
    }
    state["canonical_state_sha256"] = stable_hash(_fingerprint_payload(state))
    state["planning_context_sha256"] = planning_context_fingerprint(state)
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
    if planning_context_fingerprint(state) != state.get(
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


def director_canonical_view(
    state: dict[str, Any],
    *,
    run_dir: Path,
    claim_graph: dict[str, Any] | None = None,
    claim_graph_sha256: str | None = None,
) -> dict[str, Any]:
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
        "authority": "controller_claim_graph",
        "precedence": (
            "The controller ClaimGraph below is the sole domain-status and research-"
            "frontier authority. Startup-frozen Markdown inputs are context only and may "
            "not override it. Marked Markdown state blocks are accepted only after exact "
            "ClaimGraph consistency checks. Claim/trust transitions remain audit-gated."
        ),
        "canonical_state_sha256": state["canonical_state_sha256"],
        "planning_context_sha256": state["planning_context_sha256"],
        "git_revision": state["git_revision"],
        "planning_mirror": state["planning_mirror"],
        "inputs": inputs,
        "input_semantics": "context_only",
        "claim_graph": claim_graph,
        "claim_graph_sha256": claim_graph_sha256,
        "canonical_markdown_state_views": state.get(
            "canonical_markdown_state_views", []
        ),
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
