from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .profiles import builtin_profile
from .storage import atomic_write_json, atomic_write_text


_PROMPTS = {
    "director.md": """# Director

Plan falsification-first research from the controller-supplied compact state.
Do not claim proof or modify canonical state. Return only Output Protocol v2.
""",
    "prover.md": """# Prover

Work only on the exact assigned statement and representation. Preserve gaps and
emit candidate evidence separately. Return only Output Protocol v2.
""",
    "falsifier.md": """# Falsifier

Seek the cheapest exact counterexample within explicit bounds. Scope exhaustion
is not proof. Return only Output Protocol v2.
""",
    "explorer.md": """# Explorer

Explore only the assigned route and representation. Record observations without
turning them into trusted conclusions. Return only Output Protocol v2.
""",
    "auditor.md": """# Auditor

Reconstruct the candidate independently from its sealed bundle. Do not read the
producer transcript. Return only PASS, REJECT, or UNRESOLVED in Output Protocol v2.
""",
    "evaluator_auditor.md": """# Evaluator Auditor

Independently reproduce bounded computational evidence and evaluate only the
assigned evidence contract. Never promote finite evidence to proof. Return only
Output Protocol v2.
""",
    "smoke.md": """# Smoke

Exercise only the configured provider protocol and output schema. Do not perform
mathematical research or modify canonical state. Return only Output Protocol v2.
""",
    "mechanical_worker.md": """# Mechanical Worker

Execute one finite, mechanically checkable packet. Do not select research
strategy, spawn another worker, modify canonical state, or claim proof.
""",
}

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_CLAIM_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,99}$")


def _project_id(directory: Path) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", directory.name.lower()).strip("-._")
    if not value or not value[0].isalnum():
        value = "math-project"
    return value[:100]


def _config(project_id: str, final_claim_id: str) -> dict:
    return builtin_profile(project_id, final_claim_id)


def initialize_project(
    directory: Path,
    *,
    project_id: str | None = None,
    final_claim_id: str = "C_ROOT",
    force_empty: bool = False,
) -> Path:
    root = directory.resolve()
    if root.exists() and any(root.iterdir()) and not force_empty:
        raise ValueError("init target must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    selected_project_id = project_id or _project_id(root)
    if not _PROJECT_ID_RE.fullmatch(selected_project_id):
        raise ValueError("project_id must be a normalized portable identifier")
    if not _CLAIM_ID_RE.fullmatch(final_claim_id):
        raise ValueError("final_claim_id must be a portable claim identifier")
    manifest = {
        "schema_version": 1, "project_id": selected_project_id,
        "final_claim_id": final_claim_id, "config": "autonomous/config.yaml",
        "claim_graph": "autonomous/state/claim_graph.json",
        "trusted_state": "autonomous/state/nightly_trusted.json",
        "runtime_root": "autonomous", "prompt_root": "autonomous/prompts",
        "canonical_inputs": {
            "director": ["claims/CLAIMS.md", "state/PROGRESS.md"],
            "research": ["claims/CLAIMS.md", "state/PROGRESS.md"],
            "audit": ["claims/CLAIMS.md", "state/PROGRESS.md"],
        },
        "protected_paths": [
            "claims", "proofs", "state", "artifacts", "experiments",
            "certificates", "audit",
        ],
    }
    atomic_write_json(root / "autonomous" / "project.json", manifest)
    atomic_write_json(
        root / "autonomous" / "config.yaml",
        deepcopy(_config(selected_project_id, final_claim_id)),
    )
    atomic_write_json(root / "autonomous" / "state" / "claim_graph.json", {
        "schema_version": 2,
        "claims": [{
            "claim_id": final_claim_id,
            "statement": "AMR_PLACEHOLDER: replace with the exact final claim statement.",
            "assumptions": [], "math_status": "OPEN",
            "trust_status": "CANONICAL_TRUSTED", "dependencies": [],
            "downstream_dependents": [], "evidence_paths": [],
            "known_counterexamples": [],
            "current_gaps": ["AMR_PLACEHOLDER: mathematical content is not configured."],
            "active_tasks": [], "last_meaningful_progress": None,
            "priority": {"score": 1.0}, "source_status": "OPEN",
            "evidence_level": "E0_SPECULATIVE",
        }],
    })
    atomic_write_json(root / "autonomous" / "state" / "nightly_trusted.json", {
        "audited_candidate_fingerprints": [],
        "claim_evidence_levels": {final_claim_id: "E0_SPECULATIVE"},
        "last_updated": None, "schema_version": 1,
    })
    for name, content in _PROMPTS.items():
        atomic_write_text(root / "autonomous" / "prompts" / name, content)
    atomic_write_text(
        root / "claims" / "CLAIMS.md",
        f"# Claims\n\n- `{final_claim_id}`: AMR_PLACEHOLDER — replace with the exact final claim.\n",
    )
    atomic_write_text(
        root / "state" / "PROGRESS.md",
        "# Progress\n\nNo research has been run.\n",
    )
    atomic_write_text(
        root / "README.md",
        f"# {selected_project_id}\n\n"
        "Neutral project scaffold for Autonomous Math AI. Complete "
        "`INITIALIZATION_CHECKLIST.md` before a real campaign.\n",
    )
    atomic_write_text(
        root / "AGENTS.md",
        "# Project instructions\n\n"
        "Preserve falsification-first scheduling, fresh independent audit, append-only "
        "evidence, schema preflight, crash recovery, representation compatibility, and "
        "canonical gates. Model or mechanical output is never proof by itself.\n",
    )
    atomic_write_text(
        root / "INITIALIZATION_CHECKLIST.md",
        "# Initialization checklist\n\n"
        "- [ ] Replace every `AMR_PLACEHOLDER` marker.\n"
        f"- [ ] Confirm project id `{selected_project_id}` and final claim id `{final_claim_id}`.\n"
        "- [ ] Record the exact claim, domain, quantifiers, assumptions, and dependencies.\n"
        "- [ ] Review canonical inputs and protected paths.\n"
        "- [ ] Review every role's provider, model, effort, timeout, retries, budgets, and concurrency.\n"
        "- [ ] Review mechanical-worker policy, routes, backpressure, and separate budget.\n"
        "- [ ] Keep credentials as environment/system/profile references only.\n"
        "- [ ] Run `amr config validate`, `amr config explain`, and `amr validate --strict`.\n",
    )
    atomic_write_text(
        root / "autonomous" / "README.md",
        "# Autonomous adapter\n\n"
        "`project.json` maps this project into the generic harness. Runtime evidence is "
        "append-only; canonical project files remain behind controller and audit gates.\n",
    )
    directory_readmes = {
        "proofs": "Informal or formal proof material; trust changes still require audit.",
        "tasks": "Human-authored bounded task packets and planning inputs.",
        "experiments": "Reproducible exact or symbolic experiment definitions.",
        "certificates": "Machine-checkable certificates and verification metadata.",
        "audit": "Independent audit inputs and durable audit records.",
        "sources": "Source bibliography, snapshots, and provenance notes.",
        "conversations": "Optional human conversation exports; never canonical proof evidence.",
        "artifacts": "Content-addressed or reproducible research artifacts.",
    }
    for name, description in directory_readmes.items():
        atomic_write_text(root / name / "README.md", f"# {name.title()}\n\n{description}\n")
    return root
