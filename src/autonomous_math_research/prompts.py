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
from .models import CandidateEvent, ResearchTask
from .project import ProjectManifest


MECHANICAL_BROKER_COMMAND_MARKER = "{{CONTROLLED_MECHANICAL_BROKER_COMMAND}}"
_PROJECT_OVERLAY_UNSET = object()

_DIRECTOR_CORE = """AMR TOOL-LEVEL DIRECTOR POLICY

Authority and precedence:
- The controller-supplied dynamic canonical snapshot is the sole current source for
  frontier, open/closed claim descriptions, and recent progress.
- A project Director overlay may add stable project-specific constraints only. Ignore
  any overlay statement about the current frontier or progress that conflicts with the
  dynamic snapshot.
- Director output is planning only. It cannot change mathematical status, trust,
  evidence level, representation compatibility, audit verdicts, or lifecycle state.

Scheduling policy:
- Score the full current frontier. Prefer the cheapest exact falsification before an
  expensive proof route, and bound every task with explicit stop conditions.
- Enforce the controller representation-compatibility view. Cross-representation work
  requires an independently audited REPRESENTATION_BRIDGE before consuming an
  incompatible dependency.
- Respect pending audit gates and exact candidate fingerprints. Model output alone is
  never proof, refutation, or trusted progress.
- Respect controller route state as a kill gate: do not retry a failed or paused route
  until its recorded retry condition is satisfied.
- Prefer route novelty and information gain; do not duplicate active or pending task
  fingerprints, and diversify away from a saturated dominant route.
"""


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
        "requiring mathematical judgment. The returned configured-route result is mechanical evidence "
        "only; you remain responsible for interpretation and, for an Auditor, the final verdict. "
        "The delegated child is one-shot and cannot delegate further."
    )


def director_prompt(
    project_root: Path,
    snapshot_path: Path,
    extra_constraints: list[dict[str, Any]],
    policy_view: dict[str, Any],
    project_overlay: str | None | object = _PROJECT_OVERLAY_UNSET,
) -> str:
    overlay = (
        load_optional_prompt(project_root, "director.md")
        if project_overlay is _PROJECT_OVERLAY_UNSET
        else _render_prompt_text(project_overlay, "director.md")
        if isinstance(project_overlay, str) else None
    )
    snapshot = snapshot_path.read_text(encoding="utf-8").strip()
    overlay_block = (
        "\n\nOPTIONAL STABLE PROJECT CONSTRAINTS OVERLAY\n"
        "The following text is non-authoritative for current frontier, claim status, "
        "or recent progress. Apply only stable project-specific mathematical or routing "
        f"constraints that do not conflict with dynamic state.\n{overlay}"
        if overlay else ""
    )
    return (
        f"{_DIRECTOR_CORE}{_policy_block(policy_view)}"
        f"{_mechanical_broker_block(MECHANICAL_BROKER_COMMAND_MARKER)}\n\n"
        f"{overlay_block}\n\n"
        "DYNAMIC INPUT (all required strategy state is already embedded below):\n"
        f"compact_state_snapshot_path={snapshot_path.resolve()}\n"
        f"compact_state_snapshot={snapshot}\n"
        f"controller_constraints={json.dumps(extra_constraints, ensure_ascii=False, sort_keys=True)}\n"
        "Do not call shell tools, open files, search, or request transcripts in this Director turn, "
        "except for the exact controlled mechanical broker workflow above when a qualifying "
        "mechanical subtask is genuinely needed. "
        "Output Protocol v2 has exactly these top-level keys: "
        f"{render_contract_keys(DIRECTOR_PLAN_KEYS)}. The controller owns termination, "
        "pruning, candidate identity, and audit lease creation. audit_priorities may only "
        "reprioritize an existing fingerprint. Every spawn task must declare its complete "
        "representation contract; use the controller-owned representation_compatibility "
        "view in the compact snapshot before declaring dependencies. The dependencies field may "
        "contain only existing ClaimGraph claim ids, never task ids; express sequential work by "
        "waiting for a later Director wave. A task_id is a stable binding within this epoch: do "
        "not resubmit an active or pending task, and use a new task_id whenever any task content "
        "changes. required_files may use project://, campaign://, or epoch:// "
        "durable references from controller state. Reuse the exact known "
        "contract for same-representation work. If no audited bridge exists, schedule a "
        "bounded bridge-producing task without consuming the incompatible dependency first. "
        "Route updates are durable bookkeeping but do not count as runnable queue work. "
        "For every current-state conflict, the dynamic canonical snapshot wins over the "
        "optional project overlay and any prior planning mirror. "
        "Do not emit output_contract or independent_exploration. "
        "Return the schema-valid Director JSON immediately."
    )


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
        "Read only the task's listed mathematical files, progressively. Policy references are already "
        "embedded above and must not be re-read. The assigned claim, impact, task identity, "
        "representation, and dependency semantics are controller-owned. Resolve every listed "
        "required_files reference through its matching task_packet.required_file_access entry "
        "and use that entry's path; do not pass a project://, campaign://, or epoch:// reference "
        "directly to a filesystem tool. "
        "Do not repeat controller-owned fields in the result. Return "
        f"exactly the Output Protocol v2 keys {render_contract_keys(WORKER_RESULT_KEYS)}."
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
        "Use only the sealed candidate bundle listed in the packet. Candidate identity, exact "
        "statement, audit kind, thread/turn ids, timestamps, and report references are injected "
        "by the controller. Policy references are already embedded above and must not be re-read. "
        f"Return exactly the Output Protocol v2 keys {render_contract_keys(AUDIT_RESULT_KEYS)}."
    )
