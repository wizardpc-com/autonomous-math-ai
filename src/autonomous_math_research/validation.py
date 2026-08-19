from __future__ import annotations

from pathlib import Path
from typing import Any

from .claim_graph import ClaimGraph
from .config import load_config
from .policy import build_policy_manifest
from .project import ProjectManifest
from .resources import schema_resource
from .schema import preflight_output_schema_files
from .storage import CanonicalGuard, ProjectLayout


def validate_project(project_root: Path, *, workspace_root: Path | None = None) -> dict[str, Any]:
    """Perform every local trust-boundary check without starting App Server."""
    root = project_root.resolve()
    manifest = ProjectManifest.load(root)
    config = load_config(
        root, workspace_root=workspace_root, require_manifest=True,
    )
    layout = ProjectLayout(root)
    graph = ClaimGraph.load(layout.claim_graph_path)
    graph.validate()
    if manifest.final_claim_id not in graph.claims:
        raise ValueError("manifest final_claim_id is absent from the claim graph")
    schema_paths: list[Path] = []
    contexts = []
    try:
        for name in (
            "director_plan.schema.json", "worker_result.schema.json",
            "audit_result.schema.json",
        ):
            context = schema_resource(name)
            contexts.append(context)
            schema_paths.append(context.__enter__())
        schemas = preflight_output_schema_files(schema_paths)
    finally:
        for context in reversed(contexts):
            context.__exit__(None, None, None)
    with schema_resource("candidate_event.schema.json") as candidate_schema:
        # Candidate events use the local append-only inbox protocol, not
        # turn/start.outputSchema. Loading it here still catches malformed JSON
        # without incorrectly imposing the Structured Outputs dialect.
        from .schema import load_schema
        load_schema(candidate_schema)
    policy = build_policy_manifest(config)
    guard = CanonicalGuard(root, config.protected_paths)
    protected = guard.snapshot()
    return {
        "valid": True,
        "project_id": manifest.project_id,
        "final_claim_id": manifest.final_claim_id,
        "workspace_root": str(config.workspace_root),
        "output_protocol": 2,
        "schemas": [*sorted(schemas), "candidate_event.schema.json (local inbox)"],
        "policy_manifest_sha256": policy["manifest_sha256"],
        "mechanical_primary": policy["one_shot_compute_worker"]["primary_route"],
        "mechanical_fallback": policy["one_shot_compute_worker"]["fallback_route"],
        "protected_files": len(protected),
        "model_turns_started": 0,
    }
