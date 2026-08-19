from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import (
    CandidateEvent, Claim, EvidenceLevel, MathStatus, TrustStatus, evidence_rank, utc_now,
)
from .storage import atomic_write_json


class ClaimGraph:
    def __init__(self, claims: dict[str, Claim], path: Path | None = None):
        self.claims = claims
        self.path = path
        self._rebuild_dependents()

    @classmethod
    def load(cls, path: Path) -> "ClaimGraph":
        raw = json.loads(path.read_text(encoding="utf-8"))
        version = int(raw.get("schema_version", 1))
        if version not in {1, 2}:
            raise ValueError(f"unsupported claim graph schema_version: {version}")
        claims = {item["claim_id"]: Claim.from_dict(item) for item in raw["claims"]}
        return cls(claims, path)

    def _rebuild_dependents(self) -> None:
        for claim in self.claims.values():
            claim.downstream_dependents = []
        for claim in self.claims.values():
            for dep in claim.dependencies:
                if dep in self.claims:
                    self.claims[dep].downstream_dependents.append(claim.claim_id)
        for claim in self.claims.values():
            claim.downstream_dependents.sort()

    def validate(self) -> None:
        for claim in self.claims.values():
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
            row = {
                "claim_id": claim.claim_id,
                "parent_claim_id": claim.parent_claim_id,
                "statement": claim.statement,
                "math_status": claim.math_status,
                "trust_status": claim.trust_status,
                "evidence_level": claim.evidence_level,
                "dependencies": claim.dependencies,
                "gaps": claim.current_gaps,
                "priority": claim.priority,
                "evidence_paths": claim.evidence_paths,
            }
            if claim.trust_status in {TrustStatus.CANONICAL_TRUSTED, TrustStatus.AUDITED_NIGHTLY, TrustStatus.FORMALLY_VERIFIED}:
                trusted.append(row)
            if claim.math_status == MathStatus.FAILED:
                rejected.append(row)
            if claim.math_status in {MathStatus.OPEN, MathStatus.PLAUSIBLE, MathStatus.REDUCED_TO}:
                frontier.append(row)
        return {
            "strictly_trusted": trusted,
            "strictly_refuted": rejected,
            "open_frontier": sorted(frontier, key=lambda x: float(x["priority"].get("score", 0)), reverse=True),
            "active_tasks": active_tasks,
            "recent_changes": recent[-20:],
            "budget": budget,
        }

    def mark_candidate(self, event: CandidateEvent) -> None:
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
                math_status=MathStatus.PLAUSIBLE,
                trust_status=TrustStatus.UNTRUSTED_CANDIDATE,
                dependencies=list(event.dependencies), downstream_dependents=[], evidence_paths=event.artifact_paths,
                known_counterexamples=[], current_gaps=["independent audit pending"], active_tasks=[],
                last_meaningful_progress=event.timestamp,
                priority={"score": 0.5, "impact": event.impact},
                parent_claim_id=event.parent_claim_id,
                evidence_level=EvidenceLevel.E0_SPECULATIVE,
            )
            self.claims[event.claim_id] = claim
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
        claim.trust_status = TrustStatus.AUDITED_NIGHTLY
        if evidence_rank(verified_evidence_level) > evidence_rank(claim.evidence_level):
            claim.evidence_level = verified_evidence_level
        claim.evidence_paths = sorted(set(claim.evidence_paths + event.artifact_paths))
        if event.type in {"COUNTEREXAMPLE", "KEY_REFUTATION"}:
            claim.math_status = MathStatus.FAILED
            claim.known_counterexamples = sorted(set(claim.known_counterexamples + event.artifact_paths))
        elif event.type in {"THEOREM_CANDIDATE", "KEY_LEMMA", "EQUIVALENCE"}:
            claim.math_status = MathStatus.PROVED
        elif event.type == "REDUCTION":
            if claim.math_status not in {MathStatus.PROVED, MathStatus.FAILED}:
                claim.math_status = MathStatus.REDUCED_TO
        else:
            if claim.math_status not in {MathStatus.PROVED, MathStatus.FAILED, MathStatus.REDUCED_TO}:
                claim.math_status = MathStatus.COMPUTATION_ONLY
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
            if claim.math_status == MathStatus.FAILED
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

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("claim graph has no output path")
        self._rebuild_dependents()
        self.validate()
        atomic_write_json(target, {
            "schema_version": 2,
            "updated_at": utc_now(),
            "claims": [self.claims[key].to_dict() for key in sorted(self.claims)],
        })
