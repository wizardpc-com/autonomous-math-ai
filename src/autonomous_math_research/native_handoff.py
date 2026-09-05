"""Frozen native inputs and unaudited imports over the existing research contracts."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .claim_graph import ClaimGraph
from .config import load_config
from .domain_semantics import domain_semantics_from_contract
from .models import ResearchTask, stable_hash, utc_now
from .policy import build_policy_manifest, domain_contract_from_manifest, pin_policy_manifest
from .project import ProjectManifest
from .research_memory import AssetCard, ExternalResult, ResearchMemoryStore
from .research_record import _git_state
from .resources import schema_resource
from .schema import load_schema, validate
from .storage import atomic_write_json, file_digest, validate_storage_id


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path.name}")
    return value


def _inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("evidence requires a relative path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or ".." in Path(relative).parts:
        raise ValueError(f"evidence path escapes its input root: {relative}")
    if not path.is_file():
        raise ValueError(f"missing evidence: {relative}")
    return path


def _graph(project: Path, profile: Path | None = None):
    manifest = ProjectManifest.load(project)
    config = load_config(project, require_manifest=True, profile_path=profile)
    semantics = domain_semantics_from_contract(domain_contract_from_manifest(build_policy_manifest(config)))
    graph = ClaimGraph.load(manifest.resolve(manifest.claim_graph), semantics=semantics)
    graph.validate()
    return manifest, config, graph


def _dependencies(ids: list[str], graph: ClaimGraph) -> None:
    unknown = set(ids) - set(graph.claims)
    if unknown:
        raise ValueError(f"dependencies must be ClaimGraph claim IDs: {sorted(unknown)}")


def _fresh(project: Path, manifest: ProjectManifest, config: Any, graph: ClaimGraph):
    store = ResearchMemoryStore(project, manifest.resolve(manifest.runtime_root))
    store.validate_current_freshness(
        graph=graph, claim_graph_path=manifest.resolve(manifest.claim_graph),
        trusted_state_path=manifest.resolve(manifest.trusted_state),
        final_claim_id=config.final_conjecture_claim_id,
    )
    if not store.current_path.is_file():
        raise ValueError("build the Audited Frontier before exporting native inputs")
    state = _read(store.current_path)
    # CURRENT binds the derived registry; checking its own hash alone is insufficient.
    if stable_hash(_read(store.registry_path)["assets"]) != state["asset_registry_sha256"]:
        raise ValueError("derived Asset Registry is stale; run amr frontier rebuild")
    if stable_hash(_read(store.method_ledger_path)["routes"]) != state["method_ledger_sha256"]:
        raise ValueError("derived Method Ledger is stale; run amr frontier rebuild")
    return store, state


def export_inputs(project: Path, task_path: Path, output: Path, *, budget: int, profile: Path | None = None) -> dict[str, Any]:
    project, output = project.resolve(), output.resolve()
    if budget <= 0:
        raise ValueError("native task budget must be positive")
    if output.is_relative_to(project) or project.is_relative_to(output):
        raise ValueError("native output must be separate from the project tree")
    if output.exists():
        raise ValueError("native workspace already exists; export a new input binding")
    manifest, config, graph = _graph(project, profile)
    raw_task = _read(task_path)
    with schema_resource("director_plan.schema.json") as schema_path:
        task_schema = load_schema(schema_path)
    task = ResearchTask.from_dict(raw_task)
    normalized = task.to_dict()
    normalized.pop("output_contract", None)
    validate(normalized, task_schema["properties"]["spawn"]["items"])
    _dependencies([task.target_claim, *task.dependencies], graph)
    store, state = _fresh(project, manifest, config, graph)
    theme = store.load_theme(Path(state["campaign_theme"]["source_path"])) if state.get("campaign_theme") else None
    error = store.task_admission_error(task, theme=theme, state=state)
    if error:
        raise ValueError(error)
    scope = (task.input_closure or {}).get("canonical_object_id") or task.route_family
    context = store.relevant_context_bundle(
        claim_ids=[task.target_claim, *task.dependencies], scope_ids=[scope],
        representation_ids=[task.representation_id], method_ids=[task.route_family], state=state,
    )
    sources = {manifest.path.relative_to(project).as_posix(), manifest.claim_graph,
               manifest.trusted_state, *manifest.canonical_inputs["research"], *task.required_files}
    closure = task.input_closure
    if closure:
        if closure["target_representation_id"] != task.representation_id:
            raise ValueError("task representation and input_closure disagree")
        source_ids = [row["source_id"] for row in closure["source_bindings"]]
        if len(source_ids) != len(set(source_ids)) or set(source_ids) != set(closure["required_source_ids"]):
            raise ValueError("input_closure source bindings are incomplete or duplicated")
        sources.update(row["path"] for row in closure["source_bindings"])
    sources = {path.removeprefix("project://") for path in sources}
    if theme:
        sources.add(theme.source_path)
    semantics_path = project / "autonomous" / "semantics.json"
    if semantics_path.is_file():
        sources.add(semantics_path.relative_to(project).as_posix())
    for rows in context.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or "asset_id" not in row:
                continue
            asset = AssetCard.load(project, _inside(project, row["source_manifest"]))
            sources.add(asset.source_manifest)
            sources.update(ref.path for ref in (*asset.evidence_refs, *asset.audit.report_refs))
    bindings = [{"path": relative, "sha256": file_digest(_inside(project, relative))} for relative in sorted(sources)]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".handoff-", dir=output.parent) as staging:
        stage = Path(staging) / "workspace"
        stage.mkdir()
        for row in bindings:
            target = stage / "input" / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_inside(project, row["path"]), target)
            if file_digest(target) != row["sha256"]:
                raise ValueError("source changed while freezing inputs")
        atomic_write_json(stage / "task.json", raw_task)
        atomic_write_json(stage / "context.json", context)
        pin_policy_manifest(config, stage / "policy" / "POLICY_MANIFEST.json")
        (stage / "NATIVE_README.md").write_text(
            "# Native research input\n\nRead task.json, context.json, and binding.json first. "
            "Original project paths map to input/<project-relative-path>. Read the pinned "
            "policy/ POLICY_MANIFEST.json snapshot paths for the shared research policy.\n\n"
            "Write research artifacts only in output/ and edit result.template.json. "
            "Keep the frozen inputs unchanged. Missing inputs require a new export binding. "
            "Keep the export receipt outside the producer workspace and use its input_sha256 "
            "when sealing. Budget and write boundaries are supervised process constraints, "
            "not attested OS isolation.\n\n"
            "Report the exact conclusion, proof, gaps, dependencies, producer/checker code, "
            "actual commands, versions, logs and replay instructions. Add these files to "
            "the existing evidence reference lists; the sealer computes hashes. "
            "All native output is unaudited. A fresh top-level audit session reads the "
            "sealed proof and frozen definitions, reconstructs key computations independently, "
            "and does not inherit producer chat history. Canonical promotion belongs to AMR.\n",
            encoding="utf-8",
        )
        binding = {
            "schema_version": 1, "project_id": manifest.project_id,
            "task": raw_task, "budget_tokens": budget,
            "source_bindings": bindings, "git": _git_state(project),
            "requested_route": config.route_for(task.role),
            "permission_isolation": "SUPERVISED_PROCESS_SEPARATION",
            "canonical_writer": False, "created_at": utc_now(),
        }
        atomic_write_json(stage / "binding.json", binding)
        files = [{"path": path.relative_to(stage).as_posix(), "sha256": file_digest(path)}
                 for path in sorted(stage.rglob("*")) if path.is_file()]
        packet = {"schema_version": 1, "files": files, "binding_sha256": file_digest(stage / "binding.json")}
        atomic_write_json(stage / "INPUT_MANIFEST.json", packet)
        template = {
            "schema_version": 1, "result_id": task.task_id,
            "exact_statement": task.exact_objective, "scope_ids": [scope],
            "claim_ids": [task.target_claim], "representation_id": task.representation_id,
            "dependencies": task.dependencies, "classification": "UNAUDITED_EXTERNAL_RESULT",
            "conclusion": "INCONCLUSIVE", "maturity_level": "RESULT",
            "proof_refs": [{"kind": "proof", "path": "output/proof.md", "sha256": "0" * 64}],
            "certificate_refs": [], "source_refs": [],
            "audit": {"verdict": None, "independent": False, "auditor": None,
                      "audit_level": "RESULT", "policy_version": "native-result-v1",
                      "report_refs": [], "reuse_audit_key": None},
            "provenance": {"producer": "native-codex", "origin": "native-handoff",
                           "produced_at": utc_now(), "lineage": []}, "supersedes": [],
        }
        atomic_write_json(stage / "result.template.json", template)
        (stage / "output").mkdir()
        (stage / "output" / "proof.md").write_text(
            "# Candidate result\n\nExact conclusion and scope:\n\nProof or partial argument:\n\n"
            "Gaps and failed routes (including reopen conditions):\n\n"
            "Dependencies and applicability:\n\nProducer/checker paths, commands, versions, logs and replay instructions:\n",
            encoding="utf-8",
        )
        # Recheck live identities before publishing the complete package.
        for row in bindings:
            if file_digest(_inside(project, row["path"])) != row["sha256"]:
                raise ValueError("source changed during export")
        stage.rename(output)
    return {"exported": True, "workspace": str(output), "input_sha256": file_digest(output / "INPUT_MANIFEST.json"),
            "model_turns_started": 0, "permission_isolation": "SUPERVISED_PROCESS_SEPARATION",
            "canonical_authority_changed": False}


def _unaudited(result: ExternalResult) -> None:
    if result.classification not in {"UNAUDITED_EXTERNAL_RESULT", "COMPUTATION_ONLY"}:
        raise ValueError("native imports must be unaudited or computation-only")
    audit = result.audit
    if audit.verdict is not None or audit.independent or audit.auditor or audit.report_refs or audit.reuse_audit_key:
        raise ValueError("producer output cannot supply audit authority")
    if result.supersedes:
        raise ValueError("native imports cannot supersede existing results")
    if result.classification == "COMPUTATION_ONLY" and result.conclusion != "COMPUTATION":
        raise ValueError("computation-only imports require a computation conclusion")
    if not result.proof_refs and not result.certificate_refs:
        raise ValueError("native result requires a proof or computation artifact")


def seal_result(workspace: Path, result_path: Path, output: Path, *, input_sha256: str) -> dict[str, Any]:
    workspace, output = workspace.resolve(), output.resolve()
    if output.exists() or output.is_relative_to(workspace) or workspace.is_relative_to(output):
        raise ValueError("sealed output must be a new directory separate from the native workspace")
    if file_digest(workspace / "INPUT_MANIFEST.json") != input_sha256:
        raise ValueError("input manifest differs from the retained export receipt")
    packet = _read(workspace / "INPUT_MANIFEST.json")
    for row in packet["files"]:
        if file_digest(_inside(workspace, row["path"])) != row["sha256"]:
            raise ValueError(f"frozen native input changed: {row['path']}")
    if file_digest(workspace / "binding.json") != packet["binding_sha256"]:
        raise ValueError("native input binding changed")
    raw = _read(result_path)
    with schema_resource("external_result.schema.json") as schema_path:
        validate(raw, load_schema(schema_path))
    raw = deepcopy(raw)
    raw["source_refs"].extend({"kind": "frozen_input", **row} for row in packet["files"])
    raw["source_refs"].append({"kind": "input_manifest", "path": "INPUT_MANIFEST.json", "sha256": "0" * 64})
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".seal-", dir=output.parent) as staging:
        stage = Path(staging) / "bundle"
        stage.mkdir()
        for field in ("proof_refs", "certificate_refs", "source_refs"):
            refs = []
            for row in raw[field]:
                source = _inside(workspace, row["path"])
                digest = file_digest(source)
                relative = f"evidence/{digest}/{source.name}"
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if file_digest(target) != digest:
                    raise ValueError("evidence changed while sealing")
                refs.append({"kind": row["kind"], "path": relative, "sha256": digest})
            raw[field] = list({(row["kind"], row["path"]): row for row in refs}.values())
        atomic_write_json(stage / "external_result.json", raw)
        result = ExternalResult.load(stage, stage / "external_result.json")
        _unaudited(result)
        for row in packet["files"]:
            if file_digest(_inside(workspace, row["path"])) != row["sha256"]:
                raise ValueError("frozen input changed during sealing")
        stage.rename(output)
    return {"sealed": True, "bundle": str(output), "result_sha256": file_digest(output / "external_result.json"),
            "classification": result.classification, "audit_receipt_created": False}


def import_result(project: Path, bundle: Path) -> dict[str, Any]:
    project, bundle = project.resolve(), bundle.resolve()
    manifest, config, graph = _graph(project)
    digest = file_digest(_inside(bundle, "external_result.json"))
    result = ExternalResult.load(bundle, _inside(bundle, "external_result.json"))
    if file_digest(bundle / "external_result.json") != digest:
        raise ValueError("source manifest changed during validation")
    _unaudited(result)
    _dependencies([*result.claim_ids, *result.dependencies], graph)
    for ref in result.evidence_refs:
        if error := ref.validate(bundle):
            raise ValueError(error)
    bindings = [ref for ref in result.source_refs if Path(ref.path).name == "binding.json" and ref.kind == "frozen_input"]
    if len(bindings) != 1:
        raise ValueError("native import requires one frozen input binding")
    binding = _read(_inside(bundle, bindings[0].path))
    if binding["project_id"] != manifest.project_id:
        raise ValueError("native input belongs to another project")
    task = ResearchTask.from_dict(binding["task"])
    if result.representation_id != task.representation_id:
        raise ValueError("result representation differs from frozen task")
    expected_scope = (task.input_closure or {}).get("canonical_object_id") or task.route_family
    if result.claim_ids != (task.target_claim,) or result.scope_ids != (expected_scope,):
        raise ValueError("native result must retain the frozen target and exact scope")
    for row in binding["source_bindings"]:
        if file_digest(_inside(project, row["path"])) != row["sha256"]:
            raise ValueError(f"native source binding is stale: {row['path']}")
    store = ResearchMemoryStore(project, manifest.resolve(manifest.runtime_root))
    result_id = validate_storage_id(result.result_id, "result_id")
    evidence_root = store.input_root / "native_evidence" / digest
    destination = store.external_results_root / f"{result_id}.json"
    protected = [manifest.resolve(path) for path in (*manifest.protected_paths, manifest.claim_graph, manifest.trusted_state)]
    for target in (evidence_root, destination):
        if not target.resolve().is_relative_to(project) or any(target.resolve().is_relative_to(path) for path in protected):
            raise ValueError("native import destination overlaps protected or external paths")
    raw = result.to_object()
    raw.pop("source_manifest")
    for field in ("proof_refs", "certificate_refs", "source_refs"):
        for ref in raw[field]:
            ref["path"] = (evidence_root / ref["path"]).relative_to(project).as_posix()
    if destination.exists():
        if _read(destination) != raw:
            raise ValueError("result ID already has different content; use a new result ID")
        existing = ExternalResult.load(project, destination)
        if file_digest(_inside(evidence_root, "external_result.json")) != digest:
            raise ValueError("imported source manifest is damaged")
        for ref in existing.evidence_refs:
            if error := ref.validate(project):
                raise ValueError(error)
    else:
        _fresh(project, manifest, config, graph)
        evidence_root.parent.mkdir(parents=True, exist_ok=True)
        if evidence_root.exists():
            if file_digest(_inside(evidence_root, "external_result.json")) != digest:
                raise ValueError("imported source manifest is damaged")
            for ref in result.evidence_refs:
                if error := ref.validate(evidence_root):
                    raise ValueError(error)
        else:
            with tempfile.TemporaryDirectory(prefix=".import-", dir=evidence_root.parent) as staging:
                stage = Path(staging) / "evidence"
                stage.mkdir()
                shutil.copyfile(bundle / "external_result.json", stage / "external_result.json")
                if file_digest(stage / "external_result.json") != digest:
                    raise ValueError("source manifest changed during import")
                for ref in result.evidence_refs:
                    target = stage / ref.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(_inside(bundle, ref.path), target)
                    if file_digest(target) != ref.sha256:
                        raise ValueError("evidence changed during import")
                stage.rename(evidence_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        for row in binding["source_bindings"]:
            if file_digest(_inside(project, row["path"])) != row["sha256"]:
                raise ValueError("native source binding changed during import")
        # Publish complete bytes without replacing an existing ID, even across processes.
        with tempfile.TemporaryDirectory(prefix=".publish-", dir=destination.parent) as staging:
            temporary = Path(staging) / "result.json"
            atomic_write_json(temporary, raw)
            os.link(temporary, destination)
    return {"imported": True, "manifest": str(destination), "classification": result.classification,
            "routing": "PENDING_FRONTIER_REBUILD", "candidate_queue_entered": False,
            "audit_receipt_created": False, "canonical_authority_changed": False}
