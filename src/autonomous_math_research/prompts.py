from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import (
    AUDIT_RESULT_KEYS,
    DIRECTOR_PLAN_KEYS,
    OUTPUT_PROTOCOL_VERSION,
    WORKER_RESULT_KEYS,
    render_contract_keys,
)
from .director_context import enforce_director_prompt_limit
from .models import CandidateEvent, ResearchTask
from .project import ProjectManifest


MECHANICAL_BROKER_COMMAND_MARKER = "{{CONTROLLED_MECHANICAL_BROKER_COMMAND}}"
_PROJECT_OVERLAY_UNSET = object()

def _render_prompt_text(text: str, name: str) -> str:
    replacements = {
        "{{OUTPUT_PROTOCOL_VERSION}}": str(OUTPUT_PROTOCOL_VERSION),
        "{{DIRECTOR_PLAN_KEYS}}": render_contract_keys(DIRECTOR_PLAN_KEYS),
        "{{WORKER_RESULT_KEYS}}": render_contract_keys(WORKER_RESULT_KEYS),
        "{{AUDIT_RESULT_KEYS}}": render_contract_keys(AUDIT_RESULT_KEYS),
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    unresolved = [marker for marker in replacements if marker in text]
    if unresolved:
        raise ValueError(f"unresolved prompt protocol markers in {name}: {unresolved}")
    return text.strip()


def load_prompt(project_root: Path, name: str) -> str:
    manifest_path = project_root / "autonomous" / "project.json"
    if manifest_path.is_file():
        manifest = ProjectManifest.load(project_root)
        prompt_root = manifest.resolve(manifest.prompt_root)
    else:
        prompt_root = project_root / "autonomous" / "prompts"
    return _render_prompt_text(
        (prompt_root / name).read_text(encoding="utf-8"), name,
    )


def load_optional_prompt(project_root: Path, name: str) -> str | None:
    manifest = ProjectManifest.load(project_root)
    path = manifest.resolve(manifest.prompt_root) / name
    if not path.is_file():
        return None
    return _render_prompt_text(path.read_text(encoding="utf-8"), name)


def _policy_block(policy_view: dict[str, Any]) -> str:
    references: list[str] = []
    for raw in policy_view.get("required_reference_snapshots") or []:
        path = Path(str(raw))
        references.append(f"--- {path.name} ---\n{path.read_text(encoding='utf-8').strip()}")
    policy_sections: list[str] = []
    raw_role_prompt = policy_view.get("role_prompt_snapshot")
    if (
        not raw_role_prompt
        and policy_view.get("policy_name") == "math-research"
    ):
        return (
            "\n\nPINNED MATH-RESEARCH POLICY\n"
            f"manifest_sha256={policy_view['manifest_sha256']}\n"
            f"role={policy_view['role']}\n"
            f"stable_core={policy_view['stable_core']}\n"
            f"precedence={policy_view['precedence']}\n"
            "one_shot_compute_worker="
            f"{json.dumps(policy_view.get('one_shot_compute_worker'), ensure_ascii=False, sort_keys=True)}\n"
            "The complete math-research skill is injected as an App Server skill input. "
            "The controller has also loaded every required role reference below; do not re-read "
            "these policy files with shell tools.\n"
            + "\n\n".join(references)
        )
    if raw_role_prompt:
        prompt_path = Path(str(raw_role_prompt))
        policy_sections.append(
            f"PINNED DOMAIN ROLE PROMPT\n"
            f"--- {prompt_path.name} ---\n"
            f"{prompt_path.read_text(encoding='utf-8').strip()}"
        )
    policy_sections.extend(references)
    return (
        "\n\nPINNED DOMAIN POLICY\n"
        f"policy_name={policy_view.get('policy_name', 'unknown')}\n"
        f"domain={policy_view.get('domain', 'unknown')}\n"
        f"manifest_sha256={policy_view['manifest_sha256']}\n"
        f"role={policy_view['role']}\n"
        f"stable_core={policy_view['stable_core']}\n"
        f"precedence={policy_view['precedence']}\n"
        "one_shot_compute_worker="
        f"{json.dumps(policy_view.get('one_shot_compute_worker'), ensure_ascii=False, sort_keys=True)}\n"
        "The complete domain policy is injected as an App Server skill input. "
        "The controller has also loaded every required role reference below; do not re-read "
        "these policy files with shell tools.\n"
        + "\n\n".join(policy_sections)
    )


def _mechanical_broker_block(command: str) -> str:
    return (
        "\n\nCONTROLLED ONE-SHOT MECHANICAL DELEGATION\n"
        f"delegate_mechanical_task_command={command}\n"
        "Prefer this controller-owned broker whenever finite enumeration, data normalization, "
        "formula expansion, code preparation, deterministic reproduction, or artifact checking "
        "can be stated as a finite mechanical packet. Use it only for a simple, bounded task "
        "with a fixed method and mechanically checkable acceptance. Write one schema-v1 task "
        "packet inside this job workspace, then replace PATH_TO_MECHANICAL_TASK_PACKET.json in "
        "the command with that path. Never call codex exec or another agent directly. Never "
        "delegate strategy, lemma/invariant choice, interpretation, prioritization, or any task "
        "requiring research judgment. The returned configured-route result is mechanical evidence "
        "only; you remain responsible for interpretation and, for an Auditor, the final verdict. "
        "The delegated child is one-shot and cannot delegate further."
    )


def director_prompt(
    project_root: Path,
    snapshot_path: Path,
    extra_constraints: list[dict[str, Any]],
    policy_view: dict[str, Any],
    project_overlay: str | None | object = _PROJECT_OVERLAY_UNSET,
    *,
    task_packet_path: Path | None = None,
    full_context_path: Path | None = None,
) -> str:
    del project_root, project_overlay  # Complete policy/overlay text is external state.
    compact = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if not isinstance(compact, dict):
        raise ValueError("Director compact snapshot must be a JSON object")
    provenance = compact.get("snapshot_provenance") or {}
    watermark = compact.get("controller_watermark") or {}
    counts = compact.get("summary_counts") or {}
    scheduling = compact.get("research_scheduling") or {}
    archive = compact.get("full_context_archive") or {}
    if full_context_path is None:
        relative = archive.get("relative_path") if isinstance(archive, dict) else None
        if isinstance(relative, str) and relative:
            full_context_path = snapshot_path.parent / relative
    prompt = (
        "AMR DIRECTOR TURN\n"
        "Plan the next bounded, highest-information portfolio for the exact "
        "current controller state. This message is only a routing envelope; no snapshot "
        "or transcript is inline.\n\n"
        f"run_id={provenance.get('epoch_id') or provenance.get('run_id') or 'unknown'}\n"
        f"campaign_id={provenance.get('campaign_id') or 'unknown'}\n"
        f"attempt_id={provenance.get('attempt_id') or 'unknown'}\n"
        f"state_version={watermark.get('state_version', 'unknown')}\n"
        f"constraint_count={len(extra_constraints)}\n"
        f"frontier_counts={json.dumps(counts, ensure_ascii=False, sort_keys=True)}\n"
        "independent_slots="
        f"{scheduling.get('configured_independent_exploration_slots', 'unknown')}\n"
        f"task_packet_path={task_packet_path.resolve() if task_packet_path else 'not-provided'}\n"
        f"compact_state_path={snapshot_path.resolve()}\n"
        f"full_context_archive_path={full_context_path.resolve() if full_context_path else 'missing'}\n"
        f"history_archive_path={snapshot_path.parent.parent / 'EVENTS.jsonl'}\n"
        f"policy_manifest_sha256={policy_view.get('manifest_sha256', 'unknown')}\n\n"
        "READ ORDER AND AUTHORITY\n"
        "1. Read task_packet_path and compact_state_path.\n"
        "2. When present, semantic_contract freezes the goal, registry, and bridges; only "
        "controller receipts confer trust.\n"
        "3. Use full_context_archive only for omitted detail and history only for provenance; "
        "never copy history into output.\n"
        "ClaimGraph/controller receipts are authority; Audited Frontier is routing only. "
        "Model output cannot change trust, evidence, compatibility, verdicts, or lifecycle.\n\n"
        "SCHEDULING RULES\n"
        "Apply pinned policy and Theme. Prefer cheap decisive checks, explicit stops, novelty, "
        "and information gain; never duplicate active/pending fingerprints or leave the Theme. "
        "Dependencies are existing ClaimGraph claim IDs. Cross-representation work requires an "
        "audited bridge; no unverified bridge enters a trusted final claim. Apply dependency "
        "closure, closed/duplicate removal, and kill gates in order. Query assets first; reuse an "
        "equivalent audited asset or state exact non-applicability. Unregistered core_terms are "
        "TERM_AMBIGUOUS; multi-agent agreement is not bridge evidence. Respect route and audit gates. "
        "Only pending_research is current-epoch work; never spawn checkpointed next-epoch tasks. "
        "Only controller state satisfies retry_condition. If every applicable route waits behind "
        "one, an empty plan is valid; never relabel the same obligation as fresh/independent. "
        "Set metadata.independent_exploration=true only for a real reserved slot; "
        "role=explorer alone does not satisfy it. If independent_slots=0, set it false.\n"
        f"mechanical_broker_command={MECHANICAL_BROKER_COMMAND_MARKER}\n"
        "Use that broker only for a finite deterministic packet; never delegate strategy or judgment.\n\n"
        "OUTPUT\n"
        f"Return one schema-valid Output Protocol v{OUTPUT_PROTOCOL_VERSION} JSON object with exactly: "
        f"{render_contract_keys(DIRECTOR_PLAN_KEYS)}. "
        "Do not emit output_contract. The controller owns termination, "
        "pruning, candidate identity, audit leases, and route-state application."
    )
    enforce_director_prompt_limit(prompt)
    return prompt


def worker_prompt(
    project_root: Path,
    task: ResearchTask,
    packet_path: Path,
    event_command: str,
    policy_view: dict[str, Any],
) -> str:
    static = load_prompt(project_root, f"{task.role}.md")
    return (
        f"{static}{_policy_block(policy_view)}"
        f"{_mechanical_broker_block(MECHANICAL_BROKER_COMMAND_MARKER)}\n\nDYNAMIC TASK:\n"
        f"task_packet={packet_path.resolve()}\n"
        f"candidate_event_helper={event_command}\n"
        "Read only the task's listed research files, progressively. Policy references are already "
        "embedded above and must not be re-read. Read task_packet.semantic_contract when present "
        "and task_packet.research_context before interpreting the assigned object or creating a "
        "lemma, representation bridge, generator, "
        "checker, or research tool. Reuse an equivalent audited asset by default. If an existing "
        "asset is inapplicable, preserve its id and record the violated precondition or do-not-use "
        "condition plus the exact mathematical or interface difference. Never use an UNPROVED "
        "hypothesis as a theorem. Respect every exact-scope kill gate without broadening it. "
        "Report unresolved terminology found in free text "
        "to the Auditor; deterministic registry checks cover only declared core_terms. The assigned claim, "
        "impact, task identity, "
        "representation, and dependency semantics are controller-owned. Resolve every listed "
        "candidate_protocol.semantic_binding requirement exactly when emitting a final-claim "
        "candidate, and select its type only from candidate_protocol.allowed_event_types for "
        "candidate_protocol.domain. Dynamic internal subclaims may omit semantic bridge ids. "
        "candidate_event.dependencies may contain only existing ClaimGraph claim IDs listed in "
        "candidate_protocol.allowed_dependency_claim_ids. Never place external source IDs, "
        "asset IDs, task IDs, or representation IDs there; keep source bindings in input_closure/"
        "source_bindings and keep evidence or provenance identifiers in their designated fields. "
        "Resolve every listed "
        "required_files reference through its matching task_packet.required_file_access entry "
        "and use that entry's path; do not pass a project://, campaign://, or epoch:// reference "
        "directly to a filesystem tool. "
        "Do not repeat controller-owned fields in the result. Return "
        f"exactly the Output Protocol v{OUTPUT_PROTOCOL_VERSION} keys "
        f"{render_contract_keys(WORKER_RESULT_KEYS)}. Treat "
        "task_packet.research_context.loaded_asset_ids as the sole current-turn "
        "asset-usage authority; asset_usage inside a continuation checkpoint is "
        "historical and must not be copied. For every id in loaded_asset_ids, include "
        "exactly one asset_usage row: USED "
        "with whether it is cited in the result, or REJECTED with the precise "
        "applicability reason. Do not claim use merely because an asset was loaded."
    )


def auditor_prompt(
    project_root: Path,
    event: CandidateEvent,
    audit_kind: str,
    packet_path: Path,
    policy_view: dict[str, Any],
) -> str:
    static = load_prompt(project_root, "auditor.md")
    return (
        f"{static}{_policy_block(policy_view)}"
        f"{_mechanical_broker_block(MECHANICAL_BROKER_COMMAND_MARKER)}\n\nDYNAMIC AUDIT:\n"
        f"audit_kind={audit_kind}\n"
        f"audit_packet={packet_path.resolve()}\n"
        f"candidate_fingerprint={event.fingerprint}\n"
        "Producer transcript is intentionally unavailable. Reconstruct independently from artifacts. "
        "Read audit_packet.semantic_contract when present. Check that the candidate still answers "
        "the frozen original goal, uses the same canonical object, proves every representation "
        "equivalence, and that the validator's exact PASS scope entails the target claim. A validator "
        "PASS or agreement among agents is not representation-bridge evidence. "
        "Use only the sealed candidate bundle listed in the packet. Candidate identity, exact "
        "statement, audit kind, thread/turn ids, timestamps, and report references are injected "
        "by the controller. Policy references are already embedded above and must not be re-read. "
        f"Return exactly the Output Protocol v{OUTPUT_PROTOCOL_VERSION} keys "
        f"{render_contract_keys(AUDIT_RESULT_KEYS)}."
    )
