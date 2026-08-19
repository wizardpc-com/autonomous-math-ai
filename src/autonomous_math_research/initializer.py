from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

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
}


def _project_id(directory: Path) -> str:
    value = re.sub(r"[^a-z0-9._-]+", "-", directory.name.lower()).strip("-._")
    if not value or not value[0].isalnum():
        value = "math-project"
    return value[:100]


def _config(project_id: str) -> dict:
    return {
        "schema_version": 7,
        "project": {"name": project_id, "final_conjecture_claim_id": "C_ROOT"},
        "engine": {
            "poll_interval_seconds": 0.1, "max_consecutive_controller_errors": 5,
            "error_rate_threshold": 0.5, "error_rate_min_jobs": 4,
            "max_retries": 1, "transient_protocol_max_retries": 1,
            "model_protocol_max_retries": 1, "director_max_retries": 1,
            "director_debounce_seconds": 2.0,
        },
        "scheduler": {
            "max_director": 1, "max_research_workers": 8, "max_audit": 8,
            "max_mechanical_subworkers": 8,
            "independent_exploration_fraction": 0.25,
        },
        "budgets": {
            "global_tokens": 2_000_000_000, "soft_fraction": 0.75,
            "hard_fraction": 0.95, "per_thread_limit_action": "observe",
            "per_thread_default": 100_000,
            "per_thread": {
                "director": 60_000, "prover": 160_000, "falsifier": 120_000,
                "explorer": 120_000, "auditor": 160_000,
                "evaluator_auditor": 120_000,
            },
            "per_role": {
                role: 2_000_000_000 for role in (
                    "director", "prover", "falsifier", "explorer", "auditor",
                    "evaluator_auditor",
                )
            },
            "estimated_tokens": {
                "LOW": 60_000, "MEDIUM": 120_000, "HIGH": 180_000,
                "director": 60_000, "auditor": 160_000,
                "evaluator_auditor": 120_000,
            },
        },
        "models": {
            "director": {"model": "gpt-5.6-sol", "effort": "high", "service_tier": None},
            "prover": {"model": "gpt-5.6-sol", "effort": "xhigh", "service_tier": None},
            "falsifier": {"model": "gpt-5.6-sol", "effort": "high", "service_tier": None},
            "explorer": {"model": "gpt-5.6-sol", "effort": "high", "service_tier": None},
            "auditor": {"model": "gpt-5.6-sol", "effort": "xhigh", "service_tier": None},
            "evaluator_auditor": {"model": "gpt-5.6-sol", "effort": "xhigh", "service_tier": None},
            "smoke": {"model": "gpt-5.6-terra", "effort": "medium", "service_tier": None},
        },
        "audit": {
            "immediate_threshold": "HIGH", "critical_double_audit": True,
            "low_impact_batch_size": 8,
        },
        "stagnation": {
            "attempt_threshold": 3, "priority_penalty": 0.2,
            "force_diversification": True,
        },
        "rate_limits": {
            "reduce_exploration_percent": 75, "drain_percent": 90,
            "stop_percent": 98, "poll_interval_seconds": 60,
        },
        "observability": {
            "live_agent_feed": True, "flush_interval_seconds": 0.5,
            "max_text_chunk_chars": 2000, "max_channel_chars_per_turn": 24000,
            "capture_command_output": True,
            "max_command_output_chars_per_item": 4000,
        },
        "timeouts": {
            "default_seconds": 3600, "director": 900, "prover": 3600,
            "falsifier": 1800, "explorer": 2400, "auditor": 3000,
            "evaluator_auditor": 2400,
        },
        "workspace": {
            "protected_paths": ["claims", "proofs", "state", "artifacts", "experiments"],
            "use_worktree_for_code_modification": True, "network_access": False,
        },
        "policy": {
            "pack": "math-research", "stable_core": "persistent_filesystem_controller",
            "one_shot_compute_worker": {
                "enabled": True, "service_tier": None,
                "primary_route": {
                    "model": "gpt-5.3-codex-spark", "effort": "high",
                    "service_tier": None,
                },
                "fallback_route": {
                    "model": "gpt-5.6-luna", "effort": "medium",
                    "service_tier": None,
                },
                "fallback_condition": "permanent_unavailable_or_access_denied",
                "recursive_spawn_allowed": False, "transient_max_retries": 1,
                "model_protocol_max_retries": 1, "estimated_tokens": 60_000,
            },
        },
    }


def initialize_project(directory: Path, *, force_empty: bool = False) -> Path:
    root = directory.resolve()
    if root.exists() and any(root.iterdir()) and not force_empty:
        raise ValueError("init target must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    project_id = _project_id(root)
    manifest = {
        "schema_version": 1, "project_id": project_id,
        "final_claim_id": "C_ROOT", "config": "autonomous/config.json",
        "claim_graph": "autonomous/state/claim_graph.json",
        "trusted_state": "autonomous/state/nightly_trusted.json",
        "runtime_root": "autonomous", "prompt_root": "autonomous/prompts",
        "canonical_inputs": {
            "director": ["claims/CLAIMS.md", "state/PROGRESS.md"],
            "research": ["claims/CLAIMS.md", "state/PROGRESS.md"],
            "audit": ["claims/CLAIMS.md", "state/PROGRESS.md"],
        },
        "protected_paths": ["claims", "proofs", "state", "artifacts", "experiments"],
    }
    atomic_write_json(root / "autonomous" / "project.json", manifest)
    atomic_write_json(root / "autonomous" / "config.json", deepcopy(_config(project_id)))
    atomic_write_json(root / "autonomous" / "state" / "claim_graph.json", {
        "schema_version": 2,
        "claims": [{
            "claim_id": "C_ROOT",
            "statement": "Replace this neutral example with the exact conjecture statement.",
            "assumptions": [], "math_status": "OPEN",
            "trust_status": "CANONICAL_TRUSTED", "dependencies": [],
            "downstream_dependents": [], "evidence_paths": [],
            "known_counterexamples": [],
            "current_gaps": ["Project has not yet supplied mathematical content."],
            "active_tasks": [], "last_meaningful_progress": None,
            "priority": {"score": 1.0}, "source_status": "OPEN",
            "evidence_level": "E0_SPECULATIVE",
        }],
    })
    atomic_write_json(root / "autonomous" / "state" / "nightly_trusted.json", {
        "audited_candidate_fingerprints": [],
        "claim_evidence_levels": {"C_ROOT": "E0_SPECULATIVE"},
        "last_updated": None, "schema_version": 1,
    })
    for name, content in _PROMPTS.items():
        atomic_write_text(root / "autonomous" / "prompts" / name, content)
    atomic_write_text(
        root / "claims" / "CLAIMS.md",
        "# Claims\n\n- `C_ROOT`: Replace with an exact conjecture statement.\n",
    )
    atomic_write_text(
        root / "state" / "PROGRESS.md",
        "# Progress\n\nNo research has been run.\n",
    )
    for name in ("proofs", "artifacts", "experiments"):
        marker = root / name / ".gitkeep"
        atomic_write_text(marker, "")
    return root
