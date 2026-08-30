from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .claim_graph import ClaimGraph
from .canonical_transition import CanonicalTransitionStore
from .config import DEFAULT_PROTECTED, load_config
from .contracts import OUTPUT_PROTOCOL_VERSION
from .models import TrustStatus
from .mechanical import attest_mechanical_host_capability
from .policy import build_policy_manifest
from .project import ProjectManifest
from .reconciliation import ReconciliationStore
from .resources import schema_resource
from .schema import preflight_output_schema_files
from .semantic_alignment import (
    SEMANTICS_FILENAME, SemanticAlignment, SemanticTrustState,
    build_validation_authority_head,
)
from .storage import CanonicalGuard, ProjectLayout


STRICT_PROJECT_DIRECTORIES = (
    "claims", "state", "proofs", "tasks", "experiments", "certificates",
    "audit", "sources", "conversations", "artifacts", "autonomous",
)
_PLACEHOLDER_MARKERS = (
    "AMR_PLACEHOLDER", "replace with the exact", "replace this neutral",
    "todo: exact claim", "your conjecture here",
)


def _markdown_declares_claim_id(text: str, claim_id: str) -> bool:
    escaped = re.escape(claim_id)
    token = rf"(?:`{escaped}`|{escaped})"
    patterns = (
        rf"(?m)^\s*[-*]\s+{token}\s*:",
        rf"(?m)^\s*\|\s*{token}\s*(?:\||:)",
        rf"(?m)^\s*#{{1,6}}\s+{token}(?:\s|:|—|-|$)",
        rf"(?m)^\s*<!--\s*AMR-CLAIM-ID:\s*{escaped}\s*-->\s*$",
    )
    return any(re.search(pattern, text) is not None for pattern in patterns)


def _strict_project_checks(
    root: Path,
    manifest: ProjectManifest,
    config: Any,
    graph: ClaimGraph,
) -> dict[str, Any]:
    missing_dirs = [name for name in STRICT_PROJECT_DIRECTORIES if not (root / name).is_dir()]
    if missing_dirs:
        raise ValueError(f"strict initialization is missing directories: {missing_dirs}")
    checklist = root / "INITIALIZATION_CHECKLIST.md"
    if not checklist.is_file():
        raise ValueError("strict initialization checklist is missing")
    protected = set(manifest.protected_paths)
    missing_protected = set(DEFAULT_PROTECTED) - protected
    if missing_protected:
        raise ValueError(f"manifest removes core protected paths: {sorted(missing_protected)}")
    for relative in manifest.protected_paths:
        if not manifest.resolve(relative).exists():
            raise ValueError(f"protected path does not exist: {relative}")
    configured_protected = set(config.raw["workspace"].get("protected_paths", []))
    if configured_protected != protected:
        raise ValueError("manifest and config protected_paths are inconsistent")
    for role, paths in manifest.canonical_inputs.items():
        if not paths:
            raise ValueError(f"canonical_inputs.{role} must not be empty in strict mode")
    claims_text = (root / "claims" / "CLAIMS.md").read_text(encoding="utf-8")
    if not _markdown_declares_claim_id(claims_text, manifest.final_claim_id):
        raise ValueError(
            "final_claim_id is not explicitly declared in claims/CLAIMS.md"
        )
    placeholder_files: list[str] = []
    inspected = {
        root / "claims" / "CLAIMS.md",
        manifest.resolve(manifest.claim_graph),
        *(manifest.resolve(item) for paths in manifest.canonical_inputs.values() for item in paths),
    }
    for path in sorted(inspected):
        text = path.read_text(encoding="utf-8", errors="replace").casefold()
        if any(marker.casefold() in text for marker in _PLACEHOLDER_MARKERS):
            placeholder_files.append(path.relative_to(root).as_posix())
    if placeholder_files:
        label = (
            "mathematical content"
            if graph.semantics.domain == "math-research"
            else "research content"
        )
        raise ValueError(
            f"strict validation found placeholder {label}: {placeholder_files}"
        )
    return {
        "strict": True,
        "initialization_checklist": str(checklist),
        "required_directories": list(STRICT_PROJECT_DIRECTORIES),
    }


def validate_project(
    project_root: Path,
    *,
    workspace_root: Path | None = None,
    strict: bool = False,
    profile_path: Path | None = None,
) -> dict[str, Any]:
    """Perform every local trust-boundary check without starting any provider."""
    root = project_root.resolve()
    manifest = ProjectManifest.load(root)
    config = load_config(
        root, workspace_root=workspace_root, require_manifest=True,
        profile_path=profile_path,
    )
    layout = ProjectLayout(root)
    graph = ClaimGraph.load(layout.claim_graph_path)
    graph.validate()
    selected_domain = str(config.raw["policy"]["pack"])
    if graph.semantics.domain != selected_domain:
        raise ValueError(
            "claim graph domain does not match configured policy pack: "
            f"{graph.semantics.domain} != {selected_domain}"
        )
    if manifest.final_claim_id not in graph.claims:
        raise ValueError("manifest final_claim_id is absent from the claim graph")
    trusted_payload = json.loads(
        layout.trusted_state_path.read_text(encoding="utf-8")
    )
    policy = build_policy_manifest(config)
    worker_policy = config.raw["policy"]["one_shot_compute_worker"]
    mechanical_capability = attest_mechanical_host_capability(
        declared=bool(worker_policy.get("enabled", False)),
        selection_mode=str(
            (worker_policy.get("selection_policy") or {}).get("mode")
            or "preferred"
        ),
    )
    validation_authority_head = build_validation_authority_head(
        audit_config=dict(config.raw["audit"]),
        policy_manifest_sha256=str(policy["manifest_sha256"]),
    )
    semantic_trust = SemanticTrustState.from_trusted_payload(trusted_payload)
    transition_store = CanonicalTransitionStore(
        project_root=root,
        runtime_root=layout.autonomous_root,
    )
    if semantic_trust.opted_in:
        semantic_trust.require_committed_journal(
            transition_store.verified_committed_authorizations()
        )
    semantic_alignment = SemanticAlignment.load_optional(
        root,
        layout.autonomous_root / SEMANTICS_FILENAME,
        required=semantic_trust.opted_in,
    )
    semantic_trust = semantic_alignment.reconcile_trust_state(semantic_trust)
    if semantic_trust.opted_in:
        semantic_alignment.require_positive_terminal_transition_history(
            transactions=transition_store.verified_committed_transactions(),
            claim_graph_path=layout.claim_graph_path,
            trusted_state_path=layout.trusted_state_path,
            semantics=graph.semantics,
        )
    if semantic_alignment.present:
        semantic_path = semantic_alignment.path
        missing_roles = [
            role for role in ("director", "research", "audit")
            if semantic_path not in manifest.canonical_for(role)
        ]
        if missing_roles:
            raise ValueError(
                "semantic alignment must be a canonical input for every role; "
                f"missing={missing_roles}"
            )
    semantic_result = semantic_alignment.validate_project(
        final_claim_id=manifest.final_claim_id,
        final_claim_statement=graph.claims[manifest.final_claim_id].statement,
        claim_ids=graph.claims,
        trust_state=semantic_trust,
        claim_statements={
            claim_id: claim.statement for claim_id, claim in graph.claims.items()
        },
        claim_graph=graph,
        validation_authority_head=validation_authority_head,
    )
    reconciliation_result = ReconciliationStore(
        project_root=root,
        runtime_root=layout.autonomous_root,
    ).summary(transition_store=transition_store)
    final_claim = graph.claims[manifest.final_claim_id]
    if (
        semantic_alignment.present
        and final_claim.research_status in graph.semantics.terminal_positive
        and final_claim.trust_status in {
            TrustStatus.AUDITED_NIGHTLY,
            TrustStatus.FORMALLY_VERIFIED,
            TrustStatus.CANONICAL_TRUSTED,
        }
    ):
        semantic_alignment.require_final_claim_acceptance(
            manifest.final_claim_id,
            claim_graph=graph,
            trust_state=semantic_trust,
            validation_authority_head=validation_authority_head,
        )
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
    guard = CanonicalGuard(root, config.protected_paths)
    protected = guard.snapshot()
    strict_result = _strict_project_checks(root, manifest, config, graph) if strict else {
        "strict": False,
    }
    return {
        "valid": True,
        "project_id": manifest.project_id,
        "domain": graph.semantics.domain,
        "final_claim_id": manifest.final_claim_id,
        "workspace_root": str(config.workspace_root),
        "output_protocol": OUTPUT_PROTOCOL_VERSION,
        "schemas": [*sorted(schemas), "candidate_event.schema.json (local inbox)"],
        "config_schema": "config.schema.json",
        "policy_manifest_sha256": policy["manifest_sha256"],
        "mechanical_primary": policy["one_shot_compute_worker"]["primary_route"],
        "mechanical_fallback": policy["one_shot_compute_worker"]["fallback_route"],
        "mechanical_capability": mechanical_capability,
        "config_schema_version": config.raw["schema_version"],
        "config_profile": config.profile_name,
        "providers": sorted(config.raw["providers"]),
        "migrations_applied": list(config.migrations_applied),
        "protected_files": len(protected),
        "model_turns_started": 0,
        "semantic_alignment": semantic_result,
        "reconciliation": reconciliation_result,
        **strict_result,
    }
