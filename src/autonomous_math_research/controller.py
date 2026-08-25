from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Iterable
from uuid import uuid4

from .app_server import redact_auth_material
from .audit_gate import AuditGate
from .backend import AppServerBackend, CodexBackend, MockCodexBackend, TurnDirective
from .canonical_state import (
    capture_canonical_state, director_canonical_view, director_overlay_text,
    load_canonical_state, planning_context_fingerprint,
    updated_markdown_state_views,
    validate_canonical_research_state, validate_canonical_state,
    verify_live_startup_sources,
)
from .canonical_transition import (
    CanonicalTransitionStore, bytes_sha256, json_bytes,
)
from .claim_graph import ClaimGraph
from .config import CONFIG_SCHEMA_VERSION, HarnessConfig, default_max_audit
from .contracts import (
    LOCAL_STRUCTURAL_FAILURES,
    MODEL_PROTOCOL_FAILURES,
    OUTPUT_PROTOCOL_VERSION,
    TRANSIENT_FAILURES,
    job_lifecycle_metrics,
    mechanical_lifecycle_metrics,
)
from .eventing import CandidateInbox
from .director_context import (
    DIRECTOR_PROMPT_HARD_LIMIT_BYTES,
    DIRECTOR_PROMPT_TARGET_BYTES,
    DirectorPromptTooLarge,
    build_compact_snapshot,
    enforce_director_prompt_limit,
    load_full_context_archive,
)
from .domain_semantics import domain_semantics_from_contract
from .engine import DynamicScheduler
from .experiment import ExperimentRunner
from .lifecycle import (
    AuditLeaseBook, AuditLeaseStatus, MonotoneLifecycle, RouteLedger,
    write_core_capsule, write_research_map,
)
from .lifecycle.campaign import (
    CampaignStore, DEFAULT_CAMPAIGN_HOURS, DEFAULT_EPOCH_HOURS,
)
from .mechanical import (
    MECHANICAL_FALLBACK_FAILURE_KINDS,
    MECHANICAL_PARENT_ROLES,
    MECHANICAL_ROLE,
    MechanicalExecution,
    MechanicalRunner,
    MechanicalTaskRejected,
    SubprocessMechanicalRunner,
    build_mechanical_runner,
    validate_mechanical_request,
    validate_mechanical_response,
)
from .models import (
    AuditResult, CandidateEvent, DirectorPlan, EvidenceLevel, Impact, JobOutcome,
    LifecyclePhase, ResearchTask, Role, TokenUsage, TrustStatus,
    derived_claim_id, evidence_rank,
    stable_hash, utc_now,
)
from .outcomes import write_outcome_archive
from .prompts import (
    MECHANICAL_BROKER_COMMAND_MARKER,
    auditor_prompt,
    director_prompt,
    worker_prompt,
)
from .provenance import capture_runtime_provenance
from .policy import (
    build_policy_manifest, domain_contract_for_run, domain_contract_from_manifest,
    pin_policy_manifest, policy_view_for_role,
)
from .provider_backend import ProviderRouterBackend
from .provider_config import (
    allowed_observed_service_tiers, mapped_reasoning_effort,
    validate_service_tier,
)
from .profiles import migrate_config
from .reporting import write_report
from .representation import RepresentationContract, require_compatible_representations
from .reasoning_health import ReasoningHealthMonitor
from .research_job import ResearchTurnPolicy
from .resources import schema_resource
from .schema import (
    OutputSchemaCompatibilityError, load_schema, preflight_output_schema_files,
    validate_output_schema_compatibility,
)
from .semantic_alignment import (
    SEMANTICS_FILENAME, SEMANTIC_AUDIT_AUTHORITY_CONTEXT_SCHEMA_VERSION,
    SEMANTIC_VALIDATOR_VERSION, SemanticAlignment,
    SemanticPromotionError, SemanticTrustState, build_validation_authority_head,
    normalize_semantic_audit_authority_context, semantic_validator_identity,
    text_sha256,
)
from .stagnation import StagnationTracker
from .storage import (
    CanonicalGuard, EventStore, ProjectLayout, atomic_write_json, file_digest,
    read_jsonl,
)
from .storage import ArtifactStore
from .storage.artifacts import (
    PORTABLE_SCHEMES, portable_project_uri, resolve_portable_uri,
)
from .token_governor import TokenGovernor
from .workspace import WorkspaceManager


DEFAULT_RUN_HOURS = 5.0
CONTINUATION_CHECKPOINT_REASONS = frozenset({
    "bounded same-thread turn limit reached",
    "controller token budget reached",
    "token telemetry unavailable; bounded continuation stopped fail-closed",
})
_PYTHON_DELEGATION_CODE_RE = re.compile(
    r"(?is)(?:subprocess\.(?:run|popen|call)|os\.system)\s*\(.{0,500}?"
    r"(?:^|[\s\"'/\\])codex(?:\.cmd|\.exe)?(?:[\s\"']|$)"
)


def _split_unquoted_shell_segments(command: str) -> list[str]:
    """Split shell pipelines without treating quoted evidence as executable syntax."""
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in command:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quote:
            current.append(character)
            if character == "`":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            current.append(character)
        elif character in ";&|\r\n":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
        else:
            current.append(character)
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _shell_words(segment: str) -> list[str]:
    """Return simple shell words with surrounding quotes removed."""
    words: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for character in segment:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quote:
            if character == "`":
                escaped = True
            elif character == quote:
                quote = None
            else:
                current.append(character)
            continue
        if character in {"'", '"'}:
            quote = character
        elif character.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(character)
    if current:
        words.append("".join(current))
    return words


def _command_basename(value: str) -> str:
    return value.strip().replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_codex_executable(value: str) -> bool:
    return _command_basename(value) in {"codex", "codex.cmd", "codex.exe"}


def _is_forbidden_python_script(value: str) -> bool:
    normalized = value.strip().replace("\\", "/").lower()
    if normalized.rsplit("/", 1)[-1] == "run_worker.py":
        return True
    return bool(re.search(
        r"(?:^|/)tools/autonomous_math_research/"
        r"(?:delegate_mechanical_task|mechanical|controller|cli|__main__|smoke)\.py$",
        normalized,
    ))


def _is_unauthorized_top_level_delegation(command: str, *, depth: int = 0) -> bool:
    """Detect execution of a delegation entry point, not a harmless mention of it."""
    if not command or depth > 3:
        return False
    for segment in _split_unquoted_shell_segments(command):
        words = _shell_words(segment)
        if not words:
            continue

        # Shell launch helpers still put the actual executable in command position.
        while words and _command_basename(words[0]) in {
            "call", "command", "exec", "nohup", "start",
        }:
            words = words[1:]
        if not words:
            continue
        executable = _command_basename(words[0])
        if _is_codex_executable(words[0]) or executable in {
            "spawn_agent", "create_thread",
        }:
            return True

        if executable in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}:
            lowered = [word.lower() for word in words]
            for flag in ("-command", "-c"):
                if flag in lowered[1:]:
                    index = lowered.index(flag, 1)
                    return _is_unauthorized_top_level_delegation(
                        " ".join(words[index + 1:]), depth=depth + 1,
                    )
            continue
        if executable in {"cmd", "cmd.exe"}:
            lowered = [word.lower() for word in words]
            if "/c" in lowered:
                index = lowered.index("/c")
                return _is_unauthorized_top_level_delegation(
                    " ".join(words[index + 1:]), depth=depth + 1,
                )
            continue
        if executable in {"sh", "bash", "zsh"}:
            lowered = [word.lower() for word in words]
            for flag in ("-c", "-lc"):
                if flag in lowered:
                    index = lowered.index(flag)
                    return _is_unauthorized_top_level_delegation(
                        " ".join(words[index + 1:]), depth=depth + 1,
                    )
            continue
        if executable in {"start-process", "start-process.exe"}:
            # This branch is reached only when Start-Process is itself executed.
            if any(_is_codex_executable(word) for word in words[1:]):
                return True
            continue

        if executable in {"npx", "bunx"}:
            if any(
                word.lower().replace("\\", "/") == "@openai/codex"
                for word in words[1:]
            ):
                return True
        if executable in {"npm", "pnpm"} and len(words) > 2:
            if words[1].lower() == "exec" and any(
                word.lower().replace("\\", "/") == "@openai/codex"
                for word in words[2:]
            ):
                return True

        if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?(?:\.exe)?", executable):
            lowered = [word.lower() for word in words]
            if "-m" in lowered:
                index = lowered.index("-m")
                legacy_module_prefix = "tools" + ".autonomous_math_research"
                if index + 1 < len(words) and (
                    words[index + 1].lower() == "codex"
                    or words[index + 1].lower().startswith(legacy_module_prefix)
                ):
                    return True
            if "-c" in lowered:
                index = lowered.index("-c")
                code = " ".join(words[index + 1:])
                if _PYTHON_DELEGATION_CODE_RE.search(code):
                    return True
            if any(_is_forbidden_python_script(word) for word in words[1:]):
                return True
        elif _is_forbidden_python_script(words[0]):
            return True
    return False


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<depth-limit>"
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:499] + "…"
    if isinstance(value, dict):
        return {
            str(key): _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [_bounded_value(item, depth=depth + 1) for item in value[:20]]
    return value


def _candidate_fingerprint_matches_persisted(
    raw: dict[str, Any], event: CandidateEvent, expected: str,
) -> bool:
    """Accept only exact current or known pre-representation fingerprints.

    Historical candidate payloads did not persist a representation contract.
    Once either representation field is present, the current formula is the
    only valid identity; this prevents a tampered modern payload from being
    accepted through a legacy compatibility path.
    """
    if event.fingerprint == expected:
        return True
    if "representation" in raw or "bridge_representation_ids" in raw:
        return False
    common = {
        "claim_id": event.claim_id,
        "type": event.type,
        "exact_statement": " ".join(event.exact_statement.split()),
        "assumptions": sorted(
            " ".join(item.split()) for item in event.assumptions
        ),
        "dependencies": sorted(event.dependencies),
    }
    # v0 preceded derived-claim parent identity; v1 added it. Both formulas
    # are deterministic and are used only when the persisted payload itself
    # proves that it predates Representation Contract.
    return expected in {
        stable_hash(common),
        stable_hash({
            "claim_id": event.claim_id,
            "parent_claim_id": event.parent_claim_id,
            "type": event.type,
            "exact_statement": common["exact_statement"],
            "assumptions": common["assumptions"],
            "dependencies": common["dependencies"],
        }),
    }


def _bounded_notification_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Keep lifecycle evidence without copying model messages into the trace."""
    if method == "thread/started":
        thread = params.get("thread") or {}
        return {"thread": {
            key: _bounded_value(thread.get(key))
            for key in (
                "id", "status", "modelProvider", "cliVersion", "createdAt",
                "serviceTier", "canAcceptDirectInput",
            )
            if key in thread
        }}
    if method in {"turn/started", "turn/completed"}:
        turn = params.get("turn") or {}
        items = turn.get("items") or []
        compact_turn = {
            key: _bounded_value(turn.get(key))
            for key in (
                "id", "status", "startedAt", "completedAt", "durationMs",
                "error", "serviceTier",
            )
            if key in turn
        }
        compact_turn["itemCount"] = len(items)
        compact_turn["itemTypes"] = [
            str(item.get("type") or "unknown") for item in items[:50]
            if isinstance(item, dict)
        ]
        return {
            "threadId": params.get("threadId"),
            "turn": compact_turn,
        }
    if method == "thread/tokenUsage/updated":
        usage = params.get("tokenUsage") or {}
        return {
            "threadId": params.get("threadId"), "turnId": params.get("turnId"),
            "tokenUsage": {
                "last": _bounded_value(usage.get("last") or {}),
                "total": _bounded_value(usage.get("total") or {}),
                "modelContextWindow": usage.get("modelContextWindow"),
            },
        }
    if method == "thread/goal/updated":
        goal = params.get("goal") or {}
        return {
            "threadId": params.get("threadId"), "turnId": params.get("turnId"),
            "goal": {
                key: _bounded_value(goal.get(key))
                for key in (
                    "status", "tokenBudget", "tokensUsed", "timeUsedSeconds",
                    "createdAt", "updatedAt",
                )
                if key in goal
            },
        }
    return _bounded_value(params)


_LIVE_SECRET_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED_OPENAI_KEY]"),
    (
        re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
)


def _sanitize_live_text(value: Any) -> str:
    text = str(redact_auth_material(value or ""))
    text = "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)
    for pattern, replacement in _LIVE_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _live_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    """Return useful tool activity without results, raw reasoning, or diffs."""
    item_type = str(item.get("type") or "unknown")
    summary: dict[str, Any] = {
        "item_id": item.get("id"), "item_type": item_type,
        "status": item.get("status"),
    }
    if item_type == "commandExecution":
        summary.update({
            "command": _sanitize_live_text(item.get("command"))[:500],
            "cwd": _sanitize_live_text(item.get("cwd"))[:300],
            "exit_code": item.get("exitCode"), "duration_ms": item.get("durationMs"),
        })
    elif item_type == "fileChange":
        summary["changes"] = [
            {"path": str(change.get("path", ""))[:300], "kind": change.get("kind")}
            for change in (item.get("changes") or [])[:20]
            if isinstance(change, dict)
        ]
    elif item_type == "mcpToolCall":
        summary.update({"server": item.get("server"), "tool": item.get("tool")})
    elif item_type == "dynamicToolCall":
        summary.update({
            "tool": item.get("tool"), "success": item.get("success"),
            "duration_ms": item.get("durationMs"),
        })
    elif item_type in {
        "collabToolCall", "collabAgentToolCall", "subAgentActivity",
    }:
        summary.update({
            "tool": item.get("tool"), "agent_status": item.get("agentStatus"),
            "new_thread_id": item.get("newThreadId"),
            "agent_thread_id": item.get("agentThreadId"),
            "receiver_thread_ids": item.get("receiverThreadIds"),
            "agent_path": item.get("agentPath"),
            "activity_kind": item.get("kind"),
        })
    elif item_type == "webSearch":
        summary["query"] = _sanitize_live_text(item.get("query"))[:500]
    elif item_type == "imageView":
        summary["path"] = _sanitize_live_text(item.get("path"))[:300]
    elif item_type in {"enteredReviewMode", "exitedReviewMode"}:
        summary["review"] = _sanitize_live_text(item.get("review"))[:500]
    return {
        key: value for key, value in summary.items()
        if value is not None and value != "" and value != []
    }


def _reasoning_summary_text(summary: Any) -> str:
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        pieces: list[str] = []
        for item in summary:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("summary")
                if isinstance(value, str):
                    pieces.append(value)
        return "\n".join(pieces)
    return ""


@dataclass(slots=True)
class ActiveJob:
    logical_job_id: str
    task: ResearchTask
    future: asyncio.Task[JobOutcome]
    started_monotonic: float
    timeout: float
    kind: str
    workspace: str | None = None
    started_at: str | None = None
    workspace_metadata: dict[str, Any] | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    provider: str | None = None
    provider_profile: str | None = None
    requested_service_tier: str | None = None
    broker_client_sha256: str | None = None
    broker_config_sha256: str | None = None


@dataclass(slots=True)
class MechanicalRequestState:
    parent_job_id: str
    parent_task_id: str
    parent_role: str
    parent_workspace: str
    request_path: str
    response_path: str
    request_sha256: str
    packet: dict[str, Any]
    recovered: bool = False
    attempts_started: int = 0
    accumulated_usage: TokenUsage = field(default_factory=TokenUsage)
    telemetry_observed: int = 0
    telemetry_unknown: int = 0
    accumulated_cost_usd: float = 0.0
    cost_telemetry_observed: int = 0
    cost_telemetry_unknown: int = 0
    fallback_emitted: bool = False
    retry_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class ActiveMechanicalJob:
    logical_job_id: str
    request: MechanicalRequestState
    future: asyncio.Task[MechanicalExecution]
    started_monotonic: float
    started_at: str
    estimated_tokens: int


@dataclass(slots=True)
class RunResult:
    run_id: str
    report_path: Path
    stopped_reason: str
    job_count: int
    event_count: int
    jobs_started: int = 0
    jobs_completed: int = 0
    jobs_cancelled: int = 0
    jobs_terminal: int = 0
    mechanical_subtasks_requested: int = 0
    mechanical_attempts_started: int = 0
    mechanical_subtasks_terminal: int = 0
    internal_failure: bool = False
    run_mode: str = "real"
    outcome_path: Path | None = None
    campaign_id: str = ""
    epoch_id: str = ""
    campaign_status: str = "PAUSED"
    artifacts_finalized: bool = False


class AutonomousController:
    def __init__(
        self,
        config: HarnessConfig,
        *,
        backend: CodexBackend | None = None,
        run_id: str | None = None,
        global_budget: int | None = None,
        max_director: int | None = None,
        max_research_workers: int | None = None,
        max_research: int | None = None,
        max_audit: int | None = None,
        max_mechanical_subworkers: int | str | None = None,
        mechanical_runner: MechanicalRunner | None = None,
        mock: bool = False,
        resume: bool = False,
        recover_candidates_from: str | None = None,
        campaign_id: str | None = None,
        previous_epoch_id: str | None = None,
        campaign_hours: float = DEFAULT_CAMPAIGN_HOURS,
        epoch_hours: float = DEFAULT_EPOCH_HOURS,
    ):
        self.config = config
        self.layout = ProjectLayout(config.project_root)
        self.layout.ensure()
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_dir = self.layout.run_dir(self.run_id)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.policy_manifest_path = self.run_dir / "policy" / "MANIFEST.json"
        self.domain_contract = domain_contract_for_run(
            config, self.policy_manifest_path, resume=resume,
        )
        self.domain_semantics = domain_semantics_from_contract(self.domain_contract)
        self.store = EventStore(self.run_dir / "EVENTS.jsonl", self.run_id)
        self.campaign_id = campaign_id or self.run_id
        self.epoch_id = self.run_id
        self.previous_epoch_id = previous_epoch_id
        self._previous_epoch_checkpoint_imported = previous_epoch_id is None
        self._checkpoint_planning_mirror: dict[str, Any] | None = None
        self._attempt_id = uuid4().hex
        self._attempt_started_sequence = 0
        self._recovery_completed = not resume
        self._bootstrap_completed = False
        self._secondary_failures: list[dict[str, Any]] = []
        self._operator_interrupted = False
        self.campaign_hours = float(campaign_hours)
        self.epoch_hours = float(epoch_hours)
        if self.campaign_hours <= 0 or self.epoch_hours <= 0:
            raise ValueError("campaign_hours and epoch_hours must be positive")
        self.campaign_store = CampaignStore(
            self.layout.autonomous_root, self.campaign_id,
        )
        # A lease belongs to the campaign frontier, not to one process epoch.
        # Keeping the append-only book here makes deduplication and retry state
        # survive a sealed epoch without rewriting historical run evidence.
        self.audit_leases = AuditLeaseBook(
            self.campaign_store.root / "AUDIT_LEASES.jsonl"
        )
        self.route_ledger = RouteLedger(self.campaign_store.root / "ROUTE_LEDGER.jsonl")
        self.artifact_store = ArtifactStore(
            project_root=config.project_root,
            campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            epoch_root=self.run_dir,
        )
        self.live_store = EventStore(self.run_dir / "LIVE_EVENTS.jsonl", self.run_id)
        self.persist_shared_state = not mock
        self.canonical_transitions = CanonicalTransitionStore(
            project_root=config.project_root,
            runtime_root=self.layout.autonomous_root,
        )
        self.graph = ClaimGraph.load(
            self.layout.claim_graph_path, semantics=self.domain_semantics,
        )
        self.graph.validate()
        legacy_representation = RepresentationContract.legacy()
        legacy_representation_id = legacy_representation.representation_id
        self.claim_representations: dict[str, str] = {
            claim_id: legacy_representation_id for claim_id in self.graph.claims
        }
        self.representation_contracts: dict[str, dict[str, Any]] = {
            legacy_representation_id: legacy_representation.to_dict(),
        }
        self.audited_representation_bridges: set[tuple[str, str]] = set()
        trusted: dict[str, Any] = {}
        if self.layout.trusted_state_path.is_file():
            trusted = json.loads(
                self.layout.trusted_state_path.read_text(encoding="utf-8")
            )
            if not isinstance(trusted, dict):
                raise ValueError("canonical trusted state must be a JSON object")
            for representation_id, raw_contract in dict(
                trusted.get("representation_contracts") or {}
            ).items():
                if (
                    not isinstance(representation_id, str)
                    or not representation_id.startswith("rep:")
                ):
                    raise ValueError("trusted representation contract id is invalid")
                contract = RepresentationContract.from_dict(raw_contract)
                if contract.representation_id != representation_id:
                    raise ValueError(
                        "trusted representation contract id does not match its content: "
                        f"{representation_id}"
                    )
                self.representation_contracts[representation_id] = contract.to_dict()
            for claim_id, representation_id in dict(
                trusted.get("claim_representations") or {}
            ).items():
                if claim_id in self.claim_representations and isinstance(representation_id, str):
                    self.claim_representations[claim_id] = representation_id
            for pair in trusted.get("audited_representation_bridges") or []:
                if (
                    isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(item, str) and item for item in pair)
                ):
                    self.audited_representation_bridges.add(tuple(sorted(pair)))
        self.final_conjecture_claim_id = config.final_conjecture_claim_id
        if (
            self.final_conjecture_claim_id
            and self.final_conjecture_claim_id not in self.graph.claims
        ):
            raise ValueError(
                "configured final conjecture claim is absent from the claim graph: "
                f"{self.final_conjecture_claim_id}"
            )
        loaded_semantic_trust = SemanticTrustState.from_trusted_payload(trusted)
        self.semantic_alignment = SemanticAlignment.load_optional(
            config.project_root,
            self.layout.autonomous_root / SEMANTICS_FILENAME,
            required=loaded_semantic_trust.opted_in,
        )
        self.semantic_trust = self.semantic_alignment.reconcile_trust_state(
            loaded_semantic_trust
        )
        self._semantic_trust_dirty = self.semantic_trust != loaded_semantic_trust
        self._candidate_producer_identities: dict[str, dict[str, str]] = {}
        self._pending_semantic_authorization: dict[str, Any] | None = None
        self._require_semantics_as_canonical_input()
        if mock:
            mock_graph_path = self.run_dir / "state" / "claim_graph.json"
            if resume and mock_graph_path.is_file():
                self.graph = ClaimGraph.load(
                    mock_graph_path, semantics=self.domain_semantics,
                )
                self.graph.validate()
            else:
                self.graph.path = mock_graph_path
                self.graph.save()
            self.inbox = CandidateInbox(
                self.layout,
                inbox_root=self.run_dir / "events" / "inbox",
                event_log=self.run_dir / "events" / "CANDIDATES.jsonl",
                candidate_root=self.run_dir / "candidates",
            )
            self.audit_root = self.run_dir / "audits"
        else:
            self.inbox = CandidateInbox(self.layout)
            self.audit_root = self.layout.autonomous_root / "audits"
        if self.final_conjecture_claim_id:
            self.semantic_alignment.validate_project(
                final_claim_id=self.final_conjecture_claim_id,
                final_claim_statement=self.graph.claims[
                    self.final_conjecture_claim_id
                ].statement,
                claim_ids=self.graph.claims,
                trust_state=self.semantic_trust,
                claim_statements={
                    claim_id: claim.statement
                    for claim_id, claim in self.graph.claims.items()
                },
                claim_graph=self.graph,
                validation_authority_head=self._current_validation_authority_head(),
            )
        self._committed_claim_statuses = {
            claim_id: claim.research_status
            for claim_id, claim in self.graph.claims.items()
        }
        self._committed_semantic_trust = deepcopy(self.semantic_trust)
        self.graph.set_terminal_transition_guard(
            self._guard_graph_terminal_transition
        )
        self.audit_root.mkdir(parents=True, exist_ok=True)
        self.audit_gate = AuditGate(
            high_threshold=config.raw["audit"].get("immediate_threshold", "HIGH"),
            critical_double_audit=bool(config.raw["audit"].get("critical_double_audit", True)),
            semantics=self.domain_semantics,
        )
        if max_research_workers is not None and max_research is not None:
            raise ValueError("use max_research_workers or legacy max_research, not both")
        research_override = (
            max_research_workers if max_research_workers is not None else max_research
        )
        self.max_director = (
            int(max_director) if max_director is not None else config.max_director
        )
        self.max_research_workers = (
            int(research_override)
            if research_override is not None else config.max_research_workers
        )
        # Compatibility for callers that inspect the old attribute. Scheduling
        # uses max_research_workers exclusively from manifest schema v5 onward.
        self.max_research = self.max_research_workers
        audit_override = max_audit
        if audit_override is None and research_override is not None:
            audit_override = default_max_audit(self.max_research_workers)
        self.max_audit = (
            int(audit_override) if audit_override is not None else config.max_audit
        )
        self.dynamic_scheduler = DynamicScheduler(
            max_research=self.max_research_workers,
            max_audit=self.max_audit,
        )
        self.route_dispatch_counts: dict[str, int] = {}
        self._last_dynamic_scheduler_signature: tuple[Any, ...] | None = None
        worker_policy = config.raw["policy"].get("one_shot_compute_worker", {})
        selection_mode = str(
            (worker_policy.get("selection_policy") or {}).get("mode") or "preferred"
        )
        self.mechanical_worker_enabled = bool(
            worker_policy.get("enabled", False) and selection_mode != "disabled"
        )
        configured_mechanical = config.raw["scheduler"].get("max_mechanical_subworkers")
        selected_mechanical = (
            max_mechanical_subworkers
            if max_mechanical_subworkers is not None else configured_mechanical
        )
        self.max_mechanical_subworkers = (
            None
            if selected_mechanical is None or str(selected_mechanical).casefold() == "unbounded"
            else int(selected_mechanical)
        )
        if (
            self.mechanical_worker_enabled
            and self.max_mechanical_subworkers is not None
            and self.max_mechanical_subworkers < 1
        ):
            raise ValueError("max_mechanical_subworkers must be positive when enabled")
        self.mechanical_primary_route = self._mechanical_route(
            worker_policy["primary_route"]
        )
        self.mechanical_fallback_route = self._mechanical_route(
            worker_policy["fallback_route"]
        )
        self._validate_concurrency_limits(enforce_audit_ratio=not resume)
        budget = global_budget if global_budget is not None else config.raw["budgets"].get("global_tokens")
        if budget is not None and int(budget) <= 0:
            raise ValueError("global token budget must be positive")
        self.governor = TokenGovernor(
            global_budget=int(budget) if budget is not None else None,
            configured_max_research=self.max_research_workers,
            global_cost_budget=(
                float(config.raw["budgets"]["global_cost_usd"])
                if config.raw["budgets"].get("global_cost_usd") is not None else None
            ),
            soft_fraction=float(config.raw["budgets"].get("soft_fraction", 0.75)),
            hard_fraction=float(config.raw["budgets"].get("hard_fraction", 0.95)),
            role_budgets={k: int(v) for k, v in config.raw["budgets"].get("per_role", {}).items()},
            role_cost_budgets={
                role: float(limit)
                for role, route in config.raw["models"].items()
                for limit in [
                    route.get("cost_limit_usd")
                    if route.get("cost_limit_usd") is not None
                    else config.raw["budgets"].get("per_role_cost_usd", {}).get(role)
                ]
                if limit is not None
            },
            rate_reduce_percent=float(config.raw["rate_limits"].get("reduce_exploration_percent", 75)),
            rate_drain_percent=float(config.raw["rate_limits"].get("drain_percent", 90)),
            rate_stop_percent=float(config.raw["rate_limits"].get("stop_percent", 98)),
        )
        mechanical_budget = config.raw["budgets"].get("mechanical_tokens")
        mechanical_cost_budget = config.raw["budgets"].get("mechanical_cost_usd")
        self.mechanical_governor = TokenGovernor(
            global_budget=(int(mechanical_budget) if mechanical_budget is not None else None),
            configured_max_research=max(1, os.cpu_count() or 1),
            global_cost_budget=(
                float(mechanical_cost_budget)
                if mechanical_cost_budget is not None else None
            ),
            soft_fraction=float(config.raw["budgets"].get("soft_fraction", 0.75)),
            hard_fraction=float(config.raw["budgets"].get("hard_fraction", 0.95)),
            role_budgets={},
            role_cost_budgets={},
            rate_reduce_percent=float(config.raw["rate_limits"].get("reduce_exploration_percent", 75)),
            rate_drain_percent=float(config.raw["rate_limits"].get("drain_percent", 90)),
            rate_stop_percent=float(config.raw["rate_limits"].get("stop_percent", 98)),
        )
        self.stagnation = StagnationTracker(int(config.raw["stagnation"]["attempt_threshold"]))
        engine_config = config.raw["engine"]
        self.research_turn_policy = ResearchTurnPolicy(
            max_turns=dict(engine_config["research_max_turns"]),
            domain=self.domain_semantics.domain,
        )
        self.reasoning_health = ReasoningHealthMonitor(
            short_reasoning_tokens=int(engine_config["reasoning_health_short_tokens"]),
            repeated_token_tolerance=int(
                engine_config["reasoning_health_repeated_token_tolerance"]
            ),
            retry_limit=int(engine_config["reasoning_health_retry_limit"]),
        )
        self.workspace = WorkspaceManager(
            config.workspace_root or config.project_root, self.run_dir,
        )
        self._mechanical_broker_client_source: Path | None = None
        self._mechanical_broker_client_sha256: str | None = None
        self.backend = backend or (
            MockCodexBackend()
            if mock
            else ProviderRouterBackend(config, self._trace_notification)
        )
        self.mock = mock
        self.resume = resume
        if resume and recover_candidates_from:
            raise ValueError("--resume cannot be combined with candidate recovery into a new run")
        self.recover_candidates_from = recover_candidates_from
        self._budget_override = global_budget
        self._max_director_override = max_director
        self._max_research_workers_override = research_override
        self._max_audit_override = audit_override
        self._max_mechanical_subworkers_override = max_mechanical_subworkers
        self.guard = CanonicalGuard(config.project_root, config.protected_paths)
        self.pending_research: list[ResearchTask] = []
        self.deferred_research_continuations: list[ResearchTask] = []
        self.pending_audits: list[ResearchTask] = []
        self.active: dict[str, ActiveJob] = {}
        self._scheduled_backend_cancellations: set[asyncio.Task[bool]] = set()
        self.pending_mechanical: list[MechanicalRequestState] = []
        self.active_mechanical: dict[str, ActiveMechanicalJob] = {}
        self.completed_mechanical_jobs: list[dict[str, Any]] = []
        self._mechanical_request_keys: set[tuple[str, str]] = set()
        self._mechanical_result_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._mechanical_unavailable_routes: set[tuple[str, str | None, None]] = set()
        self._last_mechanical_dispatch = 0.0
        self.mechanical_runner = mechanical_runner or build_mechanical_runner(
            config,
            config.project_root,
            primary_route=self.mechanical_primary_route,
            fallback_route=self.mechanical_fallback_route,
        )
        self.completed_jobs: list[dict[str, Any]] = []
        self.seen_task_fingerprints: set[str] = set()
        self.task_fingerprints_by_id: dict[str, str] = {}
        self.candidate_queue: asyncio.Queue[CandidateEvent] = asyncio.Queue()
        self.recent_changes: list[dict[str, Any]] = []
        self.director_needed = True
        self._state_version = 0
        self._director_requested_version = 0
        self._director_snapshot_version = 0
        self._director_applied_version = -1
        self._snapshot_generation = 0
        self._latest_director_context_path: Path | None = None
        self._latest_director_prompt_bytes = 0
        self._director_not_before = 0.0
        self._replan_after_wave = False
        self.director_constraints: list[dict[str, Any]] = []
        self._director_active = False
        self._director_incremental = False
        self._bound_jobs: dict[str, tuple[str, str]] = {}
        self._blocker_repair_jobs: set[str] = set()
        self.scheduler_stop_reason: str | None = None
        self._provider_transport_lost: dict[str, Any] | None = None
        self.lifecycle = MonotoneLifecycle()
        self.final_claim_resolved = False
        self.final_claim_outcome: str | None = None
        self.final_conjecture_proved = False
        self.final_conjecture_refuted = False
        self._finalization_started = False
        self._scheduler_event_keys: set[str] = set()
        self.capability_snapshot: dict[str, Any] | None = None
        self.policy_manifest: dict[str, Any] | None = None
        self.policy_status: dict[str, Any] | None = None
        self._run_manifest: dict[str, Any] = {}
        self._runtime_provenance: dict[str, Any] | None = None
        self._canonical_state: dict[str, Any] | None = None
        self._director_overlay: str | None = None
        self._latest_rate_limits: dict[str, Any] | None = None
        self._last_rate_check = 0.0
        self.stale_remote_turns: list[tuple[str, str, str]] = []
        self.retry_counts: dict[tuple[str, str], int] = {}
        self.audit_retry_counts: dict[tuple[str, str], int] = {}
        self.director_retry_count = 0
        self.director_retry_counts: dict[str, int] = {}
        self.cancelled_jobs: set[str] = set()
        self._thread_budget_limit_reported: set[str] = set()
        self._budget_draining = False
        self._budget_drain_initial_active = 0
        self._budget_drain_last_active: int | None = None
        self._budget_drain_completed = False
        self.batched_observations: list[CandidateEvent] = []
        self.stop_for_review: str | None = None
        self.conflicted_candidates: set[str] = set()
        self.candidate_artifact_hashes: dict[str, dict[str, str]] = {}
        self.candidate_semantic_evidence_hashes: dict[str, dict[str, str]] = {}
        self.domain_evidence_receipts: dict[str, list[dict[str, Any]]] = {}
        self._schema_cache: dict[str, dict[str, Any]] = {}
        self._internal_failure = False
        self._dry_run = False
        self._effective_run_hours = DEFAULT_RUN_HOURS
        self._run_started_monotonic = time.monotonic()
        self._campaign_recorded_started = False
        self._seen_campaign_inputs: set[str] = set()
        self._stop_after_epoch = False
        self.satisfied_route_conditions: set[str] = set()
        observability = config.raw.get("observability", {})
        self.live_feed_enabled = bool(observability.get("live_agent_feed", True))
        self._live_flush_seconds = float(observability.get("flush_interval_seconds", 0.5))
        self._live_max_chunk = int(observability.get("max_text_chunk_chars", 2000))
        self._live_max_channel = int(observability.get("max_channel_chars_per_turn", 24000))
        self._live_capture_command_output = bool(observability.get("capture_command_output", True))
        self._live_max_tool_output = int(observability.get("max_command_output_chars_per_item", 4000))
        self._live_buffers: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._live_channel_chars: dict[tuple[str, str, str], int] = {}
        self._live_tool_chars: dict[str, int] = {}
        self._live_truncated: set[tuple[str, str, str]] = set()
        self._live_seen_delta_items: set[tuple[str, str]] = set()
        # Monotone per-item indexes let a watcher distinguish the beginning of
        # a public message from a tail fragment when it attaches mid-stream.
        self._live_chunk_indexes: dict[tuple[str, str, str, str], int] = {}

    def _validate_concurrency_limits(self, *, enforce_audit_ratio: bool = True) -> None:
        if min(
            self.max_director, self.max_research_workers,
            self.max_audit,
        ) < 1:
            raise ValueError("concurrency limits must be positive")
        if self.max_director != 1:
            raise ValueError(
                "max_director must be 1; the controller has one authoritative Director"
            )
        if self.max_mechanical_subworkers is not None and self.max_mechanical_subworkers < 0:
            raise ValueError("max_mechanical_subworkers must not be negative")
        if (
            self.mechanical_worker_enabled
            and self.max_mechanical_subworkers is not None
            and self.max_mechanical_subworkers < 1
        ):
            raise ValueError("enabled mechanical worker requires a positive independent cap")
        audit_ceiling = default_max_audit(self.max_research_workers)
        if enforce_audit_ratio and self.max_audit > audit_ceiling:
            raise ValueError(
                "max_audit must not exceed max_research_workers: "
                f"got max_audit={self.max_audit}, ceiling={audit_ceiling}; "
                "lower --max-audit together with --max-research-workers"
            )

    def _mechanical_route(self, raw: dict[str, Any]) -> dict[str, Any]:
        provider_name = str(raw["provider"])
        provider = self.config.raw["providers"][provider_name]
        return {
            "provider": provider_name,
            "model": str(raw["model"]),
            "reasoning_effort": mapped_reasoning_effort(provider, raw),
            "service_tier": None,
            "endpoint": raw.get("endpoint") or provider.get("endpoint"),
            "profile": raw.get("profile") or provider.get("profile"),
        }

    def _allowed_mechanical_routes(self) -> set[tuple[str | None, str | None, None]]:
        return {
            (
                str(self.mechanical_primary_route["model"]),
                str(self.mechanical_primary_route["reasoning_effort"]),
                None,
            ),
            (
                str(self.mechanical_fallback_route["model"]),
                str(self.mechanical_fallback_route["reasoning_effort"]),
                None,
            ),
            (None, None, None),
        }

    def _mechanical_resource_capacity(self) -> int:
        worker = self.config.raw["policy"]["one_shot_compute_worker"]
        factor = float(worker["backpressure"]["max_active_per_cpu"])
        resource_capacity = max(1, int((os.cpu_count() or 1) * factor))
        if self.max_mechanical_subworkers is None:
            return resource_capacity
        return min(resource_capacity, self.max_mechanical_subworkers)

    def _mechanical_policy_rejection(self, packet: dict[str, Any]) -> str | None:
        selection = self.config.raw["policy"]["one_shot_compute_worker"]["selection_policy"]
        mode = str(selection["mode"])
        if mode == "disabled":
            return "mechanical delegation is disabled by selection policy"
        if mode == "preferred":
            return None
        kind = str(packet.get("task_kind") or "")
        if mode == "balanced" and kind in {"code_modification", "mechanical_analysis"}:
            return f"balanced policy reserves {kind} for the parent role"
        conservative = {
            "finite_enumeration", "finite_exact_computation", "data_normalization",
            "deterministic_reproduction", "artifact_verification", "specified_formal_check",
        }
        if mode == "conservative" and kind not in conservative:
            return f"conservative policy does not admit task kind {kind}"
        if mode != "custom":
            return None
        thresholds = selection["custom_thresholds"]
        allowed = thresholds.get("allowed_task_kinds")
        if isinstance(allowed, list) and kind not in allowed:
            return f"custom policy does not admit task kind {kind}"
        scalar_limits = {
            "max_timeout_seconds": int(packet.get("timeout_seconds") or 0),
            "max_expected_artifacts": len(packet.get("expected_artifacts") or []),
            "max_input_files": len(packet.get("input_files") or []),
        }
        for key, observed in scalar_limits.items():
            if key in thresholds and observed > int(thresholds[key]):
                return f"custom policy threshold {key}={thresholds[key]} was exceeded"
        return None

    @staticmethod
    def _retry_class(failure_kind: str | None) -> str:
        kind = str(failure_kind or "")
        if kind in LOCAL_STRUCTURAL_FAILURES:
            return "local_structural"
        if kind in TRANSIENT_FAILURES:
            return "transport"
        if kind in MODEL_PROTOCOL_FAILURES:
            return "model_protocol"
        return "non_retryable"

    def _retry_limit(self, failure_kind: str | None, role: str) -> int:
        retry_class = self._retry_class(failure_kind)
        if retry_class in {"transport", "model_protocol"} and role in self.config.raw["models"]:
            return self.config.retry_limit(role, retry_class)
        engine = self.config.raw["engine"]
        if retry_class == "transport":
            if role == Role.DIRECTOR:
                return int(engine.get(
                    "director_max_retries",
                    engine.get("transient_protocol_max_retries", 1),
                ))
            return int(engine.get(
                "transient_protocol_max_retries", engine.get("max_retries", 1)
            ))
        if retry_class == "model_protocol":
            return int(engine.get("model_protocol_max_retries", 1))
        return 0

    def _retry_count(
        self,
        counters: dict[tuple[str, str], int],
        identity: str,
        failure_kind: str | None,
    ) -> int:
        return counters.get((identity, self._retry_class(failure_kind)), 0)

    def _set_retry_count(
        self,
        counters: dict[tuple[str, str], int],
        identity: str,
        failure_kind: str | None,
        value: int,
    ) -> None:
        counters[(identity, self._retry_class(failure_kind))] = int(value)

    @staticmethod
    def _clear_retry_counts(
        counters: dict[tuple[str, str], int], identity: str,
    ) -> None:
        for key in [key for key in counters if key[0] == identity]:
            counters.pop(key, None)

    @staticmethod
    def _failure_payload(outcome: JobOutcome) -> dict[str, Any]:
        return {
            "job_id": outcome.job_id,
            "task_id": outcome.task_id,
            "role": outcome.role,
            "claim_id": outcome.claim_id,
            "status": outcome.status,
            "error": outcome.failure_message,
            "failure_kind": outcome.failure_kind or "job_failure",
            "retryable": bool(outcome.retryable),
            "server_error": _bounded_value(outcome.server_error),
            "thread_id": outcome.thread_id,
            "turn_id": outcome.turn_id,
            "token_telemetry": outcome.token_telemetry,
        }

    def _request_director(
        self,
        reason: str,
        *,
        meaningful_change: bool,
        immediate: bool = False,
    ) -> None:
        """Coalesce replans behind a monotone state-version watermark."""
        if (
            self._finalization_started
            or self.scheduler_stop_reason
            or not self.lifecycle.can_dispatch
        ):
            return
        previous_requested = self._director_requested_version
        if meaningful_change:
            self._state_version += 1
        self._director_requested_version = max(
            self._director_requested_version, self._state_version
        )
        self.director_needed = True
        debounce = float(
            self.config.raw["engine"].get("director_debounce_seconds", 0.2)
        )
        if immediate or self._director_applied_version < 0:
            self._director_not_before = 0.0
        elif not self._director_not_before or self._director_not_before <= time.monotonic():
            # Leading-edge debounce: merge a burst into one snapshot without
            # allowing a continuous stream of changes to starve the Director.
            self._director_not_before = time.monotonic() + debounce
        self.store.append("DIRECTOR_REPLAN_REQUESTED", {
            "reason": reason,
            "meaningful_change": meaningful_change,
            "state_version": self._state_version,
            "requested_version": self._director_requested_version,
            "previous_requested_version": previous_requested,
            "director_active": self._director_active,
            "debounced": bool(self._director_not_before),
        })

    def _record_recent_change(self, change: dict[str, Any]) -> None:
        self.recent_changes.append(change)
        if len(self.recent_changes) > 50:
            del self.recent_changes[:-50]

    def _begin_internal_failure_drain(self, reason: str, *, source: str) -> None:
        """Stop new dispatch while preserving queues and healthy in-flight work."""
        self._internal_failure = True
        self.director_needed = False
        if self.lifecycle.phase not in {
            LifecyclePhase.DRAINING_FAILURE,
            LifecyclePhase.SEALED,
            LifecyclePhase.COMPLETED,
        }:
            self.lifecycle.transition(LifecyclePhase.DRAINING_FAILURE, reason=reason)
        self.scheduler_stop_reason = self.scheduler_stop_reason or reason
        self.store.append("INTERNAL_FAILURE_DRAIN_STARTED", {
            "reason": reason,
            "source": source,
            "in_flight_jobs": len(self.active),
            "in_flight_mechanical_subtasks": len(self.active_mechanical),
            "pending_research_preserved": len(self.pending_research),
            "pending_audits_preserved": len(self.pending_audits),
            "pending_mechanical_preserved": len(self.pending_mechanical),
            "action": "stop new dispatch and wait for healthy in-flight jobs",
            "lifecycle_phase": self.lifecycle.phase,
        })

    def _pause_for_provider_quota(
        self,
        outcome: JobOutcome,
        *,
        task: ResearchTask | None = None,
        checkpoint_job_record: dict[str, Any] | None = None,
    ) -> None:
        details = outcome.server_error if isinstance(outcome.server_error, dict) else {}
        reset_value = details.get("provider_reset_at")
        reset_at = (
            _sanitize_live_text(reset_value)[:200]
            if isinstance(reset_value, (str, int, float))
            and not isinstance(reset_value, bool)
            else None
        )
        provider = outcome.provider or (
            self.config.raw["models"].get(outcome.role, {}).get("provider")
        )
        reason = "campaign paused: provider quota exhausted"
        if reset_at not in {None, ""}:
            reason += f" until {reset_at}"
        if task is not None:
            if outcome.turn_history and outcome.result:
                outcome.logical_stop_reason = "provider quota exhausted"
                continued = self._checkpoint_research_continuation(
                    outcome, task, checkpoint_job_record,
                )
                requeued_task_id = continued.task_id
                requeue_mode = "noncanonical_checkpoint"
            else:
                if not any(item.task_id == task.task_id for item in self.pending_research):
                    self.pending_research.append(task)
                requeued_task_id = task.task_id
                requeue_mode = "exact_task"
            self.store.append("TASK_REQUEUED_AFTER_PROVIDER_QUOTA", {
                "job_id": outcome.job_id,
                "task_id": task.task_id,
                "requeued_task_id": requeued_task_id,
                "claim_id": task.target_claim,
                "mode": requeue_mode,
                **self._research_failure_payload(),
                "stagnation_effect": "none",
            })
        if self.lifecycle.phase is LifecyclePhase.RUNNING:
            self.lifecycle.transition(LifecyclePhase.DRAINING_EPOCH, reason=reason)
        if not self.scheduler_stop_reason:
            self.scheduler_stop_reason = reason
        self.store.append("PROVIDER_QUOTA_EXHAUSTED", {
            "job_id": outcome.job_id,
            "task_id": outcome.task_id,
            "role": outcome.role,
            "provider": provider,
            "provider_reset_at": reset_at,
            "action": (
                "pause campaign, preserve frontier, and wait for the provider reset; "
                + (
                    "do not count as mathematical failure or stagnation"
                    if self.domain_semantics.domain == "math-research"
                    else "do not count as research failure or stagnation"
                )
            ),
            "internal_failure": False,
        })

    def _pause_for_mechanical_provider_quota(
        self,
        *,
        mechanical_job_id: str,
        state: MechanicalRequestState,
        execution: MechanicalExecution,
    ) -> None:
        reset_at = (
            _sanitize_live_text(execution.provider_reset_at)[:200]
            if execution.provider_reset_at else None
        )
        reason = "campaign paused: mechanical provider quota exhausted"
        if reset_at:
            reason += f" until {reset_at}"
        if self.lifecycle.phase is LifecyclePhase.RUNNING:
            self.lifecycle.transition(LifecyclePhase.DRAINING_EPOCH, reason=reason)
        if not self.scheduler_stop_reason:
            self.scheduler_stop_reason = reason
        self.store.append("MECHANICAL_PROVIDER_QUOTA_EXHAUSTED", {
            "mechanical_job_id": mechanical_job_id,
            "parent_job_id": state.parent_job_id,
            "parent_task_id": state.parent_task_id,
            "parent_role": state.parent_role,
            "subtask_id": state.packet["task_id"],
            "provider": execution.provider,
            "provider_profile": execution.provider_profile,
            "model": execution.model,
            "reasoning_effort": execution.reasoning_effort,
            "provider_reset_at": reset_at,
                **self._research_failure_payload(),
            "stagnation_effect": "none",
            "action": (
                "do not retry or cache the route as unavailable; drain the epoch, "
                "retain unfinished parent research, and wait for provider reset"
            ),
            "internal_failure": False,
        })

    def _pause_for_provider_transport_loss(
        self,
        outcome: JobOutcome,
        *,
        task: ResearchTask | None = None,
        checkpoint_job_record: dict[str, Any] | None = None,
    ) -> None:
        """End the epoch without turning shared transport loss into research failure."""
        provider = outcome.provider or (
            self.config.raw["models"].get(outcome.role, {}).get("provider")
        )
        reason = "campaign paused: provider transport lost; start a fresh epoch"
        requeued_task_id: str | None = None
        requeue_mode: str | None = None
        if task is not None:
            if outcome.turn_history and outcome.result:
                outcome.logical_stop_reason = "provider transport lost"
                continued = self._checkpoint_research_continuation(
                    outcome, task, checkpoint_job_record,
                )
                requeued_task_id = continued.task_id
                requeue_mode = "noncanonical_checkpoint"
            else:
                if not any(
                    item.task_id == task.task_id
                    for item in [
                        *self.pending_research,
                        *self.deferred_research_continuations,
                    ]
                ):
                    self.pending_research.append(task)
                requeued_task_id = task.task_id
                requeue_mode = "exact_task"
            self.store.append("TASK_REQUEUED_AFTER_PROVIDER_TRANSPORT_LOSS", {
                "job_id": outcome.job_id,
                "task_id": task.task_id,
                "requeued_task_id": requeued_task_id,
                "claim_id": task.target_claim,
                "mode": requeue_mode,
                **self._research_failure_payload(),
                "stagnation_effect": "none",
            })
        if self.lifecycle.phase is LifecyclePhase.RUNNING:
            self.lifecycle.transition(LifecyclePhase.DRAINING_EPOCH, reason=reason)
        self.scheduler_stop_reason = self.scheduler_stop_reason or reason
        payload = {
            "job_id": outcome.job_id,
            "task_id": outcome.task_id,
            "role": outcome.role,
            "provider": provider,
            "server_error": _bounded_value(outcome.server_error),
            "action": (
                "stop dispatch, contain the shared provider, preserve the frontier, "
                "and restart transport in a fresh epoch"
            ),
            "internal_failure": False,
        }
        self._provider_transport_lost = payload
        self.store.append("PROVIDER_TRANSPORT_LOST", payload)

    def _queue_director_retry(
        self, failure_kind: str, *, retryable: bool, source: str,
    ) -> bool:
        retry_class = self._retry_class(failure_kind)
        max_retries = self._retry_limit(failure_kind, Role.DIRECTOR)
        retry = self.director_retry_counts.get(retry_class, 0)
        if retryable and retry < max_retries:
            retry += 1
            self.director_retry_counts[retry_class] = retry
            self.director_retry_count = sum(self.director_retry_counts.values())
            self._request_director(
                f"bounded Director retry after {failure_kind}",
                meaningful_change=False,
                immediate=True,
            )
            self.store.append("DIRECTOR_RETRY_QUEUED", {
                "retry": retry,
                "max_retries": max_retries,
                "retry_class": retry_class,
                "failure_kind": failure_kind,
                "source": source,
            })
            return True
        exhausted = bool(retryable and max_retries > 0)
        reason = (
            f"director failed after bounded retries: {failure_kind}"
            if exhausted else f"director failed: {failure_kind}"
        )
        if failure_kind in LOCAL_STRUCTURAL_FAILURES or failure_kind in {
            "controller_failure", "canonical_guard", "backend_internal",
        }:
            self._begin_internal_failure_drain(reason, source=source)
            return False
        self.director_needed = False
        self.director_constraints.append({
            "action": "DIVERSIFY",
            "claim_id": self.final_conjecture_claim_id or "FRONTIER",
            "forbidden_route": None,
            "reason": reason,
            "source": source,
        })
        self.store.append("DIRECTOR_FAILURE_ISOLATED", {
            "reason": reason,
            "failure_kind": failure_kind,
            "source": source,
            "pending_research_preserved": len(self.pending_research),
            "pending_audits_preserved": len(self.pending_audits),
            "in_flight_jobs_preserved": len(self.active),
            "action": "continue healthy work; checkpoint for a later epoch if no work remains",
        })
        healthy_work = bool(
            self.pending_research
            or self.pending_audits
            or any(item.kind in {"research", "audit"} for item in self.active.values())
        )
        if not healthy_work and self.lifecycle.phase is LifecyclePhase.RUNNING:
            pause_reason = f"campaign paused: {reason}"
            self.lifecycle.transition(
                LifecyclePhase.DRAINING_EPOCH, reason=pause_reason,
            )
            self.scheduler_stop_reason = pause_reason
        return False

    @property
    def active_graph_path(self) -> Path:
        return self.graph.path or self.layout.claim_graph_path

    def _final_claim(self) -> Any | None:
        if not self.final_conjecture_claim_id:
            return None
        return self.graph.claims.get(self.final_conjecture_claim_id)

    @property
    def _claim_status_field(self) -> str:
        return (
            "math_status"
            if self.domain_semantics.domain == "math-research"
            else "research_status"
        )

    def _claim_status_payload(self, claim: Any) -> dict[str, Any]:
        payload = {self._claim_status_field: claim.research_status}
        if self.domain_semantics.domain != "math-research":
            payload["domain"] = self.domain_semantics.domain
        return payload

    def _research_failure_payload(self, failed: bool = False) -> dict[str, bool]:
        field = (
            "mathematical_failure"
            if self.domain_semantics.domain == "math-research"
            else "research_failure"
        )
        return {field: failed}

    def _current_validation_authority_head(self) -> str:
        policy_manifest = build_policy_manifest(self.config)
        return build_validation_authority_head(
            audit_config=dict(self.config.raw["audit"]),
            policy_manifest_sha256=str(policy_manifest["manifest_sha256"]),
        )

    def _semantic_final_postcondition(self) -> dict[Path, str]:
        if not self.semantic_alignment.present:
            return {}
        self.semantic_alignment.assert_unchanged()
        claim = self._final_claim()
        if (
            claim is None
            or claim.research_status not in self.domain_semantics.terminal_positive
            or claim.trust_status not in {
                TrustStatus.AUDITED_NIGHTLY,
                TrustStatus.FORMALLY_VERIFIED,
                TrustStatus.CANONICAL_TRUSTED,
            }
        ):
            return {}
        self._require_semantic_trust_journal()
        return self.semantic_alignment.require_final_claim_acceptance(
            self.final_conjecture_claim_id,
            claim_graph=self.graph,
            trust_state=self.semantic_trust,
            validation_authority_head=self._current_validation_authority_head(),
        )

    def _guard_graph_terminal_transition(
        self,
        event: CandidateEvent,
        target_status: str,
        verified_evidence_level: str,
    ) -> None:
        del verified_evidence_level
        if (
            not self.semantic_alignment.present
            or target_status not in self.domain_semantics.terminal_positive
        ):
            return
        binding = self.semantic_alignment.claims.get(event.claim_id)
        if binding is None and event.claim_id != self.final_conjecture_claim_id:
            return
        self._require_semantic_trust_journal()
        if event.claim_id == self.final_conjecture_claim_id:
            self.semantic_alignment.require_final_claim_acceptance(
                event.claim_id,
                claim_graph=self.graph,
                trust_state=self.semantic_trust,
                validation_authority_head=self._current_validation_authority_head(),
                pending_statuses={event.claim_id: target_status},
                expected_candidates={event.claim_id: event.fingerprint},
            )
            return
        self.semantic_alignment.require_terminal_claim_acceptance(
            event.claim_id,
            claim_graph=self.graph,
            terminal_status=target_status,
            trust_state=self.semantic_trust,
            validation_authority_head=self._current_validation_authority_head(),
            expected_candidate_fingerprint=event.fingerprint,
        )

    def _begin_finalization_if_resolved(self, source: str) -> bool:
        claim = self._final_claim()
        if claim is None:
            return False
        outcome = self.domain_semantics.final_outcome(claim.research_status)
        if outcome is None:
            return False
        if claim.trust_status not in {
            TrustStatus.AUDITED_NIGHTLY,
            TrustStatus.FORMALLY_VERIFIED,
            TrustStatus.CANONICAL_TRUSTED,
        }:
            return False
        if self.semantic_alignment.present:
            try:
                self._semantic_final_postcondition()
            except (SemanticPromotionError, ValueError) as exc:
                self.store.append("SEMANTIC_FINALIZATION_BLOCKED", {
                    "claim_id": self.final_conjecture_claim_id,
                    "source": source,
                    "error": _sanitize_live_text(exc),
                    "semantic_head": self.semantic_alignment.source_sha256,
                    "contract_head": self.semantic_trust.contract_head,
                })
                self._begin_internal_failure_drain(
                    f"trusted final claim violates semantic postcondition: {exc}",
                    source="semantic_finalization",
                )
                return False
        self.final_claim_resolved = True
        self.final_claim_outcome = outcome
        if self.domain_semantics.domain == "math-research":
            self.final_conjecture_proved = outcome == "positive"
            self.final_conjecture_refuted = outcome == "negative"
        if self._internal_failure:
            event_kind = (
                "FINAL_CONJECTURE_RESOLVED_AFTER_INTERNAL_FAILURE"
                if self.domain_semantics.domain == "math-research"
                else "FINAL_CLAIM_RESOLVED_AFTER_INTERNAL_FAILURE"
            )
            self.store.append(event_kind, {
                "claim_id": self.final_conjecture_claim_id,
                **self._claim_status_payload(claim),
                "trust_status": claim.trust_status,
                "preserved_failure_reason": self.scheduler_stop_reason,
                "action": "preserve audited state but do not relabel the failed run as completed",
            })
            return False
        if self._finalization_started:
            return True
        self._finalization_started = True
        self.lifecycle.transition(LifecyclePhase.FINALIZING, reason=source)
        self.director_needed = False
        if self.domain_semantics.domain == "math-research":
            resolution = "proved" if self.final_conjecture_proved else "refuted"
            self.scheduler_stop_reason = (
                f"final conjecture {resolution} and independently audited: "
                f"{self.final_conjecture_claim_id}"
            )
        else:
            self.scheduler_stop_reason = (
                f"final claim reached audited {claim.research_status}: "
                f"{self.final_conjecture_claim_id}"
            )
        payload = {
            "claim_id": self.final_conjecture_claim_id,
            "statement": claim.statement,
            **self._claim_status_payload(claim),
            "trust_status": claim.trust_status,
            "evidence_level": claim.evidence_level,
            "source": source,
        }
        if self.domain_semantics.domain == "math-research":
            self.store.append(
                "FINAL_CONJECTURE_PROVED" if self.final_conjecture_proved
                else "FINAL_CONJECTURE_REFUTED",
                payload,
            )
        else:
            self.store.append("FINAL_CLAIM_RESOLVED", {
                **payload,
                "outcome_polarity": outcome,
            })
        self.store.append("FINALIZATION_STARTED", {
            **payload,
            "in_flight_jobs": len(self.active),
            "pending_research_preserved": len(self.pending_research),
            "pending_audits_preserved": len(self.pending_audits),
            "action": "stop new dispatch and wait for in-flight agents to finish naturally",
        })
        self.live_store.append("FINALIZATION_STARTED", {
            "claim_id": self.final_conjecture_claim_id,
            "in_flight_jobs": len(self.active),
            "message_zh": (
                "最终猜想的证明已通过独立审计；停止派发新任务，等待在途 Agent 自然完成。"
                if self.final_conjecture_proved else
                "最终猜想的反例已通过独立审计；停止派发新任务，等待在途 Agent 自然完成。"
                if self.domain_semantics.domain == "math-research" else
                "最终研究主张已达到该领域的审计终态；停止派发新任务，等待在途 Agent 自然完成。"
            ),
        })
        return True

    def _emit_scheduler_event_once(
        self, marker: str, kind: str, payload: dict[str, Any],
    ) -> bool:
        if marker in self._scheduler_event_keys:
            return False
        self._scheduler_event_keys.add(marker)
        self.store.append(kind, payload)
        return True

    async def _cancel_backend_job(
        self, job_id: str, reason: str, *, fatal_on_failure: bool = True,
    ) -> bool:
        try:
            return bool(await self.backend.cancel(job_id))
        except Exception as exc:
            error = _sanitize_live_text(exc)[:1000]
            payload = {"job_id": job_id, "reason": reason, "error": error}
            self.store.append("JOB_CANCEL_FAILED", payload)
            self.live_store.append("AGENT_JOB_CANCEL_FAILED", payload)
            if fatal_on_failure and not self.stop_for_review:
                self.stop_for_review = "worker cancellation failed; stopped for review"
            return False

    def _schedule_backend_cancel(self, job_id: str, reason: str) -> None:
        task = asyncio.create_task(self._cancel_backend_job(job_id, reason))
        self._scheduled_backend_cancellations.add(task)
        task.add_done_callback(self._scheduled_backend_cancellations.discard)

    async def _drain_scheduled_backend_cancellations(self) -> None:
        tasks = list(self._scheduled_backend_cancellations)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _reap_cancelled_job_future(active: ActiveJob) -> None:
        if not active.future.done():
            active.future.cancel()
        try:
            await active.future
        except (asyncio.CancelledError, Exception):
            pass

    async def _cancel_active_jobs_before_backend_close(self, reason: str) -> None:
        """Contain remote turns and reap their local owners before transport close."""
        await self._drain_scheduled_backend_cancellations()
        for job_id, active in list(self.active.items()):
            self._retain_active_research_for_next_epoch(job_id, active, reason)
            cancel_ok = await self._cancel_backend_job(
                job_id, reason, fatal_on_failure=False,
            )
            await self._reap_cancelled_job_future(active)
            self._record_job_cancelled(
                job_id, active, reason,
                remote_cancel_succeeded=cancel_ok,
            )
            self.governor.release(job_id)
        self.active.clear()

    def _retain_active_research_for_next_epoch(
        self, job_id: str, active: ActiveJob, reason: str,
    ) -> None:
        if active.kind != "research" or self._finalization_started:
            return
        task = active.task
        if any(
            item.task_id == task.task_id
            for item in [
                *self.pending_research,
                *self.deferred_research_continuations,
            ]
        ):
            return
        self.pending_research.append(task)
        self.store.append("TASK_RETAINED_FOR_NEXT_EPOCH", {
            "job_id": job_id,
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "reason": reason,
            "task": task.to_dict(),
                **self._research_failure_payload(),
            "stagnation_effect": "none",
        })

    def _record_job_cancelled(
        self,
        job_id: str,
        active: ActiveJob,
        reason: str,
        *,
        remote_cancel_succeeded: bool | None,
    ) -> None:
        if job_id in self.cancelled_jobs:
            return
        self.cancelled_jobs.add(job_id)
        usage = self.governor.by_job.get(job_id, {})
        record = {
            "job_id": job_id,
            "task_id": active.task.task_id,
            "role": active.task.role,
            "claim_id": active.task.target_claim,
            "status": "CANCELLED",
            "result": {},
            "thread_id": None,
            "turn_id": None,
            "model": active.model,
            "reasoning_effort": active.reasoning_effort,
            "provider": active.provider,
            "provider_profile": active.provider_profile,
            "requested_service_tier": active.requested_service_tier,
            "observed_service_tier": "unobservable",
            "token_usage": {
                key: int(usage.get(key, 0)) for key in TokenUsage().to_dict()
            },
            "token_telemetry": "observed" if job_id in self.governor.by_job else "unknown",
            "artifact_paths": [],
            "artifact_hashes": {},
            "artifact_validation_errors": [],
            "error": (
                "remote cancellation failed; backend will be closed"
                if remote_cancel_succeeded is False else None
            ),
            "failure_kind": "cancelled",
            "retryable": False,
            "useful": False,
            "cwd": active.workspace,
            "start_time": active.started_at,
            "workspace_metadata": active.workspace_metadata,
            "end_time": utc_now(),
            "elapsed_seconds": max(
                0.0, time.monotonic() - active.started_monotonic
            ),
            "exit_reason": reason,
            "remote_cancel_succeeded": remote_cancel_succeeded,
        }
        self.completed_jobs.append(record)
        self.store.append("JOB_CANCELLED", record)
        self.live_store.append("AGENT_JOB_CANCELLED", {
            "job_id": job_id,
            "role": active.task.role,
            "task_id": active.task.task_id,
            "claim_id": active.task.target_claim,
            "reason": reason,
        })

    @staticmethod
    def _normalized_manifest_limits(existing: dict[str, Any]) -> dict[str, Any]:
        limits = dict(existing["execution"]["limits"])
        version = int(existing.get("schema_version", 0))
        new_budget_defaults = {
            "mechanical_tokens": None,
            "global_cost_usd": None,
            "mechanical_cost_usd": None,
        }
        if version >= 11:
            return limits
        if version >= 9:
            return {
                **new_budget_defaults, **limits,
            }
        if version >= 7:
            return {
                **new_budget_defaults, **limits,
                "max_mechanical_subworkers": 0,
            }
        if version >= 5:
            # Manifest v5/v6 pinned a separate total cap. It is deliberately
            # ignored after the independent-role-cap migration.
            limits.pop("max_total_model_concurrency", None)
            return {
                **new_budget_defaults, **limits,
                "max_mechanical_subworkers": 0,
            }
        # Manifest v3/v4 used one research ceiling. Director already ran only
        # at a quiescent wave boundary, so that value is the compatible worker
        # ceiling.
        max_research = int(limits["max_research"])
        max_audit = int(limits["max_audit"])
        return {
            **new_budget_defaults,
            "global_tokens": limits.get("global_tokens"),
            "max_director": 1,
            "max_research_workers": max_research,
            "max_audit": max_audit,
            "max_mechanical_subworkers": 0,
            "duration_seconds": limits["duration_seconds"],
            "deadline_epoch": limits["deadline_epoch"],
        }

    @staticmethod
    def _verify_run_manifest(existing: dict[str, Any]) -> None:
        version = int(existing.get("schema_version", 0))
        top_level = {
            "schema_version", "run_id", "config", "research_policy", "requested_routes",
            "requested_service_tier", "observed_service_tier", "execution",
            "canonical_claim_graph", "output_schemas", "manifest_sha256",
        }
        if version >= 4:
            top_level.add("candidate_recovery")
        if version >= 6:
            top_level.add("research_target")
        if version >= 8:
            top_level.add("output_protocol")
        if version >= 10:
            top_level.add("campaign")
        if version >= 11:
            top_level.add("requested_providers")
        if version >= 12:
            top_level.add("canonical_state")
        if version >= 13:
            top_level.add("runtime_provenance")
        if set(existing) != top_level:
            raise ValueError(f"RUN_MANIFEST fields are invalid: {sorted(set(existing) ^ top_level)}")
        if version not in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}:
            raise ValueError("unsupported or legacy RUN_MANIFEST schema")
        fingerprinted = dict(existing)
        reported = str(fingerprinted.pop("manifest_sha256", ""))
        if not reported or stable_hash(fingerprinted) != reported:
            raise ValueError("RUN_MANIFEST fingerprint is invalid")
        nested_shapes = {
            "config": {"source", "snapshot", "sha256"},
            "research_policy": {"manifest", "manifest_sha256", "stable_core"},
            "canonical_claim_graph": {"path", "initial_sha256"},
            "execution": {"mode", "dry_run", "started_at", "started_epoch", "limits"},
        }
        for name, keys in nested_shapes.items():
            if not isinstance(existing.get(name), dict) or set(existing[name]) != keys:
                raise ValueError(f"RUN_MANIFEST {name} fields are invalid")
        if version >= 4 and (
            not isinstance(existing.get("candidate_recovery"), dict)
            or set(existing["candidate_recovery"]) != {"source_run_id"}
        ):
            raise ValueError("RUN_MANIFEST candidate_recovery fields are invalid")
        if version >= 6 and (
            not isinstance(existing.get("research_target"), dict)
            or set(existing["research_target"]) != {
                "project_name", "final_conjecture_claim_id",
            }
            or not str(existing["research_target"].get("project_name", "")).strip()
        ):
            raise ValueError("RUN_MANIFEST research_target fields are invalid")
        if version >= 8 and existing.get("output_protocol") != {
            "version": OUTPUT_PROTOCOL_VERSION,
            "strict_json_object": True,
        }:
            raise ValueError("RUN_MANIFEST output protocol is invalid")
        if version >= 10:
            campaign = existing.get("campaign")
            if not isinstance(campaign, dict) or set(campaign) != {
                "campaign_id", "epoch_id", "previous_epoch_id",
                "campaign_hours", "epoch_hours",
            }:
                raise ValueError("RUN_MANIFEST campaign fields are invalid")
            if (
                not str(campaign.get("campaign_id") or "")
                or not str(campaign.get("epoch_id") or "")
                or float(campaign.get("campaign_hours", 0)) <= 0
                or float(campaign.get("epoch_hours", 0)) <= 0
            ):
                raise ValueError("RUN_MANIFEST campaign values are invalid")
        if version >= 11 and not isinstance(existing.get("requested_providers"), dict):
            raise ValueError("RUN_MANIFEST requested_providers is invalid")
        if version >= 12 and (
            not isinstance(existing.get("canonical_state"), dict)
            or set(existing["canonical_state"]) != {
                "manifest", "manifest_sha256", "canonical_state_sha256",
                "planning_context_sha256", "git_revision",
            }
        ):
            raise ValueError("RUN_MANIFEST canonical_state fields are invalid")
        if version >= 13 and (
            not isinstance(existing.get("runtime_provenance"), dict)
            or set(existing["runtime_provenance"]) != {
                "amr_version", "python_version", "source_root",
                "source_git_revision", "source_tree_dirty", "source_sha256",
                "codex_cli_version", "app_server_schema_sha256",
                "app_server_required_protocol_sha256",
            }
            or not str(existing["runtime_provenance"].get("amr_version") or "")
            or not str(existing["runtime_provenance"].get("source_sha256") or "")
        ):
            raise ValueError("RUN_MANIFEST runtime_provenance fields are invalid")
        output_schemas = existing.get("output_schemas")
        if not isinstance(output_schemas, dict) or set(output_schemas) != {
            "director_plan.schema.json", "worker_result.schema.json", "audit_result.schema.json",
        }:
            raise ValueError("RUN_MANIFEST output_schemas are invalid")
        for name, entry in output_schemas.items():
            if not isinstance(entry, dict) or set(entry) != {"source", "snapshot", "sha256"}:
                raise ValueError(f"RUN_MANIFEST output schema entry is invalid: {name}")
        manifest_tier = existing.get("requested_service_tier")
        if manifest_tier not in {None, "fast"}:
            raise ValueError("RUN_MANIFEST requested an invalid service tier")
        execution = existing["execution"]
        if execution.get("mode") not in {"real", "mock"}:
            raise ValueError("RUN_MANIFEST has an invalid execution mode")
        limits = execution.get("limits")
        limit_keys = (
            {
                "global_tokens", "mechanical_tokens", "global_cost_usd",
                "mechanical_cost_usd", "max_director", "max_research_workers",
                "max_audit", "max_mechanical_subworkers", "duration_seconds",
                "deadline_epoch",
            }
            if version >= 11 else
            {
                "global_tokens", "max_director", "max_research_workers", "max_audit",
                "max_mechanical_subworkers", "duration_seconds", "deadline_epoch",
            }
            if version >= 9 else
            {
                "global_tokens", "max_director", "max_research_workers", "max_audit",
                "duration_seconds", "deadline_epoch",
            }
            if version >= 7 else
            {
                "global_tokens", "max_director", "max_research_workers", "max_audit",
                "max_total_model_concurrency", "duration_seconds", "deadline_epoch",
            }
            if version >= 5 else
            {"global_tokens", "max_research", "max_audit", "duration_seconds", "deadline_epoch"}
        )
        if not isinstance(limits, dict) or set(limits) != limit_keys:
            raise ValueError("RUN_MANIFEST is missing immutable execution limits")
        normalized_limits = AutonomousController._normalized_manifest_limits(existing)
        if min(
            int(normalized_limits["max_director"]),
            int(normalized_limits["max_research_workers"]),
            int(normalized_limits["max_audit"]),
        ) < 1:
            raise ValueError("RUN_MANIFEST concurrency limits must be positive")
        if int(normalized_limits["max_director"]) != 1:
            raise ValueError("RUN_MANIFEST max_director must be 1")
        mechanical_cap = normalized_limits.get("max_mechanical_subworkers", 0)
        if mechanical_cap is not None and int(mechanical_cap) < 0:
            raise ValueError("RUN_MANIFEST max_mechanical_subworkers must not be negative")
        audit_ceiling = default_max_audit(
            int(normalized_limits["max_research_workers"])
        )
        if int(normalized_limits["max_audit"]) > audit_ceiling:
            raise ValueError(
                "RUN_MANIFEST max_audit exceeds max_research_workers"
            )
        if float(limits["duration_seconds"]) <= 0:
            raise ValueError("RUN_MANIFEST duration must be positive")
        if limits["global_tokens"] is not None and int(limits["global_tokens"]) <= 0:
            raise ValueError("RUN_MANIFEST token budget must be positive")
        if normalized_limits.get("mechanical_tokens") is not None and int(
            normalized_limits["mechanical_tokens"]
        ) <= 0:
            raise ValueError("RUN_MANIFEST mechanical token budget must be positive")
        if existing["research_policy"].get("stable_core") != "persistent_filesystem_controller":
            raise ValueError("RUN_MANIFEST stable core is invalid")
        providers = existing.get("requested_providers", {})
        for role, route in existing.get("requested_routes", {}).items():
            if not isinstance(route, dict):
                raise ValueError("RUN_MANIFEST contains an invalid model route")
            provider = providers.get(route.get("provider"), {})
            capabilities = (
                provider.get("capabilities", {}) if isinstance(provider, dict) else {}
            )
            validate_service_tier(
                route.get("service_tier"), capabilities,
                f"RUN_MANIFEST requested_routes.{role}.service_tier",
            )
            route_tier = route.get("service_tier")
            if manifest_tier == "fast" and route_tier != "fast":
                raise ValueError(
                    "RUN_MANIFEST fast mode does not cover every main role"
                )
            if manifest_tier is None and isinstance(route_tier, str) and (
                route_tier.casefold() in {"fast", "priority", "auto"}
            ):
                raise ValueError(
                    "RUN_MANIFEST route requests fast without explicit fast mode"
                )

    def _require_semantics_as_canonical_input(self) -> None:
        if not self.semantic_trust.opted_in:
            return
        manifest = self.config.manifest
        if manifest is None or self.semantic_alignment.path is None:
            raise ValueError("semantic opt-in requires a project manifest and semantics file")
        missing = [
            role for role in ("director", "research", "audit")
            if self.semantic_alignment.path not in manifest.canonical_for(role)
        ]
        if missing:
            raise ValueError(
                "semantic alignment must be a canonical input for every role; missing="
                f"{missing}"
            )

    def _trusted_state_payload(self, *, transition_kind: str) -> dict[str, Any]:
        existing: dict[str, Any] = {}
        if self.layout.trusted_state_path.is_file():
            loaded = json.loads(
                self.layout.trusted_state_path.read_text(encoding="utf-8")
            )
            if isinstance(loaded, dict):
                existing = loaded
        audited = set(existing.get("audited_candidate_fingerprints") or [])
        audited.update(
            key for key, value in self.audit_gate.states.items()
            if value.trust_status == TrustStatus.AUDITED_NIGHTLY
            and key not in self.conflicted_candidates
        )
        payload = {
            "schema_version": 4,
            "updated_at": utc_now(),
            "source_run": self.run_id,
            "transition_kind": transition_kind,
            "claim_graph": (
                "project://" + self.layout.claim_graph_path.resolve()
                .relative_to(self.config.project_root.resolve()).as_posix()
            ),
            "policy_manifest_sha256": (
                self.policy_manifest["manifest_sha256"]
                if self.policy_manifest is not None
                else existing.get("policy_manifest_sha256")
            ),
            "claim_representations": dict(sorted(self.claim_representations.items())),
            "representation_contracts": {
                key: self.representation_contracts[key]
                for key in sorted(self.representation_contracts)
            },
            "audited_representation_bridges": [
                list(pair) for pair in sorted(self.audited_representation_bridges)
            ],
            "audited_candidate_fingerprints": sorted(audited),
            "claim_evidence_levels": {
                claim_id: claim.evidence_level
                for claim_id, claim in sorted(self.graph.claims.items())
                if claim.trust_status in {
                    TrustStatus.AUDITED_NIGHTLY,
                    TrustStatus.FORMALLY_VERIFIED,
                    TrustStatus.CANONICAL_TRUSTED,
                }
            },
        }
        if self.semantic_trust.opted_in:
            payload["semantic_alignment"] = self.semantic_trust.to_payload()
            payload["validation_authority_head"] = (
                self._current_validation_authority_head()
            )
        return payload

    def _accept_authorized_canonical_targets(
        self, paths: Iterable[Path],
    ) -> None:
        self.guard.accept(paths)
        guard_path = self.run_dir / "canonical_guard.before.json"
        if guard_path.is_file():
            atomic_write_json(guard_path, self.guard.baseline)

    def _canonical_transition_authorization(
        self, transition_kind: str, authorization: dict[str, Any],
    ) -> dict[str, Any]:
        result = {
            **authorization,
            "kind": transition_kind,
            "run_id": self.run_id,
            "semantic_head": self.semantic_alignment.source_sha256,
            "contract_head": self.semantic_trust.contract_head,
        }
        if self.semantic_alignment.present:
            result["validation_authority_head"] = (
                self._current_validation_authority_head()
            )
        return result

    def _accept_committed_semantic_snapshot(self) -> None:
        self._committed_claim_statuses = {
            claim_id: claim.research_status
            for claim_id, claim in self.graph.claims.items()
        }
        self._committed_semantic_trust = deepcopy(self.semantic_trust)

    def _commit_claim_state_transition(
        self,
        *,
        transition_kind: str,
        authorization: dict[str, Any],
        preconditions: dict[Path, str] | None = None,
    ) -> str | None:
        semantic_preconditions: dict[Path, str] = {}
        canonical_authorization = self._canonical_transition_authorization(
            transition_kind, authorization,
        )
        if self.semantic_alignment.present:
            self.semantic_alignment.assert_unchanged()
            self.semantic_alignment.require_positive_terminal_transition_authorization(
                previous_claim_statuses=self._committed_claim_statuses,
                claim_graph=self.graph,
                prior_trust_state=self._committed_semantic_trust,
                trust_state=self.semantic_trust,
                authorization=canonical_authorization,
                validation_authority_head=self._current_validation_authority_head(),
            )
            if self.semantic_alignment.path is None:
                raise ValueError("semantic opt-in lost its declaration path")
            semantic_preconditions[self.semantic_alignment.path] = str(
                self.semantic_alignment.source_sha256
            )
            semantic_preconditions.update(self._semantic_final_postcondition())
        for path, digest in (preconditions or {}).items():
            prior = semantic_preconditions.get(path)
            if prior is not None and prior != digest:
                raise ValueError(f"canonical preconditions disagree for {path}")
            semantic_preconditions[path] = digest
        if not self.persist_shared_state:
            self.graph.save()
            self._accept_committed_semantic_snapshot()
            return None
        self._assert_startup_canonical_sources()
        manifest = self.layout.manifest
        if manifest is None:
            raise ValueError("project manifest is unavailable for canonical transition")
        graph_payload = self.graph.to_payload(updated_at=utc_now())
        graph_payload_bytes = json_bytes(graph_payload)
        graph_digest = bytes_sha256(graph_payload_bytes)
        markdown_updates = updated_markdown_state_views(
            manifest,
            graph_payload=graph_payload,
            graph_digest=graph_digest,
        )
        targets = {
            self.layout.claim_graph_path: graph_payload_bytes,
            self.layout.trusted_state_path: json_bytes(
                self._trusted_state_payload(transition_kind=transition_kind)
            ),
            **{
                path: payload for path, payload in markdown_updates.items()
                if path.read_bytes() != payload
            },
        }
        transition_id = self.canonical_transitions.commit(
            targets=targets,
            authorization=canonical_authorization,
            trusted_state_path=self.layout.trusted_state_path,
            claim_graph_sha256=graph_digest,
            preconditions=semantic_preconditions,
        )
        self._accept_authorized_canonical_targets(targets)
        self._accept_committed_semantic_snapshot()
        self.store.append("CANONICAL_TRANSITION_COMMITTED", {
            "transition_id": transition_id,
            "transition_kind": transition_kind,
            "claim_graph_sha256": graph_digest,
            "ledger": (
                "project://" + self.canonical_transitions.ledger_path.resolve()
                .relative_to(self.config.project_root.resolve()).as_posix()
            ),
            "authorization": authorization,
        })
        return transition_id

    def _persist_semantic_trust_state(self, *, transition_kind: str) -> str | None:
        if not self._semantic_trust_dirty:
            return None
        if not self.persist_shared_state:
            self._semantic_trust_dirty = False
            self._accept_committed_semantic_snapshot()
            return None
        self.semantic_alignment.assert_unchanged()
        graph_bytes = self.layout.claim_graph_path.read_bytes()
        graph_digest = bytes_sha256(graph_bytes)
        preconditions = (
            {self.semantic_alignment.path: str(self.semantic_alignment.source_sha256)}
            if self.semantic_alignment.path is not None else {}
        )
        targets = {
            self.layout.claim_graph_path: graph_bytes,
            self.layout.trusted_state_path: json_bytes(
                self._trusted_state_payload(transition_kind=transition_kind)
            ),
        }
        transition_id = self.canonical_transitions.commit(
            targets=targets,
            authorization={
                "kind": transition_kind,
                "run_id": self.run_id,
                "semantic_head": self.semantic_alignment.source_sha256,
                "contract_head": self.semantic_trust.contract_head,
                "validation_authority_head": self._current_validation_authority_head(),
                "trust_upgrade": False,
            },
            trusted_state_path=self.layout.trusted_state_path,
            claim_graph_sha256=graph_digest,
            preconditions=preconditions,
        )
        self._accept_authorized_canonical_targets(targets)
        self._accept_committed_semantic_snapshot()
        self._semantic_trust_dirty = False
        self.store.append("SEMANTIC_TRUST_STATE_COMMITTED", {
            "transition_id": transition_id,
            "transition_kind": transition_kind,
            "semantic_head": self.semantic_alignment.source_sha256,
            "contract_head": self.semantic_trust.contract_head,
        })
        return transition_id

    def _require_semantic_trust_journal(self) -> None:
        if not self.persist_shared_state or not self.semantic_trust.opted_in:
            return
        authorizations = self.canonical_transitions.verified_committed_authorizations()
        self.semantic_trust.require_committed_journal(
            authorizations,
            pending_authorization=self._pending_semantic_authorization,
        )
        if self._pending_semantic_authorization is None:
            self.semantic_alignment.require_positive_terminal_transition_history(
                transactions=self.canonical_transitions.verified_committed_transactions(),
                claim_graph_path=self.layout.claim_graph_path,
                trusted_state_path=self.layout.trusted_state_path,
                semantics=self.domain_semantics,
            )

    def _reload_startup_claim_state(self) -> None:
        if self.persist_shared_state:
            recovered = self.canonical_transitions.recover()
            for transition_id in recovered:
                self._accept_authorized_canonical_targets(
                    self.canonical_transitions.target_paths(transition_id)
                )
        validate_canonical_research_state(self.config.manifest)
        graph = ClaimGraph.load(
            self.layout.claim_graph_path, semantics=self.domain_semantics,
        )
        graph.validate()
        if self.mock:
            graph.path = self.run_dir / "state" / "claim_graph.json"
            graph.save()
        self.graph = graph
        legacy = RepresentationContract.legacy()
        legacy_id = legacy.representation_id
        self.claim_representations = {
            claim_id: legacy_id for claim_id in self.graph.claims
        }
        self.representation_contracts = {legacy_id: legacy.to_dict()}
        self.audited_representation_bridges = set()
        trusted: dict[str, Any] = {}
        if self.layout.trusted_state_path.is_file():
            trusted = json.loads(
                self.layout.trusted_state_path.read_text(encoding="utf-8")
            )
            if not isinstance(trusted, dict):
                raise ValueError("canonical trusted state must be a JSON object")
            for representation_id, raw_contract in dict(
                trusted.get("representation_contracts") or {}
            ).items():
                contract = RepresentationContract.from_dict(raw_contract)
                if (
                    not isinstance(representation_id, str)
                    or contract.representation_id != representation_id
                ):
                    raise ValueError("trusted representation contract id is invalid")
                self.representation_contracts[representation_id] = contract.to_dict()
            for claim_id, representation_id in dict(
                trusted.get("claim_representations") or {}
            ).items():
                if claim_id in self.claim_representations and isinstance(
                    representation_id, str
                ):
                    self.claim_representations[claim_id] = representation_id
            for pair in trusted.get("audited_representation_bridges") or []:
                if (
                    isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(item, str) and item for item in pair)
                ):
                    self.audited_representation_bridges.add(tuple(sorted(pair)))
        loaded_semantic_trust = SemanticTrustState.from_trusted_payload(trusted)
        self.semantic_alignment = SemanticAlignment.load_optional(
            self.config.project_root,
            self.layout.autonomous_root / SEMANTICS_FILENAME,
            required=loaded_semantic_trust.opted_in,
        )
        self.semantic_trust = self.semantic_alignment.reconcile_trust_state(
            loaded_semantic_trust
        )
        self._semantic_trust_dirty = self.semantic_trust != loaded_semantic_trust
        self._require_semantics_as_canonical_input()
        self._persist_semantic_trust_state(
            transition_kind=(
                "SEMANTIC_OPT_IN"
                if not loaded_semantic_trust.opted_in
                else "SEMANTIC_CONTRACT_APPENDED"
            )
        )
        self._require_semantic_trust_journal()
        self._accept_committed_semantic_snapshot()
        self.graph.set_terminal_transition_guard(
            self._guard_graph_terminal_transition
        )
        if (
            self.final_conjecture_claim_id
            and self.final_conjecture_claim_id not in self.graph.claims
        ):
            raise ValueError(
                "configured final conjecture claim is absent from the refreshed "
                f"claim graph: {self.final_conjecture_claim_id}"
            )

    def _refresh_canonical_state(self) -> None:
        state_path = self.run_dir / "state" / "canonical_state.json"
        if self.resume:
            if not state_path.is_file():
                raise ValueError(
                    "cannot safely resume without startup canonical-state provenance"
                )
            state = load_canonical_state(state_path, epoch_id=self.epoch_id)
            validate_canonical_state(state, run_dir=self.run_dir)
            changed = verify_live_startup_sources(
                state, project_root=self.config.project_root, run_dir=self.run_dir,
                authorized_sha256=(
                    self.canonical_transitions.current_target_digests()
                    if self.persist_shared_state else None
                ),
            )
            if changed:
                raise ValueError(
                    "startup canonical inputs changed since the run was frozen: "
                    f"{changed}"
                )
            self._canonical_state = state
            self._director_overlay = director_overlay_text(
                state, run_dir=self.run_dir,
            )
            return

        current_manifest = self.layout.manifest
        if current_manifest is None or self.config.manifest is None:
            raise ValueError("project manifest is unavailable during canonical refresh")
        if current_manifest != self.config.manifest:
            raise ValueError(
                "project manifest changed after configuration load; restart amr run"
            )
        self._reload_startup_claim_state()
        previous_state: dict[str, Any] | None = None
        if self.previous_epoch_id is not None:
            previous_run = self.layout.run_dir(self.previous_epoch_id)
            previous_path = previous_run / "state" / "canonical_state.json"
            if not previous_path.is_file():
                raise ValueError(
                    "previous epoch lacks canonical-state provenance; stale planning "
                    "cannot be synchronized safely"
                )
            previous_state = load_canonical_state(
                previous_path, epoch_id=self.previous_epoch_id,
            )
            validate_canonical_state(previous_state, run_dir=previous_run)
        state = capture_canonical_state(
            manifest=current_manifest,
            run_dir=self.run_dir,
            epoch_id=self.epoch_id,
            workspace_root=self.config.workspace_root or self.config.project_root,
            previous_state=previous_state,
        )
        validate_canonical_state(state, run_dir=self.run_dir)
        self._canonical_state = state
        self._director_overlay = director_overlay_text(state, run_dir=self.run_dir)

    def _assert_startup_canonical_sources(self) -> None:
        if self._canonical_state is None:
            raise ValueError("startup canonical state has not been refreshed")
        if self.config.manifest is None:
            raise ValueError("project manifest is unavailable during state validation")
        authorized_sha256 = (
            self.canonical_transitions.current_target_digests()
            if self.persist_shared_state else {}
        )
        for key, path in (
            ("claim_graph", self.layout.claim_graph_path),
            ("trusted_state", self.layout.trusted_state_path),
        ):
            reference = portable_project_uri(self.config.project_root, path)
            expected = authorized_sha256.get(
                reference, str(self._canonical_state[key]["sha256"])
            )
            if file_digest(path) != expected:
                raise ValueError(
                    f"canonical {key} changed outside an audited transition"
                )
        validate_canonical_research_state(self.config.manifest)
        changed = verify_live_startup_sources(
            self._canonical_state,
            project_root=self.config.project_root,
            run_dir=self.run_dir,
            authorized_sha256=authorized_sha256,
        )
        if changed:
            raise ValueError(
                "canonical input changed after startup refresh: " + repr(changed)
            )

    def _pin_run_inputs(self, hours: float | None, dry_run: bool) -> float:
        if self._canonical_state is None:
            self._refresh_canonical_state()
        self.policy_manifest, self.policy_status = pin_policy_manifest(
            self.config, self.policy_manifest_path, resume=self.resume
        )
        if domain_contract_from_manifest(self.policy_manifest) != self.domain_contract:
            raise ValueError(
                "pinned policy domain contract changed after controller initialization"
            )
        if self.semantic_alignment.present:
            pinned_authority_head = build_validation_authority_head(
                audit_config=dict(self.config.raw["audit"]),
                policy_manifest_sha256=str(self.policy_manifest["manifest_sha256"]),
            )
            if pinned_authority_head != self._current_validation_authority_head():
                raise ValueError(
                    "validation authority changed relative to the pinned run policy"
                )
        configure_mechanical = getattr(
            self.mechanical_runner, "configure_pinned_policy", None
        )
        if callable(configure_mechanical):
            configure_mechanical(
                self.policy_manifest["one_shot_compute_worker"],
                self.policy_manifest_path,
            )
        broker_entry = self.policy_manifest["one_shot_compute_worker"]["broker_client"]
        broker_source = (
            self.policy_manifest_path.parent / str(broker_entry["snapshot_path"])
        ).resolve()
        if (
            not broker_source.is_relative_to(self.policy_manifest_path.parent.resolve())
            or not broker_source.is_file()
            or file_digest(broker_source) != str(broker_entry["sha256"])
        ):
            raise ValueError("pinned mechanical broker client snapshot is invalid")
        self._mechanical_broker_client_source = broker_source
        self._mechanical_broker_client_sha256 = str(broker_entry["sha256"])
        self._runtime_provenance = capture_runtime_provenance(
            include_codex=not self.mock and not dry_run,
            require_fast_service_tier=(
                not self.mock
                and not dry_run
                and self.config.fast_mode
                and any(
                    self.config.raw["providers"][str(route["provider"])]["adapter"]
                    == "codex_app_server"
                    for route in self.config.raw["models"].values()
                )
            ),
        )
        config_snapshot = self.run_dir / "config" / "config.yaml"
        config_snapshot.parent.mkdir(parents=True, exist_ok=True)
        if config_snapshot.exists():
            pinned_config = json.loads(config_snapshot.read_text(encoding="utf-8"))
            migrated_pinned_config, _ = migrate_config(pinned_config)
            if migrated_pinned_config != self.config.raw:
                raise ValueError("resume config does not match the pinned run config")
        else:
            if self.resume:
                raise ValueError("cannot resume a legacy run without a pinned config snapshot")
            atomic_write_json(config_snapshot, self.config.raw)
            pinned_config = self.config.raw
        schema_snapshot_root = self.run_dir / "config" / "output_schemas"
        schema_snapshot_root.mkdir(parents=True, exist_ok=True)
        output_schemas: dict[str, dict[str, str]] = {}
        for name in (
            "director_plan.schema.json", "worker_result.schema.json", "audit_result.schema.json",
        ):
            snapshot = schema_snapshot_root / name
            if not snapshot.exists():
                if self.resume:
                    raise ValueError(f"cannot resume without pinned output schema: {name}")
                with schema_resource(name) as source:
                    shutil.copy2(source, snapshot)
            output_schemas[name] = {
                "source": f"package://autonomous_math_research/resources/schemas/{name}",
                "snapshot": f"epoch://{self.epoch_id}/config/output_schemas/{name}",
                "sha256": file_digest(snapshot),
            }
        manifest_path = self.run_dir / "RUN_MANIFEST.json"
        now_epoch = time.time()
        requested_hours = DEFAULT_RUN_HOURS if hours is None else float(hours)
        if requested_hours <= 0:
            raise ValueError("hours must be positive")
        self._effective_run_hours = requested_hours
        payload = {
            "schema_version": 13,
            "run_id": self.run_id,
            "campaign": {
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "previous_epoch_id": self.previous_epoch_id,
                "campaign_hours": self.campaign_hours,
                "epoch_hours": self.epoch_hours,
            },
            "output_protocol": {
                "version": OUTPUT_PROTOCOL_VERSION,
                "strict_json_object": True,
            },
            "research_target": {
                "project_name": self.config.project_name,
                "final_conjecture_claim_id": self.final_conjecture_claim_id,
            },
            "config": {
                "source": (
                    "project://" + self.config.config_path.resolve()
                    .relative_to(self.config.project_root.resolve()).as_posix()
                ),
                "snapshot": f"epoch://{self.epoch_id}/config/config.yaml",
                "sha256": file_digest(config_snapshot),
            },
            "research_policy": {
                "manifest": f"epoch://{self.epoch_id}/policy/MANIFEST.json",
                "manifest_sha256": self.policy_manifest["manifest_sha256"],
                "stable_core": self.policy_manifest["stable_core"],
            },
            "requested_routes": self.config.raw["models"],
            "requested_providers": self.config.raw["providers"],
            "requested_service_tier": self.config.requested_service_tier,
            "observed_service_tier": None,
            "execution": {
                "mode": "mock" if self.mock else "real",
                "dry_run": bool(dry_run),
                "started_at": utc_now(),
                "started_epoch": now_epoch,
                "limits": {
                    "global_tokens": self.governor.global_budget,
                    "mechanical_tokens": self.mechanical_governor.global_budget,
                    "global_cost_usd": self.governor.global_cost_budget,
                    "mechanical_cost_usd": self.mechanical_governor.global_cost_budget,
                    "max_director": self.max_director,
                    "max_research_workers": self.max_research_workers,
                    "max_audit": self.max_audit,
                    "max_mechanical_subworkers": self.max_mechanical_subworkers,
                    "duration_seconds": requested_hours * 3600,
                    "deadline_epoch": now_epoch + requested_hours * 3600,
                },
            },
            "canonical_claim_graph": {
                "path": (
                    "project://" + self.layout.claim_graph_path.resolve()
                    .relative_to(self.config.project_root.resolve()).as_posix()
                ),
                "initial_sha256": file_digest(self.layout.claim_graph_path),
            },
            "canonical_state": {
                "manifest": f"epoch://{self.epoch_id}/state/canonical_state.json",
                "manifest_sha256": file_digest(
                    self.run_dir / "state" / "canonical_state.json"
                ),
                "canonical_state_sha256": self._canonical_state[
                    "canonical_state_sha256"
                ],
                "planning_context_sha256": self._canonical_state[
                    "planning_context_sha256"
                ],
                "git_revision": self._canonical_state["git_revision"],
            },
            "runtime_provenance": self._runtime_provenance,
            "candidate_recovery": {"source_run_id": self.recover_candidates_from},
            "output_schemas": output_schemas,
        }
        payload["manifest_sha256"] = stable_hash(payload)
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._verify_run_manifest(existing)
            self._run_manifest = existing
            legacy_config_snapshot = int(
                pinned_config.get("schema_version", 0)
            ) < CONFIG_SCHEMA_VERSION
            immutable_checks = {
                "run_id": self.run_id,
                "config_sha256": payload["config"]["sha256"],
                "policy_sha256": payload["research_policy"]["manifest_sha256"],
                "stable_core": payload["research_policy"]["stable_core"],
                "requested_routes": (
                    pinned_config.get("models")
                    if legacy_config_snapshot else payload["requested_routes"]
                ),
                "requested_providers": (
                    pinned_config.get("providers")
                    if legacy_config_snapshot else payload["requested_providers"]
                ),
                "requested_service_tier": (
                    None
                    if legacy_config_snapshot
                    else payload["requested_service_tier"]
                ),
                "canonical_claim_graph_path": payload["canonical_claim_graph"]["path"],
                "candidate_recovery": payload["candidate_recovery"],
                "research_target": payload["research_target"],
                "output_protocol": payload["output_protocol"],
                "campaign": payload["campaign"],
                "output_schema_hashes": {
                    name: entry["sha256"] for name, entry in payload["output_schemas"].items()
                },
                "canonical_state": payload["canonical_state"],
            }
            observed = {
                "run_id": existing.get("run_id"),
                "config_sha256": existing.get("config", {}).get("sha256"),
                "policy_sha256": existing.get("research_policy", {}).get("manifest_sha256"),
                "stable_core": existing.get("research_policy", {}).get("stable_core"),
                "requested_routes": existing.get("requested_routes"),
                "requested_providers": existing.get("requested_providers"),
                "requested_service_tier": existing.get("requested_service_tier"),
                "canonical_claim_graph_path": existing.get("canonical_claim_graph", {}).get("path"),
                "candidate_recovery": existing.get(
                    "candidate_recovery", {"source_run_id": None}
                ),
                "research_target": existing.get("research_target", {
                    "project_name": self.config.project_name,
                    "final_conjecture_claim_id": None,
                }),
                "output_protocol": existing.get("output_protocol", {
                    "version": OUTPUT_PROTOCOL_VERSION,
                    "strict_json_object": True,
                }),
                "campaign": existing.get("campaign", {
                    "campaign_id": self.run_id,
                    "epoch_id": self.run_id,
                    "previous_epoch_id": None,
                    "campaign_hours": self._effective_run_hours,
                    "epoch_hours": self._effective_run_hours,
                }),
                "output_schema_hashes": {
                    name: entry.get("sha256")
                    for name, entry in existing.get("output_schemas", {}).items()
                    if isinstance(entry, dict)
                },
                "canonical_state": existing.get("canonical_state"),
            }
            if int(existing.get("schema_version", 0)) < 10:
                immutable_checks.pop("campaign", None)
                observed.pop("campaign", None)
            if int(existing.get("schema_version", 0)) < 11:
                immutable_checks.pop("requested_providers", None)
                observed.pop("requested_providers", None)
            if int(existing.get("schema_version", 0)) < 12:
                immutable_checks.pop("canonical_state", None)
                observed.pop("canonical_state", None)
            if observed != immutable_checks:
                raise ValueError("RUN_MANIFEST immutable inputs do not match the resumed controller")
            if bool(existing["execution"].get("dry_run")) or dry_run:
                raise ValueError("dry-run records cannot be resumed and --resume --dry-run is forbidden")
            expected_mode = "mock" if self.mock else "real"
            if existing["execution"].get("mode") != expected_mode:
                raise ValueError("resume execution mode differs from the pinned run mode")
            version = int(existing.get("schema_version", 0))
            if version >= 13:
                pinned_runtime = existing["runtime_provenance"]
                for key in ("amr_version", "source_sha256"):
                    if pinned_runtime.get(key) != self._runtime_provenance.get(key):
                        raise ValueError(
                            f"resume AMR runtime provenance differs: {key}"
                        )
                pinned_protocol = pinned_runtime.get(
                    "app_server_required_protocol_sha256"
                )
                current_protocol = self._runtime_provenance.get(
                    "app_server_required_protocol_sha256"
                )
                if pinned_protocol != current_protocol:
                    raise ValueError("resume App Server protocol is incompatible")
                changed_runtime = {
                    key: {
                        "pinned": pinned_runtime.get(key),
                        "current": self._runtime_provenance.get(key),
                    }
                    for key in (
                        "python_version", "source_root", "source_git_revision",
                        "source_tree_dirty", "codex_cli_version",
                        "app_server_schema_sha256",
                    )
                    if pinned_runtime.get(key) != self._runtime_provenance.get(key)
                }
                if changed_runtime:
                    self.store.append(
                        "RUNTIME_PROVENANCE_CHANGED_COMPATIBLE", changed_runtime,
                    )
            else:
                self.store.append("RUNTIME_PROVENANCE_UNPINNED_LEGACY", {
                    "manifest_schema_version": version,
                    "current": self._runtime_provenance,
                    "action": "resume under legacy compatibility rules",
                })
            original = self._normalized_manifest_limits(existing)
            legacy_total = existing["execution"]["limits"].get(
                "max_total_model_concurrency"
            )
            if legacy_total is not None:
                self.store.append("LEGACY_TOTAL_CAP_REMOVED_ON_RESUME", {
                    "legacy_max_total_model_concurrency": legacy_total,
                    "action": "ignored; independent role caps now control dispatch",
                })
            self._effective_run_hours = float(original["duration_seconds"]) / 3600
            original_budget = original.get("global_tokens")
            if self._budget_override is not None and (
                original_budget is None or int(self._budget_override) > int(original_budget)
            ):
                raise ValueError("resume cannot increase the pinned global token budget")
            self.governor.global_budget = (
                int(self._budget_override) if self._budget_override is not None
                else (int(original_budget) if original_budget is not None else None)
            )
            original_mechanical_budget = original.get("mechanical_tokens")
            self.mechanical_governor.global_budget = (
                int(original_mechanical_budget)
                if original_mechanical_budget is not None else None
            )
            self.governor.global_cost_budget = (
                float(original["global_cost_usd"])
                if original.get("global_cost_usd") is not None else None
            )
            self.mechanical_governor.global_cost_budget = (
                float(original["mechanical_cost_usd"])
                if original.get("mechanical_cost_usd") is not None else None
            )
            for name, override, original_value in (
                ("max_director", self._max_director_override, original["max_director"]),
                (
                    "max_research_workers", self._max_research_workers_override,
                    original["max_research_workers"],
                ),
                ("max_audit", self._max_audit_override, original["max_audit"]),
            ):
                if override is not None and int(override) > int(original_value):
                    raise ValueError(f"resume cannot increase pinned {name}")
            original_mechanical_cap = original.get("max_mechanical_subworkers")
            override_mechanical_cap = self._max_mechanical_subworkers_override
            normalized_override_cap = (
                None
                if override_mechanical_cap is not None
                and str(override_mechanical_cap).casefold() == "unbounded"
                else (
                    int(override_mechanical_cap)
                    if override_mechanical_cap is not None else original_mechanical_cap
                )
            )
            if original_mechanical_cap is not None and (
                normalized_override_cap is None
                or int(normalized_override_cap) > int(original_mechanical_cap)
            ):
                raise ValueError("resume cannot increase pinned max_mechanical_subworkers")
            self.max_director = int(
                self._max_director_override
                if self._max_director_override is not None else original["max_director"]
            )
            self.max_research_workers = int(
                self._max_research_workers_override
                if self._max_research_workers_override is not None
                else original["max_research_workers"]
            )
            self.max_research = self.max_research_workers
            self.max_audit = int(
                self._max_audit_override
                if self._max_audit_override is not None else original["max_audit"]
            )
            self.max_mechanical_subworkers = (
                int(normalized_override_cap)
                if normalized_override_cap is not None else None
            )
            self._validate_concurrency_limits()
            self.governor.configured_max_research = self.max_research_workers
            deadline = float(original["deadline_epoch"])
            if hours is not None:
                deadline = min(deadline, now_epoch + requested_hours * 3600)
            return deadline
        else:
            if self.resume:
                raise ValueError("cannot resume a legacy run without RUN_MANIFEST.json")
            atomic_write_json(manifest_path, payload)
            self._run_manifest = payload
        return float(payload["execution"]["limits"]["deadline_epoch"])

    def _policy_view(self, role: str) -> dict[str, Any]:
        if self.policy_manifest is None:
            raise RuntimeError("research policy has not been pinned")
        return policy_view_for_role(self.policy_manifest, self.policy_manifest_path, role)

    def _live_identity(self, thread_id: str) -> dict[str, Any]:
        for job_id, ids in getattr(self.backend, "active", {}).items():
            if str(ids[0]) != thread_id:
                continue
            active = self.active.get(job_id)
            if active is None:
                break
            return {
                "job_id": job_id, "role": active.task.role,
                "task_id": active.task.task_id, "claim_id": active.task.target_claim,
                "model": active.model, "reasoning_effort": active.reasoning_effort,
            }
        return {
            "job_id": None, "role": None, "task_id": None, "claim_id": None,
            "model": None, "reasoning_effort": None,
        }

    def _live_base(self, params: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(params.get("threadId") or "")
        return {
            "thread_id": thread_id or None,
            "turn_id": params.get("turnId") or (params.get("turn") or {}).get("id"),
            "item_id": params.get("itemId") or (params.get("item") or {}).get("id"),
            **self._live_identity(thread_id),
        }

    def _append_live_event(
        self, kind: str, params: dict[str, Any], payload: dict[str, Any] | None = None,
    ) -> None:
        if not self.live_feed_enabled:
            return
        self.live_store.append(kind, {**self._live_base(params), **(payload or {})})

    def _emit_live_truncation(
        self, params: dict[str, Any], channel: str, limit: int,
    ) -> None:
        turn_key = str(params.get("turnId") or params.get("threadId") or "")
        marker = (turn_key, channel, str(params.get("itemId") or ""))
        if marker in self._live_truncated:
            return
        self._live_truncated.add(marker)
        self._append_live_event("AGENT_OUTPUT_TRUNCATED", params, {
            "channel": channel, "limit_chars": limit,
        })

    def _append_live_text(self, channel: str, params: dict[str, Any], raw: Any) -> None:
        if not self.live_feed_enabled:
            return
        if channel == "command_output" and not self._live_capture_command_output:
            return
        text = _sanitize_live_text(raw)
        if not text:
            return
        thread_id = str(params.get("threadId") or "")
        turn_id = str(params.get("turnId") or "")
        item_id = str(params.get("itemId") or "")
        if channel == "command_output":
            current = self._live_tool_chars.get(item_id, 0)
            limit = self._live_max_tool_output
            accepted = text[: max(0, limit - current)]
            self._live_tool_chars[item_id] = current + len(accepted)
            if len(accepted) < len(text):
                self._emit_live_truncation(params, channel, limit)
        else:
            channel_key = (turn_id or thread_id, channel, "")
            current = self._live_channel_chars.get(channel_key, 0)
            limit = self._live_max_channel
            accepted = text[: max(0, limit - current)]
            self._live_channel_chars[channel_key] = current + len(accepted)
            if len(accepted) < len(text):
                self._emit_live_truncation(params, channel, limit)
        if not accepted:
            return
        key = (thread_id, turn_id, channel, item_id)
        entry = self._live_buffers.setdefault(key, {
            "params": dict(params), "text": "", "opened_at": time.monotonic(),
        })
        entry["params"].update({
            "threadId": thread_id, "turnId": turn_id, "itemId": item_id,
        })
        entry["text"] += accepted
        if len(entry["text"]) >= self._live_max_chunk:
            self._flush_live_key(key)

    def _flush_live_key(self, key: tuple[str, str, str, str]) -> None:
        entry = self._live_buffers.pop(key, None)
        if not entry:
            return
        text = str(entry.get("text") or "")
        params = entry.get("params") or {}
        channel = key[2]
        for offset in range(0, len(text), self._live_max_chunk):
            chunk_index = self._live_chunk_indexes.get(key, 0)
            self._append_live_event("AGENT_TEXT_CHUNK", params, {
                "channel": channel,
                "text": text[offset:offset + self._live_max_chunk],
                "chunk_index": chunk_index,
            })
            self._live_chunk_indexes[key] = chunk_index + 1

    def _flush_due_live_chunks(
        self, *, force: bool = False, thread_id: str | None = None,
    ) -> None:
        now = time.monotonic()
        for key, entry in list(self._live_buffers.items()):
            if thread_id is not None and key[0] != thread_id:
                continue
            if force or now - float(entry.get("opened_at", now)) >= self._live_flush_seconds:
                self._flush_live_key(key)

    def _capture_live_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self.live_feed_enabled:
            return
        delta_channels = {
            "item/agentMessage/delta": "agent_message",
            "item/reasoning/summaryTextDelta": "reasoning_summary",
            "item/plan/delta": "plan",
            "item/commandExecution/outputDelta": "command_output",
        }
        if method == "item/reasoning/textDelta":
            # Hidden/raw reasoning is deliberately never persisted or displayed.
            return
        if method in delta_channels:
            channel = delta_channels[method]
            item_id = str(params.get("itemId") or "")
            if item_id:
                self._live_seen_delta_items.add((channel, item_id))
            self._append_live_text(channel, params, params.get("delta"))
            return
        if method == "item/reasoning/summaryPartAdded":
            self._flush_due_live_chunks(force=True, thread_id=str(params.get("threadId") or ""))
            self._append_live_event("AGENT_REASONING_SECTION", params, {
                "summary_index": params.get("summaryIndex"),
            })
            return
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            item_type = str(item.get("type") or "unknown")
            item_id = str(item.get("id") or params.get("itemId") or "")
            completed = method == "item/completed"
            fallback = {
                "agentMessage": ("agent_message", item.get("text")),
                "plan": ("plan", item.get("text")),
                "reasoning": ("reasoning_summary", _reasoning_summary_text(item.get("summary"))),
            }
            if item_type in fallback:
                channel, text = fallback[item_type]
                completed_params = {**params, "itemId": item_id}
                if completed and (channel, item_id) not in self._live_seen_delta_items:
                    self._append_live_text(channel, completed_params, text)
                if completed:
                    self._flush_due_live_chunks(
                        force=True, thread_id=str(params.get("threadId") or "")
                    )
                    self._append_live_event("AGENT_TEXT_COMPLETED", completed_params, {
                        "channel": channel,
                    })
                return
            if completed and item_type == "commandExecution":
                self._flush_live_key((
                    str(params.get("threadId") or ""), str(params.get("turnId") or ""),
                    "command_output", item_id,
                ))
            self._append_live_event(
                "AGENT_ITEM_COMPLETED" if completed else "AGENT_ITEM_STARTED",
                {**params, "itemId": item_id},
                _live_item_summary(item),
            )
            return
        if method == "turn/plan/updated":
            plan = [
                {
                    "step": _sanitize_live_text(item.get("step"))[:500],
                    "status": item.get("status"),
                }
                for item in (params.get("plan") or [])[:20]
                if isinstance(item, dict)
            ]
            self._append_live_event("AGENT_PLAN_UPDATED", params, {
                "explanation": _sanitize_live_text(params.get("explanation"))[:500],
                "plan": plan,
            })
            return
        if method == "turn/completed":
            self._flush_due_live_chunks(
                force=True, thread_id=str(params.get("threadId") or "")
            )

    async def _trace_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if method in {
            "amr/unmanagedContinuation",
            "amr/unmanagedContinuationInterruptFailed",
        }:
            reason = "App Server started a continuation outside controller ownership"
            self._internal_failure = True
            self.stop_for_review = reason
            self.store.append("UNMANAGED_CONTINUATION_DETECTED", {
                "thread_id": params.get("threadId"),
                "turn_id": params.get("turnId"),
                "action": "interrupt_and_fail_closed",
                "interrupt_error_type": params.get("errorType"),
            })
        if method == "amr/unauthorizedDelegation":
            thread_id = str(params.get("threadId") or "")
            identity = self._live_identity(thread_id)
            job_id = str(identity.get("job_id") or "")
            reason = (
                "top-level role attempted direct or recursive delegation outside the "
                "controller mechanical broker"
            )
            self._internal_failure = True
            self.stop_for_review = reason
            self.store.append("UNAUTHORIZED_DELEGATION_ATTEMPT", {
                "job_id": job_id or None,
                "role": identity.get("role"),
                "thread_id": thread_id or None,
                "turn_id": params.get("turnId"),
                "item_id": params.get("itemId"),
                "item_type": params.get("itemType"),
                "tool": params.get("tool"),
                "agent_thread_id": params.get("agentThreadId"),
                "receiver_thread_ids": params.get("receiverThreadIds"),
                "action": "interrupt parent turn and fail closed",
            })
            if job_id:
                self._schedule_backend_cancel(job_id, reason)
        if method == "amr/unauthorizedDelegationInterruptFailed":
            reason = "App Server could not interrupt an unauthorized delegation turn"
            self._internal_failure = True
            self.stop_for_review = reason
            self.store.append("UNAUTHORIZED_DELEGATION_CONTAINMENT_FAILED", {
                "thread_id": params.get("threadId"),
                "turn_id": params.get("turnId"),
                "interrupt_error_type": params.get("errorType"),
                "action": "fail closed",
            })
        if method == "model/rerouted":
            thread_id = str(params.get("threadId") or "")
            identity = self._live_identity(thread_id)
            job_id = str(identity.get("job_id") or "")
            if job_id and job_id in self.active:
                reason = (
                    "App Server reported model/rerouted despite exact pinned model "
                    "and allowProviderModelFallback=false"
                )
                self._internal_failure = True
                self.stop_for_review = reason
                self.store.append("MODEL_ROUTE_POLICY_VIOLATION", {
                    "job_id": job_id,
                    "role": identity.get("role"),
                    "thread_id": thread_id,
                    "turn_id": params.get("turnId"),
                    "requested_model": self.active[job_id].model,
                    "route_event": _bounded_notification_params(method, params),
                    "action": "interrupt affected turn and fail closed",
                })
                self._schedule_backend_cancel(job_id, reason)
        if method == "item/started":
            item = params.get("item") or {}
            item_type = str(item.get("type") or "")
            command = _sanitize_live_text(item.get("command"))
            forbidden_command = _is_unauthorized_top_level_delegation(command)
            if item_type in {
                "collabToolCall", "collabAgentToolCall", "subAgentActivity",
            } or forbidden_command:
                thread_id = str(params.get("threadId") or "")
                identity = self._live_identity(thread_id)
                job_id = str(identity.get("job_id") or "")
                reason = (
                    "top-level role attempted direct or recursive delegation outside the "
                    "controller mechanical broker"
                )
                self._internal_failure = True
                self.stop_for_review = reason
                self.store.append("UNAUTHORIZED_DELEGATION_ATTEMPT", {
                    "job_id": job_id or None,
                    "role": identity.get("role"),
                    "thread_id": thread_id or None,
                    "turn_id": params.get("turnId"),
                    "item_type": item_type,
                    "command": command[:500] if command else None,
                    "action": "interrupt parent turn and fail closed",
                })
                if job_id:
                    self._schedule_backend_cancel(job_id, reason)
        self._capture_live_notification(method, params)
        if method == "thread/tokenUsage/updated":
            turn_id = str(params.get("turnId", ""))
            thread_id = str(params.get("threadId", ""))
            for job_id, ids in getattr(self.backend, "active", {}).items():
                if (
                    (str(ids[1]) == turn_id or (thread_id and str(ids[0]) == thread_id))
                    and job_id in self.active
                ):
                    usage = TokenUsage.from_app_server((params.get("tokenUsage") or {}).get("total") or {})
                    self.governor.record(job_id, self.active[job_id].task.role, usage, None)
                    break
        if method in {
            "thread/started", "turn/started", "turn/completed", "thread/tokenUsage/updated",
            "account/rateLimits/updated", "thread/goal/updated", "model/rerouted", "warning",
            "amr/unmanagedContinuation", "amr/unmanagedContinuationInterruptFailed",
            "amr/unauthorizedDelegation",
            "amr/unauthorizedDelegationInterruptFailed",
        }:
            self.store.append("APP_SERVER_NOTIFICATION", {
                "method": method,
                "params": _bounded_notification_params(method, message.get("params") or {}),
            })

    def _schema(self, name: str) -> dict[str, Any]:
        if name in self._schema_cache:
            return self._schema_cache[name]
        with schema_resource(name) as path:
            schema = load_schema(path)
            validate_output_schema_compatibility(schema, schema_path=path)
        self._schema_cache[name] = schema
        return schema

    def _preflight_output_schemas(self) -> None:
        schema_root = self.run_dir / "config" / "output_schemas"
        paths = [
            schema_root / "director_plan.schema.json",
            schema_root / "worker_result.schema.json",
            schema_root / "audit_result.schema.json",
        ]
        self._schema_cache = preflight_output_schema_files(paths)
        if self.policy_manifest is not None:
            worker = self.policy_manifest["one_shot_compute_worker"]
            policy_root = self.policy_manifest_path.parent
            mechanical_paths = [
                policy_root / worker["task_schema"]["snapshot_path"],
                policy_root / worker["result_schema"]["snapshot_path"],
            ]
            self._schema_cache.update(preflight_output_schema_files(mechanical_paths))

    def _audit_output_schema(self, event: CandidateEvent) -> dict[str, Any]:
        """Return v2 verdict-only schema; identity is controller-injected."""
        schema = deepcopy(self._schema("audit_result.schema.json"))
        validate_output_schema_compatibility(
            schema, schema_path=(
                "audit_result.schema.json (controller-bound identity "
                f"{event.fingerprint[:12]})"
            ),
        )
        return schema

    def _load_recoverable_candidates(self, source_run_id: str) -> list[CandidateEvent]:
        source_dir = self.layout.run_dir(source_run_id)
        source_events = source_dir / "EVENTS.jsonl"
        if not source_events.is_file():
            raise ValueError(f"candidate recovery source run does not exist: {source_run_id}")
        records = EventStore(source_events, source_run_id).replay()
        if not any(item.get("kind") == "RUN_STOPPED" for item in records):
            raise ValueError("candidate recovery source run is not terminal")
        already_processed = {
            str(item.get("payload", {}).get("fingerprint") or "")
            for item in records if item.get("kind") == "CANDIDATE_PROCESSED"
        }
        processed_event_ids = {
            str(item.get("payload", {}).get("fingerprint") or ""):
            str(item.get("payload", {}).get("event_id") or "")
            for item in records
            if item.get("kind") == "CANDIDATE_PROCESSED"
            and item.get("payload", {}).get("fingerprint")
        }
        terminal_fingerprints = {
            str(item.get("payload", {}).get("fingerprint") or "")
            for item in records
            if item.get("kind") == "CANDIDATE_REJECTED"
        }
        terminal_fingerprints.update({
            str(item.get("payload", {}).get("candidate_fingerprint") or "")
            for item in records
            if item.get("kind") == "AUDIT_RECORDED"
            and str(item.get("payload", {}).get("trust_status") or "") in {
                TrustStatus.REJECTED, TrustStatus.AUDITED_NIGHTLY,
            }
        })
        recoverable_reasons = (
            "candidate statement does not match existing claim",
            "candidate claim_id does not match the producer assignment",
        )
        recovered: list[CandidateEvent] = []
        seen: set[str] = set()
        for item in records:
            if item.get("kind") != "CANDIDATE_REJECTED":
                continue
            entry = item.get("payload") or {}
            reason = str(entry.get("reason") or "")
            old_fingerprint = str(entry.get("fingerprint") or "")
            event_id = str(entry.get("event_id") or "")
            if (
                not old_fingerprint or old_fingerprint in already_processed
                or not event_id or not reason.startswith(recoverable_reasons)
            ):
                continue
            archive_root = self.layout.autonomous_root / "events" / "processed"
            expected = archive_root / f"{event_id}.{old_fingerprint[:12]}.json"
            candidates = [expected] if expected.is_file() else sorted(
                archive_root.glob(f"{event_id}.*.json")
            )
            if len(candidates) != 1:
                raise ValueError(
                    f"cannot locate one archived candidate event for {event_id} "
                    f"from {source_run_id}"
                )
            raw = json.loads(candidates[0].read_text(encoding="utf-8"))
            old_event = CandidateEvent.from_dict(raw)
            if not _candidate_fingerprint_matches_persisted(
                raw, old_event, old_fingerprint,
            ):
                raise ValueError(f"archived candidate fingerprint mismatch for {event_id}")
            if old_event.claim_id not in self.graph.claims:
                raise ValueError(
                    f"candidate recovery parent claim is unknown: {old_event.claim_id}"
                )
            payload = old_event.to_dict()
            payload.pop("fingerprint", None)
            payload.update({
                "event_id": "recovered-" + stable_hash({
                    "source_run_id": source_run_id,
                    "event_id": old_event.event_id,
                    "fingerprint": old_fingerprint,
                })[:20],
                "producer_thread_id": None,
                "producer_task_id": f"recovery-{source_run_id}-{old_event.producer_task_id}",
                "claim_id": "AUTO_DERIVED",
                "parent_claim_id": old_event.claim_id,
                "source_run_id": source_run_id,
            })
            event = CandidateEvent.from_dict(payload)
            for artifact in event.artifact_paths:
                raw_artifact = Path(artifact)
                resolved = (
                    raw_artifact.resolve() if raw_artifact.is_absolute()
                    else (self.config.project_root / raw_artifact).resolve()
                )
                if (
                    not resolved.is_relative_to(self.config.project_root.resolve())
                    or not resolved.is_file()
                ):
                    raise ValueError(
                        f"recovered candidate artifact is unavailable or outside project: {artifact}"
                    )
            event.artifact_paths = [
                str(
                    Path(artifact).resolve() if Path(artifact).is_absolute()
                    else (self.config.project_root / artifact).resolve()
                )
                for artifact in event.artifact_paths
            ]
            if not self.mock and event.fingerprint in self.inbox.accepted:
                continue
            if event.fingerprint not in seen:
                recovered.append(event)
                seen.add(event.fingerprint)
        # A terminal failed/limited run can also contain candidates that were
        # accepted but never reached a terminal audit state. Recover those
        # exact candidates under the same claim identity, discard all prior
        # audit verdicts, and require a fresh audit in the new run.
        unfinished_fingerprints = sorted(
            fingerprint for fingerprint in already_processed
            if fingerprint and fingerprint not in terminal_fingerprints
        )
        for old_fingerprint in unfinished_fingerprints:
            candidate_path = (
                self.layout.autonomous_root / "candidates" / f"{old_fingerprint}.json"
            )
            if not candidate_path.is_file():
                event_id = processed_event_ids.get(old_fingerprint, "")
                archive_root = self.layout.autonomous_root / "events" / "processed"
                expected = archive_root / f"{event_id}.{old_fingerprint[:12]}.json"
                if expected.is_file():
                    candidate_path = expected
                else:
                    raise ValueError(
                        "cannot locate persisted unfinished candidate "
                        f"{old_fingerprint} from {source_run_id}"
                    )
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
            old_event = CandidateEvent.from_dict(raw)
            if not _candidate_fingerprint_matches_persisted(
                raw, old_event, old_fingerprint,
            ):
                raise ValueError(
                    f"persisted unfinished candidate fingerprint mismatch: {old_fingerprint}"
                )
            payload = old_event.to_dict()
            payload.pop("fingerprint", None)
            payload.update({
                "event_id": "recovered-" + stable_hash({
                    "source_run_id": source_run_id,
                    "event_id": old_event.event_id,
                    "fingerprint": old_fingerprint,
                    "recovery_kind": "unfinished_audit",
                })[:20],
                "producer_thread_id": None,
                "producer_task_id": f"recovery-{source_run_id}-{old_event.producer_task_id}",
                "source_run_id": source_run_id,
            })
            event = CandidateEvent.from_dict(payload)
            for artifact in event.artifact_paths:
                raw_artifact = Path(artifact)
                resolved = (
                    raw_artifact.resolve() if raw_artifact.is_absolute()
                    else (self.config.project_root / raw_artifact).resolve()
                )
                if (
                    not resolved.is_relative_to(self.config.project_root.resolve())
                    or not resolved.is_file()
                ):
                    raise ValueError(
                        f"recovered candidate artifact is unavailable or outside project: {artifact}"
                    )
            event.artifact_paths = [
                str(
                    Path(artifact).resolve() if Path(artifact).is_absolute()
                    else (self.config.project_root / artifact).resolve()
                )
                for artifact in event.artifact_paths
            ]
            if event.fingerprint not in seen:
                recovered.append(event)
                seen.add(event.fingerprint)
        if not recovered:
            raise ValueError(
                f"source run {source_run_id} has no rejected or unfinished candidates to recover"
            )
        probe = ClaimGraph(
            deepcopy(self.graph.claims), semantics=self.domain_semantics,
        )
        for event in recovered:
            probe.mark_candidate(event)
        return recovered

    def _register_recovered_candidates(
        self, source_run_id: str, candidates: list[CandidateEvent],
    ) -> None:
        for event in candidates:
            verified_receipts = self._verify_domain_evidence_receipts(
                event, extend_artifacts=False,
            )
            artifact_hashes = self._bind_candidate_artifacts(event)
            semantic_evidence_hashes = self._bind_candidate_semantic_evidence(
                event, artifact_hashes,
            )
            self.graph.mark_candidate(event)
            self.inbox.persist(event)
            self.inbox.mark_processed(event, self.run_id, accepted=True)
            state = self.audit_gate.register(event)
            self.store.append("CANDIDATE_RESCUED_FROM_RUN", {
                "source_run_id": source_run_id,
                "event_id": event.event_id,
                "candidate_fingerprint": event.fingerprint,
                "claim_id": event.claim_id,
                "parent_claim_id": event.parent_claim_id,
                "action": "registered as an untrusted candidate for a fresh audit",
            })
            self.store.append("CANDIDATE_PROCESSED", {
                "event_id": event.event_id, "fingerprint": event.fingerprint,
                "claim_id": event.claim_id, "parent_claim_id": event.parent_claim_id,
                "impact": event.impact,
                "proposed_evidence_level": event.proposed_evidence_level,
                "source_run_id": source_run_id,
                "artifact_hashes": artifact_hashes,
                "semantic_evidence_hashes": semantic_evidence_hashes,
                "domain_evidence_receipt_fingerprints": [
                    item["receipt_fingerprint"] for item in verified_receipts
                ],
            })
            self.satisfied_route_conditions.update({
                event.fingerprint, f"new_evidence:{event.claim_id}",
            })
            if state.required:
                self._queue_next_audit(event)
            else:
                self.batched_observations.append(event)
        self._commit_claim_state_transition(
            transition_kind="RECOVERED_CANDIDATES_REGISTERED",
            authorization={
                "source_run_id": source_run_id,
                "candidate_fingerprints": [
                    event.fingerprint for event in candidates
                ],
                "trust_upgrade": False,
            },
        )

    def _task_timeout(self, role: str) -> float:
        return self.config.role_timeout(role)

    def _thread_budget(self, role: str) -> int | None:
        return self.config.role_token_limit(role)

    def _server_thread_budget(self, role: str) -> int | None:
        # In observe mode the number is telemetry guidance only. Do not send it
        # as a server-side goal that could make the turn end early.
        if self.config.per_thread_limit_action != "interrupt":
            return None
        return self._thread_budget(role)

    def _estimated_tokens(self, role: str, cost_tier: str = "MEDIUM") -> int:
        estimates = self.config.raw["budgets"].get("estimated_tokens", {})
        if role in estimates:
            return int(estimates[role])
        return int(estimates.get(cost_tier, estimates.get("MEDIUM", 50000)))

    def _canonical_progress_marker(self, claim_id: str) -> str:
        claim = self.graph.claims.get(claim_id)
        if claim is None:
            return "missing"
        frontier_field = (
            "proof_frontier"
            if self.domain_semantics.domain == "math-research"
            else "research_frontier"
        )
        return stable_hash({
            self._claim_status_field: claim.research_status,
            "trust_status": claim.trust_status,
            "evidence_level": claim.evidence_level,
            "evidence_paths": sorted(claim.evidence_paths),
            "known_counterexamples": sorted(claim.known_counterexamples),
            frontier_field: self.graph.research_frontier(claim_id),
        })

    def _task_has_accepted_candidate(self, task_id: str) -> bool:
        for fingerprint in self.inbox.accepted:
            path = self.inbox.candidate_root / f"{fingerprint}.json"
            if not path.is_file():
                continue
            try:
                candidate = CandidateEvent.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if candidate.producer_task_id == task_id:
                return True
        return False

    def _max_effort_supported(self, role: str) -> bool:
        provider = self.config.provider_for(role)
        supported = (
            provider.get("capabilities", {}).get("reasoning", {})
            .get("supported_efforts", [])
        )
        return "max" in supported

    async def _control_research_turn(
        self,
        *,
        job_id: str,
        task: ResearchTask,
        canonical_before: str,
        outcome: JobOutcome,
        turn_index: int,
    ) -> TurnDirective:
        if outcome.thread_id and outcome.turn_id:
            binding = (str(outcome.thread_id), str(outcome.turn_id))
            previous = self._bound_jobs.get(job_id)
            if previous != binding:
                self._bound_jobs[job_id] = binding
                self.store.append(
                    "JOB_BOUND" if previous is None else "JOB_REBOUND",
                    {
                        "job_id": job_id,
                        "thread_id": binding[0],
                        "turn_id": binding[1],
                        **(
                            {"previous_turn_id": previous[1]}
                            if previous is not None else {}
                        ),
                    },
                )
        # Pull worker-file evidence into the controller before deciding whether
        # a role's completed turn also completed the logical task.
        await self._poll_filesystem_candidates()
        await self._process_candidate_queue()
        candidate_accepted = self._task_has_accepted_candidate(task.task_id)
        canonical_progress = (
            self._canonical_progress_marker(task.target_claim) != canonical_before
            and self.graph.claims.get(task.target_claim) is not None
            and self.graph.claims[task.target_claim].trust_status in {
                TrustStatus.CANONICAL_TRUSTED,
                TrustStatus.AUDITED_NIGHTLY,
                TrustStatus.FORMALLY_VERIFIED,
            }
        )
        outcome.candidate_accepted = candidate_accepted
        outcome.canonical_progress = canonical_progress
        health = self.reasoning_health.observe(
            job_id=job_id,
            turn_index=turn_index,
            effort=str(outcome.reasoning_effort or ""),
            usage=outcome.token_usage,
            telemetry=outcome.token_telemetry,
            max_effort_supported=self._max_effort_supported(task.role),
        )
        blocker_reported = str(outcome.result.get("result_type") or "") == "BLOCKED"
        blocker_repair_attempted = job_id in self._blocker_repair_jobs
        blocker_verified = bool(
            blocker_repair_attempted
            and blocker_reported
            and str(outcome.result.get("status") or "").strip().upper() == "BLOCKED"
            and len(str(outcome.result.get("main_finding") or "").strip()) >= 16
            and len(
                str(outcome.result.get("next_suggested_question") or "").strip()
            ) >= 8
        )
        directive = self.research_turn_policy.decide(
            result=outcome.result,
            role=task.role,
            turn_index=turn_index,
            candidate_accepted=candidate_accepted,
            canonical_progress=canonical_progress,
            health_signal=health,
            budget_stop_reason=outcome.continuation_budget_stop_reason,
            blocker_repair_attempted=blocker_repair_attempted,
            blocker_verified=blocker_verified,
        )
        if blocker_reported and directive.continue_same_thread:
            self._blocker_repair_jobs.add(job_id)
        self.store.append("RESEARCH_TURN_COMPLETED", {
            "job_id": job_id,
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "thread_id": outcome.thread_id,
            "turn_id": outcome.turn_id,
            "turn_index": turn_index,
            "execution_status": "COMPLETED",
            "role_reported_result_type": outcome.result.get("result_type"),
            "role_reported_status": outcome.result.get("status"),
            "candidate_accepted": candidate_accepted,
            "canonical_progress": canonical_progress,
            "blocker_reported": blocker_reported,
            "blocker_repair_attempted": blocker_repair_attempted,
            "blocker_controller_verified": blocker_verified,
            "blocker_verification_scope": (
                "execution scheduling only; no mathematical or trust effect"
                if self.domain_semantics.domain == "math-research"
                else "execution scheduling only; no claim-status or trust effect"
            ),
            "reasoning_health": health.to_dict(),
            "controller_directive": (
                "CONTINUE" if directive.continue_same_thread else "STOP"
            ),
            "controller_reason": directive.reason,
            "next_effort": directive.effort_override,
            "token_usage": outcome.token_usage.to_dict(),
            "token_telemetry": outcome.token_telemetry,
        })
        if directive.continue_same_thread:
            self.store.append("RESEARCH_TURN_CONTINUATION_REQUESTED", {
                "job_id": job_id,
                "task_id": task.task_id,
                "claim_id": task.target_claim,
                "completed_turn_id": outcome.turn_id,
                "next_turn_index": turn_index + 1,
                "same_thread": True,
                "reason": directive.reason,
                "effort_override": directive.effort_override,
            })
        else:
            self.store.append("RESEARCH_LOGICAL_JOB_TERMINAL", {
                "job_id": job_id,
                "task_id": task.task_id,
                "claim_id": task.target_claim,
                "turn_count": turn_index,
                "reason": directive.reason,
                "candidate_accepted": candidate_accepted,
                "canonical_progress": canonical_progress,
            })
        return directive

    def recover(self) -> None:
        records = self.store.replay()
        last_director_success_sequence = max(
            (
                int(event.get("sequence", 0)) for event in records
                if event.get("kind") == "DIRECTOR_PLAN_ACCEPTED"
            ),
            default=-1,
        )
        terminal_jobs = {
            e["payload"].get("job_id")
            for e in records if e["kind"] in {"JOB_COMPLETED", "JOB_CANCELLED"}
        }
        completed_tasks = {e["payload"].get("task_id") for e in records if e["kind"] == "JOB_COMPLETED"}
        started = {
            e["payload"].get("job_id"): e["payload"]
            for e in records if e["kind"] == "JOB_STARTED" and e["payload"].get("job_id")
        }
        last_started_sequence_by_task: dict[str, int] = {}
        last_retry_sequence_by_task: dict[str, int] = {}
        last_retained_sequence_by_task: dict[str, int] = {}
        retained_audit_fingerprints: set[str] = set()
        checkpointed_continuations: dict[str, ResearchTask] = {}
        for event in records:
            payload = event.get("payload") or {}
            task_id = str(payload.get("task_id") or "")
            if event["kind"] == "RESEARCH_CONTINUATION_CHECKPOINTED":
                raw_task = payload.get("continuation_task")
                if not isinstance(raw_task, dict):
                    raise ValueError("continuation checkpoint event has no task packet")
                continuation = ResearchTask.from_dict(raw_task)
                checkpointed_continuations[continuation.task_id] = continuation
            if event["kind"] == "AUDIT_RETAINED_AFTER_ERROR":
                fingerprint = str(payload.get("candidate_fingerprint") or "")
                if fingerprint:
                    retained_audit_fingerprints.add(fingerprint)
            if not task_id:
                continue
            if event["kind"] == "JOB_STARTED":
                last_started_sequence_by_task[task_id] = int(event.get("sequence", 0))
            elif event["kind"] == "JOB_RETRY_QUEUED":
                last_retry_sequence_by_task[task_id] = int(event.get("sequence", 0))
            elif event["kind"] == "TASK_RETAINED_AFTER_ERROR":
                last_retained_sequence_by_task[task_id] = int(event.get("sequence", 0))
        bindings = {
            e["payload"].get("job_id"): e["payload"]
            for e in records
            if e["kind"] in {"JOB_BOUND", "JOB_REBOUND"} and e["payload"].get("job_id")
        }
        streamed_turn_by_thread: dict[str, str] = {}
        for event in records:
            if event["kind"] != "APP_SERVER_NOTIFICATION":
                continue
            notification = event.get("payload") or {}
            params = notification.get("params") or {}
            thread_id = str(params.get("threadId") or "")
            if notification.get("method") == "turn/started" and thread_id:
                turn_id = str((params.get("turn") or {}).get("id") or "")
                if turn_id:
                    streamed_turn_by_thread[thread_id] = turn_id
            elif notification.get("method") == "turn/completed" and thread_id:
                streamed_turn_by_thread.pop(thread_id, None)
        accepted: dict[str, ResearchTask] = {}
        for event in records:
            if event["kind"] == "TASK_ACCEPTED":
                fingerprint = str(event["payload"]["fingerprint"])
                self.seen_task_fingerprints.add(fingerprint)
                task_payload = event["payload"].get("task")
                if task_payload:
                    task = ResearchTask.from_dict(task_payload)
                    bound = self.task_fingerprints_by_id.get(task.task_id)
                    if bound in {None, fingerprint}:
                        self.task_fingerprints_by_id[task.task_id] = fingerprint
                        accepted[task.task_id] = task
            elif event["kind"] == "CANDIDATE_PROCESSED":
                fingerprint = event["payload"]["fingerprint"]
                self.inbox.processed.add(fingerprint)
                candidate_path = self.inbox.candidate_root / f"{fingerprint}.json"
                if candidate_path.exists():
                    candidate = CandidateEvent.from_dict(json.loads(candidate_path.read_text(encoding="utf-8")))
                    verified_receipts = self._verify_domain_evidence_receipts(
                        candidate, extend_artifacts=False,
                    )
                    self.inbox.persisted.add(fingerprint)
                    self.audit_gate.register(candidate)
                    recorded_hashes = event["payload"].get("artifact_hashes")
                    if isinstance(recorded_hashes, dict) and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in recorded_hashes.items()
                    ):
                        self.candidate_artifact_hashes[fingerprint] = dict(recorded_hashes)
                    else:
                        self._bind_candidate_artifacts(candidate)
                    semantic_hashes = self._bind_candidate_semantic_evidence(
                        candidate, self.candidate_artifact_hashes[fingerprint],
                    )
                    if event["payload"].get("semantic_evidence_hashes", {}) != semantic_hashes:
                        raise ValueError(
                            "candidate semantic evidence binding changed during resume"
                        )
                    recorded_receipts = event["payload"].get(
                        "domain_evidence_receipt_fingerprints", []
                    )
                    if recorded_receipts != [
                        item["receipt_fingerprint"] for item in verified_receipts
                    ]:
                        raise ValueError(
                            "candidate domain evidence receipt binding changed during resume"
                        )
                    self.inbox.mark_processed(candidate, self.run_id, accepted=True)
            elif event["kind"] == "JOB_COMPLETED":
                payload = event["payload"]
                self.completed_jobs.append(payload)
                usage = TokenUsage(**(payload.get("token_usage") or {}))
                if payload.get("job_id") and payload.get("role"):
                    self.governor.record(
                        payload["job_id"], payload["role"], usage,
                        payload.get("useful"), payload.get("cost_usd"),
                    )
                if payload.get("claim_id") and payload.get("role") in {Role.PROVER, Role.FALSIFIER, Role.EXPLORER}:
                    self.stagnation.record(
                        payload["claim_id"],
                        str((payload.get("result") or {}).get("result_type", "NO_PROGRESS")),
                        canonical_progress=bool(payload.get("canonical_progress", False)),
                    )
            elif event["kind"] == "JOB_CANCELLED":
                payload = event["payload"]
                self.completed_jobs.append(payload)
                job_id = str(payload.get("job_id") or "")
                if job_id:
                    self.cancelled_jobs.add(job_id)
                usage = TokenUsage(**(payload.get("token_usage") or {}))
                if job_id and payload.get("role"):
                    self.governor.record(
                        job_id, str(payload["role"]), usage, False,
                        payload.get("cost_usd"),
                    )
            elif event["kind"] in {
                "MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED",
            }:
                payload = dict(event["payload"])
                self.completed_mechanical_jobs.append(payload)
                if not payload.get("cache_reused"):
                    usage = TokenUsage(**(payload.get("token_usage") or {}))
                    recovered_job_id = (
                        "mechanical-recovered-"
                        f"{payload.get('parent_job_id')}-{payload.get('subtask_id')}-"
                        f"{event.get('sequence')}"
                    )
                    self.mechanical_governor.record(
                        recovered_job_id, MECHANICAL_ROLE, usage,
                        payload.get("status") not in {"TOOL_ERROR", "REJECTED", "BLOCKED"},
                        payload.get("cost_usd"),
                    )
                packet_hash = str(payload.get("packet_sha256") or "")
                parent_task_id = str(payload.get("parent_task_id") or "")
                subtask_id = str(payload.get("subtask_id") or "")
                if packet_hash and parent_task_id and subtask_id:
                    response = {
                        key: payload.get(key)
                        for key in (
                            "schema_version", "parent_job_id", "parent_task_id", "parent_role",
                            "subtask_id", "status", "model", "reasoning_effort", "service_tier",
                            "token_usage", "token_telemetry", "result", "artifacts",
                            "runner_directory", "fallback", "error", "failure_kind", "retryable",
                        )
                    }
                    try:
                        self._mechanical_result_cache[
                            (parent_task_id, subtask_id, packet_hash)
                        ] = validate_mechanical_response(
                            response, allowed_routes=self._allowed_mechanical_routes(),
                        )
                    except MechanicalTaskRejected:
                        self.store.append("MECHANICAL_RECOVERY_RECORD_INVALID", {
                            "sequence": event.get("sequence"),
                            "parent_job_id": payload.get("parent_job_id"),
                            "subtask_id": subtask_id,
                        })
            elif event["kind"] == "MECHANICAL_ROUTE_UNAVAILABLE":
                payload = event.get("payload") or {}
                model = str(payload.get("model") or "")
                effort = (
                    str(payload["reasoning_effort"])
                    if payload.get("reasoning_effort") is not None else None
                )
                tier = payload.get("service_tier")
                if model and tier is None:
                    self._mechanical_unavailable_routes.add((model, effort, None))
                    persist = getattr(self.mechanical_runner, "persist_unavailable", None)
                    remember = getattr(self.mechanical_runner, "remember_unavailable", None)
                    try:
                        handler = persist if callable(persist) else remember
                        if callable(handler):
                            handler(
                                model=model,
                                reasoning_effort=effort,
                                service_tier=None,
                                error=str(payload.get("error") or "event replay attestation"),
                                run_directory=str(
                                    payload.get("runner_directory")
                                    or "controller-event-replay"
                                ),
                            )
                    except MechanicalTaskRejected as exc:
                        self.store.append("MECHANICAL_ROUTE_CACHE_PERSIST_FAILED", {
                            "model": model,
                            "reasoning_effort": effort,
                            "service_tier": None,
                            "error": _sanitize_live_text(exc),
                            "source": "recovery_event_replay",
                        })
                        if not self._internal_failure:
                            self._begin_internal_failure_drain(
                                "mechanical route circuit-breaker persistence failed",
                                source="mechanical_route_cache",
                            )
            elif event["kind"] == "JOB_RETRY_QUEUED":
                task_id = str(event["payload"].get("task_id", ""))
                retry_class = str(event["payload"].get("retry_class") or "transport")
                retry_key = (task_id, retry_class)
                self.retry_counts[retry_key] = max(
                    self.retry_counts.get(retry_key, 0),
                    int(event["payload"].get("retry", 0)),
                )
            elif event["kind"] == "AUDIT_RETRY_QUEUED":
                fingerprint = str(event["payload"].get("candidate_fingerprint", ""))
                retry_class = str(event["payload"].get("retry_class") or "transport")
                retry_key = (fingerprint, retry_class)
                self.audit_retry_counts[retry_key] = max(
                    self.audit_retry_counts.get(retry_key, 0),
                    int(event["payload"].get("retry", 0)),
                )
            elif (
                event["kind"] == "DIRECTOR_RETRY_QUEUED"
                and int(event.get("sequence", 0)) > last_director_success_sequence
            ):
                retry_class = str(event["payload"].get("retry_class") or "transport")
                self.director_retry_counts[retry_class] = max(
                    self.director_retry_counts.get(retry_class, 0),
                    int(event["payload"].get("retry", 0)),
                )
                self.director_retry_count = sum(self.director_retry_counts.values())
            elif event["kind"] == "DIRECTOR_REPLAN_REQUESTED":
                self._state_version = max(
                    self._state_version, int(event["payload"].get("state_version", 0))
                )
                self._director_requested_version = max(
                    self._director_requested_version,
                    int(event["payload"].get("requested_version", 0)),
                )
            elif event["kind"] == "DIRECTOR_PLAN_ACCEPTED":
                self._director_applied_version = max(
                    self._director_applied_version,
                    int(event["payload"].get("snapshot_version", 0)),
                )
        self.recent_changes = []
        for event in records:
            if event["kind"] not in {
                "TRUST_STATE_CHANGED", "DEPENDENCY_PRUNED", "PRIORITY_CHANGED",
                "STAGNATION_DIVERSIFY", "AUDIT_RECORDED", "RESEARCH_JOB_COMPLETED",
                "MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED",
            }:
                continue
            payload = event.get("payload", {})
            if event["kind"].startswith("MECHANICAL_SUBTASK_"):
                change = {
                    "kind": event["kind"],
                    "parent_task_id": payload.get("parent_task_id"),
                    "parent_role": payload.get("parent_role"),
                    "subtask_id": payload.get("subtask_id"),
                    "status": payload.get("status"),
                    "failure_kind": payload.get("failure_kind"),
                    "mechanical_evidence_only": True,
                    "result": _bounded_value(payload.get("result") or {}),
                    "artifact_hashes": payload.get("artifact_hashes") or {},
                }
            else:
                change = {"kind": event["kind"], **payload}
            self._record_recent_change(change)
        for event in records:
            if event["kind"] != "AUDIT_RECORDED":
                continue
            payload = dict(event["payload"])
            payload.pop("trust_status", None)
            result = AuditResult.from_dict(payload)
            if result.candidate_fingerprint in self.audit_gate.states:
                state = self.audit_gate.states[result.candidate_fingerprint]
                self._restore_audit_result(state.event, result)
        latest_usage_by_thread: dict[str, dict[str, Any]] = {}
        for event in records:
            if event["kind"] != "APP_SERVER_NOTIFICATION":
                continue
            notification = event.get("payload") or {}
            if notification.get("method") != "thread/tokenUsage/updated":
                continue
            params = notification.get("params") or {}
            thread_id = str(params.get("threadId") or "")
            total = (params.get("tokenUsage") or {}).get("total") or {}
            if thread_id and total:
                latest_usage_by_thread[thread_id] = total
        job_by_thread = {
            str(binding.get("thread_id")): str(job_id)
            for job_id, binding in bindings.items() if binding.get("thread_id")
        }
        for thread_id, total in latest_usage_by_thread.items():
            job_id = job_by_thread.get(thread_id)
            started_job = started.get(job_id or "")
            if not job_id or not started_job or not started_job.get("role"):
                continue
            existing_useful = (self.governor.by_job.get(job_id) or {}).get("useful")
            self.governor.record(
                job_id, str(started_job["role"]), TokenUsage.from_app_server(total),
                existing_useful,
            )
        for lease_payload in self.audit_leases.snapshot():
            if lease_payload.get("status") != AuditLeaseStatus.ACTIVE:
                continue
            lease_id = str(lease_payload["lease_id"])
            self.audit_leases.retry_wait(lease_id)
            self.store.append("AUDIT_LEASE_RECOVERED", {
                "lease_id": lease_id,
                "candidate_fingerprint": lease_payload["candidate_fingerprint"],
                "audit_kind": lease_payload["audit_kind"],
                "from": "ACTIVE", "to": "RETRY_WAIT",
                "action": "stale epoch job will be reconciled before a fresh audit attempt",
            })
        for state in self.audit_gate.states.values():
            # A nonterminal audit is durable pending work. Explicit resume may
            # re-attempt it once even when the preceding run exhausted one
            # retry class; the preserved class counters still bound any
            # automatic retry that follows in this resumed process.
            self._queue_next_audit(state.event)
        # Retention is stronger than the automatic retry allowance.  A failed
        # audit remains pending for an explicit resume after repair even when
        # its transient retry budget was already exhausted.
        for fingerprint in retained_audit_fingerprints:
            state = self.audit_gate.states.get(fingerprint)
            if state is not None:
                self._queue_next_audit(state.event)
        for job_id in sorted(set(started) - terminal_jobs):
            self.store.append("STALE_JOB_DETECTED", {"job_id": job_id, "reason": "controller restart"})
            binding = bindings.get(job_id)
            if binding and binding.get("thread_id") and binding.get("turn_id"):
                thread_id = str(binding["thread_id"])
                turn_id = streamed_turn_by_thread.get(thread_id, str(binding["turn_id"]))
                self.stale_remote_turns.append((job_id, thread_id, turn_id))
            task_id = str(started[job_id].get("task_id", ""))
            task = accepted.get(task_id)
            failure_kind = "transport_transient"
            retry = self._retry_count(self.retry_counts, task_id, failure_kind)
            max_retries = self._retry_limit(
                failure_kind, task.role if task is not None else Role.PROVER,
            )
            if task and retry < max_retries:
                self._set_retry_count(
                    self.retry_counts, task_id, failure_kind, retry + 1,
                )
                self.pending_research.append(task)
                self.store.append("JOB_RETRY_QUEUED", {
                    "job_id": job_id, "task_id": task_id, "retry": retry + 1,
                    "max_retries": max_retries,
                    "retry_class": self._retry_class(failure_kind),
                    "failure_kind": failure_kind,
                })
            observed_usage = self.governor.by_job.get(job_id, {})
            stale_record = {
                "job_id": job_id,
                "task_id": task_id,
                "claim_id": started[job_id].get("claim_id"),
                "role": started[job_id].get("role"),
                "status": "CANCELLED",
                "thread_id": (binding or {}).get("thread_id"),
                "turn_id": (binding or {}).get("turn_id"),
                "token_usage": {
                    key: int(observed_usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens", "cached_input_tokens", "uncached_input_tokens",
                        "cache_write_input_tokens", "output_tokens",
                        "reasoning_output_tokens", "total_tokens",
                    )
                },
                "token_telemetry": (
                    "observed" if observed_usage.get("total_tokens") else "unknown"
                ),
                "cost_usd": observed_usage.get("cost_usd"),
                "failure_kind": "controller_restart",
                "retryable": bool(task and retry < max_retries),
                "useful": False,
                "end_time": utc_now(),
                "elapsed_seconds": 0.0,
                "exit_reason": "controller restart reconciled stale job",
            }
            self.completed_jobs.append(stale_record)
            self.cancelled_jobs.add(job_id)
            self.store.append("JOB_CANCELLED", stale_record)
        pending_ids = {task.task_id for task in self.pending_research}
        for task_id, retry_sequence in last_retry_sequence_by_task.items():
            if (
                retry_sequence > last_started_sequence_by_task.get(task_id, -1)
                and task_id in accepted and task_id not in pending_ids
            ):
                self.pending_research.append(accepted[task_id])
                pending_ids.add(task_id)
        for task_id, retained_sequence in last_retained_sequence_by_task.items():
            if (
                retained_sequence > last_started_sequence_by_task.get(task_id, -1)
                and task_id in accepted and task_id not in pending_ids
            ):
                self.pending_research.append(accepted[task_id])
                pending_ids.add(task_id)
        started_task_ids = {str(payload.get("task_id", "")) for payload in started.values()}
        for task_id, task in accepted.items():
            if task_id not in completed_tasks and task_id not in started_task_ids and task_id not in pending_ids:
                self.pending_research.append(task)
        for task_id, task in checkpointed_continuations.items():
            if task_id in started_task_ids or task_id in pending_ids:
                continue
            self.deferred_research_continuations.append(task)
            self.seen_task_fingerprints.add(task.fingerprint)
            self.task_fingerprints_by_id[task.task_id] = task.fingerprint
        mechanical_requested: dict[tuple[str, str], dict[str, Any]] = {}
        mechanical_terminal = {
            (
                str(event.get("payload", {}).get("parent_job_id") or ""),
                str(event.get("payload", {}).get("subtask_id") or ""),
            )
            for event in records
            if event.get("kind") in {
                "MECHANICAL_SUBTASK_COMPLETED", "MECHANICAL_SUBTASK_FAILED",
            }
        }
        for event in records:
            if event.get("kind") != "MECHANICAL_SUBTASK_REQUESTED":
                continue
            payload = event.get("payload") or {}
            key = (
                str(payload.get("parent_job_id") or ""),
                str(payload.get("subtask_id") or ""),
            )
            if all(key) and payload.get("valid") is True:
                mechanical_requested[key] = payload
        mechanical_started: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
        mechanical_finished: dict[tuple[str, str], dict[int, dict[str, Any]]] = {}
        mechanical_fallback_keys: set[tuple[str, str]] = set()
        mechanical_retry_counts: dict[tuple[str, str], dict[str, int]] = {}
        for event in records:
            payload = event.get("payload") or {}
            key = (
                str(payload.get("parent_job_id") or ""),
                str(payload.get("subtask_id") or ""),
            )
            if not all(key):
                continue
            if event.get("kind") == "MECHANICAL_SUBTASK_FALLBACK":
                mechanical_fallback_keys.add(key)
                continue
            if event.get("kind") == "MECHANICAL_SUBTASK_RETRY_QUEUED":
                retry_class = str(payload.get("retry_class") or "transient")
                retry = int(payload.get("retry") or 0)
                if retry > 0:
                    counts = mechanical_retry_counts.setdefault(key, {})
                    counts[retry_class] = max(counts.get(retry_class, 0), retry)
                continue
            if event.get("kind") not in {
                "MECHANICAL_SUBTASK_STARTED",
                "MECHANICAL_SUBTASK_ATTEMPT_FINISHED",
            }:
                continue
            attempt = int(payload.get("attempt") or 0)
            if attempt < 1:
                continue
            target = (
                mechanical_started
                if event.get("kind") == "MECHANICAL_SUBTASK_STARTED"
                else mechanical_finished
            )
            target.setdefault(key, {})[attempt] = dict(payload)
        for key, payload in mechanical_requested.items():
            if key in mechanical_terminal:
                continue
            request_path = Path(str(payload.get("request_path") or ""))
            if not request_path.is_file():
                self.store.append("MECHANICAL_RECOVERY_REQUEST_MISSING", {
                    "parent_job_id": key[0], "subtask_id": key[1],
                    "request_path": str(request_path),
                })
                continue
            try:
                workspace = self._mechanical_workspace_from_request_path(request_path)
                request = validate_mechanical_request(
                    json.loads(request_path.read_text(encoding="utf-8")),
                    repository_root=workspace,
                    expected_parent_job_id=key[0],
                    expected_parent_task_id=str(payload["parent_task_id"]),
                    expected_parent_role=str(payload["parent_role"]),
                )
            except (OSError, ValueError, json.JSONDecodeError, MechanicalTaskRejected) as exc:
                self.store.append("MECHANICAL_RECOVERY_REQUEST_INVALID", {
                    "parent_job_id": key[0], "subtask_id": key[1],
                    "error": _sanitize_live_text(exc),
                })
                continue
            state = MechanicalRequestState(
                parent_job_id=key[0],
                parent_task_id=str(payload["parent_task_id"]),
                parent_role=str(payload["parent_role"]),
                parent_workspace=str(workspace),
                request_path=str(request_path),
                response_path=str(
                    self.workspace.mechanical_broker_response_root(workspace)
                    / f"{key[1]}.json"
                ),
                request_sha256=str(request["request_sha256"]),
                packet=dict(request["task_packet"]),
                recovered=True,
            )
            started_attempts = mechanical_started.get(key, {})
            finished_attempts = mechanical_finished.get(key, {})
            state.attempts_started = max(
                [0, *started_attempts.keys(), *finished_attempts.keys()]
            )
            state.fallback_emitted = key in mechanical_fallback_keys
            state.retry_counts = dict(mechanical_retry_counts.get(key, {}))
            response_path = Path(state.response_path)
            if response_path.is_file():
                try:
                    response = validate_mechanical_response(
                        json.loads(response_path.read_text(encoding="utf-8")),
                        allowed_routes=self._allowed_mechanical_routes(),
                    )
                    latest_finished = (
                        finished_attempts[max(finished_attempts)]
                        if finished_attempts else {}
                    )
                    recovered_route_execution = MechanicalExecution(
                        status=str(response["status"]),
                        result=dict(response.get("result") or {}),
                        model=(str(response["model"]) if response.get("model") else None),
                        reasoning_effort=(
                            str(response["reasoning_effort"])
                            if response.get("reasoning_effort") else None
                        ),
                        actual_model=(
                            str(latest_finished["actual_model"])
                            if latest_finished.get("actual_model") else None
                        ),
                        actual_reasoning_effort=(
                            str(latest_finished["actual_reasoning_effort"])
                            if latest_finished.get("actual_reasoning_effort") else None
                        ),
                        model_route_attestation=str(
                            latest_finished.get("model_route_attestation")
                            or "unobservable"
                        ),
                        cost_usd=(
                            float(latest_finished["cost_usd"])
                            if latest_finished.get("cost_usd") is not None else None
                        ),
                        cost_telemetry=str(
                            latest_finished.get("cost_telemetry") or "unknown"
                        ),
                    )
                    state.accumulated_usage = TokenUsage(**response["token_usage"])
                    state.telemetry_observed = int(
                        response["token_telemetry"] in {"observed", "synthetic", "partial"}
                    )
                    state.telemetry_unknown = int(
                        response["token_telemetry"] in {"unknown", "partial"}
                    )
                    recovered_costs = [
                        float(item["cost_usd"])
                        for item in finished_attempts.values()
                        if item.get("cost_usd") is not None
                    ]
                    state.accumulated_cost_usd = sum(recovered_costs)
                    state.cost_telemetry_observed = len(recovered_costs)
                    state.cost_telemetry_unknown = sum(
                        item.get("cost_usd") is None
                        for item in finished_attempts.values()
                    )
                    self._persist_mechanical_terminal(
                        state, response, execution=recovered_route_execution,
                    )
                    self.mechanical_governor.record(
                        f"mechanical-reconciled-{key[0]}-{key[1]}",
                        MECHANICAL_ROLE,
                        state.accumulated_usage,
                        response["status"] not in {"TOOL_ERROR", "REJECTED", "BLOCKED"},
                        (
                            state.accumulated_cost_usd
                            if state.cost_telemetry_observed else None
                        ),
                    )
                    continue
                except (OSError, json.JSONDecodeError, MechanicalTaskRejected) as exc:
                    self.store.append("MECHANICAL_RECOVERY_RESPONSE_INVALID", {
                        "parent_job_id": key[0], "subtask_id": key[1],
                        "error": _sanitize_live_text(exc),
                    })
            finished_retryable_counts: dict[str, int] = {}
            for attempt, attempt_payload in sorted(finished_attempts.items()):
                usage = TokenUsage(**(attempt_payload.get("token_usage") or {}))
                self._add_token_usage(state.accumulated_usage, usage)
                telemetry = str(attempt_payload.get("token_telemetry") or "unknown")
                if telemetry in {"observed", "synthetic"}:
                    state.telemetry_observed += 1
                elif telemetry == "partial":
                    state.telemetry_observed += 1
                    state.telemetry_unknown += 1
                else:
                    state.telemetry_unknown += 1
                if attempt_payload.get("cost_usd") is not None:
                    state.accumulated_cost_usd += float(attempt_payload["cost_usd"])
                    state.cost_telemetry_observed += 1
                else:
                    state.cost_telemetry_unknown += 1
                self.mechanical_governor.record(
                    f"mechanical-recovered-attempt-{key[0]}-{key[1]}-{attempt}",
                    MECHANICAL_ROLE,
                    usage,
                    attempt_payload.get("status")
                    not in {"TOOL_ERROR", "REJECTED", "BLOCKED"},
                    attempt_payload.get("cost_usd"),
                )
                if bool(attempt_payload.get("retryable", False)):
                    failure_kind = str(attempt_payload.get("failure_kind") or "")
                    retry_class = (
                        "model_protocol"
                        if failure_kind in {"model_output_protocol", "runner_protocol"}
                        else "transient"
                    )
                    finished_retryable_counts[retry_class] = (
                        finished_retryable_counts.get(retry_class, 0) + 1
                    )
            unfinished_attempts = sorted(set(started_attempts) - set(finished_attempts))
            if unfinished_attempts:
                # Only the latest unfinished attempt can still be live. Older
                # unmatched STARTED records remain immutable unknown telemetry.
                state.telemetry_unknown += max(0, len(unfinished_attempts) - 1)
                latest_attempt = unfinished_attempts[-1]
                lease_payload = started_attempts[latest_attempt]
                recover_runner = getattr(self.mechanical_runner, "recover", None)
                receipt_path = lease_payload.get("receipt_path")
                packet_path = lease_payload.get("packet_path")
                output_root = lease_payload.get("output_root")
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None
                if (
                    callable(recover_runner)
                    and running_loop is not None
                    and receipt_path
                    and packet_path
                    and output_root
                ):
                    logical_job_id = str(
                        lease_payload.get("mechanical_job_id")
                        or f"mechanical-{key[0]}-{key[1]}-a{latest_attempt}"
                    )
                    recovery_deadline = float(
                        lease_payload.get("recovery_deadline_epoch") or time.time() + 5
                    )
                    remaining = max(1, math.ceil(recovery_deadline - time.time()))
                    estimated = int(
                        lease_payload.get("estimated_token_reservation")
                        or self.config.raw["policy"].get(
                            "one_shot_compute_worker", {}
                        ).get("estimated_tokens", 60000)
                    )
                    estimated_cost = self.config.raw["policy"].get(
                        "one_shot_compute_worker", {}
                    ).get("estimated_cost_usd")
                    future = running_loop.create_task(recover_runner(
                        packet_path=Path(str(packet_path)),
                        output_root=Path(str(output_root)),
                        receipt_path=Path(str(receipt_path)),
                        timeout_seconds=remaining,
                    ))
                    self.mechanical_governor.restore_reservation(
                        logical_job_id, MECHANICAL_ROLE, estimated, estimated_cost,
                    )
                    self.active_mechanical[logical_job_id] = ActiveMechanicalJob(
                        logical_job_id=logical_job_id,
                        request=state,
                        future=future,
                        started_monotonic=time.monotonic(),
                        started_at=str(lease_payload.get("started_at") or utc_now()),
                        estimated_tokens=estimated,
                    )
                    self._mechanical_request_keys.add(key)
                    self.store.append("MECHANICAL_SUBTASK_LEASE_REATTACHED", {
                        "mechanical_job_id": logical_job_id,
                        "parent_job_id": key[0],
                        "parent_task_id": state.parent_task_id,
                        "parent_role": state.parent_role,
                        "subtask_id": key[1],
                        "attempt": latest_attempt,
                        "receipt_path": str(receipt_path),
                        "recovery_deadline_epoch": recovery_deadline,
                        "action": (
                            "observe the surviving exact runner attempt; do not duplicate dispatch"
                        ),
                    })
                    continue
                state.telemetry_unknown += 1

            last_finished = (
                finished_attempts[max(finished_attempts)]
                if finished_attempts else None
            )
            if last_finished is not None:
                last_execution = MechanicalExecution(
                    status=str(last_finished.get("status") or "TOOL_ERROR"),
                    result=(
                        dict(last_finished.get("result"))
                        if isinstance(last_finished.get("result"), dict) else {}
                    ),
                    model=(
                        str(last_finished["model"])
                        if last_finished.get("model") else None
                    ),
                    reasoning_effort=(
                        str(last_finished["reasoning_effort"])
                        if last_finished.get("reasoning_effort") else None
                    ),
                    actual_model=(
                        str(last_finished["actual_model"])
                        if last_finished.get("actual_model") else None
                    ),
                    actual_reasoning_effort=(
                        str(last_finished["actual_reasoning_effort"])
                        if last_finished.get("actual_reasoning_effort") else None
                    ),
                    model_route_attestation=str(
                        last_finished.get("model_route_attestation")
                        or "unobservable"
                    ),
                    token_usage=TokenUsage(**(last_finished.get("token_usage") or {})),
                    token_telemetry=str(
                        last_finished.get("token_telemetry") or "unknown"
                    ),
                    cost_usd=(
                        float(last_finished["cost_usd"])
                        if last_finished.get("cost_usd") is not None else None
                    ),
                    cost_telemetry=str(
                        last_finished.get("cost_telemetry") or "unknown"
                    ),
                    artifacts=list(last_finished.get("artifacts") or []),
                    runner_directory=(
                        str(last_finished["runner_directory"])
                        if last_finished.get("runner_directory") else None
                    ),
                    fallback=(
                        dict(last_finished["fallback"])
                        if isinstance(last_finished.get("fallback"), dict) else None
                    ),
                    error=(
                        str(last_finished["error"])
                        if last_finished.get("error") else None
                    ),
                    failure_kind=(
                        str(last_finished["failure_kind"])
                        if last_finished.get("failure_kind") else None
                    ),
                    retryable=bool(last_finished.get("retryable", False)),
                    unavailable_routes=list(last_finished.get("unavailable_routes") or []),
                )
                primary_fallback_continuation = (
                    last_execution.failure_kind in MECHANICAL_FALLBACK_FAILURE_KINDS
                    and last_execution.model == self.mechanical_primary_route["model"]
                    and isinstance(last_execution.fallback, dict)
                    and last_execution.fallback.get("continuation_required") is True
                    and not any(
                        attempt > max(finished_attempts)
                        for attempt in started_attempts
                    )
                )
                retry_class = (
                    "model_protocol"
                    if last_execution.failure_kind
                    in {"model_output_protocol", "runner_protocol"}
                    else "transient"
                )
                worker_policy = self.config.raw["policy"].get(
                    "one_shot_compute_worker", {}
                )
                max_retries = int(worker_policy.get(
                    "model_protocol_max_retries"
                    if retry_class == "model_protocol"
                    else "transient_max_retries",
                    1,
                ))
                failures_in_class = finished_retryable_counts.get(retry_class, 0)
                retries_used = max(
                    state.retry_counts.get(retry_class, 0),
                    max(0, failures_in_class - 1),
                )
                state.retry_counts[retry_class] = retries_used
                retry_already_queued = bool(
                    last_execution.retryable
                    and failures_in_class > 0
                    and retries_used >= failures_in_class
                )
                can_queue_new_retry = bool(
                    last_execution.retryable and retries_used < max_retries
                )
                should_retry = (
                    primary_fallback_continuation
                    or retry_already_queued
                    or can_queue_new_retry
                )
                if can_queue_new_retry and not retry_already_queued:
                    retries_used += 1
                    state.retry_counts[retry_class] = retries_used
                    self.store.append("MECHANICAL_SUBTASK_RETRY_QUEUED", {
                        "mechanical_job_id": last_finished.get("mechanical_job_id"),
                        "parent_job_id": key[0],
                        "subtask_id": key[1],
                        "failure_kind": last_execution.failure_kind,
                        "retry_class": retry_class,
                        "retry": retries_used,
                        "max_retries": max_retries,
                        "source": "recovery_reconciliation",
                        "action": (
                            "same fixed route policy; transient failure is not cached unavailable"
                        ),
                    })
                if not should_retry:
                    response = self._mechanical_response(
                        state,
                        status=last_execution.status,
                        execution=last_execution,
                        error=last_execution.error,
                        failure_kind=last_execution.failure_kind,
                        retryable=False,
                    )
                    self._persist_mechanical_terminal(
                        state, response, execution=last_execution,
                    )
                    self.store.append("MECHANICAL_RECOVERY_TERMINAL_RECONCILED", {
                        "parent_job_id": key[0],
                        "subtask_id": key[1],
                        "attempts_started": state.attempts_started,
                        "status": response["status"],
                        "failure_kind": response["failure_kind"],
                        "action": "reconstructed terminal response from durable attempt event",
                    })
                    continue
            elif state.attempts_started:
                worker_policy = self.config.raw["policy"].get(
                    "one_shot_compute_worker", {}
                )
                maximum_retry_budget = max(
                    int(worker_policy.get("transient_max_retries", 1)),
                    int(worker_policy.get("model_protocol_max_retries", 1)),
                )
                if state.attempts_started >= 1 + maximum_retry_budget:
                    response = self._mechanical_response(
                        state,
                        status="TOOL_ERROR",
                        error=(
                            "controller restarted after a mechanical attempt began but "
                            "before an outcome was durably recorded; finite retry budget exhausted"
                        ),
                        failure_kind="mechanical_crash_unknown",
                        retryable=False,
                    )
                    self._persist_mechanical_terminal(state, response)
                    continue
            self._mechanical_request_keys.add(key)
            self.pending_mechanical.append(state)
            self.store.append("MECHANICAL_SUBTASK_RECOVERED", {
                "parent_job_id": key[0],
                "parent_task_id": state.parent_task_id,
                "parent_role": state.parent_role,
                "subtask_id": key[1],
                "request_sha256": state.request_sha256,
                "action": "idempotently requeue nonterminal controller-owned subtask",
            })
        self._request_director(
            "resume reconstructed pending lifecycle state",
            meaningful_change=True,
            immediate=True,
        )

    async def _interrupt_recovered_stale(self) -> None:
        if not self.stale_remote_turns:
            return
        interrupt_remote = getattr(self.backend, "interrupt_remote", None)
        if isinstance(self.backend, AppServerBackend):
            interrupt_remote = self.backend.client.interrupt
        if not callable(interrupt_remote):
            return
        for job_id, thread_id, turn_id in self.stale_remote_turns:
            try:
                await interrupt_remote(thread_id, turn_id)
                self.store.append("STALE_TURN_INTERRUPTED", {
                    "job_id": job_id, "thread_id": thread_id, "turn_id": turn_id,
                })
            except Exception as exc:
                self.store.append("STALE_TURN_INTERRUPT_FAILED", {
                    "job_id": job_id, "thread_id": thread_id, "turn_id": turn_id,
                    "error": _sanitize_live_text(exc),
                })

    def _poll_campaign_inputs(self) -> None:
        steering_path = self.campaign_store.root / "STEERING.jsonl"
        asset_path = self.campaign_store.root / "ASSETS.jsonl"
        for record in read_jsonl(steering_path):
            steering_id = str(record.get("steering_id") or "")
            key = f"steering:{steering_id}"
            if not steering_id or key in self._seen_campaign_inputs:
                continue
            kind = str(record.get("kind") or "")
            if kind not in {
                "NOTE", "PRIORITIZE_CLAIM", "PAUSE_ROUTE", "RESUME_ROUTE",
                "REQUEST_AUDIT", "STOP_AFTER_EPOCH",
            }:
                raise ValueError(f"campaign contains unsupported steering kind: {kind}")
            claim_id = str(record.get("claim_id") or "")
            route_id = str(record.get("route_id") or "")
            if kind == "PRIORITIZE_CLAIM":
                if claim_id not in self.graph.claims:
                    raise ValueError(f"steering refers to unknown claim: {claim_id}")
                claim = self.graph.claims[claim_id]
                claim.priority["score"] = max(1.0, float(claim.priority.get("score", 0.0)))
                claim.priority["human_steering_id"] = steering_id
                self._commit_claim_state_transition(
                    transition_kind="HUMAN_PRIORITY_UPDATE",
                    authorization={
                        "steering_id": steering_id,
                        "claim_id": claim_id,
                        "trust_upgrade": False,
                    },
                )
            elif kind == "PAUSE_ROUTE":
                self.route_ledger.append(
                    route_id=route_id,
                    representation_id="HUMAN_STEERING",
                    method_tags=["human-steering"], status="PAUSED",
                    failure_class=None, retry_condition=f"resume:{route_id}",
                    evidence_refs=[], source=f"human:{steering_id}",
                )
                self._defer_nonretryable_pending_routes(
                    source=f"human_steering:{steering_id}",
                )
            elif kind == "RESUME_ROUTE":
                self.satisfied_route_conditions.add(f"resume:{route_id}")
                self.route_ledger.append(
                    route_id=route_id,
                    representation_id="HUMAN_STEERING",
                    method_tags=["human-steering"], status="ACTIVE",
                    failure_class=None, retry_condition=None,
                    evidence_refs=[], source=f"human:{steering_id}",
                )
            elif kind == "REQUEST_AUDIT" and claim_id:
                for state in self.audit_gate.states.values():
                    if state.event.claim_id == claim_id and not state.terminal:
                        self._queue_next_audit(state.event)
            elif kind == "STOP_AFTER_EPOCH":
                self._stop_after_epoch = True
            self.director_constraints.append({
                "action": "HUMAN_STEERING",
                "steering_id": steering_id,
                "kind": kind,
                "claim_id": claim_id or None,
                "route_id": route_id or None,
                "note": str(record.get("note") or ""),
            })
            self.store.append("HUMAN_STEERING_INGESTED", {
                **record, "authority": "planning_input_only",
            })
            self.campaign_store.mark_input_applied(key, epoch_id=self.epoch_id)
            self._seen_campaign_inputs.add(key)
            self._request_director(
                f"human steering {steering_id} ingested",
                meaningful_change=True,
            )
        for record in read_jsonl(asset_path):
            asset_id = str(record.get("asset_id") or "")
            key = f"asset:{asset_id}"
            if not asset_id or key in self._seen_campaign_inputs:
                continue
            self.satisfied_route_conditions.update({
                asset_id, str(record.get("sha256") or ""),
            })
            self.director_constraints.append({
                "action": "EXTERNAL_ASSET_AVAILABLE",
                "asset_id": asset_id,
                "uri": record.get("uri"),
                "sha256": record.get("sha256"),
                "source_description": record.get("source_description"),
                "authority": "research_input_only",
            })
            self.store.append("RESEARCH_ASSET_INGESTED", {
                **record, "authority": "research_input_only",
            })
            self.campaign_store.mark_input_applied(key, epoch_id=self.epoch_id)
            self._seen_campaign_inputs.add(key)
            self._request_director(
                f"external asset {asset_id} ingested",
                meaningful_change=True,
            )

    def _hydrate_applied_campaign_inputs(self) -> None:
        """Expose prior epoch inputs without replaying their side effects."""
        self._seen_campaign_inputs = self.campaign_store.applied_inputs()
        for record in read_jsonl(self.campaign_store.root / "STEERING.jsonl"):
            steering_id = str(record.get("steering_id") or "")
            if f"steering:{steering_id}" not in self._seen_campaign_inputs:
                continue
            self.director_constraints.append({
                "action": "HUMAN_STEERING",
                "steering_id": steering_id,
                "kind": record.get("kind"),
                "claim_id": record.get("claim_id"),
                "route_id": record.get("route_id"),
                "note": record.get("note"),
                "historical_campaign_input": True,
            })
        for record in read_jsonl(self.campaign_store.root / "ASSETS.jsonl"):
            asset_id = str(record.get("asset_id") or "")
            if f"asset:{asset_id}" not in self._seen_campaign_inputs:
                continue
            self.satisfied_route_conditions.update({
                asset_id, str(record.get("sha256") or ""),
            })
            self.director_constraints.append({
                "action": "EXTERNAL_ASSET_AVAILABLE",
                "asset_id": asset_id,
                "uri": record.get("uri"),
                "sha256": record.get("sha256"),
                "source_description": record.get("source_description"),
                "authority": "research_input_only",
                "historical_campaign_input": True,
            })

    def _checkpoint_claim_state_provenance(
        self, claim_graph_sha256: str,
    ) -> dict[str, Any]:
        if not self.persist_shared_state:
            return {
                "schema_version": 1,
                "planning_context_sha256": self._canonical_state[
                    "planning_context_sha256"
                ],
            }
        live_graph_sha256 = file_digest(self.layout.claim_graph_path)
        if live_graph_sha256 != claim_graph_sha256:
            raise ValueError(
                "checkpoint ClaimGraph bytes differ from controller state"
            )
        trusted_bytes = self.layout.trusted_state_path.read_bytes()
        trusted_sha256 = bytes_sha256(trusted_bytes)
        trusted = json.loads(trusted_bytes)
        if not isinstance(trusted, dict):
            raise ValueError("checkpoint trusted state is not a JSON object")
        bound_graph_sha256 = str(trusted.get("claim_graph_sha256") or "")
        if bound_graph_sha256 and bound_graph_sha256 != claim_graph_sha256:
            raise ValueError(
                "checkpoint trusted state is not bound to the controller ClaimGraph"
            )
        transition_id = str(trusted.get("last_transition_id") or "") or None
        committed = self.canonical_transitions.verified_committed_records()
        if committed and str(committed[-1].get("transition_id") or "") != transition_id:
            raise ValueError(
                "checkpoint trusted state is not bound to the latest canonical transition"
            )
        if not bound_graph_sha256 and (
            committed
            or claim_graph_sha256 != self._canonical_state["claim_graph"]["sha256"]
            or trusted_sha256 != self._canonical_state["trusted_state"]["sha256"]
        ):
            raise ValueError(
                "changed checkpoint claim state lacks an audited transition binding"
            )
        return {
            "schema_version": 2,
            "planning_context_sha256": planning_context_fingerprint(
                self._canonical_state,
                claim_graph_sha256=claim_graph_sha256,
                trusted_state_sha256=trusted_sha256,
            ),
            "claim_graph_sha256": claim_graph_sha256,
            "trusted_state_sha256": trusted_sha256,
            "canonical_transition_id": transition_id,
        }

    def _legacy_checkpoint_planning_context(
        self,
        *,
        snapshot: dict[str, Any],
        previous_run: Path,
        previous_state: dict[str, Any],
    ) -> tuple[str, str] | None:
        """Rebind a v1 checkpoint only to its own last committed transition."""
        provenance = snapshot.get("snapshot_provenance") or {}
        canonical_view = snapshot.get("canonical_state") or {}
        if (
            int(provenance.get("schema_version", 0)) != 1
            or canonical_view.get("canonical_state_sha256")
            != previous_state.get("canonical_state_sha256")
        ):
            return None
        graph_sha256 = str(canonical_view.get("claim_graph_sha256") or "")
        if not graph_sha256 or graph_sha256 != str(
            self._canonical_state["claim_graph"]["sha256"]
        ):
            return None
        trusted_path = self.layout.trusted_state_path
        trusted_bytes = trusted_path.read_bytes()
        trusted_sha256 = bytes_sha256(trusted_bytes)
        if trusted_sha256 != str(
            self._canonical_state["trusted_state"]["sha256"]
        ):
            return None
        trusted = json.loads(trusted_bytes)
        if not isinstance(trusted, dict) or str(
            trusted.get("claim_graph_sha256") or ""
        ) != graph_sha256:
            return None
        transition_id = str(trusted.get("last_transition_id") or "")
        if not transition_id:
            return None
        event_watermark = int(provenance.get("event_watermark", 0))
        transition_events = [
            event for event in read_jsonl(previous_run / "EVENTS.jsonl")
            if event.get("kind") == "CANONICAL_TRANSITION_COMMITTED"
            and int(event.get("sequence", 0)) <= event_watermark
        ]
        if not transition_events:
            return None
        last_transition = transition_events[-1].get("payload") or {}
        if (
            str(last_transition.get("transition_id") or "") != transition_id
            or str(last_transition.get("claim_graph_sha256") or "")
            != graph_sha256
        ):
            return None
        committed = self.canonical_transitions.verified_committed_records()
        if not committed or str(
            committed[-1].get("transition_id") or ""
        ) != transition_id:
            return None
        return (
            planning_context_fingerprint(
                previous_state,
                claim_graph_sha256=graph_sha256,
                trusted_state_sha256=trusted_sha256,
            ),
            transition_id,
        )

    def _import_previous_epoch_checkpoint(self) -> None:
        """Import only durable, nonterminal frontier state into a new epoch.

        This is intentionally distinct from ``--resume``: the previous epoch
        remains immutable and no remote turn is resumed.  Portable artifact
        URIs continue to point at its sealed content-addressed bundles.
        """
        if self.previous_epoch_id is None:
            return
        previous_run = self.layout.run_dir(self.previous_epoch_id)
        manifest_path = previous_run / "RUN_MANIFEST.json"
        snapshot_path = previous_run / "state" / "compact_snapshot.json"
        if not manifest_path.is_file() or not snapshot_path.is_file():
            raise ValueError("previous epoch checkpoint is incomplete")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        campaign = manifest.get("campaign") or {}
        if (
            campaign.get("campaign_id") != self.campaign_id
            or campaign.get("epoch_id") != self.previous_epoch_id
        ):
            raise ValueError("previous epoch checkpoint belongs to another campaign")
        compact_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(compact_snapshot, dict):
            raise ValueError("previous epoch compact snapshot is invalid")
        snapshot = load_full_context_archive(snapshot_path, compact_snapshot)
        previous_canonical = snapshot.get("canonical_state")
        if not isinstance(previous_canonical, dict):
            raise ValueError(
                "previous epoch planning mirror lacks canonical-state provenance"
            )
        previous_state_path = previous_run / "state" / "canonical_state.json"
        previous_state = load_canonical_state(
            previous_state_path, epoch_id=self.previous_epoch_id,
        )
        validate_canonical_state(previous_state, run_dir=previous_run)
        provenance = snapshot.get("snapshot_provenance") or {}
        provenance_schema = int(provenance.get("schema_version", 0))
        previous_context = str(
            provenance.get("planning_context_sha256")
            or previous_canonical.get("planning_context_sha256")
            or ""
        )
        if provenance_schema >= 2:
            graph_sha256 = str(provenance.get("claim_graph_sha256") or "")
            trusted_sha256 = str(provenance.get("trusted_state_sha256") or "")
            transition_id = str(
                provenance.get("canonical_transition_id") or ""
            ) or None
            if (
                not graph_sha256
                or not trusted_sha256
                or graph_sha256
                != str(previous_canonical.get("claim_graph_sha256") or "")
                or previous_canonical.get("canonical_transition_id")
                != transition_id
                or planning_context_fingerprint(
                    previous_state,
                    claim_graph_sha256=graph_sha256,
                    trusted_state_sha256=trusted_sha256,
                ) != previous_context
            ):
                raise ValueError(
                    "previous epoch checkpoint canonical provenance is invalid"
                )
            if transition_id is None:
                if (
                    graph_sha256 != previous_state["claim_graph"]["sha256"]
                    or trusted_sha256
                    != previous_state["trusted_state"]["sha256"]
                ):
                    raise ValueError(
                        "previous epoch checkpoint lacks its canonical transition id"
                    )
            else:
                try:
                    prepared = self.canonical_transitions.verified_prepared_record(
                        transition_id,
                    )
                except ValueError as exc:
                    raise ValueError(
                        "previous epoch checkpoint transition binding is invalid"
                    ) from exc
                expected_targets = {
                    "project://" + self.layout.claim_graph_path.resolve()
                    .relative_to(self.config.project_root.resolve()).as_posix():
                    graph_sha256,
                    "project://" + self.layout.trusted_state_path.resolve()
                    .relative_to(self.config.project_root.resolve()).as_posix():
                    trusted_sha256,
                }
                observed_targets = {
                    str(target.get("path") or ""): str(
                        target.get("after_sha256") or ""
                    )
                    for target in (prepared or {}).get("targets") or []
                }
                if any(
                    observed_targets.get(path) != digest
                    for path, digest in expected_targets.items()
                ):
                    raise ValueError(
                        "previous epoch checkpoint transition binding is invalid"
                    )
        current_context = str(
            (self._canonical_state or {}).get("planning_context_sha256") or ""
        )
        if not previous_context or not current_context:
            raise ValueError("canonical planning-context provenance is incomplete")
        if previous_context != current_context and provenance_schema == 1:
            reconciled = self._legacy_checkpoint_planning_context(
                snapshot=snapshot,
                previous_run=previous_run,
                previous_state=previous_state,
            )
            if reconciled is not None and reconciled[0] == current_context:
                previous_context = reconciled[0]
                self.store.append(
                    "LEGACY_CHECKPOINT_CANONICAL_PROVENANCE_RECONCILED",
                    {
                        "source_epoch_id": self.previous_epoch_id,
                        "canonical_transition_id": reconciled[1],
                        "planning_context_sha256": reconciled[0],
                        "action": (
                            "accepted the sealed v1 checkpoint at its own last "
                            "audited canonical transition"
                        ),
                    },
                )
        if previous_context != current_context:
            unsafe_audit_state = bool(
                snapshot.get("candidate_audit_frontier")
                or snapshot.get("pending_audits")
                or any(
                    item.get("status") in {"PENDING", "ACTIVE", "RETRY_WAIT"}
                    for item in self.audit_leases.snapshot()
                )
            )
            if unsafe_audit_state:
                raise ValueError(
                    "canonical state drifted while an audit frontier remains; "
                    "automatic synchronization is unsafe"
                )
            self.store.append("STALE_PLANNING_STATE_DISCARDED", {
                "source_epoch_id": self.previous_epoch_id,
                "previous_planning_context_sha256": previous_context,
                "current_planning_context_sha256": current_context,
                "discarded_pending_research": len(
                    snapshot.get("pending_research") or []
                ),
                "discarded_recent_changes": len(
                    snapshot.get("recent_changes") or []
                ),
                "action": (
                    "rebuild derived planning state and launch a fresh Director "
                    "from the current ClaimGraph and startup-frozen context"
                ),
            })
            self._request_director(
                "startup canonical state superseded the previous planning mirror",
                meaningful_change=True,
                immediate=True,
            )
            return

        self._checkpoint_planning_mirror = {
            "previous_state_available": True,
            "previous_planning_context_sha256": previous_context,
            "drift_detected": False,
            "disposition": "reuse_after_checkpoint_integrity_checks",
        }

        recovered_active_leases = self.audit_leases.recover_stale_active()
        if recovered_active_leases:
            self.store.append("EPOCH_AUDIT_LEASES_RECOVERED", {
                "source_epoch_id": self.previous_epoch_id,
                "count": recovered_active_leases,
                "action": "ACTIVE leases became RETRY_WAIT before fresh dispatch",
            })

        if self.mock:
            previous_graph = previous_run / "state" / "claim_graph.json"
            if previous_graph.is_file():
                imported_graph = ClaimGraph.load(
                    previous_graph, semantics=self.domain_semantics,
                )
                imported_graph.validate()
                imported_graph.path = self.graph.path
                self.graph = imported_graph
                self.graph.set_terminal_transition_guard(
                    self._guard_graph_terminal_transition
                )
                self.graph.save()

        imported_research = 0
        pending_ids = {item.task_id for item in self.pending_research}
        imported_continuation_ids: set[str] = set()
        continuation_ids_raw = snapshot.get("deferred_research_continuation_ids") or []
        if (
            not isinstance(continuation_ids_raw, list)
            or not all(isinstance(item, str) and item for item in continuation_ids_raw)
            or len(continuation_ids_raw) != len(set(continuation_ids_raw))
        ):
            raise ValueError("previous epoch continuation id index is invalid")
        continuation_ids = set(continuation_ids_raw)
        continuation_index_raw = snapshot.get("research_continuation_checkpoints") or []
        if not isinstance(continuation_index_raw, list):
            raise ValueError("previous epoch continuation checkpoint index is invalid")
        continuation_index: dict[str, dict[str, str]] = {}
        for item in continuation_index_raw:
            if not isinstance(item, dict) or set(item) != {"task_id", "uri", "sha256"}:
                raise ValueError("previous epoch continuation checkpoint index is invalid")
            task_id = str(item["task_id"])
            if not task_id or task_id in continuation_index:
                raise ValueError("previous epoch continuation checkpoint index is invalid")
            continuation_index[task_id] = {
                "uri": str(item["uri"]), "sha256": str(item["sha256"]),
            }
        if set(continuation_index) != continuation_ids:
            raise ValueError("previous epoch continuation indexes disagree")
        for raw in snapshot.get("pending_research") or []:
            if not isinstance(raw, dict):
                raise ValueError("previous epoch pending research task is invalid")
            try:
                task = ResearchTask.from_dict(raw)
            except Exception as exc:
                raise ValueError(
                    "previous epoch pending research task is invalid: "
                    f"{_sanitize_live_text(exc)}"
                ) from exc
            if task.task_id in pending_ids:
                if task.task_id in continuation_ids:
                    raise ValueError(
                        "previous epoch continuation task id collides with pending work"
                    )
                continue
            checkpoint_uri: str | None = None
            checkpoint: dict[str, Any] | None = None
            if task.task_id in continuation_ids:
                checkpoint_entry = continuation_index[task.task_id]
                checkpoint_uri, checkpoint = self._validate_research_continuation_checkpoint(
                    task,
                    source_epoch_id=self.previous_epoch_id,
                    expected_uri=checkpoint_entry["uri"],
                    expected_sha256=checkpoint_entry["sha256"],
                )
                self.satisfied_route_conditions.add(
                    f"next_epoch:{self.previous_epoch_id}"
                )
                imported_continuation_ids.add(task.task_id)
            validation_error = self._validate_director_task(task, check_route=False)
            if validation_error:
                raise ValueError(
                    "previous epoch pending research task is invalid: "
                    f"{task.task_id}: {validation_error}"
                )
            if not self.route_ledger.route_is_retryable(
                task.route_family, self.satisfied_route_conditions,
            ):
                self._record_task_deferred_by_route_policy(
                    task,
                    source=f"epoch:{self.previous_epoch_id}",
                    checkpoint_uri=checkpoint_uri,
                )
                continue
            if checkpoint_uri is not None and checkpoint is not None:
                self.route_ledger.append(
                    route_id=task.route_family,
                    representation_id=task.representation_id,
                    method_tags=[task.role, "controller-continuation"],
                    status="ACTIVE",
                    failure_class=None,
                    retry_condition=None,
                    evidence_refs=[checkpoint_uri],
                    source=f"epoch:{self.previous_epoch_id}",
                )
                self.store.append("RESEARCH_CONTINUATION_IMPORTED", {
                    "source_epoch_id": self.previous_epoch_id,
                    "source_job_id": checkpoint["source_job_id"],
                    "source_task_id": checkpoint["source_task_id"],
                    "task_id": task.task_id,
                    "claim_id": task.target_claim,
                    "checkpoint_uri": checkpoint_uri,
                    "action": "admit as fresh-epoch work; prior output remains noncanonical",
                })
            self.pending_research.append(task)
            pending_ids.add(task.task_id)
            self.seen_task_fingerprints.add(task.fingerprint)
            self.task_fingerprints_by_id[task.task_id] = task.fingerprint
            self.store.append("TASK_ACCEPTED", {
                "task_id": task.task_id,
                "fingerprint": task.fingerprint,
                "representation_id": task.representation_id,
                "task": task.to_dict(),
                "source": f"epoch://{self.previous_epoch_id}/state/compact_snapshot.json",
            })
            imported_research += 1

        # Schema-v10 releases before TASK_RETAINED_FOR_NEXT_EPOCH wrote active
        # research jobs only under active_tasks and then removed them while
        # cancelling during shutdown.  The snapshot therefore omitted work
        # cancelled by an operator or dead transport.  Recover only the latest
        # cancelled terminal attempt of an accepted research task, and only
        # from a PAUSED epoch; successful or completed-campaign jobs stay
        # terminal.
        previous_events = read_jsonl(previous_run / "EVENTS.jsonl")
        stopped = next(
            (
                event.get("payload") or {}
                for event in reversed(previous_events)
                if event.get("kind") == "RUN_STOPPED"
            ),
            {},
        )
        if stopped.get("campaign_status") == "PAUSED":
            accepted_from_events: dict[str, ResearchTask] = {}
            started_by_job: dict[str, dict[str, Any]] = {}
            latest_terminal_by_task: dict[
                str, tuple[int, str, dict[str, Any]]
            ] = {}
            for event in previous_events:
                payload = event.get("payload") or {}
                kind = str(event.get("kind") or "")
                if kind == "TASK_ACCEPTED" and isinstance(payload.get("task"), dict):
                    recovered = ResearchTask.from_dict(payload["task"])
                    accepted_from_events[recovered.task_id] = recovered
                elif kind == "JOB_STARTED" and payload.get("job_id"):
                    started_by_job[str(payload["job_id"])] = dict(payload)
                elif kind in {"JOB_COMPLETED", "JOB_CANCELLED"}:
                    job_id = str(payload.get("job_id") or "")
                    started_payload = started_by_job.get(job_id, {})
                    task_id = str(
                        payload.get("task_id") or started_payload.get("task_id") or ""
                    )
                    if task_id:
                        latest_terminal_by_task[task_id] = (
                            int(event.get("sequence", 0)), kind, dict(payload),
                        )
            for task_id, (_sequence, kind, terminal) in sorted(
                latest_terminal_by_task.items()
            ):
                task = accepted_from_events.get(task_id)
                started_payload = started_by_job.get(
                    str(terminal.get("job_id") or ""), {}
                )
                role = str(terminal.get("role") or started_payload.get("role") or "")
                if (
                    kind != "JOB_CANCELLED"
                    or role not in {Role.PROVER, Role.FALSIFIER, Role.EXPLORER}
                    or task is None
                    or task_id in pending_ids
                    or terminal.get("exit_reason") != stopped.get("reason")
                ):
                    continue
                validation_error = self._validate_director_task(
                    task, check_route=False,
                )
                if validation_error:
                    raise ValueError(
                        "legacy cancelled research task is invalid: "
                        f"{task.task_id}: {validation_error}"
                    )
                if not self.route_ledger.route_is_retryable(
                    task.route_family, self.satisfied_route_conditions,
                ):
                    self._record_task_deferred_by_route_policy(
                        task,
                        source=f"legacy-events:{self.previous_epoch_id}",
                    )
                    continue
                self.pending_research.append(task)
                pending_ids.add(task.task_id)
                self.seen_task_fingerprints.add(task.fingerprint)
                self.task_fingerprints_by_id[task.task_id] = task.fingerprint
                self.store.append("TASK_ACCEPTED", {
                    "task_id": task.task_id,
                    "fingerprint": task.fingerprint,
                    "representation_id": task.representation_id,
                    "task": task.to_dict(),
                    "source": f"epoch://{self.previous_epoch_id}/EVENTS.jsonl",
                })
                self.store.append("LEGACY_CANCELLED_TASK_IMPORTED", {
                    "source_epoch_id": self.previous_epoch_id,
                    "source_job_id": terminal.get("job_id"),
                    "task_id": task.task_id,
                    "claim_id": task.target_claim,
                    "action": (
                        "restore a pre-retention cancelled active task without "
                        "claiming progress or failure"
                    ),
                })
                imported_research += 1

        missing_continuations = continuation_ids - imported_continuation_ids
        if missing_continuations:
            raise ValueError(
                "previous epoch continuation index refers to missing tasks: "
                f"{sorted(missing_continuations)}"
            )

        imported_candidates = 0
        for raw in snapshot.get("candidate_audit_frontier") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("event"), dict):
                raise ValueError("previous epoch candidate frontier is invalid")
            event_payload = dict(raw["event"])
            expected_fingerprint = str(event_payload.pop("fingerprint", ""))
            event = CandidateEvent.from_dict(event_payload)
            if expected_fingerprint and event.fingerprint != expected_fingerprint:
                raise ValueError("previous epoch candidate fingerprint changed")
            hashes = dict(raw.get("artifact_hashes") or {})
            self._verify_domain_evidence_receipts(
                event, extend_artifacts=False,
            )
            artifacts_ok, observed = self.artifact_store.verify(hashes)
            if not artifacts_ok:
                raise ValueError(
                    "previous epoch candidate bundle is unavailable or changed: "
                    f"{event.fingerprint}; observed={observed}"
                )
            self.candidate_artifact_hashes[event.fingerprint] = hashes
            semantic_hashes = self._bind_candidate_semantic_evidence(event, hashes)
            if raw.get("semantic_evidence_hashes", {}) != semantic_hashes:
                raise ValueError(
                    "previous epoch candidate semantic evidence binding changed"
                )
            state = self.audit_gate.register(event)
            for result_raw in raw.get("audit_results") or []:
                if not isinstance(result_raw, dict):
                    raise ValueError("previous epoch audit result is invalid")
                self._restore_audit_result(
                    event, AuditResult.from_dict(result_raw),
                )
            self.satisfied_route_conditions.update({
                event.fingerprint, f"new_evidence:{event.claim_id}",
            })
            if not state.terminal:
                self._queue_next_audit(event)
            imported_candidates += 1

        self.store.append("EPOCH_CHECKPOINT_IMPORTED", {
            "campaign_id": self.campaign_id,
            "source_epoch_id": self.previous_epoch_id,
            "research_tasks": imported_research,
            "candidate_frontier": imported_candidates,
            "active_audit_leases_recovered": recovered_active_leases,
            "source": f"epoch://{self.previous_epoch_id}/state/compact_snapshot.json",
            "action": "append-only import into a fresh epoch",
        })
        if imported_research or imported_candidates:
            self._request_director(
                "previous epoch frontier imported",
                meaningful_change=True,
                immediate=True,
            )

    def _attempt_failure_result(self, reason: str) -> RunResult:
        self._internal_failure = True
        report_path = self.layout.nightly_root / self.run_id / "NIGHTLY_REPORT.md"
        outcome_path = self.layout.outcomes_root / self.run_id / "OUTCOME.md"
        try:
            campaign_status = self.campaign_store.load().status
        except (OSError, ValueError, json.JSONDecodeError):
            campaign_status = "PAUSED"
        payload = {
            "attempt_id": self._attempt_id,
            "reason": reason,
            "internal_failure": True,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "recovery_completed": self._recovery_completed,
            "campaign_status": campaign_status,
            "report": str(report_path) if report_path.is_file() else None,
            "outcome": str(outcome_path) if outcome_path.is_file() else None,
            "artifacts_finalized": False,
            "action": "epoch remains unsealed and resumable",
        }
        self.live_store.append("LIVE_ATTEMPT_FAILED", payload)
        self.store.append("ATTEMPT_FAILED", payload)
        events = self.store.replay()
        lifecycle = job_lifecycle_metrics(events)
        mechanical = mechanical_lifecycle_metrics(events)
        return RunResult(
            run_id=self.run_id,
            report_path=report_path,
            stopped_reason=reason,
            job_count=lifecycle.jobs_terminal,
            event_count=len(events),
            jobs_started=lifecycle.jobs_started,
            jobs_completed=lifecycle.jobs_completed,
            jobs_cancelled=lifecycle.jobs_cancelled,
            jobs_terminal=lifecycle.jobs_terminal,
            mechanical_subtasks_requested=mechanical.requested,
            mechanical_attempts_started=mechanical.attempts_started,
            mechanical_subtasks_terminal=mechanical.terminal,
            internal_failure=True,
            run_mode="dry-run" if self._dry_run else ("mock" if self.mock else "real"),
            outcome_path=outcome_path if outcome_path.is_file() else None,
            campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            campaign_status=campaign_status,
        )

    def _reconcile_legacy_failed_resume(self) -> None:
        records = [
            event for event in self.store.replay()
            if int(event.get("sequence", 0)) < self._attempt_started_sequence
        ]
        wrong_stops = [
            event for event in records
            if event.get("kind") == "RUN_STOPPED"
            and str((event.get("payload") or {}).get("campaign_id") or "")
            not in {"", self.campaign_id}
        ]
        if not wrong_stops:
            return
        stop = wrong_stops[-1]
        stop_sequence = int(stop.get("sequence", 0))
        pin_failures = [
            event for event in records
            if event.get("kind") == "RUN_INPUT_PINNING_FAILED"
            and int(event.get("sequence", 0)) < stop_sequence
        ]
        if not pin_failures:
            raise ValueError("mismatched historical campaign stop has no pinning failure")
        pin_sequence = int(pin_failures[-1].get("sequence", 0))
        if any(
            event.get("kind") == "RECOVERY_COMPLETED"
            and pin_sequence < int(event.get("sequence", 0)) < stop_sequence
            for event in records
        ):
            raise ValueError("mismatched historical campaign stop occurred after recovery")
        ghost_id = str((stop.get("payload") or {})["campaign_id"])
        ghost = CampaignStore(self.layout.autonomous_root, ghost_id)
        if not ghost.manifest_path.is_file():
            raise ValueError("mismatched historical campaign has no reconciliation ledger")
        ghost_checkpoint = ghost.load()
        ghost_events = ghost.events()
        if (
            ghost_checkpoint.project_id != self.config.project_name
            or ghost_checkpoint.epochs != (self.epoch_id,)
            or sum(item.get("kind") == "EPOCH_STARTED" for item in ghost_events) != 1
            or sum(item.get("kind") == "EPOCH_SEALED" for item in ghost_events) != 1
        ):
            raise ValueError("mismatched historical campaign is not a safe bootstrap ghost")
        ghost.mark_superseded(
            by_campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            reason="legacy resume constructed the controller before hydrating RUN_MANIFEST",
        )
        self.store.append("RESUME_METADATA_REBOUND", {
            "attempt_id": self._attempt_id,
            "from_campaign_id": ghost_id,
            "to_campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "pinning_failure_sequence": pin_sequence,
            "wrong_stop_sequence": stop_sequence,
            "action": "append-only reconciliation; historical events retained",
        })

    async def run(self, hours: float | None, dry_run: bool = False) -> RunResult:
        self._dry_run = bool(dry_run)
        mode = "dry-run" if dry_run else ("mock" if self.mock else "real")
        attempt = self.store.append("ATTEMPT_STARTED", {
            "attempt_id": self._attempt_id,
            "resume": self.resume,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "mode": mode,
        })
        self._attempt_started_sequence = int(attempt["sequence"])
        guard_path = self.run_dir / "canonical_guard.before.json"
        try:
            if self.resume:
                if not guard_path.is_file():
                    raise ValueError("cannot resume without the original canonical guard baseline")
                baseline = json.loads(guard_path.read_text(encoding="utf-8"))
                if not isinstance(baseline, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in baseline.items()
                ):
                    raise ValueError("canonical guard baseline is invalid")
                self.guard.baseline = dict(baseline)
                changed_before_recovery = self.guard.verify()
                if changed_before_recovery:
                    self.store.append("RESUME_CANONICAL_GUARD_FAILED", {
                        "changed": changed_before_recovery,
                        "action": "stopped before recovery or backend start",
                    })
                    return self._attempt_failure_result(
                        f"resume refused: canonical guard failed: {changed_before_recovery}"
                    )
            else:
                baseline = self.guard.snapshot()
                atomic_write_json(guard_path, baseline)
        except Exception as exc:
            return self._attempt_failure_result(
                f"bootstrap failed: canonical guard: {_sanitize_live_text(exc)[:500]}"
            )
        try:
            self._refresh_canonical_state()
        except Exception as exc:
            self.store.append("CANONICAL_STATE_REFRESH_FAILED", {
                "error": _sanitize_live_text(exc),
                "action": "stopped before recovery, backend start, or model turn",
            })
            return self._attempt_failure_result(
                "bootstrap failed: canonical-state refresh: "
                f"{_sanitize_live_text(exc)[:500]}"
            )
        try:
            deadline_epoch = self._pin_run_inputs(hours, dry_run)
        except Exception as exc:
            self.store.append("RUN_INPUT_PINNING_FAILED", {
                "error": _sanitize_live_text(exc),
                "action": "stopped before recovery, backend start, or model turn",
            })
            return self._attempt_failure_result(
                "bootstrap failed: run-input pinning: "
                f"{_sanitize_live_text(exc)[:500]}"
            )
        try:
            campaign = (
                self.campaign_store.load()
                if self.resume else self.campaign_store.create(
                    project_id=self.config.project_name,
                    campaign_hours=self.campaign_hours,
                    epoch_hours=self.epoch_hours,
                )
            )
            if campaign.project_id != self.config.project_name:
                raise ValueError("campaign belongs to a different project")
            if (
                abs(campaign.campaign_hours - self.campaign_hours) > 1e-9
                or abs(campaign.epoch_hours - self.epoch_hours) > 1e-9
            ):
                raise ValueError("campaign duration settings differ from the pinned campaign")
            if self.resume:
                self.campaign_store.require_unsealed_epoch(
                    epoch_id=self.epoch_id,
                    previous_epoch_id=self.previous_epoch_id,
                )
                self._campaign_recorded_started = True
                self._reconcile_legacy_failed_resume()
            else:
                if self.previous_epoch_id is not None:
                    self.campaign_store.require_current_continuation_source(
                        self.previous_epoch_id
                    )
                self.campaign_store.append_epoch_started(
                    epoch_id=self.epoch_id,
                    previous_epoch_id=self.previous_epoch_id,
                    mode=mode,
                )
                self._campaign_recorded_started = True
            self._hydrate_applied_campaign_inputs()
            if self.resume:
                self.recover()
                recovered_kinds = {
                    event.get("kind") for event in self.store.replay()
                    if int(event.get("sequence", 0)) < self._attempt_started_sequence
                }
                self._previous_epoch_checkpoint_imported = bool(
                    self.previous_epoch_id is None
                    or "EPOCH_CHECKPOINT_IMPORTED" in recovered_kinds
                )
                self._recovery_completed = True
                self.store.append("RECOVERY_COMPLETED", {
                    "attempt_id": self._attempt_id,
                    "campaign_id": self.campaign_id,
                    "epoch_id": self.epoch_id,
                    "event_watermark": self.store.replay()[-1]["sequence"],
                    "pending_research": len(self.pending_research),
                    "pending_audits": len(self.pending_audits),
                    "active_mechanical": len(self.active_mechanical),
                })
        except Exception as exc:
            return self._attempt_failure_result(
                f"bootstrap failed: recovery: {_sanitize_live_text(exc)[:500]}"
            )
        run_started_payload = {
            "hours": self._effective_run_hours,
            "requested_hours_override": hours,
            "deadline_epoch": deadline_epoch, "dry_run": dry_run,
            "execution_mode": "dry-run" if dry_run else ("mock" if self.mock else "real"),
            "resume": self.resume,
            "config": self._run_manifest["config"]["source"],
            "canonical_claim_graph": self._run_manifest["canonical_claim_graph"]["path"],
            "canonical_state": self._run_manifest["canonical_state"]["manifest"],
            "canonical_state_sha256": self._run_manifest["canonical_state"][
                "canonical_state_sha256"
            ],
            "planning_context_sha256": self._run_manifest["canonical_state"][
                "planning_context_sha256"
            ],
            "project_name": self.config.project_name,
            "final_conjecture_claim_id": self.final_conjecture_claim_id,
            "policy_manifest": self._run_manifest["research_policy"]["manifest"],
            "policy_manifest_sha256": self.policy_manifest["manifest_sha256"],
            "policy_source_drift": bool(self.policy_status and self.policy_status["source_drift"]),
            "max_director": self.max_director,
            "max_research_workers": self.max_research_workers,
            "max_audit": self.max_audit,
            "max_mechanical_subworkers": self.max_mechanical_subworkers,
            "global_budget": self.governor.global_budget,
            "global_cost_budget_usd": self.governor.global_cost_budget,
            "mechanical_budget": self.mechanical_governor.global_budget,
            "mechanical_cost_budget_usd": self.mechanical_governor.global_cost_budget,
            "mechanical_effective_resource_cap": self._mechanical_resource_capacity(),
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "previous_epoch_id": self.previous_epoch_id,
            "campaign_hours": self.campaign_hours,
            "epoch_hours": self.epoch_hours,
        }
        if not self.resume:
            self.store.append("RUN_STARTED", run_started_payload)
        self.live_store.append("LIVE_MONITOR_READY", {
            "schema_version": 1, "resume": self.resume,
            "captures": [
                "agent_message", "reasoning_summary", "plan",
                "item_lifecycle", "bounded_command_output",
            ],
            "raw_reasoning_captured": False,
        })
        self.store.append("RUN_POLICY_PINNED", {
            "manifest": self._run_manifest["research_policy"]["manifest"],
            "manifest_sha256": self.policy_manifest["manifest_sha256"],
            "stable_core": self.policy_manifest["stable_core"],
            "source_drift": bool(self.policy_status and self.policy_status["source_drift"]),
        })
        if self.policy_status and self.policy_status["source_drift"]:
            self.store.append("POLICY_SOURCE_DRIFT_DETECTED", self.policy_status)
        try:
            self._preflight_output_schemas()
        except OutputSchemaCompatibilityError as exc:
            self._internal_failure = True
            payload = {
                "error": _sanitize_live_text(exc),
                "issues": [
                    {
                        "schema_file": item.schema_path,
                        "json_path": item.json_path,
                        "reason": item.reason,
                    }
                    for item in exc.issues
                ],
                "action": "stopped before backend start or model turn",
            }
            self.store.append("SCHEMA_PREFLIGHT_FAILED", payload)
            self.live_store.append("SCHEMA_PREFLIGHT_FAILED", payload)
            names = sorted({Path(item.schema_path).name for item in exc.issues})
            return self._finish(
                f"bootstrap failed: invalid output schema ({', '.join(names)})",
                internal_failure=True,
            )
        self.store.append("SCHEMA_PREFLIGHT_PASSED", {
            "schemas": sorted(self._schema_cache),
            "backend": "mock" if self.mock else "app-server",
        })
        self.lifecycle.transition(
            LifecyclePhase.RUNNING,
            reason="bootstrap and local protocol preflight completed",
        )
        self.store.append("LIFECYCLE_TRANSITION", {
            "phase": self.lifecycle.phase,
            "reason": self.lifecycle.reason,
        })
        try:
            if not self._previous_epoch_checkpoint_imported:
                self._import_previous_epoch_checkpoint()
                self._previous_epoch_checkpoint_imported = True
        except Exception as exc:
            self.store.append("EPOCH_CHECKPOINT_IMPORT_FAILED", {
                "campaign_id": self.campaign_id,
                "source_epoch_id": self.previous_epoch_id,
                "error": _sanitize_live_text(exc),
                "action": "stopped before backend start or model turn",
            })
            return self._finish(
                f"bootstrap failed: epoch checkpoint import: "
                f"{_sanitize_live_text(exc)[:500]}",
                internal_failure=True,
            )
        self._poll_campaign_inputs()
        recovered_candidates: list[CandidateEvent] = []
        if self.recover_candidates_from:
            try:
                recovered_candidates = self._load_recoverable_candidates(
                    self.recover_candidates_from
                )
            except Exception as exc:
                self._internal_failure = True
                self.store.append("CANDIDATE_RECOVERY_PREFLIGHT_FAILED", {
                    "source_run_id": self.recover_candidates_from,
                    "error": _sanitize_live_text(exc),
                    "action": "stopped before backend start or model turn",
                })
                return self._finish(
                    f"bootstrap failed: candidate recovery: {_sanitize_live_text(exc)[:500]}",
                    internal_failure=True,
                )
            self.store.append("CANDIDATE_RECOVERY_PREFLIGHT_PASSED", {
                "source_run_id": self.recover_candidates_from,
                "candidate_count": len(recovered_candidates),
                "action": "validated without starting a model turn",
            })
        try:
            startup_snapshot = self._write_compact_snapshot()
        except Exception as exc:
            self.store.append("CANONICAL_STATE_SYNCHRONIZATION_FAILED", {
                "error": _sanitize_live_text(exc),
                "action": "stopped before backend start or model turn",
            })
            return self._finish(
                "bootstrap failed: canonical-state synchronization: "
                f"{_sanitize_live_text(exc)[:500]}",
                internal_failure=True,
            )
        self._bootstrap_completed = True
        self.store.append("CANONICAL_STATE_REFRESHED", {
            "canonical_state_sha256": self._canonical_state[
                "canonical_state_sha256"
            ],
            "planning_context_sha256": self._canonical_state[
                "planning_context_sha256"
            ],
            "planning_mirror": (
                self._checkpoint_planning_mirror
                or self._canonical_state["planning_mirror"]
            ),
            "snapshot": str(startup_snapshot),
            "canonical_files_modified": False,
        })
        if dry_run:
            self.store.append("DRY_RUN_VALIDATED", {
                "snapshot": str(startup_snapshot),
                "recoverable_candidates": len(recovered_candidates),
            })
            return self._finish("dry-run validation complete")

        if self.recover_candidates_from:
            self._register_recovered_candidates(
                self.recover_candidates_from, recovered_candidates,
            )

        if (
            self._begin_finalization_if_resolved("initial audited claim graph")
            and not self.stale_remote_turns
        ):
            self.store.append("FINALIZATION_COMPLETED", {
                "claim_id": self.final_conjecture_claim_id,
                "in_flight_jobs_completed": 0,
                "pending_research_preserved": len(self.pending_research),
                "pending_audits_preserved": len(self.pending_audits),
            })
            final_reason = self.scheduler_stop_reason or "final conjecture resolved"
            self._emit_scheduler_event_once(
                f"scheduler-stop:{final_reason}", "SCHEDULER_STOPPED",
                {"reason": final_reason},
            )
            return self._finish(final_reason)

        try:
            await self.backend.start()
            await self._interrupt_recovered_stale()
            probe = getattr(self.backend, "probe_capabilities", None)
            if isinstance(self.backend, AppServerBackend):
                probe = self.backend.client.probe_capabilities
            if callable(probe):
                self.capability_snapshot = await probe(self.config.project_root)
            if self.capability_snapshot is not None:
                atomic_write_json(self.run_dir / "app_server_capabilities.json", self.capability_snapshot)
                self.store.append("APP_SERVER_PROBED", {
                    key: value.get("supported") for key, value in self.capability_snapshot.items() if isinstance(value, dict)
                })
        except Exception as exc:
            self._internal_failure = True
            reason = (
                f"bootstrap failed: {type(exc).__name__}: "
                f"{_sanitize_live_text(exc)[:1000]}"
            )
            self.store.append("BOOTSTRAP_FAILED", {"error": reason})
            try:
                await self.backend.close()
            except Exception as close_exc:
                self.store.append("BACKEND_CLOSE_FAILED", {
                    "error": _sanitize_live_text(close_exc)[:1000],
                })
            return self._finish(reason, internal_failure=True)
        stopped_reason = "controller stopped without a terminal decision"
        try:
            while True:
                self._poll_campaign_inputs()
                await self._poll_filesystem_candidates()
                await self._process_candidate_queue()
                await self._collect_completed()
                await self._collect_mechanical_completed()
                await self._poll_mechanical_requests()
                await self._cancel_stale_jobs()
                self._record_backend_bindings()
                await self._handle_per_thread_budget_limits()
                self._flush_due_live_chunks()

                if self.scheduler_stop_reason:
                    if self._provider_transport_lost:
                        stopped_reason = self.scheduler_stop_reason
                        self._emit_scheduler_event_once(
                            f"provider-transport-containment:{stopped_reason}",
                            "PROVIDER_TRANSPORT_CONTAINMENT_STARTED",
                            {
                                "reason": stopped_reason,
                                "in_flight_jobs": len(self.active),
                                "in_flight_mechanical_subtasks": len(
                                    self.active_mechanical
                                ),
                                "action": (
                                    "cancel local owners without waiting for the dead "
                                    "transport; retain research for the next epoch"
                                ),
                            },
                        )
                        break
                    if self.active or self.active_mechanical:
                        in_flight = len(self.active) + len(self.active_mechanical)
                        self._emit_scheduler_event_once(
                            f"scheduler-drain:{self.scheduler_stop_reason}:{in_flight}",
                            "SCHEDULER_DRAINING_IN_FLIGHT",
                            {
                                "reason": self.scheduler_stop_reason,
                                "in_flight_jobs": len(self.active),
                                "in_flight_mechanical_subtasks": len(self.active_mechanical),
                                "action": "no new dispatch; wait for natural completion",
                            },
                        )
                        await asyncio.sleep(float(
                            self.config.raw["engine"].get("poll_interval_seconds", 0.2)
                        ))
                        continue
                    stopped_reason = self.scheduler_stop_reason
                    if self._finalization_started:
                        self.store.append("FINALIZATION_COMPLETED", {
                            "claim_id": self.final_conjecture_claim_id,
                            "pending_research_preserved": len(self.pending_research),
                            "pending_audits_preserved": len(self.pending_audits),
                            "reason": stopped_reason,
                        })
                        self.lifecycle.transition(
                            LifecyclePhase.COMPLETED, reason=stopped_reason,
                        )
                    elif self.lifecycle.phase not in {
                        LifecyclePhase.SEALED, LifecyclePhase.COMPLETED,
                    }:
                        self.lifecycle.transition(
                            LifecyclePhase.SEALED, reason=stopped_reason,
                        )
                    self.store.append("LIFECYCLE_TRANSITION", {
                        "phase": self.lifecycle.phase,
                        "reason": self.lifecycle.reason,
                    })
                    self._emit_scheduler_event_once(
                        f"scheduler-stop:{stopped_reason}", "SCHEDULER_STOPPED",
                        {"reason": stopped_reason},
                    )
                    break

                if self._error_rate_exceeded():
                    self._begin_internal_failure_drain(
                        "controller error-rate threshold reached",
                        source="error_rate_guard",
                    )
                    continue

                now = time.monotonic()
                interval = float(self.config.raw["rate_limits"].get("poll_interval_seconds", 60))
                if now - self._last_rate_check >= interval:
                    self._latest_rate_limits = await self.backend.rate_limits()
                    self._last_rate_check = now
                decision = self.governor.decide(self._latest_rate_limits)
                self.backend.set_economy_mode(decision.use_economy_routes)
                if decision.action == "STOP":
                    self.lifecycle.transition(
                        LifecyclePhase.DRAINING_BUDGET, reason=decision.reason,
                    )
                    self.scheduler_stop_reason = decision.reason
                    continue
                if self.stop_for_review:
                    stopped_reason = self.stop_for_review
                    break
                if decision.action == "DRAIN_TO_STOP":
                    if self.lifecycle.phase is LifecyclePhase.RUNNING:
                        self.lifecycle.transition(
                            LifecyclePhase.DRAINING_BUDGET, reason=decision.reason,
                        )
                    if self._continue_global_budget_drain(decision.reason):
                        await asyncio.sleep(float(
                            self.config.raw["engine"].get("poll_interval_seconds", 0.2)
                        ))
                        continue
                    self.scheduler_stop_reason = decision.reason
                    continue
                if time.time() >= deadline_epoch:
                    self.lifecycle.transition(
                        LifecyclePhase.DRAINING_EPOCH,
                        reason="epoch time limit reached",
                    )
                    self.scheduler_stop_reason = "epoch time limit reached"
                    self.store.append("EPOCH_DRAIN_STARTED", {
                        "reason": self.scheduler_stop_reason,
                        "in_flight_jobs": len(self.active),
                        "in_flight_mechanical_subtasks": len(self.active_mechanical),
                        "pending_research_preserved": len(self.pending_research),
                        "pending_audits_preserved": len(self.pending_audits),
                    })
                    continue

                self._request_replan_when_cycle_idle()

                await self._maybe_launch_director()
                await self._launch_audits()
                await self._launch_research(
                    min(decision.max_research or 0, self.max_research_workers),
                    decision.allow_exploration,
                )

                if (
                    not self.active and not self.active_mechanical
                    and not self.pending_mechanical
                    and not self.pending_research and not self.pending_audits
                    and not self.director_needed and self.batched_observations
                ):
                    self._release_observation_batch("idle flush")
                if (
                    not self.active and not self.active_mechanical
                    and not self.pending_mechanical
                    and not self.pending_research and not self.pending_audits
                    and not self.director_needed
                ):
                    stopped_reason = (
                        "controller invariant failed: idle without a scheduler stop "
                        "or pending Director replan"
                    )
                    self._internal_failure = True
                    self.store.append("CONTROLLER_INVARIANT_FAILED", {
                        "reason": stopped_reason,
                        "action": "internal failure; queue exhaustion is not a normal terminal state",
                    })
                    break
                await asyncio.sleep(float(self.config.raw["engine"].get("poll_interval_seconds", 0.2)))
        except asyncio.CancelledError:
            stopped_reason = "controller interrupted by operator"
            self._stop_after_epoch = True
            self._operator_interrupted = True
            self.store.append("CONTROLLER_INTERRUPTED", {"reason": stopped_reason})
        except Exception as exc:
            stopped_reason = (
                f"controller internal error: {type(exc).__name__}: "
                f"{_sanitize_live_text(exc)[:1000]}"
            )
            self._internal_failure = True
            self.store.append("CONTROLLER_ERROR", {"reason": stopped_reason})
        finally:
            await self._collect_terminal_envelopes_before_shutdown()
            for job_id, active in list(self.active_mechanical.items()):
                if not active.future.done():
                    active.future.cancel()
                try:
                    await active.future
                except (asyncio.CancelledError, Exception):
                    pass
                self.governor.release(job_id)
                response = self._mechanical_response(
                    active.request,
                    status="TOOL_ERROR",
                    error=f"mechanical subtask cancelled during controller stop: {stopped_reason}",
                    failure_kind="cancelled",
                    retryable=False,
                )
                self._persist_mechanical_terminal(active.request, response)
            self.active_mechanical.clear()
            for state in list(self.pending_mechanical):
                response = self._mechanical_response(
                    state,
                    status="TOOL_ERROR",
                    error=f"mechanical subtask never dispatched before controller stop: {stopped_reason}",
                    failure_kind="dispatch_stopped",
                    retryable=False,
                )
                self._persist_mechanical_terminal(state, response)
            self.pending_mechanical.clear()
            await self._cancel_active_jobs_before_backend_close(stopped_reason)
            try:
                await self.backend.close()
            except Exception as exc:
                self.store.append("BACKEND_CLOSE_FAILED", {
                    "error": _sanitize_live_text(exc)[:1000],
                })
        return self._finish(stopped_reason)

    def _write_compact_snapshot(self) -> Path:
        self._assert_startup_canonical_sources()
        self._semantic_final_postcondition()
        active_tasks = [active.task.to_dict() for active in self.active.values()]
        current_changes = [
            _bounded_value(item) for item in self.recent_changes[-20:]
        ]
        snapshot = self.graph.compact_snapshot(
            active_tasks, self.governor.snapshot(), current_changes,
        )
        graph_payload = self.graph.to_payload()
        graph_digest = (
            file_digest(self.layout.claim_graph_path)
            if self.persist_shared_state
            else bytes_sha256(json_bytes(graph_payload))
        )
        checkpoint_claim_state = self._checkpoint_claim_state_provenance(
            graph_digest,
        )
        canonical_view = director_canonical_view(
            self._canonical_state,
            run_dir=self.run_dir,
            claim_graph=graph_payload,
            claim_graph_sha256=graph_digest,
        )
        if self._checkpoint_planning_mirror is not None:
            canonical_view["planning_mirror"] = self._checkpoint_planning_mirror
        canonical_view["planning_context_sha256"] = checkpoint_claim_state[
            "planning_context_sha256"
        ]
        if int(checkpoint_claim_state["schema_version"]) >= 2:
            canonical_view["trusted_state_sha256"] = checkpoint_claim_state[
                "trusted_state_sha256"
            ]
            canonical_view["canonical_transition_id"] = checkpoint_claim_state[
                "canonical_transition_id"
            ]
        snapshot["canonical_state"] = canonical_view
        claim_state_payloads: dict[str, dict[str, Any]] = {}
        for claim_id, claim in sorted(self.graph.claims.items()):
            claim_payload = claim.to_dict()
            if self.domain_semantics.domain != "math-research":
                claim_payload["research_status"] = claim_payload.pop("math_status")
            claim_state_payloads[claim_id] = claim_payload
        snapshot["claim_state_provenance"] = {
            "authority": "controller_claim_graph",
            "sha256": stable_hash(claim_state_payloads),
            "startup_path": self._canonical_state["claim_graph"]["path"],
            "startup_sha256": self._canonical_state["claim_graph"]["sha256"],
            "status_rule": (
                "ClaimGraph is the sole mathematical-status and proof-frontier "
                "authority. Canonical Markdown is context-only unless it contains a "
                "strict generated state block. Every status upgrade is audit-gated."
                if self.domain_semantics.domain == "math-research" else
                "ClaimGraph is the sole domain-status and research-frontier authority. "
                "Canonical Markdown is context-only unless it contains a strict generated "
                "state block. Every status transition is audit-gated."
            ),
        }
        snapshot["mechanical_token_governor"] = self.mechanical_governor.snapshot()
        if self.domain_semantics.domain == "math-research":
            snapshot["research_target"] = {
                "project_name": self.config.project_name,
                "final_conjecture_claim_id": self.final_conjecture_claim_id,
                "finalization_rule": (
                    "stop new dispatch only after the exact final claim is PROVED and trusted "
                    "by the controller audit gate"
                ),
            }
        else:
            snapshot["research_target"] = {
                "project_name": self.config.project_name,
                "domain": self.domain_semantics.domain,
                "final_claim_id": self.final_conjecture_claim_id,
                "terminal_statuses": sorted(
                    self.domain_semantics.terminal_positive
                    | self.domain_semantics.terminal_negative
                ),
                "finalization_rule": (
                    "stop new dispatch only after the exact final claim reaches a "
                    "domain terminal status through the controller audit gate"
                ),
            }
        snapshot["controller_watermark"] = {
            "state_version": self._state_version,
            "director_requested_version": self._director_requested_version,
            "director_applied_version": self._director_applied_version,
            "generated_at": utc_now(),
        }
        self._snapshot_generation = max(
            self._snapshot_generation, self._director_requested_version,
        ) + 1
        replayed = self.store.replay()
        snapshot["snapshot_provenance"] = {
            **checkpoint_claim_state,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "attempt_id": self._attempt_id,
            "generation": self._snapshot_generation,
            "event_watermark": (
                int(replayed[-1].get("sequence", 0)) if replayed else 0
            ),
            "canonical_state_sha256": self._canonical_state[
                "canonical_state_sha256"
            ],
            "rebuilt_from_events": self.resume and self._recovery_completed,
        }
        snapshot["mechanical_subworkers"] = {
            "enabled": self.mechanical_worker_enabled,
            "max_concurrent": self.max_mechanical_subworkers,
            "pending": len(self.pending_mechanical),
            "active": [
                {
                    "mechanical_job_id": job_id,
                    "parent_job_id": active.request.parent_job_id,
                    "parent_role": active.request.parent_role,
                    "subtask_id": active.request.packet["task_id"],
                    "attempt": active.request.attempts_started,
                }
                for job_id, active in sorted(self.active_mechanical.items())
            ],
            "completed": len(self.completed_mechanical_jobs),
            "recent_results": [
                {
                    "parent_task_id": item.get("parent_task_id"),
                    "parent_role": item.get("parent_role"),
                    "subtask_id": item.get("subtask_id"),
                    "status": item.get("status"),
                    "failure_kind": item.get("failure_kind"),
                    "mechanical_evidence_only": True,
                    "result": _bounded_value(item.get("result") or {}),
                    "artifact_hashes": item.get("artifact_hashes") or {},
                }
                for item in self.completed_mechanical_jobs[-10:]
            ],
            "trust_boundary": "mechanical evidence only; never a proof or audit verdict",
        }
        snapshot["candidate_audit_frontier"] = [
            {
                "event": state.event.to_dict(),
                "artifact_hashes": dict(
                    self.candidate_artifact_hashes.get(fingerprint) or {}
                ),
                "semantic_evidence_hashes": dict(
                    self.candidate_semantic_evidence_hashes.get(fingerprint) or {}
                ),
                "audit_status": state.trust_status,
                "audit_results": [item.to_dict() for item in state.results],
                "audits_required": state.required,
            }
            for fingerprint, state in sorted(self.audit_gate.states.items())
            if not state.terminal
        ]
        self._defer_nonretryable_pending_routes(source="checkpoint_preflight")
        pending_for_next_epoch = [
            *self.pending_research, *self.deferred_research_continuations,
        ]
        pending_task_ids = [task.task_id for task in pending_for_next_epoch]
        if len(pending_task_ids) != len(set(pending_task_ids)):
            raise ValueError("next-epoch research frontier contains duplicate task ids")
        snapshot["pending_research"] = [
            task.to_dict() for task in pending_for_next_epoch
        ]
        snapshot["deferred_research_continuation_ids"] = [
            task.task_id for task in self.deferred_research_continuations
        ]
        continuation_checkpoints: list[dict[str, str]] = []
        checkpoint_prefix = (
            f"epoch://{self.epoch_id}/state/research_checkpoints/"
        )
        for task in self.deferred_research_continuations:
            references = [
                item for item in task.required_files
                if item.startswith(checkpoint_prefix)
            ]
            if len(references) != 1:
                raise ValueError(
                    "deferred continuation must reference exactly one current-epoch "
                    "checkpoint"
                )
            checkpoint_uri = references[0]
            checkpoint_path = self.artifact_store.resolve_uri(checkpoint_uri)
            continuation_checkpoints.append({
                "task_id": task.task_id,
                "uri": checkpoint_uri,
                "sha256": file_digest(checkpoint_path),
            })
        snapshot["research_continuation_checkpoints"] = continuation_checkpoints
        snapshot["pending_audits"] = [
            task.to_dict() for task in self.pending_audits
        ]
        snapshot["representation_compatibility"] = self._representation_compatibility_view()
        snapshot["semantic_alignment"] = self._semantic_alignment_view()
        latest_routes: dict[str, dict[str, Any]] = {}
        for record in self.route_ledger.records():
            route_id = str(record.get("route_id") or "")
            if route_id:
                latest_routes[route_id] = {
                    key: record.get(key)
                    for key in (
                        "route_id", "representation_id", "method_tags", "status",
                        "failure_class", "retry_condition", "evidence_refs", "source",
                    )
                }
        snapshot["route_state"] = [
            latest_routes[key] for key in sorted(latest_routes)
        ]
        snapshot["research_policy"] = self._policy_view(Role.DIRECTOR)
        state_root = self.run_dir / "state"
        snapshot["director_constraints"] = deepcopy(self.director_constraints)
        snapshot["director_overlay"] = (
            {
                "text": self._director_overlay,
                "sha256": bytes_sha256(self._director_overlay.encode("utf-8")),
            }
            if isinstance(self._director_overlay, str) else None
        )
        snapshot["history_archive"] = {
            "events_path": str(self.run_dir / "EVENTS.jsonl"),
            "live_events_path": str(self.run_dir / "LIVE_EVENTS.jsonl"),
            "rule": (
                "append-only complete event history; current context does not embed "
                "prior prompts, transcripts, or prior snapshots"
            ),
        }
        snapshot["snapshot_kind"] = "complete_current_director_context"
        archive_relative = Path("director_context_archive") / (
            f"context-{self._snapshot_generation:08d}.json"
        )
        archive_path = state_root / archive_relative
        archive_payload = (
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if archive_path.exists():
            if archive_path.read_bytes() != archive_payload:
                raise ValueError(
                    "append-only Director context archive generation already exists "
                    "with different content"
                )
        else:
            atomic_write_json(archive_path, snapshot)
        archive_reference = {
            "relative_path": archive_relative.as_posix(),
            "uri": (
                f"epoch://{self.epoch_id}/state/{archive_relative.as_posix()}"
            ),
            "sha256": file_digest(archive_path),
            "bytes": archive_path.stat().st_size,
            "generation": self._snapshot_generation,
            "kind": "complete_current_context_without_recursive_history",
        }
        compact = build_compact_snapshot(
            snapshot,
            full_context_reference=archive_reference,
            history_archive={
                "events_path": str(self.run_dir / "EVENTS.jsonl"),
                "live_events_path": str(self.run_dir / "LIVE_EVENTS.jsonl"),
                "context_archive_directory": str(archive_path.parent),
                "rule": "append-only full history; never inline into a Director prompt",
            },
        )
        path = state_root / "compact_snapshot.json"
        atomic_write_json(path, compact)
        self._latest_director_context_path = archive_path
        route_records = self.route_ledger.records()
        write_core_capsule(
            state_root / "CORE_CAPSULE.json",
            graph=self.graph,
            recent_changes=self.recent_changes,
            active_tasks=active_tasks,
            audit_leases=self.audit_leases.snapshot(),
            route_records=route_records,
            representations=self.claim_representations,
            canonical_state=canonical_view,
        )
        write_research_map(
            state_root / "RESEARCH_MAP.json",
            state_root / "RESEARCH_MAP.md",
            graph=self.graph,
            route_records=route_records,
            representations=self.claim_representations,
            canonical_state=canonical_view,
        )
        return path

    def _representation_compatibility_view(self) -> dict[str, Any]:
        claims_by_representation: dict[str, list[str]] = {}
        for claim_id, representation_id in sorted(self.claim_representations.items()):
            claims_by_representation.setdefault(representation_id, []).append(claim_id)
        known_ids = set(self.representation_contracts)
        referenced_ids = set(claims_by_representation)
        return {
            "dispatch_rule": (
                "Every task dependency must use the exact same representation contract, "
                "or the representation-id pair must already appear in audited_bridges. "
                "A new cross-representation route must first produce a dedicated "
                "REPRESENTATION_BRIDGE candidate and pass fresh independent audit."
            ),
            "claims_by_representation_id": {
                key: value for key, value in sorted(claims_by_representation.items())
            },
            "known_contracts": {
                key: self.representation_contracts[key]
                for key in sorted(self.representation_contracts)
            },
            "contract_missing_for_ids": sorted(referenced_ids - known_ids),
            "audited_bridges": [
                list(pair) for pair in sorted(self.audited_representation_bridges)
            ],
            "known_contract_does_not_imply_compatibility": True,
        }

    def _semantic_alignment_view(self) -> dict[str, Any]:
        if not self.final_conjecture_claim_id:
            return self.semantic_alignment.summary(
                self.graph.claims,
                trust_state=self.semantic_trust,
                claim_statements={
                    claim_id: claim.statement
                    for claim_id, claim in self.graph.claims.items()
                },
                claim_graph=self.graph,
                validation_authority_head=self._current_validation_authority_head(),
            )
        return self.semantic_alignment.validate_project(
            final_claim_id=self.final_conjecture_claim_id,
            final_claim_statement=self.graph.claims[
                self.final_conjecture_claim_id
            ].statement,
            claim_ids=self.graph.claims,
            trust_state=self.semantic_trust,
            claim_statements={
                claim_id: claim.statement
                for claim_id, claim in self.graph.claims.items()
            },
            claim_graph=self.graph,
            validation_authority_head=self._current_validation_authority_head(),
        )

    def _semantic_contract_input(self, workspace: Path) -> str | None:
        if not self.semantic_alignment.present:
            return None
        self.semantic_alignment.assert_unchanged()
        path = self.semantic_alignment.path
        if path is None:
            raise ValueError("semantic alignment path is unavailable")
        return str(self.workspace.materialize_input(workspace, path))

    def _request_replan_when_cycle_idle(self) -> None:
        if not self._replan_after_wave or self.director_needed or self.scheduler_stop_reason:
            return
        if self.pending_research or self.pending_audits:
            return
        if any(item.kind in {"research", "audit", "director"} for item in self.active.values()):
            return
        if self.pending_mechanical or self.active_mechanical:
            return
        self._replan_after_wave = False
        self._request_director(
            "research/audit wave completed",
            meaningful_change=False,
            immediate=True,
        )
        self.store.append("REPLAN_REQUESTED", {
            "reason": "research/audit wave completed",
            "action": "launch a fresh Director before declaring completion",
        })

    def _require_continuation(
        self,
        *,
        source: str,
        reason: str,
        forbidden_route: str | None = None,
    ) -> None:
        """Keep a real unresolved run alive and force a differentiated replan."""
        constraint = {
            "action": "DIVERSIFY",
            "claim_id": self.final_conjecture_claim_id or "FRONTIER",
            "forbidden_route": forbidden_route,
            "reason": reason,
            "source": source,
        }
        self.director_constraints.append(constraint)
        if not self.lifecycle.can_dispatch:
            self.store.append("DIRECTOR_CONTINUATION_DEFERRED", {
                **constraint,
                "lifecycle_phase": self.lifecycle.phase,
                "action_detail": "record as a next-epoch constraint; do not reopen dispatch",
            })
            return
        self._request_director(
            f"continuation required by {source}",
            meaningful_change=True,
        )
        self._replan_after_wave = False
        self.store.append("DIRECTOR_CONTINUATION_REQUIRED", {
            **constraint,
            "action_detail": (
                "final conjecture is unresolved; launch a fresh Director and choose "
                "new bounded proof, falsification, or independent exploration work"
            ),
        })

    async def _maybe_launch_director(self) -> None:
        if (
            not self.lifecycle.can_dispatch
            or not self.director_needed
            or self._director_active
            or self.scheduler_stop_reason
        ):
            return
        if self._director_not_before and time.monotonic() < self._director_not_before:
            return
        # Audit/research state changes are coalesced by director_needed. A
        # single Director may inspect still-active work instead of blocking
        # behind the whole wave.
        concurrent_work = bool(
            self.pending_research or self.pending_audits or any(
                item.kind in {"research", "audit"} for item in self.active.values()
            )
        )
        director_active = sum(item.kind == "director" for item in self.active.values())
        if director_active >= self.max_director:
            return
        estimated = self._estimated_tokens(Role.DIRECTOR, "MEDIUM")
        if not self.governor.may_start(Role.DIRECTOR, estimated):
            self._emit_scheduler_event_once(
                "director-paused:budget", "DIRECTOR_PAUSED",
                {"reason": "insufficient role/global token budget"},
            )
            self.director_needed = False
            if not self.pending_research and not self.pending_audits:
                self.scheduler_stop_reason = "fresh Director cannot start within remaining token budget"
            return
        try:
            snapshot = self._write_compact_snapshot()
        except Exception as exc:
            reason = (
                "canonical-state synchronization failed before Director launch: "
                f"{_sanitize_live_text(exc)[:500]}"
            )
            self.store.append("DIRECTOR_CANONICAL_STATE_FAILED", {
                "error": _sanitize_live_text(exc),
                "action": "fail closed without launching a Director",
            })
            self._begin_internal_failure_drain(reason, source="canonical_state")
            return
        snapshot_version = self._director_requested_version
        full_context = self._latest_director_context_path
        if full_context is None or not full_context.is_file():
            reason = "external Director context archive was not created"
            self.store.append("DIRECTOR_CONTEXT_ARCHIVE_FAILED", {
                "snapshot": str(snapshot),
                "action": "fail closed without launching a Director",
            })
            self._begin_internal_failure_drain(reason, source="director_context")
            return
        task = ResearchTask(
            task_id=f"director-{uuid4().hex[:12]}", role=Role.DIRECTOR,
            target_claim="FRONTIER", exact_objective="Select the next highest-value research portfolio.",
            why_now="initial planning or audited state change", dependencies=[],
            expected_information_gain="portfolio decision", research_impact="HIGH",
            estimated_cost_tier="MEDIUM",
            required_files=[str(snapshot), str(full_context)],
            stop_conditions=["return one schema-valid plan"], output_contract="director_plan.schema.json",
        )
        job_id = self._new_job_id()
        workspace, writable, metadata = self.workspace.create_job_workspace(
            task.task_id, job_id=job_id,
        )
        snapshot_input = self.workspace.materialize_input(workspace, snapshot)
        full_context_input = self.workspace.materialize_input(workspace, full_context)
        history_input = self.workspace.materialize_input(
            workspace, self.run_dir / "EVENTS.jsonl",
        )
        semantic_contract_input = self._semantic_contract_input(workspace)
        task.required_files = [str(snapshot_input), str(full_context_input)]
        packet = self.workspace.write_task_packet(workspace, {
            "task": task.to_dict(),
            "compact_snapshot": str(snapshot_input),
            "full_context_archive": str(full_context_input),
            "history_archive": str(history_input),
            "constraints": self.director_constraints,
            "output_protocol_version": OUTPUT_PROTOCOL_VERSION,
            "state_version": snapshot_version,
            "semantic_contract": semantic_contract_input,
            "research_policy": self._policy_view(Role.DIRECTOR),
            "workspace": metadata,
        })
        try:
            prompt = director_prompt(
                self.config.project_root, snapshot_input, self.director_constraints,
                self._policy_view(Role.DIRECTOR),
                project_overlay=self._director_overlay,
                task_packet_path=packet,
                full_context_path=full_context_input,
            )
            prompt_bytes = enforce_director_prompt_limit(prompt)
            self._start_job(
                task, prompt, self._schema("director_plan.schema.json"), workspace, writable,
                "director", estimated_tokens=estimated, job_id=job_id,
            )
            prompt_bytes = self._latest_director_prompt_bytes
        except DirectorPromptTooLarge as exc:
            self.store.append("DIRECTOR_PROMPT_REJECTED", {
                "prompt_bytes": exc.size_bytes,
                "hard_limit_bytes": exc.hard_limit_bytes,
                "target_bytes": DIRECTOR_PROMPT_TARGET_BYTES,
                "snapshot": str(snapshot),
                "full_context_archive": str(full_context),
                "action": "rejected before App Server thread/start",
            })
            self._begin_internal_failure_drain(
                str(exc), source="director_prompt_guard",
            )
            return
        self.director_needed = False
        self._director_active = True
        self._director_incremental = concurrent_work
        self._director_snapshot_version = snapshot_version
        self._director_not_before = 0.0
        self.store.append("DIRECTOR_PROMPT_PREPARED", {
            "prompt_bytes": prompt_bytes,
            "target_bytes": DIRECTOR_PROMPT_TARGET_BYTES,
            "hard_limit_bytes": DIRECTOR_PROMPT_HARD_LIMIT_BYTES,
            "target_met": prompt_bytes < DIRECTOR_PROMPT_TARGET_BYTES,
            "compact_snapshot": str(snapshot),
            "full_context_archive": str(full_context),
            "task_packet": str(packet),
            "inline_snapshot": False,
            "inline_transcript": False,
        })
        if concurrent_work:
            self.store.append("DIRECTOR_INCREMENTAL_LAUNCHED", {
                "snapshot": str(snapshot),
                "full_context_archive": str(full_context),
                "prompt_bytes": prompt_bytes,
                "snapshot_version": snapshot_version,
                "requested_version": self._director_requested_version,
                "pending_research": len(self.pending_research),
                "pending_audits": len(self.pending_audits),
                "active_research": sum(
                    item.kind == "research" for item in self.active.values()
                ),
                "active_audits": sum(
                    item.kind == "audit" for item in self.active.values()
                ),
                "action": "coalesced state update; do not wait for global wave drain",
            })

    def _new_job_id(self) -> str:
        while True:
            job_id = f"job-{uuid4().hex[:16]}"
            if job_id not in self.active:
                return job_id

    def _start_job(
        self,
        task: ResearchTask,
        prompt: str,
        schema: dict[str, Any],
        workspace: Path,
        writable: list[Path],
        kind: str,
        *,
        estimated_tokens: int,
        job_id: str | None = None,
    ) -> str:
        validate_output_schema_compatibility(
            schema, schema_path=f"{task.output_contract} ({task.role}, controller)",
        )
        kind_caps = {
            "director": self.max_director,
            "research": self.max_research_workers,
            "audit": self.max_audit,
        }
        if kind not in kind_caps:
            raise RuntimeError(f"unknown model job kind: {kind}")
        job_id = job_id or self._new_job_id()
        if job_id in self.active:
            raise RuntimeError(f"job_id {job_id!r} is already active")
        duplicate = next(
            (
                (active_job_id, active)
                for active_job_id, active in self.active.items()
                if active.task.task_id == task.task_id
            ),
            None,
        )
        if duplicate is not None:
            active_job_id, active = duplicate
            raise RuntimeError(
                f"task_id {task.task_id!r} is already active as "
                f"{active.kind} job {active_job_id}"
            )
        active_for_kind = sum(item.kind == kind for item in self.active.values())
        if active_for_kind >= kind_caps[kind]:
            raise RuntimeError(f"{kind} concurrency limit reached before model dispatch")
        active_for_role = sum(
            item.task.role == task.role for item in self.active.values()
        )
        if active_for_role >= self.config.role_concurrency(task.role):
            raise RuntimeError(
                f"{task.role} role concurrency limit reached before model dispatch"
            )
        if self.mock:
            selected_model, selected_effort = "mock-no-fast", "mock"
            selected_provider, selected_profile, selected_tier = "mock", None, None
        else:
            route_selector = getattr(self.backend, "_model_for", None)
            selected_model, selected_effort = (
                route_selector(task.role) if callable(route_selector)
                else self.config.model_for(task.role)
            )
            route = self.config.route_for(task.role)
            selected_provider = str(route["provider"])
            selected_profile = route.get("profile")
            selected_tier = route.get("service_tier")
        estimated_cost = self.config.raw["models"][task.role].get("estimated_cost_usd")
        if not self.governor.reserve(
            job_id, task.role, estimated_tokens, estimated_cost,
        ):
            raise RuntimeError(
                f"token reservation rejected after scheduler admission: {task.role} {estimated_tokens}"
            )
        timeout = self._task_timeout(task.role)
        continuation_checker = getattr(
            self.backend, "supports_same_thread_continuation", None,
        )
        same_thread_research = bool(
            kind == "research"
            and callable(continuation_checker)
            and continuation_checker(task.role)
        )
        max_turns = (
            self.research_turn_policy.max_turns_for(task.role)
            if same_thread_research else 1
        )
        logical_timeout = timeout * max_turns
        canonical_before = self._canonical_progress_marker(task.target_claim)
        broker_client_sha256: str | None = None
        broker_config_sha256: str | None = None
        if task.role in MECHANICAL_PARENT_ROLES:
            broker_command = self.workspace.install_mechanical_broker_client(
                workspace,
                parent_job_id=job_id,
                parent_task_id=task.task_id,
                parent_role=task.role,
                parent_timeout_seconds=timeout,
                enabled=self.mechanical_worker_enabled,
                broker_client_source=self._mechanical_broker_client_source,
                broker_client_sha256=self._mechanical_broker_client_sha256,
            )
            if self.mechanical_worker_enabled:
                broker_client_sha256 = file_digest(
                    workspace / "delegate_mechanical_task.py"
                )
                broker_config_sha256 = file_digest(
                    self.workspace.mechanical_broker_config_path(workspace)
                )
        else:
            broker_command = "DISABLED_FOR_THIS_ROLE"
        prompt = prompt.replace(MECHANICAL_BROKER_COMMAND_MARKER, broker_command)
        if MECHANICAL_BROKER_COMMAND_MARKER in prompt:
            self.governor.release(job_id)
            raise RuntimeError("mechanical broker command marker was not resolved")
        if task.role == Role.DIRECTOR:
            try:
                self._latest_director_prompt_bytes = enforce_director_prompt_limit(
                    prompt
                )
            except DirectorPromptTooLarge:
                self.governor.release(job_id)
                raise
        try:
            skill_path = self.workspace.materialize_input(
                workspace, Path(self._policy_view(task.role)["skill_snapshot"]),
            )
            backend_kwargs: dict[str, Any] = {
                "job_id": job_id,
                "task": task,
                "prompt": prompt,
                "output_schema": schema,
                "workspace": workspace,
                "writable_roots": writable,
                "timeout": timeout,
                "token_budget": self._server_thread_budget(task.role),
                "candidate_sink": lambda event, assigned=task: self._candidate_sink(
                    event, assigned
                ),
                "skill_path": skill_path,
            }
            if same_thread_research:
                backend_kwargs["turn_controller"] = (
                    lambda outcome, turn_index: self._control_research_turn(
                        job_id=job_id,
                        task=task,
                        canonical_before=canonical_before,
                        outcome=outcome,
                        turn_index=turn_index,
                    )
                )
            future = asyncio.create_task(self.backend.run_job(
                **backend_kwargs,
            ))
        except Exception:
            self.governor.release(job_id)
            raise
        started_at = utc_now()
        metadata_path = workspace / "workspace.json"
        workspace_metadata = (
            json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        )
        self.active[job_id] = ActiveJob(
            job_id, task, future, time.monotonic(), logical_timeout, kind,
            str(workspace), started_at, workspace_metadata,
            selected_model, selected_effort,
            selected_provider, selected_profile,
            selected_tier,
            broker_client_sha256, broker_config_sha256,
        )
        self.store.append("JOB_STARTED", {
            "job_id": job_id, "task_id": task.task_id, "role": task.role,
            "claim_id": task.target_claim, "workspace": str(workspace),
            "timeout": logical_timeout, "per_turn_timeout": timeout,
            "same_thread_multi_turn": same_thread_research,
            "max_turns": max_turns,
            "start_time": started_at, "workspace_metadata": workspace_metadata,
            "estimated_token_reservation": estimated_tokens,
            "model": selected_model, "reasoning_effort": selected_effort,
            "provider": selected_provider, "provider_profile": selected_profile,
            "requested_service_tier": selected_tier,
            "estimated_cost_reservation_usd": estimated_cost,
            "mechanical_broker_enabled": bool(
                self.mechanical_worker_enabled and task.role in MECHANICAL_PARENT_ROLES
            ),
            "mechanical_broker_client_sha256": broker_client_sha256,
            "mechanical_broker_config_sha256": broker_config_sha256,
        })
        self.live_store.append("AGENT_JOB_STARTED", {
            "job_id": job_id, "role": task.role, "task_id": task.task_id,
            "claim_id": task.target_claim, "timeout_seconds": timeout,
            "model": selected_model, "reasoning_effort": selected_effort,
            "provider": selected_provider, "provider_profile": selected_profile,
            "requested_service_tier": selected_tier,
            "start_time": started_at,
        })
        return job_id

    @staticmethod
    def _add_token_usage(target: TokenUsage, source: TokenUsage) -> None:
        for name in target.to_dict():
            setattr(target, name, getattr(target, name) + getattr(source, name))

    def _mechanical_response(
        self,
        state: MechanicalRequestState,
        *,
        status: str,
        execution: MechanicalExecution | None = None,
        error: str | None = None,
        failure_kind: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        observed = state.telemetry_observed
        unknown = state.telemetry_unknown
        telemetry = "partial" if observed and unknown else "observed" if observed else "unknown"
        result = execution.result if execution is not None else {}
        response = {
            "schema_version": 1,
            "parent_job_id": state.parent_job_id,
            "parent_task_id": state.parent_task_id,
            "parent_role": state.parent_role,
            "subtask_id": str(state.packet["task_id"]),
            "status": status,
            "model": execution.model if execution is not None else None,
            "reasoning_effort": (
                execution.reasoning_effort if execution is not None else None
            ),
            "service_tier": None,
            "token_usage": state.accumulated_usage.to_dict(),
            "token_telemetry": telemetry,
            "result": result,
            "artifacts": list(execution.artifacts) if execution is not None else [],
            "runner_directory": (
                execution.runner_directory if execution is not None else None
            ),
            "fallback": execution.fallback if execution is not None else None,
            "error": error if error is not None else (
                execution.error if execution is not None else None
            ),
            "failure_kind": failure_kind if failure_kind is not None else (
                execution.failure_kind if execution is not None else None
            ),
            "retryable": bool(retryable),
        }
        return validate_mechanical_response(
            response, allowed_routes=self._allowed_mechanical_routes(),
        )

    def _persist_mechanical_terminal(
        self,
        state: MechanicalRequestState,
        response: dict[str, Any],
        *,
        cache_reused: bool = False,
        execution: MechanicalExecution | None = None,
    ) -> None:
        response = dict(response)
        runner_reported_status = str(response["status"])
        artifacts = list(response.get("artifacts") or [])
        artifact_hashes: dict[str, str] = {}
        artifact_errors: list[str] = []
        runner_directory = response.get("runner_directory")
        root = Path(str(runner_directory)).resolve() if runner_directory else None
        successful_statuses = {
            "COMPLETED", "FALSIFIED", "NO_COUNTEREXAMPLE_WITHIN_SCOPE",
            "FORMAL_CHECK_PASSED",
        }
        declared_paths: set[Path] = set()
        for raw in artifacts:
            path = Path(str(raw)).resolve()
            if root is None or not path.is_relative_to(root):
                artifact_errors.append(f"escapes mechanical runner directory: {raw}")
            elif not path.is_file():
                artifact_errors.append(f"missing mechanical artifact: {raw}")
            else:
                declared_paths.add(path)
                artifact_hashes[str(path)] = file_digest(path)
        if runner_reported_status in successful_statuses:
            if root is None:
                artifact_errors.append("successful mechanical result has no runner directory")
            else:
                for raw in state.packet["expected_artifacts"]:
                    expected = (root / str(raw)).resolve()
                    if not expected.is_relative_to(root):
                        artifact_errors.append(f"expected artifact escapes runner directory: {raw}")
                    elif expected not in declared_paths:
                        artifact_errors.append(f"expected artifact was not declared: {raw}")
                    elif not expected.is_file():
                        artifact_errors.append(f"expected artifact is missing: {raw}")
        if artifact_errors:
            response.update({
                "status": "TOOL_ERROR",
                "error": "mechanical artifact validation failed: " + "; ".join(artifact_errors),
                "failure_kind": "artifact_validation",
                "retryable": False,
            })
            response = validate_mechanical_response(
                response, allowed_routes=self._allowed_mechanical_routes(),
            )

        response_path = Path(state.response_path)
        atomic_write_json(response_path, response)
        packet_hash = stable_hash(state.packet)
        cache_key = (
            state.parent_task_id, str(state.packet["task_id"]), packet_hash,
        )
        self._mechanical_result_cache[cache_key] = dict(response)
        record = {
            **response,
            "role": MECHANICAL_ROLE,
            "provider": execution.provider if execution is not None else None,
            "provider_profile": (
                execution.provider_profile if execution is not None else None
            ),
            "requested_model": response.get("model"),
            "requested_reasoning_effort": response.get("reasoning_effort"),
            "actual_model": (
                execution.actual_model if execution is not None else None
            ),
            "actual_reasoning_effort": (
                execution.actual_reasoning_effort if execution is not None else None
            ),
            "model_route_attestation": (
                execution.model_route_attestation
                if execution is not None else "unobservable"
            ),
            "runner_reported_status": runner_reported_status,
            "request_sha256": state.request_sha256,
            "packet_sha256": packet_hash,
            "attempts_started": state.attempts_started,
            "cache_reused": bool(cache_reused),
            "artifact_hashes": artifact_hashes,
            "artifact_validation_errors": artifact_errors,
            "cost_usd": (
                state.accumulated_cost_usd
                if state.cost_telemetry_observed else None
            ),
            "cost_telemetry": (
                "partial"
                if state.cost_telemetry_observed and state.cost_telemetry_unknown
                else "observed" if state.cost_telemetry_observed else "unknown"
            ),
            "recovery_state": (
                "recovered" if state.recovered else "fresh"
            ),
            "completed_at": utc_now(),
        }
        self.completed_mechanical_jobs.append(record)
        terminal_kind = (
            "MECHANICAL_SUBTASK_FAILED"
            if response["status"] in {"TOOL_ERROR", "REJECTED"}
            else "MECHANICAL_SUBTASK_COMPLETED"
        )
        self.store.append(terminal_kind, record)
        self._record_recent_change({
            "kind": terminal_kind,
            "parent_task_id": state.parent_task_id,
            "parent_role": state.parent_role,
            "subtask_id": state.packet["task_id"],
            "status": response["status"],
            "failure_kind": response["failure_kind"],
            "mechanical_evidence_only": True,
            "result": _bounded_value(response.get("result") or {}),
            "artifact_hashes": artifact_hashes,
        })
        self.live_store.append(terminal_kind, {
            "parent_job_id": state.parent_job_id,
            "parent_role": state.parent_role,
            "subtask_id": state.packet["task_id"],
            "status": response["status"],
            "provider": record["provider"],
            "model": response["model"],
            "reasoning_effort": response["reasoning_effort"],
            "token_usage": response["token_usage"],
            "token_telemetry": response["token_telemetry"],
            "cost_usd": record["cost_usd"],
            "cost_telemetry": record["cost_telemetry"],
            "cache_reused": bool(cache_reused),
            "error": response["error"],
        })

    def _reject_mechanical_request(
        self,
        state: MechanicalRequestState,
        *,
        error: str,
        failure_kind: str,
    ) -> None:
        response = self._mechanical_response(
            state,
            status="REJECTED",
            error=error,
            failure_kind=failure_kind,
            retryable=False,
        )
        self._persist_mechanical_terminal(state, response)

    def _mechanical_broker_integrity_error(
        self, active: ActiveJob, workspace: Path,
    ) -> str | None:
        if not active.broker_client_sha256 or not active.broker_config_sha256:
            return "active parent job has no sealed mechanical broker digests"
        targets = (
            (
                workspace / "delegate_mechanical_task.py",
                active.broker_client_sha256,
                "client",
            ),
            (
                self.workspace.mechanical_broker_config_path(workspace),
                active.broker_config_sha256,
                "configuration",
            ),
        )
        for path, expected, label in targets:
            try:
                observed = file_digest(path)
            except OSError as exc:
                return f"mechanical broker {label} is missing: {exc}"
            if observed != expected:
                return f"mechanical broker {label} digest changed after installation"
        return None

    def _mechanical_workspace_from_request_path(self, request_path: Path) -> Path:
        """Recover the only input root a broker request is permitted to name."""
        resolved = request_path.resolve()
        if len(resolved.parents) < 3:
            raise ValueError("mechanical request path has no parent job workspace")
        workspace = resolved.parents[2]
        expected_parent = (workspace / "mechanical_broker" / "requests").resolve()
        if resolved.parent != expected_parent:
            raise ValueError("mechanical request path escapes its broker request queue")
        run_root = self.workspace.run_dir.resolve()
        if not workspace.is_relative_to(run_root):
            raise ValueError("mechanical parent workspace escapes the active run")
        return workspace

    async def _poll_mechanical_requests(self) -> None:
        if not self.mechanical_worker_enabled:
            return
        for parent_job_id, active in list(self.active.items()):
            if active.task.role not in MECHANICAL_PARENT_ROLES or not active.workspace:
                continue
            workspace = Path(active.workspace).resolve()
            request_root = workspace / "mechanical_broker" / "requests"
            response_root = self.workspace.mechanical_broker_response_root(workspace)
            integrity_error = self._mechanical_broker_integrity_error(active, workspace)
            if integrity_error is not None:
                failure_key = f"mechanical-broker-integrity:{parent_job_id}"
                if failure_key not in self._scheduler_event_keys:
                    self._scheduler_event_keys.add(failure_key)
                    reason = f"mechanical broker integrity failure: {integrity_error}"
                    self.store.append("MECHANICAL_BROKER_INTEGRITY_FAILURE", {
                        "parent_job_id": parent_job_id,
                        "parent_task_id": active.task.task_id,
                        "parent_role": active.task.role,
                        "workspace": str(workspace),
                        "error": integrity_error,
                        "action": "cancel offending parent and fail closed",
                    })
                    if not self._internal_failure:
                        self._begin_internal_failure_drain(
                            reason, source="mechanical_broker_integrity",
                        )
                    self._schedule_backend_cancel(parent_job_id, reason)
                continue
            if not request_root.is_dir():
                continue
            for request_path in sorted(request_root.glob("*.json")):
                subtask_id = request_path.stem
                request_key = (parent_job_id, subtask_id)
                if request_key in self._mechanical_request_keys:
                    continue
                self._mechanical_request_keys.add(request_key)
                try:
                    raw = json.loads(request_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    packet = {
                        "task_id": subtask_id,
                    }
                    state = MechanicalRequestState(
                        parent_job_id, active.task.task_id, active.task.role,
                        str(workspace), str(request_path),
                        str(response_root / f"{subtask_id}.json"), "", packet,
                    )
                    self.store.append("MECHANICAL_SUBTASK_REQUESTED", {
                        "parent_job_id": parent_job_id,
                        "parent_task_id": active.task.task_id,
                        "parent_role": active.task.role,
                        "subtask_id": subtask_id,
                        "request_path": str(request_path),
                        "response_path": str(response_root / f"{subtask_id}.json"),
                        "valid": False,
                    })
                    self._reject_mechanical_request(
                        state, error=f"invalid request JSON: {exc}",
                        failure_kind="invalid_mechanical_request",
                    )
                    continue
                remaining = max(
                    1,
                    int(active.timeout - (time.monotonic() - active.started_monotonic)),
                )
                try:
                    request = validate_mechanical_request(
                        raw,
                        repository_root=workspace,
                        expected_parent_job_id=parent_job_id,
                        expected_parent_task_id=active.task.task_id,
                        expected_parent_role=active.task.role,
                        maximum_timeout_seconds=remaining,
                    )
                    packet = dict(request["task_packet"])
                    state = MechanicalRequestState(
                        parent_job_id, active.task.task_id, active.task.role,
                        str(workspace), str(request_path),
                        str(response_root / f"{packet['task_id']}.json"),
                        str(request["request_sha256"]), packet,
                    )
                except MechanicalTaskRejected as exc:
                    fallback_packet = raw.get("task_packet") if isinstance(raw, dict) else None
                    if not isinstance(fallback_packet, dict):
                        fallback_packet = {"task_id": subtask_id}
                    else:
                        fallback_packet = dict(fallback_packet)
                        fallback_packet["task_id"] = str(
                            fallback_packet.get("task_id") or subtask_id
                        )
                    state = MechanicalRequestState(
                        parent_job_id, active.task.task_id, active.task.role,
                        str(workspace), str(request_path),
                        str(response_root / f"{subtask_id}.json"),
                        str(raw.get("request_sha256") or "") if isinstance(raw, dict) else "",
                        fallback_packet,
                    )
                    self.store.append("MECHANICAL_SUBTASK_REQUESTED", {
                        "parent_job_id": parent_job_id,
                        "parent_task_id": active.task.task_id,
                        "parent_role": active.task.role,
                        "subtask_id": subtask_id,
                        "request_path": str(request_path),
                        "response_path": str(response_root / f"{subtask_id}.json"),
                        "valid": False,
                    })
                    self._reject_mechanical_request(
                        state, error=_sanitize_live_text(exc),
                        failure_kind="ineligible_mechanical_task",
                    )
                    continue
                self.store.append("MECHANICAL_SUBTASK_REQUESTED", {
                    "parent_job_id": state.parent_job_id,
                    "parent_task_id": state.parent_task_id,
                    "parent_role": state.parent_role,
                    "subtask_id": state.packet["task_id"],
                    "task_kind": state.packet["task_kind"],
                    "request_path": state.request_path,
                    "response_path": state.response_path,
                    "request_sha256": state.request_sha256,
                    "packet_sha256": stable_hash(state.packet),
                    "task_packet": state.packet,
                    "valid": True,
                })
                policy_rejection = self._mechanical_policy_rejection(state.packet)
                if policy_rejection is not None:
                    self._reject_mechanical_request(
                        state,
                        error=policy_rejection,
                        failure_kind="mechanical_selection_policy",
                    )
                    continue
                cache_key = (
                    state.parent_task_id,
                    str(state.packet["task_id"]),
                    stable_hash(state.packet),
                )
                cached = self._mechanical_result_cache.get(cache_key)
                if cached is not None:
                    reused = {
                        **cached,
                        "parent_job_id": state.parent_job_id,
                        "parent_task_id": state.parent_task_id,
                        "parent_role": state.parent_role,
                    }
                    self._persist_mechanical_terminal(
                        state,
                        validate_mechanical_response(
                            reused, allowed_routes=self._allowed_mechanical_routes(),
                        ),
                        cache_reused=True,
                    )
                else:
                    backpressure = self.config.raw["policy"][
                        "one_shot_compute_worker"
                    ]["backpressure"]
                    queued = len(self.pending_mechanical) + len(self.active_mechanical)
                    if queued >= int(backpressure["max_queue_depth"]):
                        self._reject_mechanical_request(
                            state,
                            error="mechanical broker queue backpressure limit reached",
                            failure_kind="broker_backpressure",
                        )
                    else:
                        self.pending_mechanical.append(state)
        await self._launch_mechanical_subtasks()

    async def _launch_mechanical_subtasks(self) -> None:
        if not self.lifecycle.can_dispatch:
            return
        worker_policy = self.config.raw["policy"].get("one_shot_compute_worker", {})
        estimated = int(worker_policy.get("estimated_tokens", 60000))
        backpressure = worker_policy["backpressure"]
        minimum_interval = float(backpressure["minimum_dispatch_interval_seconds"])
        if time.monotonic() - self._last_mechanical_dispatch < minimum_interval:
            return
        rate_decision = self.mechanical_governor.decide(self._latest_rate_limits)
        if rate_decision.action in {"STOP", "DRAIN", "DRAIN_TO_STOP"}:
            return
        capacity = self._mechanical_resource_capacity()
        launched = 0
        while (
            self.pending_mechanical
            and len(self.active_mechanical) < capacity
            and launched < int(backpressure["dispatch_batch_size"])
        ):
            state = self.pending_mechanical.pop(0)
            if self.scheduler_stop_reason:
                self._reject_mechanical_request(
                    state,
                    error=(
                        "controller stopped new dispatch before the mechanical subtask "
                        f"could start: {self.scheduler_stop_reason}"
                    ),
                    failure_kind="dispatch_stopped",
                )
                continue
            if not state.recovered and state.parent_job_id not in self.active:
                self._reject_mechanical_request(
                    state,
                    error="parent job ended before the mechanical subtask could start",
                    failure_kind="parent_job_ended",
                )
                continue
            attempt = state.attempts_started + 1
            logical_job_id = (
                f"mechanical-{state.parent_job_id}-{state.packet['task_id']}-a{attempt}"
            )
            estimated_cost = worker_policy.get("estimated_cost_usd")
            if not self.mechanical_governor.reserve(
                logical_job_id, MECHANICAL_ROLE, estimated, estimated_cost,
            ):
                self._reject_mechanical_request(
                    state,
                    error="mechanical token/cost budget does not permit a new subtask",
                    failure_kind="budget_exhausted",
                )
                continue
            workspace = Path(state.parent_workspace)
            packet_root = workspace / "mechanical_subtasks" / "packets"
            packet_path = packet_root / f"{state.packet['task_id']}.attempt-{attempt}.json"
            atomic_write_json(packet_path, state.packet)
            output_root = workspace / "mechanical_subtasks" / "runs"
            receipt_path = SubprocessMechanicalRunner.receipt_path(
                packet_path, output_root,
            )
            model_status_path = SubprocessMechanicalRunner.model_status_path(
                packet_path, output_root,
            )
            recovery_envelope_seconds = (
                int(state.packet["timeout_seconds"])
                + 30
            )
            state.attempts_started = attempt
            attempt_route = "fallback" if state.fallback_emitted else "primary"
            future = asyncio.create_task(self.mechanical_runner.run(
                packet_path=packet_path,
                output_root=output_root,
                timeout_seconds=int(state.packet["timeout_seconds"]),
                route=attempt_route,
            ))
            started_at = utc_now()
            self.active_mechanical[logical_job_id] = ActiveMechanicalJob(
                logical_job_id, state, future, time.monotonic(), started_at, estimated,
            )
            configured_route = (
                self.mechanical_fallback_route
                if attempt_route == "fallback" else self.mechanical_primary_route
            )
            payload = {
                "mechanical_job_id": logical_job_id,
                "parent_job_id": state.parent_job_id,
                "parent_task_id": state.parent_task_id,
                "parent_role": state.parent_role,
                "subtask_id": state.packet["task_id"],
                "task_kind": state.packet["task_kind"],
                "attempt": attempt,
                "provider": configured_route["provider"],
                "model": configured_route["model"],
                "reasoning_effort": configured_route["reasoning_effort"],
                "provider_profile": configured_route["profile"],
                "service_tier": None,
                "estimated_token_reservation": estimated,
                "estimated_cost_reservation_usd": estimated_cost,
                "packet_path": str(packet_path),
                "output_root": str(output_root),
                "receipt_path": str(receipt_path),
                "model_status_path": str(model_status_path),
                "recovery_deadline_epoch": time.time() + recovery_envelope_seconds,
                "started_at": started_at,
            }
            self.store.append("MECHANICAL_SUBTASK_STARTED", payload)
            self.live_store.append("MECHANICAL_SUBTASK_STARTED", payload)
            launched += 1
            self._last_mechanical_dispatch = time.monotonic()

    async def _collect_mechanical_completed(self) -> None:
        worker_policy = self.config.raw["policy"].get("one_shot_compute_worker", {})
        done = [
            job_id for job_id, active in self.active_mechanical.items()
            if active.future.done()
        ]
        for job_id in done:
            active = self.active_mechanical.pop(job_id)
            state = active.request
            try:
                execution = active.future.result()
            except asyncio.CancelledError:
                execution = MechanicalExecution(
                    status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                    error="mechanical subtask was cancelled",
                    failure_kind="cancelled", retryable=False,
                )
            except Exception as exc:
                execution = MechanicalExecution(
                    status="TOOL_ERROR", result={}, model=None, reasoning_effort=None,
                    error=f"mechanical runner exception: {exc}",
                    failure_kind="runner_internal", retryable=False,
                )
            route = (execution.model, execution.reasoning_effort, execution.service_tier)
            allowed_routes = self._allowed_mechanical_routes()
            expected_provider = None
            expected_profile = None
            if execution.model == self.mechanical_primary_route["model"]:
                expected_provider = self.mechanical_primary_route["provider"]
                expected_profile = self.mechanical_primary_route["profile"]
            elif execution.model == self.mechanical_fallback_route["model"]:
                expected_provider = self.mechanical_fallback_route["provider"]
                expected_profile = self.mechanical_fallback_route["profile"]
            if execution.provider is None:
                execution.provider = expected_provider
            if execution.provider_profile is None:
                execution.provider_profile = expected_profile
            actual_route_mismatch = bool(
                execution.model_route_attestation == "violation"
                or (
                    expected_provider is not None
                    and execution.provider is not None
                    and execution.provider != expected_provider
                )
                or (
                    execution.actual_model is not None
                    and execution.actual_model != execution.model
                )
                or (
                    execution.actual_reasoning_effort is not None
                    and execution.actual_reasoning_effort
                    != execution.reasoning_effort
                )
            )
            if route not in allowed_routes or actual_route_mismatch:
                execution = MechanicalExecution(
                    status="TOOL_ERROR", result={}, model=execution.model,
                    reasoning_effort=execution.reasoning_effort,
                    provider=execution.provider,
                    provider_profile=execution.provider_profile,
                    actual_model=execution.actual_model,
                    actual_reasoning_effort=execution.actual_reasoning_effort,
                    model_route_attestation=execution.model_route_attestation,
                    token_usage=execution.token_usage,
                    token_telemetry=execution.token_telemetry,
                    cost_usd=execution.cost_usd,
                    cost_telemetry=execution.cost_telemetry,
                    error=(
                        "mechanical runner used or observed an unapproved "
                        "model/effort/tier route"
                    ),
                    failure_kind="model_route_policy", retryable=False,
                    unavailable_routes=list(execution.unavailable_routes),
                )
            self.mechanical_governor.record(
                job_id, MECHANICAL_ROLE, execution.token_usage,
                execution.status not in {"TOOL_ERROR", "BLOCKED"},
                execution.cost_usd,
            )
            self.mechanical_governor.release(job_id)
            self._add_token_usage(state.accumulated_usage, execution.token_usage)
            if execution.token_telemetry in {"observed", "synthetic"}:
                state.telemetry_observed += 1
            elif execution.token_telemetry == "partial":
                state.telemetry_observed += 1
                state.telemetry_unknown += 1
            else:
                state.telemetry_unknown += 1
            if execution.cost_usd is not None:
                state.accumulated_cost_usd += float(execution.cost_usd)
                state.cost_telemetry_observed += 1
            else:
                state.cost_telemetry_unknown += 1
            attempt_record = {
                "mechanical_job_id": job_id,
                "parent_job_id": state.parent_job_id,
                "parent_task_id": state.parent_task_id,
                "parent_role": state.parent_role,
                "subtask_id": state.packet["task_id"],
                "attempt": state.attempts_started,
                "status": execution.status,
                "provider": execution.provider,
                "provider_profile": execution.provider_profile,
                "model": execution.model,
                "reasoning_effort": execution.reasoning_effort,
                "actual_model": execution.actual_model,
                "actual_reasoning_effort": execution.actual_reasoning_effort,
                "model_route_attestation": execution.model_route_attestation,
                "service_tier": None,
                "token_usage": execution.token_usage.to_dict(),
                "token_telemetry": execution.token_telemetry,
                "cost_usd": execution.cost_usd,
                "cost_telemetry": execution.cost_telemetry,
                "result": execution.result,
                "artifacts": list(execution.artifacts),
                "runner_directory": execution.runner_directory,
                "fallback": execution.fallback,
                "error": execution.error,
                "failure_kind": execution.failure_kind,
                "retryable": bool(execution.retryable),
                "provider_reset_at": execution.provider_reset_at,
                "unavailable_routes": list(execution.unavailable_routes),
                "finished_at": utc_now(),
            }
            # This write precedes every retry/fallback/terminal transition. A
            # resumed controller can therefore restore token accounting and
            # the finite retry watermark without replaying an already-finished
            # attempt or losing its original route/error.
            self.store.append("MECHANICAL_SUBTASK_ATTEMPT_FINISHED", attempt_record)
            self.live_store.append("MECHANICAL_SUBTASK_ATTEMPT_FINISHED", {
                key: attempt_record[key]
                for key in (
                    "mechanical_job_id", "parent_job_id", "parent_role",
                    "subtask_id", "attempt", "status", "model",
                    "provider", "provider_profile",
                    "reasoning_effort", "actual_model",
                    "actual_reasoning_effort", "model_route_attestation",
                    "service_tier", "token_usage",
                    "token_telemetry", "cost_usd", "cost_telemetry",
                    "error", "failure_kind", "retryable",
                    "provider_reset_at",
                )
            })
            for unavailable in execution.unavailable_routes:
                unavailable_key = (
                    str(unavailable.get("model") or ""),
                    (
                        str(unavailable["reasoning_effort"])
                        if unavailable.get("reasoning_effort") is not None else None
                    ),
                    None,
                )
                if (
                    not unavailable_key[0]
                    or unavailable.get("service_tier") is not None
                    or unavailable_key in self._mechanical_unavailable_routes
                ):
                    continue
                self._mechanical_unavailable_routes.add(unavailable_key)
                self.store.append("MECHANICAL_ROUTE_UNAVAILABLE", {
                    "mechanical_job_id": job_id,
                    "parent_job_id": state.parent_job_id,
                    "parent_role": state.parent_role,
                    "subtask_id": state.packet["task_id"],
                    "attempt": state.attempts_started,
                    "model": unavailable_key[0],
                    "reasoning_effort": unavailable_key[1],
                    "service_tier": None,
                    "error": unavailable.get("error"),
                    "runner_directory": unavailable.get("run_directory"),
                    "action": "cache exact permanent unavailability; do not probe again",
                })
                persist = getattr(self.mechanical_runner, "persist_unavailable", None)
                if callable(persist):
                    try:
                        persist(
                            model=unavailable_key[0],
                            reasoning_effort=unavailable_key[1],
                            service_tier=None,
                            error=str(unavailable.get("error") or "permanent unavailable"),
                            run_directory=str(
                                unavailable.get("run_directory")
                                or execution.runner_directory
                                or "controller-event"
                            ),
                        )
                    except MechanicalTaskRejected as exc:
                        self.store.append("MECHANICAL_ROUTE_CACHE_PERSIST_FAILED", {
                            "mechanical_job_id": job_id,
                            "parent_job_id": state.parent_job_id,
                            "subtask_id": state.packet["task_id"],
                            "model": unavailable_key[0],
                            "reasoning_effort": unavailable_key[1],
                            "service_tier": None,
                            "error": _sanitize_live_text(exc),
                            "source": "attempt_completion",
                        })
                        if not self._internal_failure:
                            self._begin_internal_failure_drain(
                                "mechanical route circuit-breaker persistence failed",
                                source="mechanical_route_cache",
                            )
            fallback_was_already_emitted = state.fallback_emitted
            if execution.fallback and not fallback_was_already_emitted:
                self.store.append("MECHANICAL_SUBTASK_FALLBACK", {
                    "mechanical_job_id": job_id,
                    "parent_job_id": state.parent_job_id,
                    "parent_role": state.parent_role,
                    "subtask_id": state.packet["task_id"],
                    "attempt": state.attempts_started,
                    "from_provider": self.mechanical_primary_route["provider"],
                    "from_model": self.mechanical_primary_route["model"],
                    "to_provider": self.mechanical_fallback_route["provider"],
                    "to_model": self.mechanical_fallback_route["model"],
                    "to_reasoning_effort": self.mechanical_fallback_route["reasoning_effort"],
                    "service_tier": None,
                    "reason": execution.fallback.get("reason"),
                })
                state.fallback_emitted = True
            if (
                execution.failure_kind in MECHANICAL_FALLBACK_FAILURE_KINDS
                and execution.model == self.mechanical_primary_route["model"]
                and isinstance(execution.fallback, dict)
                and execution.fallback.get("continuation_required") is True
                and not fallback_was_already_emitted
            ):
                self.store.append("MECHANICAL_SUBTASK_FALLBACK_CONTINUATION_QUEUED", {
                    "mechanical_job_id": job_id,
                    "parent_job_id": state.parent_job_id,
                    "subtask_id": state.packet["task_id"],
                    "next_provider": self.mechanical_fallback_route["provider"],
                    "next_model": self.mechanical_fallback_route["model"],
                    "next_reasoning_effort": self.mechanical_fallback_route["reasoning_effort"],
                    "service_tier": None,
                    "trigger_failure_kind": execution.failure_kind,
                    "action": (
                        "one fallback continuation; does not consume transient retry budget"
                    ),
                })
                self.pending_mechanical.append(state)
                continue
            if execution.failure_kind == "provider_quota_exhausted":
                response = self._mechanical_response(
                    state,
                    status=execution.status,
                    execution=execution,
                    error=execution.error,
                    failure_kind=execution.failure_kind,
                    retryable=False,
                )
                self._persist_mechanical_terminal(state, response, execution=execution)
                self._pause_for_mechanical_provider_quota(
                    mechanical_job_id=job_id,
                    state=state,
                    execution=execution,
                )
                continue
            retry_class = (
                "model_protocol"
                if execution.failure_kind in {"model_output_protocol", "runner_protocol"}
                else "transient"
            )
            max_retries = int(worker_policy.get(
                "model_protocol_max_retries"
                if retry_class == "model_protocol"
                else "transient_max_retries",
                1,
            ))
            retries_used = state.retry_counts.get(retry_class, 0)
            if execution.retryable and retries_used < max_retries:
                retries_used += 1
                state.retry_counts[retry_class] = retries_used
                self.store.append("MECHANICAL_SUBTASK_RETRY_QUEUED", {
                    "mechanical_job_id": job_id,
                    "parent_job_id": state.parent_job_id,
                    "subtask_id": state.packet["task_id"],
                    "failure_kind": execution.failure_kind,
                    "retry_class": retry_class,
                    "retry": retries_used,
                    "max_retries": max_retries,
                    "action": "same fixed route policy; transient failure is not cached unavailable",
                })
                self.pending_mechanical.append(state)
                continue
            response = self._mechanical_response(
                state,
                status=execution.status,
                execution=execution,
                error=execution.error,
                failure_kind=execution.failure_kind,
                retryable=False,
            )
            self._persist_mechanical_terminal(state, response, execution=execution)
        if done:
            await self._launch_mechanical_subtasks()

    def _task_inbox(self, task_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
        return self.inbox.inbox_root / safe

    def _producer_identity_for_candidate(
        self, event: CandidateEvent,
    ) -> dict[str, str]:
        identity = self._candidate_producer_identities.get(event.fingerprint)
        if identity is None and self.persist_shared_state:
            matches = [
                item.get("producer_identity")
                for item in self.canonical_transitions.verified_committed_authorizations()
                if item.get("kind") == "CANDIDATE_REGISTERED"
                and item.get("candidate_fingerprint") == event.fingerprint
            ]
            if len(matches) == 1 and isinstance(matches[0], dict):
                identity = {
                    str(key): str(value) for key, value in matches[0].items()
                }
                self._candidate_producer_identities[event.fingerprint] = identity
        if identity is None:
            raise ValueError(
                "semantic candidate lacks controller-owned producer authorization"
            )
        return dict(identity)

    async def _candidate_sink(self, event: CandidateEvent, task: ResearchTask) -> None:
        self.inbox.submit(
            event,
            target_root=self._task_inbox(task.task_id),
        )
        await self.candidate_queue.put(event)

    async def _poll_filesystem_candidates(self) -> None:
        for event in self.inbox.poll():
            await self.candidate_queue.put(event)
        for error in self.inbox.poll_errors:
            self.store.append("CANDIDATE_QUARANTINED", error)

    def _validate_candidate_provenance(self, event: CandidateEvent) -> None:
        source = self.inbox.sources.get(event.event_id)
        expected_root = self._task_inbox(event.producer_task_id).resolve()
        if source is None or source.parent.resolve() != expected_root:
            raise ValueError("candidate did not arrive through its producer task's isolated inbox")
        job_records = [
            item["payload"] for item in self.store.replay()
            if item["kind"] == "JOB_STARTED"
            and item["payload"].get("task_id") == event.producer_task_id
        ]
        active = [
            item for item in self.active.values()
            if item.task.task_id == event.producer_task_id and item.kind == "research"
        ]
        if active:
            assigned_task = active[-1].task
        elif job_records:
            task_records = [
                item["payload"].get("task")
                for item in self.store.replay()
                if item["kind"] == "TASK_ACCEPTED"
                and item["payload"].get("task_id") == event.producer_task_id
                and item["payload"].get("task")
            ]
            if not task_records:
                raise ValueError("candidate producer assignment has no durable task packet")
            assigned_task = ResearchTask.from_dict(task_records[-1])
        else:
            raise ValueError("candidate producer_task_id is not a controller-started job")
        assigned_claim = assigned_task.target_claim
        assigned_role = assigned_task.role
        if assigned_role not in {Role.PROVER, Role.FALSIFIER, Role.EXPLORER}:
            raise ValueError("candidate producer is not a research worker")
        if event.type == "REPRESENTATION_BRIDGE":
            if assigned_task.representation_id not in event.bridge_representation_ids:
                raise ValueError(
                    "representation bridge must include the producer task representation"
                )
        elif event.representation_id != assigned_task.representation_id:
            raise ValueError(
                "candidate representation does not match its controller-assigned task"
            )
        if assigned_claim == event.claim_id:
            if event.parent_claim_id is not None:
                raise ValueError("an exact assigned-claim candidate must set parent_claim_id to null")
        else:
            if assigned_task.metadata.get("allow_derived_claims") is not True:
                raise ValueError("producer assignment does not allow derived claims")
            if event.parent_claim_id != assigned_claim:
                raise ValueError("derived candidate parent_claim_id does not match the producer assignment")
            expected = derived_claim_id(
                assigned_claim, event.exact_statement,
                event.assumptions, event.dependencies,
            )
            if event.claim_id != expected:
                raise ValueError(
                    f"derived candidate claim_id must be the deterministic id {expected}"
                )
            parent = self.graph.claims.get(assigned_claim)
            if parent and " ".join(parent.statement.split()) == " ".join(event.exact_statement.split()):
                raise ValueError("a derived candidate must state a genuinely different subclaim")
        producer_job_id = (
            active[-1].logical_job_id
            if active else str(job_records[-1].get("job_id") or "")
        )
        bindings = [
            item["payload"] for item in self.store.replay()
            if item["kind"] in {"JOB_BOUND", "JOB_REBOUND"}
            and str(item["payload"].get("job_id") or "") == producer_job_id
            and item["payload"].get("thread_id")
        ]
        if not producer_job_id:
            raise ValueError("candidate producer lacks a controller-owned execution identity")
        controller_thread_id = (
            str(bindings[-1]["thread_id"])
            if bindings else f"job:{producer_job_id}"
        )
        if (
            event.producer_thread_id is not None
            and str(event.producer_thread_id) != controller_thread_id
        ):
            raise ValueError(
                "candidate self-reported producer identity disagrees with controller state"
            )
        event.producer_thread_id = controller_thread_id
        self._candidate_producer_identities[event.fingerprint] = {
            "run_id": self.run_id,
            "job_id": producer_job_id,
            "task_id": event.producer_task_id,
            "thread_id": controller_thread_id,
            "role": str(assigned_role),
        }

    def _validate_final_target_candidate(self, event: CandidateEvent) -> None:
        if (
            self.final_conjecture_claim_id
            and event.claim_id == self.final_conjecture_claim_id
            and event.impact != Impact.CRITICAL
        ):
            raise ValueError(
                "final claim candidates must use CRITICAL impact and two independent audits"
            )
        if not self.semantic_alignment.present:
            if event.semantic_bridge_ids:
                raise ValueError("legacy candidates cannot claim semantic bridge verification")
            return
        transition_status = self.domain_semantics.event_transitions[event.type].get(
            "status"
        )
        required_now = transition_status in self.domain_semantics.terminal_positive
        binding = self.semantic_alignment.claims.get(event.claim_id)
        if binding is None:
            if event.semantic_bridge_ids:
                raise ValueError("candidate names semantic bridges without a claim binding")
            if required_now and event.claim_id == self.final_conjecture_claim_id:
                raise ValueError("terminal-positive final claim lacks a semantic binding")
            return
        if event.semantic_bridge_ids or required_now:
            if tuple(event.semantic_bridge_ids) != binding.required_bridges:
                raise ValueError(
                    "candidate semantic_bridge_ids must exactly match the declared ordered path"
                )
            if event.representation_id != binding.representation_id:
                raise ValueError(
                    "candidate RepresentationContract does not match its semantic binding"
                )

    def _validate_candidate_evidence(self, event: CandidateEvent) -> None:
        level = event.proposed_evidence_level
        self.domain_semantics.validate_event_type(event.type)
        transition = self.domain_semantics.event_transitions[event.type]
        minimum = str(transition["min_evidence"])
        independently_upgradable = (
            level == EvidenceLevel.E2_EXACT_TESTED
            and minimum == EvidenceLevel.E3_REDUNDANT_EXACT
            and self.domain_semantics.requires_frozen_protocol(event.type)
        )
        if evidence_rank(level) < evidence_rank(minimum) and not independently_upgradable:
            raise ValueError(
                f"{event.type} requires at least {minimum} evidence for "
                f"{self.domain_semantics.domain}"
            )
        if level == EvidenceLevel.E0_SPECULATIVE:
            return
        if not event.artifact_paths:
            raise ValueError(f"{level} requires at least one durable artifact")
        if level == EvidenceLevel.E1_NUMERIC:
            return
        if level == EvidenceLevel.E2_EXACT_TESTED:
            if not event.reproduction_commands:
                raise ValueError("E2_EXACT_TESTED requires an exact reproduction command")
            return
        if level == EvidenceLevel.E4_CERTIFIED:
            if (
                self.domain_semantics.domain != "certified-computational-research"
                or event.type != "CERTIFICATE"
                or not self.domain_semantics.requires_deterministic_checker(event.type)
            ):
                raise ValueError(
                    "E4_CERTIFIED is reserved for the certified-computational "
                    "CERTIFICATE gate"
                )
            if not event.reproduction_commands:
                raise ValueError(
                    "E4_CERTIFIED requires a deterministic checker reproduction command"
                )
            return
        raise ValueError(
            f"{level} cannot be self-certified by an autonomous worker in this MVP; "
            "E3 requires an independent evaluator and E5 requires a dedicated kernel gate"
        )

    def _verify_domain_evidence_receipts(
        self,
        event: CandidateEvent,
        *,
        extend_artifacts: bool,
    ) -> list[dict[str, Any]]:
        checker_required = self.domain_semantics.requires_deterministic_checker(
            event.type
        )
        protocol_required = self.domain_semantics.requires_frozen_protocol(event.type)
        if not checker_required and not protocol_required:
            if event.evidence_receipts:
                raise ValueError(
                    f"{event.type} does not accept deterministic domain evidence receipts"
                )
            self.domain_evidence_receipts.pop(event.fingerprint, None)
            return []
        if not event.evidence_receipts:
            required = (
                "deterministic checker" if checker_required else "frozen experiment"
            )
            raise ValueError(f"{event.type} requires a verified {required} receipt")

        expected_kind = (
            "deterministic_checker_run" if checker_required else "experiment_run"
        )
        runner = ExperimentRunner(self.config.project_root)
        verified: list[dict[str, Any]] = []
        for reference in event.evidence_receipts:
            if reference["kind"] != expected_kind:
                raise ValueError(
                    f"{event.type} requires {expected_kind} evidence receipts"
                )
            receipt = runner.verify_receipt(
                Path(reference["manifest_path"]),
                run_id=reference["run_id"],
            )
            cases = list(receipt["cases"])
            if not cases:
                raise ValueError("domain evidence receipt contains no experiment cases")
            for case in cases:
                if (
                    case["termination"] != "EXITED"
                    or case["infrastructure_failure"] is not None
                ):
                    raise ValueError(
                        "infrastructure failure cannot support a domain claim transition"
                    )
                if checker_required and case["exit_code"] != 0:
                    raise ValueError(
                        "deterministic checker receipt contains a nonzero checker result"
                    )
            verified.append({
                "kind": reference["kind"],
                **receipt,
            })

        if len({item["run_id"] for item in verified}) != len(verified):
            raise ValueError("domain evidence receipts must name distinct runs")
        if extend_artifacts:
            existing = set(event.artifact_paths)
            for receipt in verified:
                for relative in receipt["artifact_paths"]:
                    path = (self.config.project_root / relative).resolve()
                    uri = portable_project_uri(self.config.project_root, path)
                    if uri not in existing:
                        event.artifact_paths.append(uri)
                        existing.add(uri)
        self.domain_evidence_receipts[event.fingerprint] = verified
        return verified

    def _validate_domain_audit_evidence(
        self,
        event: CandidateEvent,
        verified_evidence_level: str,
    ) -> list[dict[str, Any]]:
        receipts = self._verify_domain_evidence_receipts(
            event, extend_artifacts=False,
        )
        if (
            self.domain_semantics.requires_frozen_protocol(event.type)
            and evidence_rank(verified_evidence_level)
            >= evidence_rank(EvidenceLevel.E3_REDUNDANT_EXACT)
            and len({item["run_id"] for item in receipts}) < 2
        ):
            raise ValueError(
                "E3_REDUNDANT_EXACT empirical evidence requires two distinct verified runs"
            )
        return receipts

    def _bind_candidate_artifacts(self, event: CandidateEvent) -> dict[str, str]:
        hashes = self.artifact_store.seal_candidate(event)
        self.candidate_artifact_hashes[event.fingerprint] = hashes
        return dict(hashes)

    def _bind_candidate_semantic_evidence(
        self,
        event: CandidateEvent,
        artifact_hashes: dict[str, str],
    ) -> dict[str, str]:
        if not event.semantic_bridge_ids:
            self.candidate_semantic_evidence_hashes.pop(event.fingerprint, None)
            return {}
        binding = self.semantic_alignment.claims.get(event.claim_id)
        if binding is None or tuple(event.semantic_bridge_ids) != binding.required_bridges:
            raise ValueError("candidate semantic bridge binding changed before sealing")
        evidence_hashes: dict[str, str] = {}
        for bridge_id in binding.required_bridges:
            bridge = self.semantic_alignment.bridges.get(bridge_id)
            if bridge is None:
                raise ValueError(f"candidate semantic bridge is undeclared: {bridge_id}")
            for relative in bridge.evidence:
                path = (self.config.project_root / relative).resolve()
                if not path.is_relative_to(self.config.project_root.resolve()) or not path.is_file():
                    raise ValueError(
                        f"semantic bridge evidence is unavailable: {relative}"
                    )
                evidence_hashes[relative] = file_digest(path)
        if not set(evidence_hashes.values()).issubset(set(artifact_hashes.values())):
            raise ValueError(
                "candidate artifact bundle does not contain every declared semantic evidence file"
            )
        self.candidate_semantic_evidence_hashes[event.fingerprint] = dict(
            sorted(evidence_hashes.items())
        )
        return dict(self.candidate_semantic_evidence_hashes[event.fingerprint])

    def _verify_candidate_artifacts(
        self, event: CandidateEvent,
    ) -> tuple[bool, dict[str, str], dict[str, str]]:
        expected = dict(self.candidate_artifact_hashes.get(event.fingerprint) or {})
        if event.artifact_paths and not expected:
            return False, expected, {}
        _, current = self.artifact_store.verify(expected)
        return current == expected, expected, current

    def _record_candidate_artifact_drift(
        self,
        event: CandidateEvent,
        *,
        phase: str,
        expected: dict[str, str],
        observed: dict[str, str],
    ) -> None:
        self.store.append("CANDIDATE_ARTIFACT_DRIFT", {
            "event_id": event.event_id,
            "candidate_fingerprint": event.fingerprint,
            "claim_id": event.claim_id,
            "phase": phase,
            "expected_artifact_hashes": expected,
            "observed_artifact_hashes": observed,
            "action": "retain candidate; block audit/trust transition and request replan",
        })
        self._request_director(
            f"candidate artifact drift detected during {phase}",
            meaningful_change=True,
        )

    async def _process_candidate_queue(self) -> None:
        while not self.candidate_queue.empty():
            event = await self.candidate_queue.get()
            if event.fingerprint in self.inbox.processed:
                self.inbox.mark_processed(event, self.run_id)
                self.store.append("CANDIDATE_DEDUPLICATED", {
                    "event_id": event.event_id, "fingerprint": event.fingerprint,
                    "claim_id": event.claim_id,
                })
                continue
            if event.fingerprint in self.inbox.accepted:
                self.inbox.mark_processed(event, self.run_id, accepted=True)
                self.store.append("CANDIDATE_DEDUPLICATED", {
                    "event_id": event.event_id, "fingerprint": event.fingerprint,
                    "claim_id": event.claim_id,
                })
                continue
            try:
                self._validate_candidate_provenance(event)
                self._validate_final_target_candidate(event)
                self._validate_candidate_evidence(event)
                verified_receipts = self._verify_domain_evidence_receipts(
                    event, extend_artifacts=True,
                )
                artifact_hashes = self._bind_candidate_artifacts(event)
                semantic_evidence_hashes = self._bind_candidate_semantic_evidence(
                    event, artifact_hashes,
                )
                self.graph.mark_candidate(event)
            except ValueError as exc:
                self.candidate_artifact_hashes.pop(event.fingerprint, None)
                self.candidate_semantic_evidence_hashes.pop(event.fingerprint, None)
                self.domain_evidence_receipts.pop(event.fingerprint, None)
                self.store.append("CANDIDATE_REJECTED", {
                    "event_id": event.event_id, "fingerprint": event.fingerprint,
                    "claim_id": event.claim_id, "reason": _sanitize_live_text(exc),
                })
                self.inbox.mark_processed(event, self.run_id)
                continue
            self.inbox.persist(event)
            audit_state = self.audit_gate.register(event)
            self._commit_claim_state_transition(
                transition_kind="CANDIDATE_REGISTERED",
                authorization={
                    "controller_gate": "candidate provenance and schema validation",
                    "candidate_fingerprint": event.fingerprint,
                    "claim_id": event.claim_id,
                    "producer_identity": self._producer_identity_for_candidate(event),
                    "trust_upgrade": False,
                },
            )
            self.store.append("CANDIDATE_PROCESSED", {
                "event_id": event.event_id, "fingerprint": event.fingerprint,
                "claim_id": event.claim_id, "parent_claim_id": event.parent_claim_id,
                "impact": event.impact,
                "proposed_evidence_level": event.proposed_evidence_level,
                "artifact_hashes": artifact_hashes,
                "semantic_evidence_hashes": semantic_evidence_hashes,
                "domain_evidence_receipt_fingerprints": [
                    item["receipt_fingerprint"] for item in verified_receipts
                ],
            })
            self._request_director(
                f"candidate {event.fingerprint} entered the audit frontier",
                meaningful_change=True,
            )
            self.inbox.mark_processed(event, self.run_id, accepted=True)
            if audit_state.required == 0:
                self.batched_observations.append(event)
                self.store.append("OBSERVATION_BATCHED", {
                    "event_id": event.event_id, "fingerprint": event.fingerprint,
                    "claim_id": event.claim_id, "impact": event.impact,
                })
                batch_size = int(self.config.raw["audit"].get("low_impact_batch_size", 8))
                if len(self.batched_observations) >= batch_size:
                    self._release_observation_batch("configured batch size")
            else:
                self._queue_next_audit(event)

    def _release_observation_batch(self, reason: str) -> None:
        if not self.batched_observations:
            return
        change = {
            "kind": "OBSERVATION_BATCH_RELEASED", "reason": reason,
            "count": len(self.batched_observations),
            "claims": sorted({event.claim_id for event in self.batched_observations}),
        }
        self._record_recent_change(change)
        self.store.append("OBSERVATION_BATCH_RELEASED", change)
        self.batched_observations.clear()
        self._request_director(
            "observation batch released",
            meaningful_change=True,
        )

    def _queue_next_audit(self, event: CandidateEvent) -> None:
        self._discard_stale_semantic_audit_passes(
            event, source="audit_queue",
        )
        audit_kind = self.audit_gate.next_audit_kind(event)
        if audit_kind is None:
            return
        priority = {
            Impact.CRITICAL: 1.0,
            Impact.HIGH: 0.85,
            Impact.MEDIUM: 0.6,
            Impact.LOW: 0.4,
        }[Impact(event.impact)]
        lease = self.audit_leases.ensure(
            event.fingerprint, audit_kind, priority=priority,
        )
        already_pending = any(
            item.metadata.get("audit_lease_id") == lease.lease_id
            for item in self.pending_audits
        )
        already_active = any(
            item.kind == "audit"
            and item.task.metadata.get("audit_lease_id") == lease.lease_id
            for item in self.active.values()
        )
        if already_pending or already_active:
            self.store.append("AUDIT_LEASE_DEDUPLICATED", {
                "lease_id": lease.lease_id,
                "candidate_fingerprint": event.fingerprint,
                "audit_kind": audit_kind,
                "status": lease.status,
            })
            return
        role = Role.EVALUATOR_AUDITOR if audit_kind == "independent_evaluator" else Role.AUDITOR
        semantic_audit_context = self._semantic_audit_context(
            event,
            audit_kind=audit_kind,
            artifact_hashes=dict(
                self.candidate_artifact_hashes.get(event.fingerprint) or {}
            ),
        )
        task = ResearchTask(
            task_id=lease.lease_id,
            role=role, target_claim=event.claim_id,
            exact_objective=event.exact_statement,
            why_now=f"{event.impact} candidate requires immediate independent audit",
            dependencies=[], expected_information_gain="trust-state decision",
            research_impact=event.impact, estimated_cost_tier="HIGH",
            required_files=list(event.artifact_paths), stop_conditions=["PASS, REJECT, or UNRESOLVED"],
            output_contract="audit_result.schema.json",
            metadata={
                "candidate_fingerprint": event.fingerprint,
                "audit_kind": audit_kind,
                "audit_lease_id": lease.lease_id,
                "audit_attempt": lease.attempt,
                "representation_id": event.representation_id,
                "artifact_hashes": dict(
                    self.candidate_artifact_hashes.get(event.fingerprint) or {}
                ),
                "semantic_audit_context": semantic_audit_context,
            },
            representation=event.representation,
        )
        self.pending_audits.append(task)
        self.store.append("AUDIT_QUEUED", {
            "task_id": task.task_id,
            "lease_status": lease.status,
            **task.metadata,
        })

    def _semantic_audit_context(
        self,
        event: CandidateEvent,
        *,
        audit_kind: str,
        artifact_hashes: dict[str, str],
    ) -> dict[str, Any] | None:
        if not event.semantic_bridge_ids:
            return None
        state = self.audit_gate.states[event.fingerprint]
        identity = semantic_validator_identity(audit_kind)
        version = SEMANTIC_VALIDATOR_VERSION
        audit_config = dict(self.config.raw["audit"])
        audit_config_sha256 = stable_hash(audit_config)
        current_policy = build_policy_manifest(self.config)
        policy_manifest_sha256 = str(current_policy["manifest_sha256"])
        validation_authority_head = self._current_validation_authority_head()
        dependency_shape_sha256 = stable_hash(
            self.graph.canonical_dependency_shape(event.claim_id)
        )
        candidate_scope_sha256 = stable_hash({
            "candidate_fingerprint": event.fingerprint,
            "dependency_shape_sha256": dependency_shape_sha256,
            "validation_authority_head": validation_authority_head,
        })
        pass_scope_sha256 = stable_hash({
            "candidate_fingerprint": event.fingerprint,
            "claim_id": event.claim_id,
            "exact_statement": " ".join(event.exact_statement.split()),
            "representation_id": event.representation_id,
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "semantic_evidence_hashes": dict(sorted(
                self.candidate_semantic_evidence_hashes.get(event.fingerprint, {}).items()
            )),
            "semantic_bridge_ids": list(event.semantic_bridge_ids),
            "candidate_scope_sha256": candidate_scope_sha256,
        })
        config_sha256 = stable_hash({
            "audit_kind": audit_kind,
            "audit_required": state.required,
            "output_schema_sha256": stable_hash(self._audit_output_schema(event)),
            "policy_manifest_sha256": policy_manifest_sha256,
            "semantic_head": self.semantic_alignment.source_sha256,
            "contract_head": self.semantic_trust.contract_head,
            "validation_authority_head": validation_authority_head,
        })
        return {
            "schema_version": SEMANTIC_AUDIT_AUTHORITY_CONTEXT_SCHEMA_VERSION,
            "validation_authority_head": validation_authority_head,
            "validator_identity": identity,
            "validator_version": version,
            "audit_config_sha256": audit_config_sha256,
            "policy_manifest_sha256": policy_manifest_sha256,
            "validator_config_sha256": config_sha256,
            "pass_scope_sha256": pass_scope_sha256,
        }

    @staticmethod
    def _require_semantic_audit_context(
        assigned: Any, expected: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if assigned != expected:
            raise ValueError(
                "semantic validator version, config, or PASS scope changed after assignment"
            )
        if expected is None:
            return None
        return normalize_semantic_audit_authority_context(assigned)

    def _semantic_audit_pass_is_current(
        self, event: CandidateEvent, result: AuditResult,
    ) -> bool:
        if result.verdict != "PASS" or not event.semantic_bridge_ids:
            return True
        if result.semantic_authority_context is None:
            return False
        context = normalize_semantic_audit_authority_context(
            result.semantic_authority_context
        )
        expected = self._semantic_audit_context(
            event,
            audit_kind=result.audit_kind,
            artifact_hashes=dict(
                self.candidate_artifact_hashes.get(event.fingerprint) or {}
            ),
        )
        return expected is not None and context == expected

    def _discard_stale_semantic_audit_passes(
        self, event: CandidateEvent, *, source: str,
    ) -> list[AuditResult]:
        state = self.audit_gate.states.get(event.fingerprint)
        if state is None or not event.semantic_bridge_ids:
            return []
        stale_ids = {
            result.audit_id for result in state.results
            if result.verdict == "PASS"
            and not self._semantic_audit_pass_is_current(event, result)
        }
        discarded = self.audit_gate.discard_pass_results(
            event.fingerprint, stale_ids,
        )
        if discarded:
            self.store.append("SEMANTIC_AUDIT_PASSES_STALE", {
                "candidate_fingerprint": event.fingerprint,
                "claim_id": event.claim_id,
                "audit_ids": [result.audit_id for result in discarded],
                "recorded_validation_authority_heads": [
                    (
                        result.semantic_authority_context or {}
                    ).get("validation_authority_head")
                    for result in discarded
                ],
                "current_validation_authority_head": (
                    self._current_validation_authority_head()
                ),
                "source": source,
                "action": "retain historical evidence and require fresh audit",
            })
        return discarded

    def _restore_audit_result(
        self, event: CandidateEvent, result: AuditResult,
    ) -> bool:
        if result.verdict == "PASS" and event.semantic_bridge_ids:
            if not self._semantic_audit_pass_is_current(event, result):
                return False
            expected_kind = self.audit_gate.next_audit_kind(event)
            if expected_kind != result.audit_kind:
                return False
        self.audit_gate.record(result)
        return True

    async def _launch_audits(self) -> None:
        if not self.lifecycle.can_dispatch:
            return
        impact_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
        self.pending_audits.sort(key=lambda task: (
            -self.audit_leases.by_id(
                str(task.metadata["audit_lease_id"])
            ).priority,
            -impact_rank.get(task.research_impact, 0),
            self.audit_leases.by_id(
                str(task.metadata["audit_lease_id"])
            ).updated_at,
            task.task_id,
        ))
        active_count = sum(item.kind == "audit" for item in self.active.values())
        launched = 0
        budget_blocked = 0
        deferred: list[ResearchTask] = []
        while (
            self.pending_audits
            and active_count < self.max_audit
        ):
            task = self.pending_audits.pop(0)
            if sum(
                item.task.role == task.role for item in self.active.values()
            ) >= self.config.role_concurrency(task.role):
                deferred.append(task)
                continue
            estimated = self._estimated_tokens(task.role, task.estimated_cost_tier)
            if not self.governor.may_start(task.role, estimated):
                budget_blocked += 1
                self._emit_scheduler_event_once(
                    f"audit-paused:{task.task_id}:budget", "AUDIT_PAUSED",
                    {"task_id": task.task_id, "reason": "insufficient role/global token budget"},
                )
                deferred.append(task)
                continue
            state = self.audit_gate.states[task.metadata["candidate_fingerprint"]]
            event = state.event
            artifacts_ok, expected_hashes, observed_hashes = (
                self._verify_candidate_artifacts(event)
            )
            if not artifacts_ok:
                self._record_candidate_artifact_drift(
                    event,
                    phase="audit_dispatch",
                    expected=expected_hashes,
                    observed=observed_hashes,
                )
                continue
            job_id = self._new_job_id()
            workspace, writable, metadata = self.workspace.create_job_workspace(
                task.task_id, job_id=job_id,
            )
            sealed_bundle_files = self.artifact_store.materialize(
                event.artifact_paths, workspace,
            )
            canonical_inputs = (
                self.config.manifest.canonical_for("audit")
                if self.config.manifest is not None else (
                    self.config.project_root / "state" / "PROGRESS.md",
                    self.config.project_root / "claims" / "CLAIMS.md",
                )
            )
            canonical_inputs = tuple(
                self.workspace.materialize_input(workspace, path)
                for path in canonical_inputs
            )
            nightly_claim_graph = self.workspace.materialize_input(
                workspace, self.active_graph_path,
            )
            semantic_contract_input = self._semantic_contract_input(workspace)
            packet = self.workspace.write_task_packet(workspace, {
                "candidate": event.to_dict(),
                "candidate_artifact_hashes": expected_hashes,
                "verified_domain_evidence_receipts": list(
                    self.domain_evidence_receipts.get(event.fingerprint) or []
                ),
                "sealed_candidate_bundle_files": [
                    str(path) for path in sealed_bundle_files
                ],
                "canonical_inputs": [str(path) for path in canonical_inputs],
                "nightly_claim_graph": str(nightly_claim_graph),
                "semantic_contract": semantic_contract_input,
                "semantic_claim": self.semantic_alignment.evaluate_claim(
                    event.claim_id,
                    trust_state=self.semantic_trust,
                    claim_statement=event.exact_statement,
                    claim_assumptions=event.assumptions,
                    claim_graph=self.graph,
                    validation_authority_head=(
                        self._current_validation_authority_head()
                    ),
                    parent_claim_id=event.parent_claim_id,
                    check_parent_claim_id=True,
                ).to_dict(),
                "semantic_audit_context": task.metadata.get(
                    "semantic_audit_context"
                ),
                "producer_transcript": None,
                "research_policy": self._policy_view(task.role),
                "workspace": metadata,
            })
            prompt = auditor_prompt(
                self.config.project_root, event, task.metadata["audit_kind"], packet,
                self._policy_view(task.role),
            )
            audit_job_id = self._start_job(
                task, prompt, self._audit_output_schema(event), workspace, writable,
                "audit", estimated_tokens=estimated, job_id=job_id,
            )
            self.audit_leases.activate(
                str(task.metadata["audit_lease_id"]), audit_job_id,
            )
            active_count += 1
            launched += 1
        self.pending_audits = deferred + self.pending_audits
        if budget_blocked and launched == 0 and not self.active:
            self.scheduler_stop_reason = "pending audit cannot start within remaining token budget"

    async def _launch_research(self, capacity: int, allow_exploration: bool) -> None:
        if not self.lifecycle.can_dispatch:
            return
        self._defer_nonretryable_pending_routes(source="dispatch_preflight")
        active_audits = sum(item.kind == "audit" for item in self.active.values())
        scheduling = self.dynamic_scheduler.decide(
            pending_audits=len(self.pending_audits),
            active_audits=active_audits,
            tasks=self.pending_research,
            route_counts=self.route_dispatch_counts,
        )
        capacity = min(
            max(0, int(capacity)),
            self.max_research_workers,
            scheduling.target_research,
        )
        active_research = sum(item.kind == "research" for item in self.active.values())
        scheduler_signature = (
            round(scheduling.audit_backlog_pressure, 6),
            scheduling.target_research, active_research, active_audits,
            len(self.pending_audits), len(self.pending_research),
        )
        if scheduler_signature != self._last_dynamic_scheduler_signature:
            self._last_dynamic_scheduler_signature = scheduler_signature
            self.store.append("DYNAMIC_CONCURRENCY_EVALUATED", {
                "audit_backlog_pressure": scheduling.audit_backlog_pressure,
                "target_research": scheduling.target_research,
                "hard_max_research": self.max_research_workers,
                "active_research": active_research,
                "active_audits": active_audits,
                "pending_audits": len(self.pending_audits),
                "pending_research": len(self.pending_research),
            })
        active_count = sum(item.kind == "research" for item in self.active.values())
        independent_fraction = float(self.config.raw["scheduler"]["independent_exploration_fraction"])
        reserve = (
            math.ceil(capacity * independent_fraction)
            if allow_exploration and len(self.pending_research) >= capacity
            else 0
        )
        independent_active = sum(
            item.kind == "research" and item.task.is_independent_exploration
            for item in self.active.values()
        )
        launched = 0
        budget_blocked = 0
        deferred: list[ResearchTask] = []
        while (
            self.pending_research
            and active_count < capacity
        ):
            known_representations = set(self.claim_representations.values())
            eligible = [
                task for task in self.pending_research
                if (allow_exploration or task.role != Role.EXPLORER)
                and sum(
                    item.task.role == task.role for item in self.active.values()
                ) < self.config.role_concurrency(task.role)
                and self.dynamic_scheduler.eligible_under_pressure(
                    task,
                    pressure=scheduling.audit_backlog_pressure,
                    known_representation_ids=known_representations,
                )
            ]
            if not eligible:
                break
            need_independent = reserve > independent_active
            choice = next(
                (task for task in eligible if task.is_independent_exploration), None
            ) if need_independent else None
            if need_independent and choice is None and any(
                task.is_independent_exploration for task in deferred
            ):
                break
            if need_independent and choice is None:
                pending_key = stable_hash(sorted(task.fingerprint for task in eligible))
                frontier_changed = self._emit_scheduler_event_once(
                    f"independent-missing:{reserve}:{pending_key}", "EXPLORATION_SLOT_RESERVED",
                    {
                        "required_independent_slots": reserve,
                        "reason": "no independent task available; request fresh Director diversification",
                    },
                )
                director_estimate = self._estimated_tokens(Role.DIRECTOR, "MEDIUM")
                if (
                    frontier_changed
                    and self.governor.may_start(Role.DIRECTOR, director_estimate)
                ):
                    self._request_director(
                        "independent exploration slot has no eligible task",
                        meaningful_change=True,
                    )
                elif frontier_changed:
                    self.director_needed = False
                    self.scheduler_stop_reason = (
                        "diversification required but fresh Director cannot start within remaining token budget"
                    )
                break
            task = choice or max(
                eligible,
                key=lambda item: (
                    scheduling.task_scores.get(item.task_id, 0.0), item.priority,
                ),
            )
            self.pending_research.remove(task)
            try:
                required_file_access = self._required_file_access(task)
            except ValueError as exc:
                reason = _sanitize_live_text(exc)
                rejection = {
                    "task_id": task.task_id,
                    "representation_id": task.representation_id,
                    "reason": reason,
                    "phase": "dispatch",
                }
                self.store.append("TASK_REJECTED", rejection)
                self.director_constraints.append({
                    "action": "REPAIR_TASK_INPUTS",
                    "claim_id": task.target_claim,
                    "task_id": task.task_id,
                    "reason": reason,
                    "source": "required_file_dispatch",
                })
                self._request_director(
                    "accepted task required file became unavailable",
                    meaningful_change=False,
                    immediate=True,
                )
                continue
            estimated = self._estimated_tokens(task.role, task.estimated_cost_tier)
            if not self.governor.may_start(task.role, estimated):
                budget_blocked += 1
                self._emit_scheduler_event_once(
                    f"task-paused:{task.fingerprint}:budget", "TASK_PAUSED",
                    {"task_id": task.task_id, "reason": "role/global budget"},
                )
                deferred.append(task)
                continue
            job_id = self._new_job_id()
            workspace, writable, metadata = self.workspace.create_job_workspace(
                task.task_id, task.modifies_code, job_id=job_id,
            )
            required_file_access = self.workspace.materialize_required_file_access(
                workspace, required_file_access,
            )
            nightly_claim_graph = self.workspace.materialize_input(
                workspace, self.active_graph_path,
            )
            semantic_contract_input = self._semantic_contract_input(workspace)
            semantic_binding = self.semantic_alignment.claims.get(
                task.target_claim,
            )
            # The worker may submit a single validated event file, but cannot
            # write the controller-owned ledger, candidates, audits, or state.
            job_inbox = self._task_inbox(task.task_id)
            job_inbox.mkdir(parents=True, exist_ok=True)
            writable = [*writable, job_inbox]
            packet = self.workspace.write_task_packet(workspace, {
                "task": task.to_dict(), "workspace": metadata,
                "canonical_project": None,
                "nightly_claim_graph": str(nightly_claim_graph),
                "semantic_contract": semantic_contract_input,
                "semantic_claim": self.semantic_alignment.evaluate_claim(
                    task.target_claim,
                    trust_state=self.semantic_trust,
                    claim_statement=(
                        self.graph.claims[task.target_claim].statement
                        if task.target_claim in self.graph.claims else None
                    ),
                    claim_assumptions=(
                        self.graph.claims[task.target_claim].assumptions
                        if task.target_claim in self.graph.claims else None
                    ),
                    claim_graph=self.graph,
                    validation_authority_head=(
                        self._current_validation_authority_head()
                    ),
                    parent_claim_id=(
                        self.graph.claims[task.target_claim].parent_claim_id
                        if task.target_claim in self.graph.claims else None
                    ),
                    check_parent_claim_id=task.target_claim in self.graph.claims,
                ).to_dict(),
                "required_file_access": required_file_access,
                "candidate_protocol": {
                    "assigned_claim_id": task.target_claim,
                    "semantic_binding": (
                        {
                            "representation_id": semantic_binding.representation_id,
                            "required_bridge_ids": list(
                                semantic_binding.required_bridges
                            ),
                            "required_for_final_claim": (
                                task.target_claim == self.final_conjecture_claim_id
                            ),
                        }
                        if semantic_binding is not None else None
                    ),
                    "allow_derived_claims": bool(
                        task.metadata.get("allow_derived_claims", False)
                    ),
                    "exact_claim": {
                        "statement": self.graph.claims[task.target_claim].statement,
                        "assumptions": self.graph.claims[task.target_claim].assumptions,
                        "dependencies": self.graph.claims[task.target_claim].dependencies,
                    } if task.target_claim in self.graph.claims else None,
                    "derived_submission": {
                        "claim_id": "AUTO_DERIVED",
                        "parent_claim_id": task.target_claim,
                        "normalization": "emit-event helper replaces AUTO_DERIVED with a content-addressed id",
                    },
                },
                "research_policy": self._policy_view(task.role),
            })
            event_command = (
                f'python "{Path(__file__).resolve().parent / "emit_event.py"}" '
                f'--project "{self.config.project_root}" '
                f'--file "{workspace / "candidate_event.json"}" '
                f'--inbox-dir "{job_inbox}"'
            )
            prompt = worker_prompt(
                self.config.project_root, task, packet, event_command,
                self._policy_view(task.role),
            )
            self._start_job(
                task, prompt, self._schema("worker_result.schema.json"), workspace, writable,
                "research", estimated_tokens=estimated, job_id=job_id,
            )
            self.route_dispatch_counts[task.route_family] = (
                self.route_dispatch_counts.get(task.route_family, 0) + 1
            )
            active_count += 1
            launched += 1
            if task.is_independent_exploration:
                independent_active += 1
        self.pending_research.extend(deferred)
        if budget_blocked and launched == 0 and not self.active:
            self.scheduler_stop_reason = "pending research cannot start within remaining token budget"

    async def _collect_completed(self) -> None:
        done = [job_id for job_id, item in self.active.items() if item.future.done()]
        # Advance the watermark for every non-Director completion before
        # accepting any Director in this batch. This makes completion ordering
        # irrelevant and prevents a plan from a stale snapshot being applied.
        for job_id in done:
            active = self.active[job_id]
            if active.kind != "director":
                self._request_director(
                    f"{active.kind} job {job_id} reached a terminal envelope",
                    meaningful_change=True,
                )
        for job_id in done:
            active = self.active.pop(job_id)
            if active.future.cancelled():
                self._record_job_cancelled(
                    job_id, active, "job future cancelled",
                    remote_cancel_succeeded=None,
                )
                self.governor.release(job_id)
                continue
            try:
                outcome = active.future.result()
            except Exception as exc:
                outcome = JobOutcome(
                    job_id=job_id, task_id=active.task.task_id, role=active.task.role,
                    claim_id=active.task.target_claim, status="ERROR", result={},
                    error=f"backend job exception: {exc}", failure_kind="backend_internal",
                )
            # Defense in depth for custom/mock backends: normalize a forbidden
            # or rerouted tier into the failure envelope before useful/telemetry accounting
            # and before JOB_COMPLETED is persisted.  A later mutation would
            # leave the append-only event looking successful.
            observed_tier = str(outcome.observed_service_tier or "unobservable")
            requested_tier = active.requested_service_tier
            allowed_observed = allowed_observed_service_tiers(requested_tier)
            tier_mismatch = observed_tier not in allowed_observed
            telemetry_required = bool(
                outcome.succeeded
                or outcome.failure_kind == "service_tier_policy"
                or observed_tier != "unobservable"
            )
            if tier_mismatch and telemetry_required:
                self.stop_for_review = (
                    "service tier policy violation: "
                    f"requested={requested_tier!r}, observed={observed_tier!r}"
                )
                self._internal_failure = True
                outcome.error = self.stop_for_review
                outcome.failure_kind = "service_tier_policy"
                outcome.retryable = False
                self.store.append("SERVICE_TIER_POLICY_VIOLATION", {
                    "job_id": job_id, "observed_service_tier": observed_tier,
                    "action": "stop run and cancel remaining work",
                })
            expected_route = (active.model, active.reasoning_effort)
            observed_route = (outcome.model, outcome.reasoning_effort)
            route_mismatch = bool(
                (outcome.model and active.model and outcome.model != active.model)
                or (
                    outcome.reasoning_effort
                    and active.reasoning_effort
                    and outcome.reasoning_effort != active.reasoning_effort
                )
                or (
                    outcome.provider and active.provider
                    and outcome.provider != active.provider
                )
                or outcome.requested_service_tier != requested_tier
            )
            if route_mismatch:
                original_failure = None if outcome.succeeded else {
                    "status": outcome.status,
                    "error": outcome.error,
                    "failure_kind": outcome.failure_kind,
                    "retryable": outcome.retryable,
                    "server_error": outcome.server_error,
                }
                reason = (
                    "provider/model route policy violation: controller expected "
                    f"{active.provider!r}:{expected_route[0]!r}/{expected_route[1]!r} "
                    f"tier={requested_tier!r}, backend reported "
                    f"{outcome.provider!r}:{observed_route[0]!r}/{observed_route[1]!r} "
                    f"tier={outcome.requested_service_tier!r}"
                )
                self.stop_for_review = reason
                self._internal_failure = True
                self.store.append("MODEL_ROUTE_POLICY_VIOLATION", {
                    "job_id": job_id,
                    "role": outcome.role,
                    "requested_model": active.model,
                    "requested_reasoning_effort": active.reasoning_effort,
                    "requested_provider": active.provider,
                    "requested_service_tier": requested_tier,
                    "observed_model": outcome.model,
                    "observed_reasoning_effort": outcome.reasoning_effort,
                    "observed_provider": outcome.provider,
                    "original_failure": original_failure,
                    "action": (
                        "fail successful envelope closed; preserve an existing failure "
                        "envelope without relabeling its root cause"
                    ),
                })
                if outcome.succeeded:
                    outcome.error = reason
                    outcome.failure_kind = "model_route_policy"
                    outcome.retryable = False
            useful = self._is_useful(outcome)
            self.governor.record(
                job_id, outcome.role, outcome.token_usage, useful, outcome.cost_usd,
            )
            self.governor.release(job_id)
            record = outcome.to_dict()
            record["useful"] = useful
            record["cwd"] = active.workspace
            record["workspace_metadata"] = active.workspace_metadata
            record["start_time"] = active.started_at
            record["end_time"] = utc_now()
            record["elapsed_seconds"] = max(0.0, time.monotonic() - active.started_monotonic)
            record["exit_reason"] = outcome.error or outcome.status
            record["failure_message"] = (
                outcome.failure_message if not outcome.succeeded else None
            )
            artifact_hashes: dict[str, str] = {}
            artifact_errors: list[str] = []
            artifact_root = (
                Path(active.workspace).resolve()
                if active.workspace else self.config.project_root.resolve()
            )
            for raw in outcome.artifact_paths:
                path = Path(raw)
                resolved = (
                    (artifact_root / path).resolve()
                    if not path.is_absolute() else path.resolve()
                )
                if not resolved.is_relative_to(artifact_root):
                    artifact_errors.append(f"escapes job workspace: {raw}")
                elif not resolved.is_file():
                    artifact_errors.append(f"missing or not a file: {raw}")
                else:
                    artifact_hashes[str(resolved)] = file_digest(resolved)
            record["artifact_hashes"] = artifact_hashes
            record["artifact_validation_errors"] = artifact_errors
            self.completed_jobs.append(record)
            self.store.append("JOB_COMPLETED", record)
            finding = (
                outcome.result.get("main_finding")
                or outcome.result.get("short_rationale")
                or outcome.result.get("assessment")
            )
            if isinstance(finding, (dict, list)):
                finding = json.dumps(_bounded_value(finding), ensure_ascii=False)
            self.live_store.append("AGENT_JOB_COMPLETED", {
                "job_id": job_id, "role": outcome.role, "task_id": outcome.task_id,
                "claim_id": outcome.claim_id, "status": outcome.status,
                "result_type": outcome.result.get("result_type"),
                "summary": _sanitize_live_text(finding)[:500],
                "error": _sanitize_live_text(outcome.error)[:500] if outcome.error else None,
                "total_tokens": outcome.token_usage.total_tokens,
                "token_telemetry": outcome.token_telemetry,
                "model": outcome.model or active.model,
                "reasoning_effort": outcome.reasoning_effort or active.reasoning_effort,
                "elapsed_seconds": record["elapsed_seconds"],
            })
            if active.kind == "director":
                self._director_active = False
                self._accept_director_result(outcome)
            elif active.kind == "audit":
                self._accept_audit_result(
                    outcome, Path(active.workspace) if active.workspace else None,
                    str(active.task.metadata.get("candidate_fingerprint", "")),
                    dict(active.task.metadata.get("artifact_hashes") or {}),
                    str(active.task.metadata.get("audit_lease_id") or ""),
                    active.task.metadata.get("semantic_audit_context"),
                )
            else:
                self._accept_research_result(outcome, active.task, record)
            self._blocker_repair_jobs.discard(job_id)

    async def _collect_terminal_envelopes_before_shutdown(self) -> None:
        """Persist futures that won the race with controller shutdown.

        A scheduler or operator stop can be observed immediately after a child
        future completes but before the next loop iteration reaches the normal
        collectors. Treating every remaining entry as cancelled would erase a
        valid result, its telemetry, and any emitted candidate/audit evidence.
        Collect completed envelopes first; only genuinely in-flight work is
        cancelled by the caller's remaining shutdown path.
        """
        collectors = (
            ("mechanical", self._collect_mechanical_completed),
            ("top_level", self._collect_completed),
        )
        for kind, collector in collectors:
            try:
                await collector()
            except Exception as exc:
                self._internal_failure = True
                self.store.append("SHUTDOWN_COLLECTION_FAILED", {
                    "kind": kind,
                    "error": (
                        f"{type(exc).__name__}: {_sanitize_live_text(exc)[:1000]}"
                    ),
                    "action": (
                        "preserve the collection error and cancel only entries "
                        "that remain registered as active"
                    ),
                })

    def _accept_director_result(self, outcome: JobOutcome) -> None:
        """Apply a v2 plan while rebasing every action against current state."""
        incremental = self._director_incremental
        self._director_incremental = False
        if (
            not outcome.succeeded
            and outcome.failure_kind == "provider_transport_lost"
        ):
            self._pause_for_provider_transport_loss(outcome)
            self.store.append("DIRECTOR_REQUEUED_AFTER_PROVIDER_TRANSPORT_LOSS", {
                "job_id": outcome.job_id,
                "action": "request a fresh Director in the next epoch",
            })
            return
        if (
            not outcome.succeeded
            and outcome.failure_kind == "provider_quota_exhausted"
        ):
            self._pause_for_provider_quota(outcome)
            self.store.append("DIRECTOR_REQUEUED_AFTER_PROVIDER_QUOTA", {
                "job_id": outcome.job_id,
                "action": "request a fresh Director after provider reset",
            })
            return
        if not outcome.succeeded:
            failure_kind = outcome.failure_kind or "director_failure"
            self.store.append("DIRECTOR_REJECTED", self._failure_payload(outcome))
            self._queue_director_retry(
                failure_kind, retryable=outcome.retryable, source="job_envelope",
            )
            return
        try:
            plan = DirectorPlan.from_dict(outcome.result)
        except Exception as exc:
            self.store.append("DIRECTOR_REJECTED", {
                "error": _sanitize_live_text(exc),
                "failure_kind": "invalid_director_result",
                "retryable": True,
                "result": _bounded_value(outcome.result),
                "status": outcome.status,
                "thread_id": outcome.thread_id,
                "turn_id": outcome.turn_id,
            })
            self._queue_director_retry(
                "invalid_director_result", retryable=True, source="role_parser",
            )
            return

        stale = self._director_snapshot_version < self._director_requested_version
        if stale:
            self.store.append("DIRECTOR_RESULT_REBASED", {
                "job_id": outcome.job_id,
                "snapshot_version": self._director_snapshot_version,
                "requested_version": self._director_requested_version,
                "state_version": self._state_version,
                "action": "validate actions against current state; retain coalesced replan watermark",
            })

        accepted_tasks: list[ResearchTask] = []
        rejected_tasks: list[dict[str, str]] = []
        provisional_task_bindings = dict(self.task_fingerprints_by_id)
        for task in plan.spawn:
            task_error = self._validate_director_task(task)
            if task_error:
                rejection = {
                    "task_id": task.task_id,
                    "representation_id": task.representation_id,
                    "reason": task_error,
                }
                rejected_tasks.append(rejection)
                self.store.append("TASK_REJECTED", rejection)
                continue
            bound_fingerprint = provisional_task_bindings.get(task.task_id)
            if bound_fingerprint is not None:
                if bound_fingerprint == task.fingerprint:
                    self.store.append("TASK_DEDUPLICATED", {
                        "task_id": task.task_id,
                        "fingerprint": task.fingerprint,
                        "reason": "stable task_id and fingerprint already accepted",
                    })
                else:
                    rejection = {
                        "task_id": task.task_id,
                        "representation_id": task.representation_id,
                        "reason": (
                            f"task_id {task.task_id!r} is already bound to fingerprint "
                            f"{bound_fingerprint}; use a new stable task_id for different "
                            "task content"
                        ),
                    }
                    rejected_tasks.append(rejection)
                    self.store.append("TASK_REJECTED", rejection)
                continue
            if task.fingerprint in self.seen_task_fingerprints:
                self.store.append("TASK_DEDUPLICATED", {
                    "task_id": task.task_id,
                    "fingerprint": task.fingerprint,
                })
                continue
            accepted_tasks.append(task)
            provisional_task_bindings[task.task_id] = task.fingerprint

        prioritized = 0
        for item in plan.audit_priorities:
            fingerprint = str(item.get("candidate_fingerprint") or "")
            priority = float(item.get("priority", 0.0))
            state = self.audit_gate.states.get(fingerprint)
            if state is None or state.terminal:
                self.store.append("DIRECTOR_AUDIT_PRIORITY_REJECTED", {
                    **item,
                    "reason_detail": "candidate has no nonterminal controller-owned audit lease",
                })
                continue
            if self.audit_leases.prioritize(fingerprint, priority):
                prioritized += 1
                self.store.append("DIRECTOR_AUDIT_PRIORITIZED", item)
        self.pending_audits.sort(
            key=lambda task: (
                -float(
                    self.audit_leases.by_id(
                        str(task.metadata.get("audit_lease_id"))
                    ).priority
                ),
                task.task_id,
            )
        )

        for update in plan.route_updates:
            self.store.append("DIRECTOR_ROUTE_UPDATE", {
                **update,
                "snapshot_version": self._director_snapshot_version,
                "rebased": stale,
            })
            route_representation = next(
                (
                    task.representation_id for task in accepted_tasks
                    if task.route_family == str(update["route_id"])
                ),
                RepresentationContract.legacy().representation_id,
            )
            route_status = {
                "OPEN": "ACTIVE",
                "PAUSE": "PAUSED",
                "RESUME": "ACTIVE",
                "RETRY": "ACTIVE",
            }[str(update["action"])]
            self.route_ledger.append(
                route_id=str(update["route_id"]),
                representation_id=route_representation,
                method_tags=[],
                status=route_status,
                failure_class=None,
                retry_condition=update.get("retry_condition"),
                evidence_refs=[],
                source="director",
            )

        for task in accepted_tasks:
            self.representation_contracts.setdefault(
                task.representation_id, task.representation_contract.to_dict(),
            )
            self.seen_task_fingerprints.add(task.fingerprint)
            self.task_fingerprints_by_id[task.task_id] = task.fingerprint
            self.pending_research.append(task)
            self.store.append("TASK_ACCEPTED", {
                "task_id": task.task_id,
                "fingerprint": task.fingerprint,
                "representation_id": task.representation_id,
                "task": task.to_dict(),
            })

        route_deferred = self._defer_nonretryable_pending_routes(
            source="director_route_update",
        )

        self._director_applied_version = max(
            self._director_applied_version, self._director_snapshot_version,
        )
        self.store.append("DIRECTOR_PLAN_ACCEPTED", {
            "assessment": plan.assessment,
            "accepted_tasks": len(accepted_tasks),
            "audit_priorities_applied": prioritized,
            "route_updates": len(plan.route_updates),
            "snapshot_version": self._director_snapshot_version,
            "requested_version": self._director_requested_version,
            "short_rationale": plan.short_rationale,
            "incremental": incremental,
            "rebased": stale,
            "tasks_deferred_by_route_policy": route_deferred,
        })
        self.director_constraints.clear()

        pending_ids_after_route_updates = {
            task.task_id for task in self.pending_research
        }
        runnable = bool(
            any(task.task_id in pending_ids_after_route_updates for task in accepted_tasks)
            or prioritized
        )
        if not runnable:
            repair_constraint: dict[str, Any] = {
                "action": "REPAIR_PLAN",
                "claim_id": self.final_conjecture_claim_id or "FRONTIER",
                "reason": (
                    "the previous Director plan left no runnable research or audit work; "
                    "route updates were recorded but do not keep the execution queue alive"
                ),
                "source": "director_no_runnable_work",
                "route_updates_applied": len(plan.route_updates),
                "rejected_tasks": rejected_tasks,
            }
            self.director_constraints.append(repair_constraint)
            self.store.append("DIRECTOR_PLAN_REPAIR_REQUIRED", repair_constraint)
        else:
            self.director_retry_count = 0
            self.director_retry_counts.clear()
        if stale:
            self._request_director(
                "Director plan was safely rebased but a newer coalesced watermark exists",
                meaningful_change=False,
                immediate=False,
            )
        elif not runnable:
            self._queue_director_retry(
                "director_no_runnable_work",
                retryable=True,
                source="director_semantic_gate",
            )
        else:
            self._replan_after_wave = False

    def _accept_director_result_v1_replay(self, outcome: JobOutcome) -> None:
        incremental = self._director_incremental
        self._director_incremental = False
        if not outcome.succeeded:
            failure_kind = outcome.failure_kind or "director_failure"
            self.store.append("DIRECTOR_REJECTED", self._failure_payload(outcome))
            self._queue_director_retry(
                failure_kind, retryable=outcome.retryable, source="job_envelope",
            )
            return
        try:
            translated = DirectorPlan.from_legacy_replay(outcome.result)
        except Exception as exc:
            self.store.append("DIRECTOR_REJECTED", {
                "error": _sanitize_live_text(exc),
                "failure_kind": "invalid_director_result",
                "retryable": True,
                "protocol_version": 1,
            })
            self._queue_director_retry(
                "invalid_director_result", retryable=True, source="v1_replay",
            )
            return
        self.store.append("DIRECTOR_V1_REPLAY_TRANSLATED", {
            "job_id": outcome.job_id,
            "source_protocol": 1,
            "target_protocol": OUTPUT_PROTOCOL_VERSION,
        })
        outcome.result = translated.to_dict()
        self._director_incremental = incremental
        self._accept_director_result(outcome)
        return
    def _record_task_deferred_by_route_policy(
        self,
        task: ResearchTask,
        *,
        source: str,
        checkpoint_uri: str | None = None,
        release_admission: bool = False,
    ) -> None:
        records = [
            item for item in self.route_ledger.records()
            if item.get("route_id") == task.route_family
        ]
        latest = records[-1] if records else {}
        if release_admission:
            self.seen_task_fingerprints.discard(task.fingerprint)
            if self.task_fingerprints_by_id.get(task.task_id) == task.fingerprint:
                self.task_fingerprints_by_id.pop(task.task_id, None)
        payload = {
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "route_family": task.route_family,
            "route_status": latest.get("status"),
            "retry_condition": latest.get("retry_condition"),
            "source": source,
            "checkpoint_uri": checkpoint_uri,
            "task": task.to_dict(),
                **self._research_failure_payload(),
            "canonical_progress": False,
            "action": (
                "do not dispatch until the durable route retry condition is satisfied; "
                "a fresh Director may re-admit the exact task afterward"
            ),
        }
        self.store.append("TASK_DEFERRED_BY_ROUTE_POLICY", payload)
        self._record_recent_change({
            "kind": "TASK_DEFERRED_BY_ROUTE_POLICY",
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "route_family": task.route_family,
            "retry_condition": latest.get("retry_condition"),
        })

    def _defer_nonretryable_pending_routes(self, *, source: str) -> int:
        deferred = 0
        keep: list[ResearchTask] = []
        for task in self.pending_research:
            if self.route_ledger.route_is_retryable(
                task.route_family, self.satisfied_route_conditions,
            ):
                keep.append(task)
                continue
            self._record_task_deferred_by_route_policy(
                task, source=source, release_admission=True,
            )
            deferred += 1
        self.pending_research = keep
        return deferred

    def _validate_director_task(
        self, task: ResearchTask, *, check_route: bool = True,
    ) -> str | None:
        if check_route and not self.route_ledger.route_is_retryable(
            task.route_family, self.satisfied_route_conditions,
        ):
            return (
                f"route {task.route_family!r} is paused or failed and its durable "
                "retry condition has not been satisfied"
            )
        unknown = set(task.dependencies) - set(self.graph.claims)
        if unknown:
            return (
                f"unknown claim dependencies: {sorted(unknown)}; the dependencies field "
                "accepts existing ClaimGraph claim ids, not task ids; schedule task "
                "sequencing in a later Director wave"
            )
        if set(task.metadata) != {"allow_derived_claims"}:
            return "research task metadata must contain exactly allow_derived_claims"
        if not all(isinstance(task.metadata[key], bool) for key in task.metadata):
            return "research task metadata values must be booleans"
        for dependency in task.dependencies:
            dependency_representation = self.claim_representations.get(dependency)
            if dependency_representation is None:
                continue
            pair = tuple(sorted((task.representation_id, dependency_representation)))
            if (
                task.representation_id != dependency_representation
                and pair not in self.audited_representation_bridges
            ):
                return (
                    "representation mismatch with dependency requires an independently "
                    f"PASSed REPRESENTATION_BRIDGE: {dependency} {pair}"
                )
        try:
            self._required_file_access(task)
        except ValueError as exc:
            return _sanitize_live_text(exc)
        return None

    def _required_file_access(self, task: ResearchTask) -> list[dict[str, str]]:
        return [
            {"reference": raw, "path": str(self._resolve_required_file(raw))}
            for raw in task.required_files
        ]

    def _resolve_required_file(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw:
            raise ValueError("required file reference must be a non-empty string")
        project = self.config.project_root.resolve()
        if raw.startswith(PORTABLE_SCHEMES):
            try:
                resolved = resolve_portable_uri(
                    project, self.layout.autonomous_root, raw,
                )
            except ValueError as exc:
                raise ValueError(f"required file is unavailable: {raw}: {exc}") from exc
        else:
            if "://" in raw:
                raise ValueError(f"unsupported required file URI: {raw}")
            path = Path(raw)
            resolved = (
                (project / path).resolve() if not path.is_absolute() else path.resolve()
            )
        if not resolved.is_relative_to(project):
            raise ValueError(f"required file escapes project: {raw}")
        if not resolved.is_file():
            raise ValueError(f"required file is unavailable: {raw}")
        relative_parts = [part.casefold() for part in resolved.relative_to(project).parts]
        name = resolved.name.casefold()
        if (
            ".git" in relative_parts
            or name == ".env"
            or name.startswith(".env.")
            or name in {
                "auth.json", "credentials.json", "secrets.json",
                "id_rsa", "id_ed25519",
            }
            or resolved.suffix.casefold() in {".key", ".pem", ".p12", ".pfx"}
        ):
            raise ValueError(
                f"required file is blocked as potentially credential-bearing: {raw}"
            )
        return resolved

    def _semantic_audit_receipts(
        self, event: CandidateEvent,
    ) -> list[dict[str, Any]]:
        state = self.audit_gate.states[event.fingerprint]
        artifact_hashes = dict(
            self.candidate_artifact_hashes.get(event.fingerprint) or {}
        )
        receipts: list[dict[str, Any]] = []
        for result in state.results:
            if result.verdict != "PASS":
                continue
            if result.semantic_authority_context is None:
                raise ValueError(
                    "semantic audit PASS lacks its assignment-time authority context"
                )
            context = normalize_semantic_audit_authority_context(
                result.semantic_authority_context
            )
            current_context = self._semantic_audit_context(
                event,
                audit_kind=result.audit_kind,
                artifact_hashes=artifact_hashes,
            )
            if current_context is None:
                raise ValueError("semantic candidate PASS lacks a validator context")
            if context != current_context:
                raise ValueError(
                    "semantic audit PASS belongs to a stale validation authority"
                )
            path = self.audit_root / event.fingerprint / f"{result.audit_id}.json"
            if not path.is_file():
                raise ValueError(f"semantic audit result is unavailable: {result.audit_id}")
            receipts.append({
                "audit_id": result.audit_id,
                "audit_kind": result.audit_kind,
                "auditor_thread_id": result.auditor_thread_id,
                "result_path": portable_project_uri(
                    self.config.project_root, path,
                ),
                "result_sha256": file_digest(path),
                "statement_checked_sha256": text_sha256(result.statement_checked),
                "verified_evidence_level": result.verified_evidence_level,
                "validator_identity": context["validator_identity"],
                "validator_version": context["validator_version"],
                "validator_config_sha256": context[
                    "validator_config_sha256"
                ],
                "pass_scope_sha256": context["pass_scope_sha256"],
                "authority_context": context,
            })
        return receipts

    def _accept_audit_result(
        self,
        outcome: JobOutcome,
        audit_workspace: Path | None = None,
        assigned_fingerprint: str = "",
        assigned_artifact_hashes: dict[str, str] | None = None,
        audit_lease_id: str = "",
        assigned_semantic_audit_context: Any = None,
    ) -> None:
        if (
            not outcome.succeeded
            and outcome.failure_kind == "provider_transport_lost"
        ):
            if audit_lease_id:
                try:
                    lease = self.audit_leases.by_id(audit_lease_id)
                    if lease.status == AuditLeaseStatus.ACTIVE:
                        self.audit_leases.retry_wait(audit_lease_id)
                except ValueError as lease_error:
                    self.store.append("AUDIT_LEASE_ERROR", {
                        "lease_id": audit_lease_id,
                        "error": _sanitize_live_text(lease_error),
                    })
            self._pause_for_provider_transport_loss(outcome)
            self.store.append("AUDIT_REQUEUED_AFTER_PROVIDER_TRANSPORT_LOSS", {
                "job_id": outcome.job_id,
                "candidate_fingerprint": assigned_fingerprint,
                "audit_lease_id": audit_lease_id,
                "action": "retain the nonterminal candidate for fresh-epoch audit",
            })
            return
        if (
            not outcome.succeeded
            and outcome.failure_kind == "provider_quota_exhausted"
        ):
            if audit_lease_id:
                try:
                    lease = self.audit_leases.by_id(audit_lease_id)
                    if lease.status == AuditLeaseStatus.ACTIVE:
                        self.audit_leases.retry_wait(audit_lease_id)
                except ValueError as lease_error:
                    self.store.append("AUDIT_LEASE_ERROR", {
                        "lease_id": audit_lease_id,
                        "error": _sanitize_live_text(lease_error),
                    })
            self._pause_for_provider_quota(outcome)
            self.store.append("AUDIT_REQUEUED_AFTER_PROVIDER_QUOTA", {
                "job_id": outcome.job_id,
                "candidate_fingerprint": assigned_fingerprint,
                "audit_lease_id": audit_lease_id,
                "action": "retain the nonterminal candidate for fresh-epoch audit",
            })
            return
        fingerprint = (
            outcome.result.get("candidate_fingerprint") if outcome.succeeded else None
        ) or assigned_fingerprint
        assignment_invalid = bool(
            not fingerprint or fingerprint not in self.audit_gate.states
            or (assigned_fingerprint and fingerprint != assigned_fingerprint)
        )
        if (
            not outcome.succeeded or assignment_invalid
        ):
            if audit_lease_id:
                try:
                    lease = self.audit_leases.by_id(audit_lease_id)
                    if lease.status == AuditLeaseStatus.ACTIVE:
                        self.audit_leases.retry_wait(audit_lease_id)
                except ValueError as lease_error:
                    self.store.append("AUDIT_LEASE_ERROR", {
                        "lease_id": audit_lease_id,
                        "error": _sanitize_live_text(lease_error),
                    })
            error = outcome.failure_message if not outcome.succeeded else (
                "audit candidate fingerprint does not match assignment"
                if assigned_fingerprint and fingerprint != assigned_fingerprint else "unknown candidate"
            )
            failure_kind = outcome.failure_kind or "role_semantic_validation"
            retryable = bool(outcome.retryable) if not assignment_invalid else bool(
                assigned_fingerprint in self.audit_gate.states
            )
            self.store.append("AUDIT_ERROR", {
                **self._failure_payload(outcome),
                "error": error,
                "failure_kind": failure_kind,
                "retryable": retryable,
                "assigned_candidate_fingerprint": assigned_fingerprint,
            })
            expected = str(assigned_fingerprint or outcome.result.get("candidate_fingerprint") or "")
            max_retries = self._retry_limit(failure_kind, outcome.role)
            retry = self._retry_count(
                self.audit_retry_counts, expected, failure_kind,
            )
            if (
                retryable and expected in self.audit_gate.states
                and retry < max_retries
            ):
                self._set_retry_count(
                    self.audit_retry_counts, expected, failure_kind, retry + 1,
                )
                event = self.audit_gate.states[expected].event
                self._queue_next_audit(event)
                self.store.append("AUDIT_RETRY_QUEUED", {
                    "candidate_fingerprint": expected, "retry": retry + 1,
                    "max_retries": max_retries,
                    "retry_class": self._retry_class(failure_kind),
                    "failure_kind": failure_kind,
                })
            elif expected in self.audit_gate.states:
                self.store.append("AUDIT_RETAINED_AFTER_ERROR", {
                    "candidate_fingerprint": expected, "failure_kind": failure_kind,
                })
                self.store.append("AUDIT_FAILURE_ISOLATED", {
                    "candidate_fingerprint": expected,
                    "failure_kind": failure_kind,
                    "action": "preserve candidate and request a fresh Director replan",
                })
                self._require_continuation(
                    source="audit_failure",
                    reason=(
                        f"audit failed after bounded retries ({failure_kind}); preserve the "
                        "candidate and choose a fresh audit or supporting research route"
                    ),
                )
            else:
                self._request_director(
                    "audit result could not be associated with a pending candidate",
                    meaningful_change=True,
                )
            return
        state = self.audit_gate.states[fingerprint]
        self._discard_stale_semantic_audit_passes(
            state.event, source="audit_completion",
        )
        baseline_hashes = dict(self.candidate_artifact_hashes.get(fingerprint) or {})
        if assigned_artifact_hashes is not None and assigned_artifact_hashes != baseline_hashes:
            self._record_candidate_artifact_drift(
                state.event,
                phase="audit_assignment_binding",
                expected=baseline_hashes,
                observed=dict(assigned_artifact_hashes),
            )
            if audit_lease_id:
                self.audit_leases.retry_wait(audit_lease_id)
            return
        artifacts_ok, expected_hashes, observed_hashes = self._verify_candidate_artifacts(
            state.event
        )
        if not artifacts_ok:
            self._record_candidate_artifact_drift(
                state.event,
                phase="audit_completion",
                expected=expected_hashes,
                observed=observed_hashes,
            )
            if audit_lease_id:
                self.audit_leases.retry_wait(audit_lease_id)
            return
        try:
            if not audit_lease_id:
                raise ValueError("audit completion is missing its controller-owned lease")
            lease = self.audit_leases.by_id(audit_lease_id)
            expected_semantic_context = self._semantic_audit_context(
                state.event,
                audit_kind=lease.audit_kind,
                artifact_hashes=baseline_hashes,
            )
            completed_semantic_context = self._require_semantic_audit_context(
                assigned_semantic_audit_context, expected_semantic_context,
            )
            result = AuditResult.from_wire_v2(
                outcome.result,
                audit_id=f"{lease.lease_id}-{outcome.job_id}",
                candidate_fingerprint=fingerprint,
                auditor_thread_id=str(outcome.thread_id or f"job:{outcome.job_id}"),
                audit_kind=lease.audit_kind,
                statement_checked=state.event.exact_statement,
                report_path=None,
                semantic_authority_context=completed_semantic_context,
            )
            if result.verdict == "PASS":
                failed_checks = [
                    str(item.get("name") or "unnamed check")
                    for item in result.checks
                    if item.get("passed") is not True
                ]
                if not result.checks or result.gaps or failed_checks:
                    original = result.to_dict()
                    blocking = list(result.gaps)
                    if not result.checks:
                        blocking.append("audit returned PASS without any check")
                    if failed_checks:
                        blocking.append(
                            "audit returned PASS with failed checks: " + ", ".join(failed_checks)
                        )
                    result.verdict = "UNRESOLVED"
                    result.gaps = blocking
                    result.verified_evidence_level = EvidenceLevel.E0_SPECULATIVE
                    self.store.append("AUDIT_RESULT_DOWNGRADED", {
                        "job_id": outcome.job_id,
                        "candidate_fingerprint": fingerprint,
                        "from_verdict": "PASS",
                        "to_verdict": "UNRESOLVED",
                        "reason": (
                            "PASS contained blocking gaps, missing checks, or failed checks; "
                            "candidate was retained without trust promotion"
                        ),
                        "original_result": _bounded_value(original),
                    })
            self._validate_domain_audit_evidence(
                state.event, result.verified_evidence_level,
            )
            trust = self.audit_gate.record(result)
            self.audit_leases.finish(audit_lease_id, result.verdict)
            self._clear_retry_counts(self.audit_retry_counts, fingerprint)
        except Exception as exc:
            if audit_lease_id:
                try:
                    lease = self.audit_leases.by_id(audit_lease_id)
                    if lease.status == AuditLeaseStatus.ACTIVE:
                        self.audit_leases.retry_wait(audit_lease_id)
                except ValueError:
                    pass
            reason = f"invalid independent audit: {exc}"
            failure_kind = "invalid_audit_result"
            self.store.append("AUDIT_ERROR", {
                "job_id": outcome.job_id, "error": reason,
                "failure_kind": failure_kind, "retryable": True,
                "thread_id": outcome.thread_id, "turn_id": outcome.turn_id,
            })
            retry = self._retry_count(
                self.audit_retry_counts, fingerprint, failure_kind,
            )
            max_retries = self._retry_limit(failure_kind, outcome.role)
            if not state.terminal and retry < max_retries:
                self._set_retry_count(
                    self.audit_retry_counts, fingerprint, failure_kind, retry + 1,
                )
                self._queue_next_audit(state.event)
                self.store.append("AUDIT_RETRY_QUEUED", {
                    "candidate_fingerprint": fingerprint,
                    "retry": retry + 1,
                    "max_retries": max_retries,
                    "retry_class": self._retry_class(failure_kind),
                    "failure_kind": failure_kind,
                })
                return
            if not state.terminal:
                self.store.append("AUDIT_RETAINED_AFTER_ERROR", {
                    "candidate_fingerprint": fingerprint,
                    "failure_kind": failure_kind,
                })
            self.store.append("AUDIT_FAILURE_ISOLATED", {
                "candidate_fingerprint": fingerprint,
                "failure_kind": failure_kind,
                "action": "preserve candidate and request a fresh Director replan",
            })
            self._require_continuation(
                source="invalid_audit_result",
                reason=(
                    "an audit result violated the local semantic contract; preserve the "
                    "candidate and select a fresh audit or supporting research route"
                ),
            )
            return
        audit_record = self.audit_root / fingerprint / f"{result.audit_id}.json"
        atomic_write_json(audit_record, result.to_dict())
        self.store.append("AUDIT_RECORDED", {**result.to_dict(), "trust_status": trust})
        self._record_recent_change({
            "kind": "AUDIT_RECORDED",
            "claim_id": state.event.claim_id,
            "candidate_fingerprint": fingerprint,
            "verdict": result.verdict,
            "trust_status": trust,
            "audit_kind": result.audit_kind,
        })
        event = state.event
        if trust == TrustStatus.REJECTED:
            self.store.append("CANDIDATE_REJECTED", {
                "event_id": event.event_id, "fingerprint": fingerprint,
                "claim_id": event.claim_id, "reason": "; ".join(result.gaps) or result.verdict,
            })
            self._record_recent_change({
                "kind": "CANDIDATE_REJECTED", "claim_id": event.claim_id,
                "fingerprint": fingerprint,
            })
            self._request_director(
                "candidate audit rejected the candidate",
                meaningful_change=False,
            )
            return
        if result.verdict == "UNRESOLVED":
            self.store.append("CANDIDATE_AUDIT_UNRESOLVED", {
                "event_id": event.event_id, "fingerprint": fingerprint,
                "claim_id": event.claim_id, "gaps": result.gaps,
            })
            self._record_recent_change({
                "kind": "CANDIDATE_AUDIT_UNRESOLVED", "claim_id": event.claim_id,
                "fingerprint": fingerprint,
            })
            self._request_director(
                "candidate audit remained unresolved",
                meaningful_change=False,
            )
            return
        if trust == TrustStatus.AUDITED_NIGHTLY:
            verified_level = self.audit_gate.verified_evidence_level(fingerprint)
            transition = self.domain_semantics.transition_for(
                event.type, verified_level,
            )
            next_status = transition.get("status")
            conflict = self._claim_transition_conflict(event)
            if conflict:
                self.conflicted_candidates.add(fingerprint)
                self.stop_for_review = f"claim conflict requires human review: {event.claim_id}"
                self.store.append("CLAIM_CONFLICT_DETECTED", {
                    "claim_id": event.claim_id, "candidate_fingerprint": fingerprint,
                    "candidate_type": event.type, "reason": conflict,
                })
                self._record_recent_change({
                    "kind": "CLAIM_CONFLICT_DETECTED", "claim_id": event.claim_id,
                    "fingerprint": fingerprint, "reason": conflict,
                })
                return
            receipt: dict[str, Any] | None = None
            terminal_binding: dict[str, Any] | None = None
            receipt_preconditions: dict[Path, str] = {}
            prior_semantic_trust = self.semantic_trust
            prior_semantic_dirty = self._semantic_trust_dirty
            authorization: dict[str, Any] = {}
            try:
                self.semantic_alignment.assert_unchanged()
                semantic_terminal = (
                    next_status is not None
                    and str(next_status) in self.domain_semantics.terminal_positive
                    and event.claim_id in self.semantic_alignment.claims
                )
                if semantic_terminal:
                    self._require_semantic_trust_journal()
                    receipt = self.semantic_alignment.build_verification_receipt(
                        trust_state=self.semantic_trust,
                        candidate=event,
                        artifact_hashes=dict(
                            self.candidate_artifact_hashes.get(fingerprint) or {}
                        ),
                        evidence_hashes=dict(
                            self.candidate_semantic_evidence_hashes.get(fingerprint) or {}
                        ),
                        domain_evidence_receipt_fingerprints=[
                            item["receipt_fingerprint"]
                            for item in self.domain_evidence_receipts.get(fingerprint, [])
                        ],
                        audit_receipts=self._semantic_audit_receipts(event),
                        producer_identity=self._producer_identity_for_candidate(event),
                        claim_graph=self.graph,
                        validation_authority_head=(
                            self._current_validation_authority_head()
                        ),
                    )
                    updated_semantic_trust = self.semantic_trust.with_receipt(receipt)
                    updated_semantic_trust, terminal_binding = (
                        updated_semantic_trust.with_terminal_binding(
                            receipt, str(next_status),
                        )
                    )
                    self.semantic_trust = updated_semantic_trust
                    self._semantic_trust_dirty = True
                    receipt_preconditions = (
                        self.semantic_alignment.receipt_file_preconditions(receipt)
                    )
                authorization = {
                    "candidate_fingerprint": fingerprint,
                    "claim_id": event.claim_id,
                    "candidate_type": event.type,
                    "audit_pass_count": self.audit_gate.pass_count(fingerprint),
                    "audit_required": state.required,
                    "verified_evidence_level": verified_level,
                    "terminal_status": str(next_status) if next_status is not None else None,
                    "representation_id": event.representation_id,
                    "representation_content_sha256": (
                        terminal_binding["representation_content_sha256"]
                        if terminal_binding is not None else None
                    ),
                    "bridge_ids": list(event.semantic_bridge_ids),
                    "domain_evidence_receipt_fingerprints": [
                        item["receipt_fingerprint"]
                        for item in self.domain_evidence_receipts.get(fingerprint, [])
                    ],
                    "semantic_receipt_fingerprint": (
                        receipt["receipt_fingerprint"] if receipt is not None else None
                    ),
                    "semantic_terminal_binding": terminal_binding,
                    "candidate_scope_sha256": (
                        terminal_binding["candidate_scope_sha256"]
                        if terminal_binding is not None else None
                    ),
                    "dependency_shape_sha256": (
                        terminal_binding["dependency_shape_sha256"]
                        if terminal_binding is not None else None
                    ),
                    "transition_authorization_fingerprint": (
                        terminal_binding["transition_authorization_fingerprint"]
                        if terminal_binding is not None else None
                    ),
                }
                if terminal_binding is not None:
                    self._pending_semantic_authorization = (
                        self._canonical_transition_authorization(
                            "AUDITED_CLAIM_TRANSITION", authorization,
                        )
                    )
                self.graph.apply_audit_pass(
                    event,
                    self.audit_gate.pass_count(fingerprint),
                    state.required,
                    verified_level,
                )
            except SemanticPromotionError as exc:
                self.semantic_trust = prior_semantic_trust
                self._semantic_trust_dirty = prior_semantic_dirty
                self._pending_semantic_authorization = None
                semantic_decision = exc.decision
                self.store.append("SEMANTIC_PROMOTION_BLOCKED", {
                    "candidate_fingerprint": fingerprint,
                    "target_status": next_status,
                    "candidate_bound": True,
                    **semantic_decision.to_dict(),
                })
                self._record_recent_change({
                    "kind": "SEMANTIC_PROMOTION_BLOCKED",
                    "claim_id": event.claim_id,
                    "semantic_status": semantic_decision.status,
                })
                self.director_constraints.append({
                    "action": "REPAIR_SEMANTIC_BRIDGE",
                    "claim_id": event.claim_id,
                    "semantic_status": semantic_decision.status,
                    "issues": list(semantic_decision.issues),
                    "source": "trusted_final_claim_gate",
                })
                self._request_director(
                    "audited candidate failed the semantic bridge gate",
                    meaningful_change=True,
                )
                return
            except ValueError as exc:
                self.semantic_trust = prior_semantic_trust
                self._semantic_trust_dirty = prior_semantic_dirty
                self._pending_semantic_authorization = None
                self._begin_internal_failure_drain(
                    f"semantic verification failed before claim transition: {exc}",
                    source="semantic_alignment",
                )
                return
            bridge: tuple[str, str] | None = None
            if event.type == "REPRESENTATION_BRIDGE":
                bridge = tuple(sorted(event.bridge_representation_ids))
                self.audited_representation_bridges.add(bridge)
            else:
                self.claim_representations[event.claim_id] = event.representation_id
            self.representation_contracts[event.representation_id] = (
                event.representation_contract.to_dict()
            )
            self.stagnation.record(
                event.claim_id,
                f"AUDITED_{event.type}",
                canonical_progress=True,
            )
            try:
                transition_id = self._commit_claim_state_transition(
                    transition_kind="AUDITED_CLAIM_TRANSITION",
                    authorization=authorization,
                    preconditions=receipt_preconditions,
                )
                self._semantic_trust_dirty = False
            finally:
                self._pending_semantic_authorization = None
            if receipt is not None:
                self.store.append("SEMANTIC_VERIFICATION_RECORDED", {
                    "candidate_fingerprint": fingerprint,
                    "claim_id": event.claim_id,
                    "semantic_receipt_fingerprint": receipt["receipt_fingerprint"],
                    "canonical_transition_id": transition_id,
                    "authoritative_terminal_transition": True,
                })
            if bridge is not None:
                self.store.append("REPRESENTATION_BRIDGE_TRUSTED", {
                    "candidate_fingerprint": fingerprint,
                    "representation_ids": list(bridge),
                    "audited": True,
                    "canonical_transition_id": transition_id,
                })
                self._record_recent_change({
                    "kind": "REPRESENTATION_BRIDGE_TRUSTED",
                    "claim_id": event.claim_id,
                    "representation_ids": list(bridge),
                })
            else:
                changed_claim = self.graph.claims[event.claim_id]
                self.store.append("TRUST_STATE_CHANGED", {
                    "claim_id": event.claim_id, "trust_status": trust,
                    **self._claim_status_payload(changed_claim),
                    "evidence_level": verified_level,
                    "representation_id": event.representation_id,
                    "semantic_status": self.semantic_alignment.evaluate_claim(
                        event.claim_id,
                        trust_state=self.semantic_trust,
                        claim_statement=changed_claim.statement,
                        claim_assumptions=changed_claim.assumptions,
                        claim_graph=self.graph,
                        validation_authority_head=(
                            self._current_validation_authority_head()
                        ),
                        parent_claim_id=changed_claim.parent_claim_id,
                        check_parent_claim_id=True,
                    ).status,
                    "canonical_transition_id": transition_id,
                })
                self._record_recent_change({
                    "kind": "TRUST_STATE_CHANGED", "claim_id": event.claim_id,
                    "trust_status": trust, "evidence_level": verified_level,
                })
            self._apply_dependency_pruning()
            if not self._begin_finalization_if_resolved("independent audit gate"):
                self._request_director(
                    "audit changed trusted state",
                    meaningful_change=False,
                )
        else:
            self.store.append("CANDIDATE_AUDIT_PROGRESS", {
                "claim_id": event.claim_id, "candidate_fingerprint": fingerprint,
                "candidate_trust_status": trust,
                "verified_evidence_level": self.audit_gate.verified_evidence_level(fingerprint),
            })
            self._request_director(
                "candidate needs another independent audit",
                meaningful_change=False,
            )
            self._queue_next_audit(event)

    def _claim_transition_conflict(self, event: CandidateEvent) -> str | None:
        claim = self.graph.claims.get(event.claim_id)
        if claim is None:
            return None
        trusted = claim.trust_status in {
            TrustStatus.CANONICAL_TRUSTED, TrustStatus.AUDITED_NIGHTLY, TrustStatus.FORMALLY_VERIFIED,
        }
        if not trusted:
            return None
        transition = self.domain_semantics.event_transitions[event.type]
        next_status = transition["status"]
        if next_status is None:
            return None
        current_outcome = self.domain_semantics.final_outcome(claim.research_status)
        candidate_outcome = self.domain_semantics.final_outcome(str(next_status))
        if current_outcome is None or candidate_outcome in {None, current_outcome}:
            return None
        if self.domain_semantics.domain == "math-research":
            if current_outcome == "positive":
                return "audited counterexample conflicts with an already trusted proof"
            return "audited proof conflicts with an already trusted refutation"
        return (
            f"audited {event.type} conflicts with trusted "
            f"{claim.research_status} status"
        )

    def _apply_dependency_pruning(self) -> None:
        blocked = self.graph.prune_failed_dependencies()
        if not blocked:
            return
        keep: list[ResearchTask] = []
        for task in self.pending_research:
            reasons = blocked.get(task.target_claim) or sorted(set(task.dependencies) & set(blocked))
            if reasons:
                self.store.append("DEPENDENCY_PRUNED", {"task_id": task.task_id, "claim_id": task.target_claim, "failed_dependencies": reasons})
                self._record_recent_change({
                    "kind": "DEPENDENCY_PRUNED", "task_id": task.task_id,
                    "claim_id": task.target_claim, "failed_dependencies": reasons,
                })
            else:
                keep.append(task)
        self.pending_research = keep
        for job_id, active in list(self.active.items()):
            reasons = blocked.get(active.task.target_claim) or sorted(set(active.task.dependencies) & set(blocked))
            if reasons and active.kind == "research":
                self._schedule_backend_cancel(job_id, "audited dependency pruning")
                active.future.cancel()
                self._record_job_cancelled(
                    job_id, active, "audited dependency pruning",
                    remote_cancel_succeeded=None,
                )
                self.store.append("DEPENDENCY_PRUNED", {"job_id": job_id, "task_id": active.task.task_id, "failed_dependencies": reasons})

    def _research_checkpoint_artifacts(
        self, job_record: dict[str, Any] | None,
    ) -> dict[str, str]:
        artifact_refs: dict[str, str] = {}
        for raw, expected in dict(
            (job_record or {}).get("artifact_hashes") or {}
        ).items():
            path = Path(str(raw)).resolve()
            if not path.is_relative_to(self.run_dir.resolve()) or not path.is_file():
                raise ValueError("continuation artifact is unavailable outside the epoch")
            observed = file_digest(path)
            if observed != str(expected):
                raise ValueError("continuation artifact changed before checkpoint")
            relative = path.relative_to(self.run_dir.resolve()).as_posix()
            artifact_refs[f"epoch://{self.epoch_id}/{relative}"] = observed
        return artifact_refs

    def _checkpoint_research_continuation(
        self,
        outcome: JobOutcome,
        task: ResearchTask,
        job_record: dict[str, Any] | None,
    ) -> ResearchTask:
        turn_count = len(outcome.turn_history)
        checkpoint_id = stable_hash({
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "source_job_id": outcome.job_id,
            "source_task_fingerprint": task.fingerprint,
            "turn_count": turn_count,
        })[:24]
        continuation_task_id = f"continuation-{checkpoint_id}"
        relative = (
            Path("state") / "research_checkpoints" / f"{checkpoint_id}.json"
        )
        checkpoint_path = self.run_dir / relative
        checkpoint_uri = f"epoch://{self.epoch_id}/{relative.as_posix()}"
        artifact_refs = self._research_checkpoint_artifacts(job_record)
        continuation_raw = task.to_dict()
        continuation_raw.update({
            "task_id": continuation_task_id,
            "why_now": (
                "Controller continuation after the prior same-thread turn bound; read "
                f"the noncanonical checkpoint {checkpoint_uri} before continuing the "
                "same exact objective and next open obligation."
            ),
            "required_files": list(dict.fromkeys([
                *task.required_files,
                checkpoint_uri,
                *artifact_refs,
            ])),
        })
        continuation = ResearchTask.from_dict(continuation_raw)
        research_frontier = (
            self.graph.research_frontier(task.target_claim)
            if task.target_claim in self.graph.claims else None
        )
        frontier_field = (
            "proof_frontier"
            if self.domain_semantics.domain == "math-research"
            else "research_frontier"
        )
        checkpoint = {
            "schema_version": 1,
            "authority": "derived_noncanonical",
            "trust_effect": "none",
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "source_job_id": outcome.job_id,
            "source_task_id": task.task_id,
            "source_task_fingerprint": task.fingerprint,
            "continuation_task_id": continuation.task_id,
            "continuation_task_fingerprint": continuation.fingerprint,
            "continuation_task_sha256": stable_hash(continuation.to_dict()),
            "claim_id": task.target_claim,
            "role": task.role,
            "route_family": task.route_family,
            "turn_count": turn_count,
            "logical_stop_reason": outcome.logical_stop_reason,
            frontier_field: research_frontier,
            "current_obligation": (
                research_frontier.get("next_obligation_id")
                if research_frontier is not None else None
            ),
            "completed_evidence": {
                "candidate_accepted": bool(outcome.candidate_accepted),
                "canonical_progress": bool(outcome.canonical_progress),
                "artifact_hashes": artifact_refs,
            },
            "next_obligation": str(
                outcome.result.get("next_suggested_question") or ""
            ).strip() or (
                research_frontier.get("next_obligation_id")
                if research_frontier is not None else None
            ),
            "last_result": outcome.result,
            "turn_history": outcome.turn_history,
            "artifact_hashes": artifact_refs,
            "retry_condition": f"next_epoch:{self.epoch_id}",
            "created_at": utc_now(),
            "boundary": (
                "Prior model output and artifacts are research evidence only; this "
                + (
                    "checkpoint cannot change mathematical, trust, or evidence status."
                    if self.domain_semantics.domain == "math-research"
                    else "checkpoint cannot change claim, trust, or evidence status."
                )
            ),
        }
        atomic_write_json(checkpoint_path, checkpoint)
        self.deferred_research_continuations.append(continuation)
        self.seen_task_fingerprints.add(continuation.fingerprint)
        self.task_fingerprints_by_id[
            continuation.task_id
        ] = continuation.fingerprint
        self.route_ledger.append(
            route_id=task.route_family,
            representation_id=task.representation_id,
            method_tags=[task.role, "controller-continuation"],
            status="PAUSED",
            failure_class=None,
            retry_condition=f"next_epoch:{self.epoch_id}",
            evidence_refs=[checkpoint_uri, *artifact_refs],
            source=f"job:{outcome.job_id}",
        )
        self.store.append("RESEARCH_CONTINUATION_CHECKPOINTED", {
            "job_id": outcome.job_id,
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "turn_count": turn_count,
            "checkpoint_uri": checkpoint_uri,
            "checkpoint_sha256": file_digest(checkpoint_path),
            "continuation_task": continuation.to_dict(),
            "retry_condition": f"next_epoch:{self.epoch_id}",
            "action": "defer to the next epoch without claiming progress or failure",
        })
        self._record_recent_change({
            "kind": "RESEARCH_CONTINUATION_CHECKPOINTED",
            "task_id": task.task_id,
            "continuation_task_id": continuation.task_id,
            "claim_id": task.target_claim,
            "turn_count": turn_count,
            "checkpoint_uri": checkpoint_uri,
        })
        return continuation

    def _validate_research_continuation_checkpoint(
        self,
        task: ResearchTask,
        *,
        source_epoch_id: str,
        expected_uri: str,
        expected_sha256: str,
    ) -> tuple[str, dict[str, Any]]:
        prefix = f"epoch://{source_epoch_id}/state/research_checkpoints/"
        references = [item for item in task.required_files if item.startswith(prefix)]
        if len(references) != 1:
            raise ValueError(
                "continuation task must reference exactly one source-epoch checkpoint"
            )
        checkpoint_uri = references[0]
        if checkpoint_uri != expected_uri:
            raise ValueError("continuation checkpoint URI does not match snapshot index")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("continuation checkpoint digest index is invalid")
        checkpoint_path = self.artifact_store.resolve_uri(checkpoint_uri)
        if file_digest(checkpoint_path) != expected_sha256:
            raise ValueError("continuation checkpoint digest changed")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        expected = {
            "schema_version": 1,
            "authority": "derived_noncanonical",
            "trust_effect": "none",
            "campaign_id": self.campaign_id,
            "epoch_id": source_epoch_id,
            "continuation_task_id": task.task_id,
            "continuation_task_fingerprint": task.fingerprint,
            "continuation_task_sha256": stable_hash(task.to_dict()),
            "claim_id": task.target_claim,
            "role": task.role,
            "route_family": task.route_family,
            "retry_condition": f"next_epoch:{source_epoch_id}",
        }
        for key, value in expected.items():
            if checkpoint.get(key) != value:
                raise ValueError(f"continuation checkpoint {key} does not match task")
        artifact_hashes = checkpoint.get("artifact_hashes") or {}
        if not isinstance(artifact_hashes, dict):
            raise ValueError("continuation checkpoint artifact hashes are invalid")
        for uri, expected_hash in artifact_hashes.items():
            if (
                not str(uri).startswith(f"epoch://{source_epoch_id}/")
                or str(uri) not in task.required_files
                or not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash))
            ):
                raise ValueError("continuation checkpoint artifact binding is invalid")
            artifact = self.artifact_store.resolve_uri(str(uri))
            if file_digest(artifact) != str(expected_hash):
                raise ValueError("continuation checkpoint artifact digest changed")
        return checkpoint_uri, checkpoint

    def _accept_research_result(
        self,
        outcome: JobOutcome,
        task: ResearchTask,
        job_record: dict[str, Any] | None = None,
    ) -> None:
        if not outcome.succeeded:
            failure_kind = outcome.failure_kind or "research_failure"
            if failure_kind == "provider_transport_lost":
                self._pause_for_provider_transport_loss(
                    outcome, task=task, checkpoint_job_record=job_record,
                )
                return
            if failure_kind == "provider_quota_exhausted":
                self._pause_for_provider_quota(
                    outcome, task=task, checkpoint_job_record=job_record,
                )
                return
            retry = self._retry_count(
                self.retry_counts, task.task_id, failure_kind,
            )
            max_retries = self._retry_limit(failure_kind, outcome.role)
            self.store.append("RESEARCH_JOB_FAILED", {
                **self._failure_payload(outcome),
                "task_id": task.task_id,
                "claim_id": task.target_claim,
            })
            if outcome.retryable and retry < max_retries:
                self._set_retry_count(
                    self.retry_counts, task.task_id, failure_kind, retry + 1,
                )
                self.pending_research.append(task)
                self.store.append("JOB_RETRY_QUEUED", {
                    "job_id": outcome.job_id, "task_id": task.task_id,
                    "retry": retry + 1, "max_retries": max_retries,
                    "retry_class": self._retry_class(failure_kind),
                    "failure_kind": failure_kind,
                })
                return
            # Preserve the exact task in append-only events, but do not
            # immediately dispatch the same non-retryable failure again. A
            # fresh Director must choose a repaired or genuinely different
            # route while the rest of the run continues.
            self.store.append("TASK_RETAINED_AFTER_ERROR", {
                "task_id": task.task_id, "claim_id": task.target_claim,
                "failure_kind": failure_kind,
                "task": task.to_dict(),
            })
            self.route_ledger.append(
                route_id=task.route_family,
                representation_id=task.representation_id,
                method_tags=[task.role], status="FAILED",
                failure_class=failure_kind,
                retry_condition=f"new_evidence:{task.target_claim}",
                evidence_refs=[], source=f"job:{outcome.job_id}",
            )
            self._record_recent_change({
                "kind": "RESEARCH_JOB_FAILED",
                "task_id": task.task_id,
                "claim_id": task.target_claim,
                "failure_kind": failure_kind,
            })
            self._require_continuation(
                source="research_failure",
                reason=(
                    f"{task.role} task {task.task_id} failed after bounded retries "
                    f"({failure_kind}); choose a repaired or independent route"
                ),
                forbidden_route=task.route_family or None,
            )
            return
        result_type = str(outcome.result.get("result_type", "NO_PROGRESS"))
        meaningful = bool(outcome.canonical_progress or outcome.candidate_accepted)
        if (
            not meaningful
            and outcome.logical_stop_reason in CONTINUATION_CHECKPOINT_REASONS
        ):
            self._checkpoint_research_continuation(outcome, task, job_record)
            self._replan_after_wave = True
            return
        if (
            not meaningful
            and outcome.logical_stop_reason == "controller-verified execution blocker"
        ):
            self.route_ledger.append(
                route_id=task.route_family,
                representation_id=task.representation_id,
                method_tags=[task.role, "controller-verified-blocker"],
                status="PAUSED",
                failure_class=None,
                retry_condition=f"new_evidence:{task.target_claim}",
                evidence_refs=list(outcome.artifact_paths),
                source=f"job:{outcome.job_id}",
            )
            self.store.append("RESEARCH_TASK_BLOCKED", {
                "job_id": outcome.job_id,
                "task_id": task.task_id,
                "claim_id": task.target_claim,
                "role": task.role,
                "blocker": _bounded_value(outcome.result.get("main_finding")),
                "next_obligation": _bounded_value(
                    outcome.result.get("next_suggested_question")
                ),
                **self._research_failure_payload(),
                "stagnation_effect": "none",
                "retry_condition": f"new_evidence:{task.target_claim}",
            })
            self._request_director(
                "controller-verified execution blocker requires a different route",
                meaningful_change=False,
            )
            self._replan_after_wave = True
            return
        self.route_ledger.append(
            route_id=task.route_family,
            representation_id=task.representation_id,
            method_tags=[task.role],
            status="COMPLETED" if meaningful else "FAILED",
            failure_class=None if meaningful else result_type,
            retry_condition=(
                None if meaningful else f"new_evidence:{task.target_claim}"
            ),
            evidence_refs=list(outcome.artifact_paths),
            source=f"job:{outcome.job_id}",
        )
        self._record_recent_change({
            "kind": "RESEARCH_JOB_COMPLETED",
            "task_id": task.task_id,
            "claim_id": task.target_claim,
            "role": task.role,
            "result_type": result_type,
            "candidate_accepted": outcome.candidate_accepted,
            "canonical_progress": outcome.canonical_progress,
            "logical_stop_reason": outcome.logical_stop_reason,
            "main_finding": _bounded_value(outcome.result.get("main_finding")),
        })
        self._replan_after_wave = True
        if self.stagnation.record(
            outcome.claim_id,
            result_type,
            canonical_progress=bool(outcome.canonical_progress),
        ):
            claim = self.graph.claims.get(outcome.claim_id)
            if claim:
                penalty = float(self.config.raw["stagnation"].get("priority_penalty", 0.2))
                claim.priority["score"] = max(0.0, float(claim.priority.get("score", 0.5)) - penalty)
                self._commit_claim_state_transition(
                    transition_kind="CONTROLLER_PRIORITY_UPDATE",
                    authorization={
                        "claim_id": outcome.claim_id,
                        "task_id": task.task_id,
                        "trust_upgrade": False,
                        "reason": "audited-state-neutral stagnation diversification",
                    },
                )
            constraint = self.stagnation.diversification_constraint(outcome.claim_id, task.route_family)
            self.director_constraints.append(constraint)
            self.store.append("STAGNATION_DIVERSIFY", constraint)
            self._record_recent_change({"kind": "STAGNATION_DIVERSIFY", **constraint})
            self._request_director(
                "stagnation requires a diversified plan",
                meaningful_change=True,
            )

    async def _cancel_stale_jobs(self) -> None:
        now = time.monotonic()
        for job_id, active in list(self.active.items()):
            if now - active.started_monotonic <= active.timeout + 20:
                continue
            if await self._cancel_backend_job(job_id, "stale worker timeout"):
                active.future.cancel()
                self._record_job_cancelled(
                    job_id, active, "stale worker timeout",
                    remote_cancel_succeeded=True,
                )

    async def _handle_per_thread_budget_limits(self) -> None:
        for job_id, active in list(self.active.items()):
            if job_id in self._thread_budget_limit_reported:
                continue
            limit = self._thread_budget(active.task.role)
            if limit is None:
                continue
            observed = int(
                (self.governor.by_job.get(job_id) or {}).get("total_tokens", 0) or 0
            )
            if observed < limit:
                continue
            self._thread_budget_limit_reported.add(job_id)
            action = self.config.per_thread_limit_action
            reason = f"per-thread token budget reached ({observed}/{limit})"
            self.store.append("THREAD_TOKEN_BUDGET_REACHED", {
                "job_id": job_id, "task_id": active.task.task_id,
                "role": active.task.role, "claim_id": active.task.target_claim,
                "observed_tokens": observed, "token_budget": limit,
                "action": action,
            })
            if action == "observe":
                self.live_store.append("AGENT_JOB_BUDGET_OBSERVED", {
                    "job_id": job_id, "role": active.task.role,
                    "task_id": active.task.task_id, "claim_id": active.task.target_claim,
                    "reason": reason,
                })
            elif await self._cancel_backend_job(job_id, reason):
                self.live_store.append("AGENT_JOB_CANCEL_REQUESTED", {
                    "job_id": job_id, "role": active.task.role,
                    "task_id": active.task.task_id, "claim_id": active.task.target_claim,
                    "reason": reason,
                })

    def _continue_global_budget_drain(self, reason: str) -> bool:
        """Stop dispatching and wait until the already-active jobs finish."""
        active_model_jobs = len(self.active)
        active_mechanical = len(self.active_mechanical)
        active_count = active_model_jobs + active_mechanical
        if not self._budget_draining:
            self._budget_draining = True
            self._budget_drain_initial_active = active_count
            self._budget_drain_last_active = active_count
            self.store.append("TOKEN_BUDGET_DRAIN_STARTED", {
                "reason": reason,
                "global_budget": self.governor.global_budget,
                "observed_tokens": self.governor.total.total_tokens,
                "active_jobs": active_model_jobs,
                "active_mechanical_subtasks": active_mechanical,
                "action": "stop_dispatch_and_wait",
            })
        elif active_count != self._budget_drain_last_active and active_count > 0:
            self.store.append("TOKEN_BUDGET_DRAIN_PROGRESS", {
                "active_jobs": active_model_jobs,
                "active_mechanical_subtasks": active_mechanical,
                "finished_jobs": max(0, self._budget_drain_initial_active - active_count),
            })
            self._budget_drain_last_active = active_count
        if active_count > 0:
            return True
        if not self._budget_drain_completed:
            self._budget_drain_completed = True
            self._budget_drain_last_active = 0
            self.store.append("TOKEN_BUDGET_DRAIN_COMPLETED", {
                "global_budget": self.governor.global_budget,
                "final_observed_tokens": self.governor.total.total_tokens,
                "finished_jobs": self._budget_drain_initial_active,
            })
        return False

    def _record_backend_bindings(self) -> None:
        active_map = getattr(self.backend, "active", {})
        for job_id, ids in active_map.items():
            binding = (str(ids[0]), str(ids[1]))
            previous = self._bound_jobs.get(job_id)
            if previous == binding:
                continue
            self._bound_jobs[job_id] = binding
            self.store.append("JOB_BOUND" if previous is None else "JOB_REBOUND", {
                "job_id": job_id, "thread_id": binding[0], "turn_id": binding[1],
                **({"previous_turn_id": previous[1]} if previous is not None else {}),
            })

    @staticmethod
    def _is_useful(outcome: JobOutcome) -> bool:
        if not outcome.succeeded:
            return False
        if outcome.role in {Role.DIRECTOR, Role.AUDITOR, Role.EVALUATOR_AUDITOR}:
            return True
        return bool(outcome.canonical_progress or outcome.candidate_accepted)

    def _error_rate_exceeded(self) -> bool:
        fatal_kinds = set(LOCAL_STRUCTURAL_FAILURES) | {
            "backend_internal", "controller_failure", "canonical_guard",
        }

        def counts_as_controller_error(job: dict[str, Any]) -> bool:
            return str(job.get("failure_kind") or "") in fatal_kinds

        consecutive = 0
        for job in reversed(self.completed_jobs):
            if not counts_as_controller_error(job):
                break
            consecutive += 1
        if consecutive >= int(self.config.raw["engine"].get("max_consecutive_controller_errors", 5)):
            return True
        minimum = int(self.config.raw["engine"].get("error_rate_min_jobs", 4))
        if len(self.completed_jobs) < minimum:
            return False
        errors = sum(counts_as_controller_error(job) for job in self.completed_jobs)
        threshold = float(self.config.raw["engine"].get("error_rate_threshold", 0.5))
        return errors / len(self.completed_jobs) >= threshold

    def _finish(self, reason: str, *, internal_failure: bool | None = None) -> RunResult:
        if internal_failure is not None:
            self._internal_failure = bool(internal_failure)
        self._flush_due_live_chunks(force=True)
        changed = self.guard.verify()
        atomic_write_json(self.run_dir / "canonical_guard.after.json", {
            "changed": changed, "verified_at": utc_now(),
        })
        if changed:
            self.store.append("CANONICAL_GUARD_FAILED", {"changed": changed})
            canonical_reason = f"canonical guard failed: {changed}"
            if self._internal_failure:
                self._secondary_failures.append({
                    "kind": "CANONICAL_GUARD_FAILED", "detail": canonical_reason,
                })
            elif "canonical guard failed" not in reason:
                reason = f"canonical guard failed: {changed}"
            self._internal_failure = True
        lifecycle = job_lifecycle_metrics(self.store.replay())
        mechanical_lifecycle = mechanical_lifecycle_metrics(self.store.replay())
        if (
            lifecycle.active_job_ids
            or lifecycle.duplicate_terminal_job_ids
            or lifecycle.orphan_terminal_job_ids
        ):
            lifecycle_reason = f"job lifecycle invariant failed: {lifecycle.to_dict()}"
            if self._internal_failure:
                self._secondary_failures.append({
                    "kind": "JOB_LIFECYCLE_INVARIANT_FAILED",
                    "detail": lifecycle_reason,
                })
            else:
                reason = lifecycle_reason
            self._internal_failure = True
            self.store.append("JOB_LIFECYCLE_INVARIANT_FAILED", lifecycle.to_dict())
        if (
            mechanical_lifecycle.active_subtasks
            or mechanical_lifecycle.duplicate_terminal_subtasks
            or mechanical_lifecycle.orphan_terminal_subtasks
        ):
            mechanical_reason = (
                "mechanical subtask lifecycle invariant failed: "
                f"{mechanical_lifecycle.to_dict()}"
            )
            if self._internal_failure:
                self._secondary_failures.append({
                    "kind": "MECHANICAL_LIFECYCLE_INVARIANT_FAILED",
                    "detail": mechanical_reason,
                })
            else:
                reason = mechanical_reason
            self._internal_failure = True
            self.store.append(
                "MECHANICAL_LIFECYCLE_INVARIANT_FAILED",
                mechanical_lifecycle.to_dict(),
            )
        if self._internal_failure:
            if self.lifecycle.phase in {
                LifecyclePhase.BOOTSTRAP,
                LifecyclePhase.RUNNING,
                LifecyclePhase.FINALIZING,
            }:
                self.lifecycle.transition(
                    LifecyclePhase.DRAINING_FAILURE, reason=reason,
                )
            if self.lifecycle.phase is LifecyclePhase.DRAINING_FAILURE:
                self.lifecycle.transition(LifecyclePhase.SEALED, reason=reason)
        elif self._finalization_started:
            if self.lifecycle.phase is LifecyclePhase.FINALIZING:
                self.lifecycle.transition(LifecyclePhase.COMPLETED, reason=reason)
        elif self.lifecycle.phase in {LifecyclePhase.BOOTSTRAP, LifecyclePhase.RUNNING}:
            if self.lifecycle.phase is LifecyclePhase.BOOTSTRAP:
                self.lifecycle.transition(
                    LifecyclePhase.RUNNING, reason="zero-turn validation lifecycle",
                )
            self.lifecycle.transition(LifecyclePhase.DRAINING_EPOCH, reason=reason)
            self.lifecycle.transition(LifecyclePhase.SEALED, reason=reason)
        elif self.lifecycle.phase in {
            LifecyclePhase.DRAINING_BUDGET, LifecyclePhase.DRAINING_EPOCH,
        }:
            self.lifecycle.transition(LifecyclePhase.SEALED, reason=reason)
        preliminary_completed = bool(
            not self._internal_failure
            and self.lifecycle.phase is LifecyclePhase.COMPLETED
            and self.final_claim_resolved
        )
        execution = self._run_manifest.get("execution") or {}
        limits = execution.get("limits") or {}
        if execution.get("started_epoch") is not None and limits.get(
            "duration_seconds"
        ) is not None:
            epoch_elapsed = min(
                float(limits["duration_seconds"]),
                max(0.0, time.time() - float(execution["started_epoch"])),
            )
        else:
            epoch_elapsed = time.monotonic() - self._run_started_monotonic
        campaign_budget_exhausted = False
        if (
            self._campaign_recorded_started
            and not self._internal_failure
            and reason == "epoch time limit reached"
        ):
            campaign_budget_exhausted = (
                self.campaign_store.load().remaining_seconds <= epoch_elapsed + 0.01
            )
            if campaign_budget_exhausted:
                reason = "campaign time budget exhausted"
        campaign_status = (
            "COMPLETED" if preliminary_completed else
            "STOPPED" if self._stop_after_epoch or campaign_budget_exhausted else "PAUSED"
        )
        checkpoint_path = self.run_dir / "state" / "compact_snapshot.json"
        try:
            if self._bootstrap_completed and self.policy_manifest is not None:
                checkpoint_path = self._write_compact_snapshot()
            if self._campaign_recorded_started:
                self.campaign_store.append_epoch_sealed(
                    epoch_id=self.epoch_id,
                    elapsed_seconds=epoch_elapsed,
                    status=campaign_status,
                    stopped_reason=reason,
                    checkpoint_uri=f"epoch://{self.epoch_id}/state/{checkpoint_path.name}",
                    checkpoint_usable=bool(
                        self._bootstrap_completed
                        and self._recovery_completed
                        and self._previous_epoch_checkpoint_imported
                        and checkpoint_path.is_file()
                    ),
                )
        except Exception as exc:
            seal_reason = f"campaign seal failed: {_sanitize_live_text(exc)[:800]}"
            if self._internal_failure:
                self._secondary_failures.append({
                    "kind": "CAMPAIGN_SEAL_FAILED", "detail": seal_reason,
                })
            else:
                reason = seal_reason
            self._internal_failure = True
            campaign_status = "PAUSED"
            self.store.append("CAMPAIGN_SEAL_FAILED", {
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "error": _sanitize_live_text(exc),
            })
        execution_mode = "dry-run" if self._dry_run else ("mock" if self.mock else "real")
        campaign_completed = bool(
            not self._internal_failure
            and self.lifecycle.phase is LifecyclePhase.COMPLETED
            and self.final_claim_resolved
        )
        outcome_dir = self.layout.outcomes_root / self.run_id
        outcome_path = outcome_dir / "OUTCOME.md"
        report_dir = self.layout.nightly_root / self.run_id
        report_path = report_dir / "NIGHTLY_REPORT.md"
        report_ready = False
        outcome_ready = False
        artifacts_finalized = False
        operator_interrupted = self._operator_interrupted

        def current_run_outcome() -> str:
            if operator_interrupted:
                return f"interrupted {execution_mode} run after epoch seal"
            if execution_mode == "real":
                return (
                    "failed real run" if self._internal_failure else
                    "completed real campaign" if campaign_completed else
                    "stopped real campaign after epoch" if campaign_status == "STOPPED" else
                    "paused real campaign epoch"
                )
            if execution_mode == "mock":
                return (
                    "failed mock run" if self._internal_failure else
                    "completed mock campaign" if campaign_completed else
                    "stopped mock campaign after epoch" if campaign_status == "STOPPED" else
                    "paused mock campaign epoch"
                )
            return "failed dry-run" if self._internal_failure else "dry-run validation"

        def write_summary(*, artifact_error: str | None = None) -> None:
            summary_events = self.store.replay()
            summary_lifecycle = job_lifecycle_metrics(summary_events)
            summary_mechanical = mechanical_lifecycle_metrics(summary_events)
            atomic_write_json(report_dir / "RUN_SUMMARY.json", {
                "run_id": self.run_id, "reason": reason,
                "report": (
                    f"project://{report_path.relative_to(self.config.project_root).as_posix()}"
                    if report_ready else None
                ),
                "outcome": (
                    f"project://{outcome_path.relative_to(self.config.project_root).as_posix()}"
                    if outcome_ready else None
                ),
                "execution_mode": execution_mode,
                "run_outcome": current_run_outcome(),
                "internal_failure": self._internal_failure,
                "campaign_id": self.campaign_id,
                "epoch_id": self.epoch_id,
                "previous_epoch_id": self.previous_epoch_id,
                "campaign_status": campaign_status,
                "domain": self.domain_semantics.domain,
                "final_claim_resolved": self.final_claim_resolved,
                "final_claim_outcome": self.final_claim_outcome,
                "final_conjecture_proved": self.final_conjecture_proved,
                "final_conjecture_refuted": self.final_conjecture_refuted,
                "events": len(summary_events),
                "artifact_event_watermark": (
                    int(summary_events[-1].get("sequence", 0)) if summary_events else 0
                ),
                "artifacts_finalized": artifacts_finalized,
                "artifact_error": artifact_error,
                "jobs": summary_lifecycle.jobs_terminal,
                **summary_lifecycle.to_dict(),
                "mechanical_subtasks": summary_mechanical.to_dict(),
                "token_governor": self.governor.snapshot(),
                "mechanical_token_governor": self.mechanical_governor.snapshot(),
                "canonical_changed": changed,
                "policy_manifest": self._run_manifest.get(
                    "research_policy", {}
                ).get("manifest"),
                "policy_manifest_sha256": (
                    self.policy_manifest["manifest_sha256"]
                    if self.policy_manifest else None
                ),
                "policy_source_drift": bool(
                    self.policy_status and self.policy_status["source_drift"]
                ),
                "requested_service_tier": self.config.requested_service_tier,
                "observed_service_tiers": sorted({
                    str(job.get("observed_service_tier") or "unobservable")
                    for job in self.completed_jobs
                }),
            })

        artifact_start = {
            "attempt_id": self._attempt_id,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "report": str(report_path),
            "outcome": str(outcome_path),
        }
        self.store.append("RUN_ARTIFACT_FINALIZATION_STARTED", artifact_start)
        self.live_store.append("LIVE_RUN_ARTIFACT_FINALIZATION_STARTED", artifact_start)
        try:
            artifact_events = self.store.replay()
            write_report(
                report_path,
                run_id=self.run_id,
                graph=self.graph,
                events=artifact_events,
                jobs=self.completed_jobs,
                mechanical_jobs=self.completed_mechanical_jobs,
                stopped_reason=reason,
                capability_snapshot=self.capability_snapshot,
                policy_manifest=self.policy_manifest,
                policy_status=self.policy_status,
                promotion_allowed=(
                    execution_mode == "real" and not self._internal_failure
                ),
                execution_mode=execution_mode,
                run_outcome=current_run_outcome(),
                internal_failure=self._internal_failure,
                final_claim_id=self.final_conjecture_claim_id,
                final_conjecture_proved=self.final_conjecture_proved,
                final_conjecture_refuted=self.final_conjecture_refuted,
                campaign_id=self.campaign_id,
                epoch_id=self.epoch_id,
                campaign_status=campaign_status,
            )
            report_ready = True
            self.live_store.append("LIVE_RUN_ARTIFACT_FINALIZATION_PROGRESS", {
                **artifact_start, "stage": "report", "completed": 1, "total": 1,
            })

            def publish_artifact_progress(update: dict[str, Any]) -> None:
                self.live_store.append(
                    "LIVE_RUN_ARTIFACT_FINALIZATION_PROGRESS",
                    {**artifact_start, **update},
                )

            outcome_path = write_outcome_archive(
                project_root=self.config.project_root,
                outcome_dir=outcome_dir,
                run_dir=self.run_dir,
                report_path=report_path,
                run_id=self.run_id,
                execution_mode=execution_mode,
                run_outcome=current_run_outcome(),
                stopped_reason=reason,
                internal_failure=self._internal_failure,
                jobs=self.completed_jobs,
                mechanical_jobs=self.completed_mechanical_jobs,
                events=artifact_events,
                final_claim_id=self.final_conjecture_claim_id,
                final_claim=self._final_claim(),
                final_conjecture_proved=self.final_conjecture_proved,
                final_conjecture_refuted=self.final_conjecture_refuted,
                domain=self.domain_semantics.domain,
                final_claim_resolved=self.final_claim_resolved,
                final_claim_outcome=self.final_claim_outcome,
                campaign_id=self.campaign_id,
                epoch_id=self.epoch_id,
                campaign_status=campaign_status,
                project_id=self.config.project_name,
                progress=publish_artifact_progress,
            )
            outcome_ready = True
            artifacts_finalized = True
            write_summary()
            completed_payload = {
                **artifact_start,
                "artifact_event_watermark": int(
                    artifact_events[-1].get("sequence", 0)
                ) if artifact_events else 0,
                "artifacts_finalized": True,
            }
            self.store.append(
                "RUN_ARTIFACT_FINALIZATION_COMPLETED", completed_payload,
            )
            self.live_store.append(
                "LIVE_RUN_ARTIFACT_FINALIZATION_COMPLETED", completed_payload,
            )
        except KeyboardInterrupt:
            artifacts_finalized = False
            operator_interrupted = True
            interruption_reason = "operator interrupted during artifact finalization"
            if self._internal_failure:
                self._secondary_failures.append({
                    "kind": "RUN_ARTIFACT_FINALIZATION_INTERRUPTED",
                    "detail": interruption_reason,
                })
            else:
                reason = interruption_reason
            interrupted_payload = {
                **artifact_start,
                "reason": interruption_reason,
                "report_ready": report_ready,
                "outcome_ready": outcome_ready,
                "artifacts_finalized": False,
            }
            self.store.append(
                "RUN_ARTIFACT_FINALIZATION_INTERRUPTED", interrupted_payload,
            )
            self.live_store.append(
                "LIVE_RUN_ARTIFACT_FINALIZATION_INTERRUPTED", interrupted_payload,
            )
            try:
                write_summary(artifact_error=interruption_reason)
            except Exception as summary_exc:
                self._secondary_failures.append({
                    "kind": "RUN_SUMMARY_WRITE_FAILED",
                    "detail": _sanitize_live_text(summary_exc)[:800],
                })
        except Exception as exc:
            artifacts_finalized = False
            artifact_reason = (
                f"artifact finalization failed: {type(exc).__name__}: "
                f"{_sanitize_live_text(exc)[:800]}"
            )
            if self._internal_failure or operator_interrupted:
                self._secondary_failures.append({
                    "kind": "RUN_ARTIFACT_FINALIZATION_FAILED",
                    "detail": artifact_reason,
                })
            else:
                reason = artifact_reason
            self._internal_failure = True
            failed_payload = {
                **artifact_start,
                "reason": artifact_reason,
                "report_ready": report_ready,
                "outcome_ready": outcome_ready,
                "artifacts_finalized": False,
            }
            self.store.append("RUN_ARTIFACT_FINALIZATION_FAILED", failed_payload)
            self.live_store.append(
                "LIVE_RUN_ARTIFACT_FINALIZATION_FAILED", failed_payload,
            )
            try:
                write_summary(artifact_error=artifact_reason)
            except Exception as summary_exc:
                self._secondary_failures.append({
                    "kind": "RUN_SUMMARY_WRITE_FAILED",
                    "detail": _sanitize_live_text(summary_exc)[:800],
                })

        attempt_kind = (
            "ATTEMPT_INTERRUPTED" if operator_interrupted else
            "ATTEMPT_FAILED" if self._internal_failure else "ATTEMPT_COMPLETED"
        )
        self.store.append(attempt_kind, {
            "attempt_id": self._attempt_id,
            "reason": reason,
            "internal_failure": self._internal_failure,
            "operator_interrupted": operator_interrupted,
            "recovery_completed": self._recovery_completed,
            "campaign_status": campaign_status,
            "artifacts_finalized": artifacts_finalized,
        })
        terminal_lifecycle = job_lifecycle_metrics(self.store.replay())
        terminal_mechanical = mechanical_lifecycle_metrics(self.store.replay())
        stop_payload = {
            "reason": reason,
            "internal_failure": self._internal_failure,
            "operator_interrupted": operator_interrupted,
            "artifacts_finalized": artifacts_finalized,
            "execution_mode": execution_mode,
            "run_outcome": current_run_outcome(),
            "lifecycle_phase": self.lifecycle.phase,
            "campaign_id": self.campaign_id,
            "epoch_id": self.epoch_id,
            "campaign_status": campaign_status,
            "token_governor": self.governor.snapshot(),
            "mechanical_token_governor": self.mechanical_governor.snapshot(),
            "secondary_failures": list(self._secondary_failures),
            "report": str(report_path) if report_ready else None,
            "outcome": str(outcome_path) if outcome_ready else None,
            **terminal_lifecycle.to_dict(),
        }
        self.live_store.append("LIVE_RUN_STOPPED", stop_payload)
        self.store.append("RUN_STOPPED", stop_payload)
        events = self.store.replay()
        lifecycle = job_lifecycle_metrics(events)
        mechanical_lifecycle = mechanical_lifecycle_metrics(events)
        if operator_interrupted:
            raise KeyboardInterrupt
        return RunResult(
            run_id=self.run_id,
            report_path=report_path,
            stopped_reason=reason,
            job_count=lifecycle.jobs_terminal,
            event_count=len(events),
            jobs_started=lifecycle.jobs_started,
            jobs_completed=lifecycle.jobs_completed,
            jobs_cancelled=lifecycle.jobs_cancelled,
            jobs_terminal=lifecycle.jobs_terminal,
            mechanical_subtasks_requested=mechanical_lifecycle.requested,
            mechanical_attempts_started=mechanical_lifecycle.attempts_started,
            mechanical_subtasks_terminal=mechanical_lifecycle.terminal,
            internal_failure=self._internal_failure,
            run_mode=execution_mode,
            outcome_path=outcome_path if outcome_ready else None,
            campaign_id=self.campaign_id,
            epoch_id=self.epoch_id,
            campaign_status=campaign_status,
            artifacts_finalized=artifacts_finalized,
        )


def build_mock_full_cycle_backend(
    *,
    claim_id: str = "C_ROOT",
    statement: str = "AMR_PLACEHOLDER: replace with the exact final claim statement.",
    assumptions: list[str] | None = None,
    dependencies: list[str] | None = None,
    domain: str = "math-research",
    evidence_path: str | None = None,
    evidence_receipts: list[dict[str, str]] | None = None,
    semantic_bridge_ids: list[str] | None = None,
) -> MockCodexBackend:
    assumptions = list(assumptions or [])
    dependencies = list(dependencies or [])
    if domain not in {
        "math-research",
        "certified-computational-research",
        "empirical-research",
    }:
        raise ValueError(f"unsupported mock lifecycle domain: {domain}")
    evidence_receipts = list(evidence_receipts or [])
    semantic_bridge_ids = list(semantic_bridge_ids or [])
    if domain != "math-research" and (
        not evidence_path or not evidence_receipts
    ):
        raise ValueError("non-mathematical mock lifecycle requires deterministic evidence")
    if domain == "certified-computational-research":
        candidate_type = "CERTIFICATE"
        proposed_evidence = EvidenceLevel.E4_CERTIFIED
        verified_evidence = EvidenceLevel.E4_CERTIFIED
        worker_result_type = "CERTIFICATE"
        worker_finding = "deterministic mock certificate"
    elif domain == "empirical-research":
        candidate_type = "CONFIRMATION"
        proposed_evidence = EvidenceLevel.E2_EXACT_TESTED
        verified_evidence = EvidenceLevel.E3_REDUNDANT_EXACT
        worker_result_type = "EXPERIMENT_RESULT"
        worker_finding = "deterministic mock experiment under a frozen protocol"
    else:
        candidate_type = "THEOREM_CANDIDATE"
        proposed_evidence = EvidenceLevel.E0_SPECULATIVE
        verified_evidence = EvidenceLevel.E0_SPECULATIVE
        worker_result_type = "PROOF"
        worker_finding = "deterministic mock proof"
    is_math = domain == "math-research"
    director_plan = {
        "assessment": (
            "Toy lifecycle should test concurrent proof and falsification."
            if is_math else
            "Toy lifecycle should test domain evidence and an adversarial lane."
        ),
        "spawn": [
            {
                "task_id": "mock-prover", "role": "prover", "target_claim": claim_id,
                "exact_objective": (
                    f"Produce a proof candidate for the assigned statement: {statement}"
                    if is_math else
                    f"Produce domain evidence for the assigned claim: {statement}"
                ),
                "why_now": "exercise candidate and audit lifecycle", "dependencies": [],
                "expected_information_gain": "HIGH", "research_impact": "HIGH",
                "estimated_cost_tier": "LOW", "required_files": [],
                "stop_conditions": [
                    "produce a proof candidate or exact flaw"
                    if is_math else
                    "produce auditable domain evidence or an exact flaw"
                ],
                "priority": 0.9,
                "route_family": "main", "modifies_code": False,
                "metadata": {"allow_derived_claims": False},
                "representation": {"branch":"LEGACY_UNSPECIFIED","localization":"LEGACY_UNSPECIFIED","saturation":"LEGACY_UNSPECIFIED","normalization":"LEGACY_UNSPECIFIED","content":"LEGACY_UNSPECIFIED","exceptional_factors":[],"combination_scope":"LEGACY_UNSPECIFIED"},
            },
            {
                "task_id": "mock-falsifier", "role": "falsifier", "target_claim": claim_id,
                "exact_objective": (
                    f"Seek a bounded counterexample to the assigned statement: {statement}"
                    if is_math else
                    f"Adversarially test the assigned claim under the frozen protocol: {statement}"
                ),
                "why_now": "independent adversarial lane", "dependencies": [],
                "expected_information_gain": "HIGH", "research_impact": "MEDIUM",
                "estimated_cost_tier": "LOW", "required_files": [],
                "stop_conditions": ["check n=0 through 20 exactly"],
                "priority": 0.8,
                "route_family": "independent", "modifies_code": False,
                "metadata": {"allow_derived_claims": False},
                "representation": {"branch":"LEGACY_UNSPECIFIED","localization":"LEGACY_UNSPECIFIED","saturation":"LEGACY_UNSPECIFIED","normalization":"LEGACY_UNSPECIFIED","content":"LEGACY_UNSPECIFIED","exceptional_factors":[],"combination_scope":"LEGACY_UNSPECIFIED"},
            },
        ],
        "audit_priorities": [], "route_updates": [],
        "short_rationale": "exercise all safety gates",
    }
    candidate = {
        "event_id": f"mock-{uuid4().hex[:12]}", "producer_thread_id": None,
        "producer_task_id": "mock-prover", "claim_id": claim_id,
        "type": candidate_type, "impact": "CRITICAL",
        "concise_summary": f"Deterministic {domain} mock candidate.",
        "exact_statement": statement,
        "artifact_paths": ([str(evidence_path)] if evidence_path else []),
        "reproduction_commands": (
            [f'python -c "import json; json.load(open(r\'{evidence_path}\'))"']
            if evidence_path else []
        ),
        "dependency_impact": ["mock lifecycle"], "assumptions": assumptions,
        "dependencies": dependencies,
        "parent_claim_id": None,
        "representation": {"branch":"LEGACY_UNSPECIFIED","localization":"LEGACY_UNSPECIFIED","saturation":"LEGACY_UNSPECIFIED","normalization":"LEGACY_UNSPECIFIED","content":"LEGACY_UNSPECIFIED","exceptional_factors":[],"combination_scope":"LEGACY_UNSPECIFIED"},
        "bridge_representation_ids": [],
        "semantic_bridge_ids": semantic_bridge_ids,
        "evidence_receipts": evidence_receipts,
        "proposed_evidence_level": proposed_evidence, "timestamp": utc_now(),
    }
    worker_result = {
        "result_type": worker_result_type, "main_finding": worker_finding,
        "status": "COMPLETED", "artifact_paths": [],
        "next_suggested_question": "independent audit",
        "evidence_level": proposed_evidence,
    }
    falsifier_result = {
        "result_type": "NO_PROGRESS",
        "main_finding": "no boundary failure in deterministic mock range", "status": "NO_COUNTEREXAMPLE_WITHIN_SCOPE",
        "artifact_paths": [], "next_suggested_question": "audit proof",
        "evidence_level": "E0_SPECULATIVE",
    }
    scripts: dict[str, list[dict[str, Any]]] = {
        "director": [{"result": director_plan, "tokens": 120}, {"result": {
            "assessment": "Toy claim audit is in progress.", "spawn": [],
            "audit_priorities": [],
            "route_updates": [{
                "route_id": "toy-complete", "action": "PAUSE",
                "reason": "await controller finalization", "retry_condition": None,
            }],
            "short_rationale": "mock cycle complete",
        }, "tokens": 80}],
        "prover": [{"candidate": candidate, "post_candidate_delay": 0.2, "result": worker_result, "tokens": 200}],
        "falsifier": [{"delay": 0.3, "result": falsifier_result, "tokens": 120}],
    }
    if domain != "math-research":
        audit_result = {
            "verdict": "PASS",
            "checks": [{
                "name": "mock deterministic domain reconstruction",
                "passed": True,
                "detail": "sealed fixture and exact command independently checked",
            }],
            "gaps": [],
            "notes": ["deterministic mock lifecycle only"],
            "verified_evidence_level": verified_evidence,
        }
        scripts["evaluator_auditor"] = [
            {"result": audit_result, "tokens": 100},
            {"result": audit_result, "tokens": 100},
        ]
    return MockCodexBackend(scripts)
