from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .domain_semantics import (
    DomainSemantics, builtin_domain_contract, domain_semantics_from_contract,
)
from .models import (
    CandidateEvent, Claim, EvidenceLevel, MathStatus, ObligationStatus,
    ProofObligation, TrustStatus, evidence_rank, stable_hash, utc_now,
)
from .storage import atomic_write_json


class ClaimGraph:
    def __init__(
        self,
        claims: dict[str, Claim],
        path: Path | None = None,
        updated_at: str | None = None,
        *,
        semantics: DomainSemantics | None = None,
        domain_contract: dict[str, Any] | None = None,
    ):
        if semantics is not None and domain_contract is not None:
            raise ValueError("use semantics or domain_contract, not both")
        self.claims = claims
        self.path = path
        self.updated_at = updated_at
        self.semantics = semantics or domain_semantics_from_contract(domain_contract)
        self._terminal_transition_guard: (
            Callable[[CandidateEvent, str, str], None] | None
        ) = None
        for claim in self.claims.values():
            self.semantics.validate_status(claim.math_status)
        self._ensure_proof_obligations()
        self._rebuild_dependents()

    def set_terminal_transition_guard(
        self,
        guard: Callable[[CandidateEvent, str, str], None] | None,
    ) -> None:
        self._terminal_transition_guard = guard

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        semantics: DomainSemantics | None = None,
        domain_contract: dict[str, Any] | None = None,
    ) -> "ClaimGraph":
        if semantics is not None and domain_contract is not None:
            raise ValueError("use semantics or domain_contract, not both")
        raw = json.loads(path.read_text(encoding="utf-8"))
        has_raw_domain = "domain" in raw
        raw_domain = raw.get("domain")
        if has_raw_domain and (
            not isinstance(raw_domain, str) or not raw_domain.strip()
        ):
            raise ValueError("claim graph domain must be a non-empty string")
        if semantics is not None:
            resolved_semantics = semantics
        elif domain_contract is not None:
            resolved_semantics = domain_semantics_from_contract(domain_contract)
        elif has_raw_domain:
            resolved_semantics = domain_semantics_from_contract(
                builtin_domain_contract(raw_domain)
            )
        else:
            resolved_semantics = domain_semantics_from_contract(None)
        if has_raw_domain and raw_domain != resolved_semantics.domain:
            raise ValueError(
                "claim graph domain does not match the selected domain semantics"
            )
        version = int(raw.get("schema_version", 1))
        if version not in {1, 2, 3}:
            raise ValueError(f"unsupported claim graph schema_version: {version}")
        claims = {
            item["claim_id"]: Claim.from_dict(item, semantics=resolved_semantics)
            for item in raw["claims"]
        }
        return cls(claims, path, raw.get("updated_at"), semantics=resolved_semantics)

    def _rebuild_dependents(self) -> None:
        for claim in self.claims.values():
            claim.downstream_dependents = []
        for claim in self.claims.values():
            for dep in claim.dependencies:
                if dep in self.claims:
                    self.claims[dep].downstream_dependents.append(claim.claim_id)
        for claim in self.claims.values():
            claim.downstream_dependents.sort()

    @staticmethod
    def _obligation_id(claim_id: str, statement: str) -> str:
        digest = stable_hash({
            "claim_id": claim_id,
            "statement": " ".join(statement.split()),
        })[:20].upper()
        return f"{claim_id}::OBL::{digest}"

    def _ensure_claim_obligations(self, claim: Claim) -> None:
        if self.semantics.obligation_kind != "proof":
            if claim.proof_obligations:
                raise ValueError(
                    f"{self.semantics.domain} claims cannot contain proof obligations"
                )
            return
        if claim.proof_obligations:
            return
        statements = [item for item in claim.current_gaps if str(item).strip()]
        if not statements:
            statements = [claim.statement]
        trusted_terminal = claim.trust_status in {
            TrustStatus.CANONICAL_TRUSTED,
            TrustStatus.AUDITED_NIGHTLY,
            TrustStatus.FORMALLY_VERIFIED,
        }
        if trusted_terminal and claim.math_status == MathStatus.PROVED:
            status = ObligationStatus.DISCHARGED
        elif trusted_terminal and claim.math_status == MathStatus.REFUTED:
            status = ObligationStatus.REFUTED
        else:
            status = ObligationStatus.OPEN
        claim.proof_obligations = [
            ProofObligation(
                obligation_id=self._obligation_id(claim.claim_id, statement),
                statement=statement,
                status=status,
                dependencies=list(claim.dependencies),
                evidence_paths=(
                    list(claim.evidence_paths)
                    if status in {ObligationStatus.DISCHARGED, ObligationStatus.REFUTED}
                    else []
                ),
                created_at=claim.last_meaningful_progress,
                updated_at=claim.last_meaningful_progress,
            )
            for statement in statements
        ]

    def _ensure_proof_obligations(self) -> None:
        for claim in self.claims.values():
            self._ensure_claim_obligations(claim)

    def validate(self) -> None:
        all_obligations = {
            obligation.obligation_id
            for claim in self.claims.values()
            for obligation in claim.proof_obligations
        }
        if sum(len(claim.proof_obligations) for claim in self.claims.values()) != len(all_obligations):
            raise ValueError("proof obligation ids must be globally unique")
        for claim in self.claims.values():
            self.semantics.validate_status(claim.math_status)
            missing = set(claim.dependencies) - set(self.claims)
            if missing:
                raise ValueError(f"{claim.claim_id} has missing dependencies: {sorted(missing)}")
            if claim.parent_claim_id is not None:
                if claim.parent_claim_id == claim.claim_id:
                    raise ValueError(f"{claim.claim_id} cannot be its own parent claim")
                if claim.parent_claim_id not in self.claims:
                    raise ValueError(
                        f"{claim.claim_id} has missing parent claim: {claim.parent_claim_id}"
                    )
            for obligation in claim.proof_obligations:
                ProofObligation.from_dict(obligation.to_dict())
                missing_obligation_dependencies = set(obligation.dependencies) - (
                    set(self.claims) | all_obligations
                )
                if missing_obligation_dependencies:
                    raise ValueError(
                        f"{obligation.obligation_id} has missing dependencies: "
                        f"{sorted(missing_obligation_dependencies)}"
                    )
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError(f"claim dependency cycle at {node}")
            if node in visited:
                return
            visiting.add(node)
            for dep in self.claims[node].dependencies:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for claim_id in self.claims:
            visit(claim_id)

    def compact_snapshot(self, active_tasks: list[dict[str, Any]], budget: dict[str, Any], recent: list[dict[str, Any]]) -> dict[str, Any]:
        trusted = []
        rejected = []
        frontier = []
        for claim in self.claims.values():
            status_key = (
                "math_status" if self.semantics.domain == "math-research"
                else "research_status"
            )
            frontier_key = (
                "proof_frontier" if self.semantics.domain == "math-research"
                else "research_frontier"
            )
            row = {
                "claim_id": claim.claim_id,
                "parent_claim_id": claim.parent_claim_id,
                "statement": claim.statement,
                status_key: claim.math_status,
                "trust_status": claim.trust_status,
                "evidence_level": claim.evidence_level,
                "dependencies": claim.dependencies,
                "gaps": claim.current_gaps,
                "priority": claim.priority,
                "evidence_paths": claim.evidence_paths,
                frontier_key: self.research_frontier(claim.claim_id),
            }
            if claim.trust_status in {TrustStatus.CANONICAL_TRUSTED, TrustStatus.AUDITED_NIGHTLY, TrustStatus.FORMALLY_VERIFIED}:
                trusted.append(row)
            if self.semantics.dependency_is_refuting(claim.math_status):
                rejected.append(row)
            if self.semantics.is_frontier(claim.math_status):
                frontier.append(row)
        negative_key = (
            "strictly_refuted" if self.semantics.domain == "math-research"
            else "strictly_negative"
        )
        snapshot = {
            "strictly_trusted": trusted,
            negative_key: rejected,
            "open_frontier": sorted(frontier, key=lambda x: float(x["priority"].get("score", 0)), reverse=True),
            "active_tasks": active_tasks,
            "recent_changes": recent[-20:],
            "budget": budget,
        }
        if self.semantics.domain != "math-research":
            snapshot["domain"] = self.semantics.domain
        return snapshot

    def research_frontier(self, claim_id: str) -> dict[str, Any]:
        """Return the canonical remaining/next view for one claim.

        Math derives this view from proof obligations. Other domains expose
        their obligation kind without synthesizing mathematical obligations.
        """
        claim = self.claims[claim_id]
        if self.semantics.obligation_kind != "proof":
            return {
                "claim_id": claim_id,
                "obligation_kind": self.semantics.obligation_kind,
                "obligations": [],
                "remaining_obligation_ids": [],
                "next_obligation_id": None,
            }
        status_by_id = {
            obligation.obligation_id: obligation.status
            for item in self.claims.values()
            for obligation in item.proof_obligations
        }
        remaining = [
            obligation for obligation in claim.proof_obligations
            if obligation.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}
        ]

        def ready(obligation: ProofObligation) -> bool:
            for dependency in obligation.dependencies:
                if dependency in self.claims:
                    dependency_claim = self.claims[dependency]
                    if not (
                        self.semantics.dependency_is_satisfied(
                            dependency_claim.math_status
                        )
                        and dependency_claim.trust_status in {
                            TrustStatus.CANONICAL_TRUSTED,
                            TrustStatus.AUDITED_NIGHTLY,
                            TrustStatus.FORMALLY_VERIFIED,
                        }
                    ):
                        return False
                elif status_by_id.get(dependency) != ObligationStatus.DISCHARGED:
                    return False
            return obligation.status == ObligationStatus.OPEN

        ready_open = [item for item in remaining if ready(item)]
        ordered = sorted(remaining, key=lambda item: item.obligation_id)
        next_item = min(ready_open, key=lambda item: item.obligation_id) if ready_open else (
            ordered[0] if ordered else None
        )
        return {
            "claim_id": claim_id,
            "obligations": [item.to_dict() for item in sorted(
                claim.proof_obligations, key=lambda item: item.obligation_id,
            )],
            "remaining_obligation_ids": [item.obligation_id for item in ordered],
            "next_obligation_id": next_item.obligation_id if next_item else None,
        }

    def proof_frontier(self, claim_id: str) -> dict[str, Any]:
        """Compatibility alias for the mathematical ClaimGraph API."""
        return self.research_frontier(claim_id)

    def mark_candidate(self, event: CandidateEvent) -> None:
        self.semantics.validate_event_type(event.type)
        claim = self.claims.get(event.claim_id)
        if claim is None:
            missing = set(event.dependencies) - set(self.claims)
            if missing:
                raise ValueError(f"candidate has unknown dependencies: {sorted(missing)}")
            if event.parent_claim_id is not None and event.parent_claim_id not in self.claims:
                raise ValueError(f"candidate has unknown parent claim: {event.parent_claim_id}")
            claim = Claim(
                claim_id=event.claim_id,
                statement=event.exact_statement,
                assumptions=list(event.assumptions),
                math_status=self.semantics.candidate_status,
                trust_status=TrustStatus.UNTRUSTED_CANDIDATE,
                dependencies=list(event.dependencies), downstream_dependents=[], evidence_paths=event.artifact_paths,
                known_counterexamples=[], current_gaps=["independent audit pending"], active_tasks=[],
                last_meaningful_progress=event.timestamp,
                priority={"score": 0.5, "impact": event.impact},
                parent_claim_id=event.parent_claim_id,
                evidence_level=EvidenceLevel.E0_SPECULATIVE,
            )
            self.claims[event.claim_id] = claim
            self._ensure_claim_obligations(claim)
        elif " ".join(claim.statement.split()) != " ".join(event.exact_statement.split()):
            raise ValueError(
                f"candidate statement does not match existing claim {event.claim_id}; "
                "use a new stable claim_id for a different statement"
            )
        elif {
            " ".join(item.split()) for item in event.assumptions
        } != {" ".join(item.split()) for item in claim.assumptions}:
            raise ValueError(f"candidate assumptions do not match existing claim {event.claim_id}")
        elif set(event.dependencies) != set(claim.dependencies):
            raise ValueError(f"candidate dependencies do not match existing claim {event.claim_id}")
        elif event.parent_claim_id != claim.parent_claim_id:
            raise ValueError(f"candidate parent_claim_id does not match existing claim {event.claim_id}")

        # Candidate state belongs to AuditGate.  In particular, registering a
        # new proof attempt for an existing trusted claim must not downgrade or
        # otherwise rewrite that claim before an independent audit finishes.

    def apply_audit_pass(
        self,
        event: CandidateEvent,
        pass_count: int,
        required: int,
        verified_evidence_level: str = EvidenceLevel.E0_SPECULATIVE,
    ) -> None:
        claim = self.claims[event.claim_id]
        if pass_count < required:
            return
        evidence_rank(verified_evidence_level)
        transition = self.semantics.transition_for(event.type, verified_evidence_level)
        if transition["status"] is None and not transition["trust_change"]:
            return
        next_status = transition["status"]
        if (
            next_status is not None
            and self.semantics.final_outcome(next_status) is not None
            and self._terminal_transition_guard is not None
        ):
            self._terminal_transition_guard(
                event, str(next_status), verified_evidence_level,
            )
        current_outcome = self.semantics.final_outcome(claim.math_status)
        next_outcome = (
            self.semantics.final_outcome(next_status)
            if next_status is not None else None
        )
        if (
            self.semantics.domain != "math-research"
            and
            current_outcome is not None
            and next_outcome is not None
            and current_outcome != next_outcome
        ):
            raise ValueError(
                f"audited {event.type} conflicts with terminal status "
                f"{claim.math_status}"
            )
        if transition["trust_change"]:
            claim.trust_status = TrustStatus.AUDITED_NIGHTLY
        if evidence_rank(verified_evidence_level) > evidence_rank(claim.evidence_level):
            claim.evidence_level = verified_evidence_level
        claim.evidence_paths = sorted(set(claim.evidence_paths + event.artifact_paths))
        applied_status = False
        if next_status is not None:
            if (
                next_outcome is not None
                or (
                    current_outcome is None
                    and (
                        claim.math_status in {
                            self.semantics.initial_status,
                            self.semantics.candidate_status,
                        }
                        or self.semantics.is_frontier(next_status)
                    )
                )
            ):
                claim.math_status = next_status
                applied_status = True
        outcome = self.semantics.final_outcome(claim.math_status)
        if (
            applied_status
            and outcome == "negative"
            and self.semantics.domain == "math-research"
        ):
            claim.known_counterexamples = sorted(set(claim.known_counterexamples + event.artifact_paths))
        terminal_obligation_status: str | None = None
        if applied_status and outcome == "negative":
            terminal_obligation_status = ObligationStatus.REFUTED
        elif applied_status and outcome == "positive":
            terminal_obligation_status = ObligationStatus.DISCHARGED
        if terminal_obligation_status is not None:
            if self.semantics.obligation_kind == "proof":
                now = utc_now()
                for obligation in claim.proof_obligations:
                    if obligation.status in {ObligationStatus.OPEN, ObligationStatus.BLOCKED}:
                        obligation.status = terminal_obligation_status
                        obligation.evidence_paths = sorted(set(
                            obligation.evidence_paths + event.artifact_paths
                        ))
                        obligation.updated_at = now
            claim.current_gaps = []
        claim.last_meaningful_progress = utc_now()

    def apply_audit_reject(self, event: CandidateEvent, reason: str) -> None:
        # A rejected or inconclusive proof artifact is not a refutation of the
        # mathematical statement.  Candidate disposition is owned by
        # AuditGate/controller event state, so rejection deliberately leaves
        # the claim graph unchanged.
        del event, reason

    def prune_failed_dependencies(self) -> dict[str, list[str]]:
        blocked: dict[str, list[str]] = {}
        failed = {
            claim_id for claim_id, claim in self.claims.items()
            if self.semantics.dependency_is_refuting(claim.math_status)
            and claim.trust_status in {TrustStatus.CANONICAL_TRUSTED, TrustStatus.AUDITED_NIGHTLY, TrustStatus.FORMALLY_VERIFIED}
        }
        changed = True
        while changed:
            changed = False
            for claim in self.claims.values():
                bad = sorted(set(claim.dependencies) & failed)
                inherited = sorted(dep for dep in claim.dependencies if dep in blocked)
                reasons = bad + inherited
                if reasons and claim.claim_id not in blocked:
                    blocked[claim.claim_id] = reasons
                    changed = True
        return blocked

    def to_payload(self, *, updated_at: str | None = None) -> dict[str, Any]:
        if updated_at is not None:
            self.updated_at = updated_at
        self._rebuild_dependents()
        self.validate()
        payload = {
            "schema_version": 3,
            "updated_at": self.updated_at,
            "claims": [self._claim_payload(self.claims[key]) for key in sorted(self.claims)],
        }
        if self.semantics.domain != "math-research":
            payload["domain"] = self.semantics.domain
        return payload

    def _claim_payload(self, claim: Claim) -> dict[str, Any]:
        payload = claim.to_dict()
        if self.semantics.domain != "math-research":
            payload["research_status"] = payload.pop("math_status")
            payload.pop("proof_obligations", None)
        return payload

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("claim graph has no output path")
        atomic_write_json(target, self.to_payload(updated_at=utc_now()))
