from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import json
from typing import Any, Iterable

from .models import ResearchTask, stable_hash, utc_now
from .storage import (
    append_jsonl,
    atomic_write_json,
    file_digest,
    read_jsonl,
    validate_storage_id,
)


FRONTIER_SCHEMA_VERSION = 1
EXTERNAL_RESULT_SCHEMA_VERSION = 1
ASSET_CARD_SCHEMA_VERSION = 1
THEME_SCHEMA_VERSION = 2
LEGACY_THEME_SCHEMA_VERSION = 1
ROUTING_AUDIT_RECEIPT_SCHEMA_VERSION = 1

ROUTE_STATUSES = frozenset({
    "ROUTE", "DO_NOT_ROUTE", "BLOCKED", "KILL_GATED", "WAIT_DEPENDENCY",
})
EXTERNAL_RESULT_CLASSES = frozenset({
    "AUDITED_EXTERNAL_RESULT",
    "UNAUDITED_EXTERNAL_RESULT",
    "COMPUTATION_ONLY",
    "CONFLICTING_RESULT",
})
RESULT_CONCLUSIONS = frozenset({
    "PROVED", "REFUTED", "INCONCLUSIVE", "COMPUTATION", "KILL_GATE",
})
MATURITY_LEVELS = frozenset({"RESULT", "THEME", "FINAL"})
ASSET_KINDS = frozenset({
    "THEOREM",
    "LEMMA",
    "REPRESENTATION_BRIDGE",
    "RESEARCH_TOOL",
    "CERTIFICATE_VERIFIER",
    "NEGATIVE_RESULT",
    "KILL_GATE",
    "RESEARCH_HYPOTHESIS",
})
ASSET_AUDIT_STATUSES = frozenset({
    "AUDITED", "UNAUDITED", "UNPROVED", "CONFLICTING",
})

_TRUSTED_STATUSES = frozenset({
    "CANONICAL_TRUSTED", "AUDITED_NIGHTLY", "FORMALLY_VERIFIED",
})
_RESULT_KEYS = frozenset({
    "schema_version", "result_id", "exact_statement", "scope_ids",
    "claim_ids", "representation_id", "dependencies", "classification",
    "conclusion", "maturity_level", "proof_refs", "certificate_refs",
    "source_refs", "audit", "provenance", "supersedes",
})
_ASSET_KEYS = frozenset({
    "schema_version", "asset_id", "kind", "title", "what_it_gives",
    "scope_ids", "claim_ids", "representation_ids", "preconditions",
    "do_not_use", "inputs", "outputs", "proof_refs", "code_refs",
    "certificate_refs", "source_refs", "audit", "known_failure_modes",
    "dependencies", "method_id", "do_not_repeat", "reopen_if",
    "representation_edge", "provenance", "supersedes",
})
_THEME_V1_KEYS = frozenset({
    "schema_version", "theme_id", "title", "objective", "include_claim_ids",
    "include_scope_ids", "exclude_claim_ids", "exclude_scope_ids",
    "allowed_method_ids", "forbidden_method_ids", "dependency_boundary",
    "combination_scope", "obligations",
})
_THEME_KEYS = _THEME_V1_KEYS | {"completion_policy"}
_COMPLETION_POLICY_KEYS = frozenset({
    "max_accepted_candidates", "post_candidate_mode",
    "max_valid_audit_attempts_per_candidate", "terminal_audit_verdicts",
})
_COMPLETION_POLICY_OPTIONAL_KEYS = frozenset({"terminal_research_outcomes"})
_TERMINAL_AUDIT_VERDICTS = frozenset({"PASS", "REJECT", "UNRESOLVED"})
_TERMINAL_RESEARCH_OUTCOMES = frozenset({
    "BLOCKED", "FALSIFIED", "OBLIGATION_EXHAUSTED",
})
_EVIDENCE_REF_KEYS = frozenset({"kind", "path", "sha256"})
_AUDIT_KEYS = frozenset({
    "verdict", "independent", "auditor", "audit_level", "policy_version",
    "report_refs", "reuse_audit_key",
})
_PROVENANCE_KEYS = frozenset({"producer", "origin", "produced_at", "lineage"})
_EDGE_KEYS = frozenset({
    "source_representation_id", "target_representation_id", "conditions",
    "localization", "saturation", "content", "exceptional_factors",
})
_OBLIGATION_KEYS = frozenset({
    "scope_id", "claim_id", "exact_objective", "representation_id",
    "dependencies", "allowed_method_ids", "forbidden_method_ids",
})


def _require_exact_keys(value: Any, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        actual = set(value) if isinstance(value, dict) else set()
        raise ValueError(
            f"{label} fields differ; missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _normalized_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return " ".join(value.split())


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _normalized_text(value, label)


def _string_list(
    value: Any,
    label: str,
    *,
    nonempty: bool = False,
    normalize_text: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings")
    normalized = tuple(
        _normalized_text(item, label) if normalize_text else item.strip()
        for item in value
    )
    if any(not item for item in normalized):
        raise ValueError(f"{label} contains an empty string")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{label} contains duplicates")
    if nonempty and not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _portable_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a project-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value != path.as_posix():
        raise ValueError(f"{label} must be a normalized project-relative POSIX path")
    if path.parts and path.parts[0].endswith(":"):
        raise ValueError(f"{label} must not contain a drive prefix")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required at {path}")
    return value


def _audit_identity_from_object(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    audit = value.get("audit") or {}
    if kind == "EXTERNAL_RESULT":
        statement = value.get("exact_statement")
        representations: Any = value.get("representation_id")
        proof_refs = value.get("proof_refs") or []
        certificate_refs = value.get("certificate_refs") or []
    elif kind == "RESEARCH_ASSET":
        statement = value.get("what_it_gives")
        representations = sorted(value.get("representation_ids") or [])
        proof_refs = value.get("proof_refs") or []
        certificate_refs = [
            *(value.get("code_refs") or []),
            *(value.get("certificate_refs") or []),
        ]
    else:
        raise ValueError(f"unsupported audit source object kind: {kind}")
    return {
        "exact_statement": statement,
        "representation_id": representations,
        "dependencies": sorted(value.get("dependencies") or []),
        "proof_hashes": sorted(str(item.get("sha256")) for item in proof_refs),
        "certificate_hashes": sorted(
            str(item.get("sha256")) for item in certificate_refs
        ),
        "source_hashes": sorted(
            str(item.get("sha256")) for item in (value.get("source_refs") or [])
        ),
        "audit_policy_version": audit.get("policy_version"),
        "audit_level": audit.get("audit_level"),
    }


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "EvidenceRef":
        value = _require_exact_keys(raw, _EVIDENCE_REF_KEYS, label)
        kind = _normalized_text(value["kind"], f"{label}.kind")
        path = _portable_path(value["path"], f"{label}.path")
        sha256 = str(value["sha256"]).lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError(f"{label}.sha256 must be a lowercase SHA-256 digest")
        return cls(kind=kind, path=path, sha256=sha256)

    def validate(self, project_root: Path) -> str | None:
        path = (project_root / self.path).resolve()
        if not path.is_relative_to(project_root.resolve()):
            return f"evidence path escapes project: {self.path}"
        if not path.is_file():
            return f"evidence file is missing: {self.path}"
        observed = file_digest(path)
        if observed != self.sha256:
            return (
                f"evidence digest changed: {self.path}; "
                f"expected={self.sha256}, observed={observed}"
            )
        return None

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "sha256": self.sha256}


def _evidence_refs(raw: Any, label: str) -> tuple[EvidenceRef, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be an array")
    refs = tuple(EvidenceRef.from_dict(item, f"{label}[{index}]") for index, item in enumerate(raw))
    identities = [(item.kind, item.path, item.sha256) for item in refs]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} contains duplicate evidence references")
    return refs


@dataclass(frozen=True, slots=True)
class AuditMetadata:
    verdict: str | None
    independent: bool
    auditor: str | None
    audit_level: str
    policy_version: str
    report_refs: tuple[EvidenceRef, ...]
    reuse_audit_key: str | None

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> "AuditMetadata":
        value = _require_exact_keys(raw, _AUDIT_KEYS, label)
        verdict = value["verdict"]
        if verdict not in {None, "PASS", "REJECT", "UNRESOLVED"}:
            raise ValueError(f"{label}.verdict is invalid")
        if type(value["independent"]) is not bool:
            raise ValueError(f"{label}.independent must be boolean")
        auditor = _optional_text(value["auditor"], f"{label}.auditor")
        audit_level = str(value["audit_level"])
        if audit_level not in {"RESULT", "THEME_INTEGRATION", "GLOBAL"}:
            raise ValueError(f"{label}.audit_level is invalid")
        policy_version = _normalized_text(
            value["policy_version"], f"{label}.policy_version"
        )
        reuse_key = value["reuse_audit_key"]
        if reuse_key is not None:
            reuse_key = str(reuse_key).lower()
            if len(reuse_key) != 64 or any(
                ch not in "0123456789abcdef" for ch in reuse_key
            ):
                raise ValueError(f"{label}.reuse_audit_key is invalid")
        return cls(
            verdict=verdict,
            independent=value["independent"],
            auditor=auditor,
            audit_level=audit_level,
            policy_version=policy_version,
            report_refs=_evidence_refs(value["report_refs"], f"{label}.report_refs"),
            reuse_audit_key=reuse_key,
        )

    @property
    def is_direct_pass(self) -> bool:
        return bool(
            self.verdict == "PASS"
            and self.independent
            and self.auditor
            and self.report_refs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "independent": self.independent,
            "auditor": self.auditor,
            "audit_level": self.audit_level,
            "policy_version": self.policy_version,
            "report_refs": [item.to_dict() for item in self.report_refs],
            "reuse_audit_key": self.reuse_audit_key,
        }


def _provenance(raw: Any, label: str) -> dict[str, Any]:
    value = _require_exact_keys(raw, _PROVENANCE_KEYS, label)
    producer = _normalized_text(value["producer"], f"{label}.producer")
    origin = _normalized_text(value["origin"], f"{label}.origin")
    produced_at = _normalized_text(value["produced_at"], f"{label}.produced_at")
    lineage = _string_list(value["lineage"], f"{label}.lineage")
    return {
        "producer": producer,
        "origin": origin,
        "produced_at": produced_at,
        "lineage": list(lineage),
    }


@dataclass(frozen=True, slots=True)
class ExternalResult:
    result_id: str
    exact_statement: str
    scope_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    representation_id: str
    dependencies: tuple[str, ...]
    classification: str
    conclusion: str
    maturity_level: str
    proof_refs: tuple[EvidenceRef, ...]
    certificate_refs: tuple[EvidenceRef, ...]
    source_refs: tuple[EvidenceRef, ...]
    audit: AuditMetadata
    provenance: dict[str, Any]
    supersedes: tuple[str, ...]
    source_manifest: str
    object_sha256: str

    @classmethod
    def load(cls, project_root: Path, path: Path) -> "ExternalResult":
        raw = _require_exact_keys(_load_json(path), _RESULT_KEYS, "external result")
        if raw["schema_version"] != EXTERNAL_RESULT_SCHEMA_VERSION:
            raise ValueError(f"unsupported external result schema at {path}")
        result_id = validate_storage_id(str(raw["result_id"]), "result_id")
        exact_statement = _normalized_text(raw["exact_statement"], "exact_statement")
        scope_ids = _string_list(raw["scope_ids"], "scope_ids", nonempty=True)
        claim_ids = _string_list(raw["claim_ids"], "claim_ids")
        representation_id = _normalized_text(raw["representation_id"], "representation_id")
        dependencies = _string_list(raw["dependencies"], "dependencies")
        classification = str(raw["classification"])
        if classification not in EXTERNAL_RESULT_CLASSES:
            raise ValueError(f"invalid external result classification: {classification}")
        conclusion = str(raw["conclusion"])
        if conclusion not in RESULT_CONCLUSIONS:
            raise ValueError(f"invalid external result conclusion: {conclusion}")
        maturity_level = str(raw["maturity_level"])
        if maturity_level not in MATURITY_LEVELS:
            raise ValueError(f"invalid proof maturity level: {maturity_level}")
        proof_refs = _evidence_refs(raw["proof_refs"], "proof_refs")
        certificate_refs = _evidence_refs(raw["certificate_refs"], "certificate_refs")
        source_refs = _evidence_refs(raw["source_refs"], "source_refs")
        if (
            classification == "AUDITED_EXTERNAL_RESULT"
            and not proof_refs
            and not certificate_refs
        ):
            raise ValueError(
                "audited external result requires a proof or certificate reference"
            )
        audit = AuditMetadata.from_dict(raw["audit"], "audit")
        required_audit_level = {
            "RESULT": "RESULT",
            "THEME": "THEME_INTEGRATION",
            "FINAL": "GLOBAL",
        }[maturity_level]
        if (
            classification == "AUDITED_EXTERNAL_RESULT"
            and audit.audit_level != required_audit_level
        ):
            raise ValueError(
                f"{maturity_level} maturity requires {required_audit_level} audit"
            )
        provenance = _provenance(raw["provenance"], "provenance")
        supersedes = _string_list(raw["supersedes"], "supersedes")
        relative = path.resolve().relative_to(project_root.resolve()).as_posix()
        normalized = {
            **raw,
            "exact_statement": exact_statement,
            "scope_ids": list(scope_ids),
            "claim_ids": list(claim_ids),
            "dependencies": list(dependencies),
            "proof_refs": [item.to_dict() for item in proof_refs],
            "certificate_refs": [item.to_dict() for item in certificate_refs],
            "source_refs": [item.to_dict() for item in source_refs],
            "audit": audit.to_dict(),
            "provenance": provenance,
            "supersedes": list(supersedes),
            "source_manifest": relative,
        }
        return cls(
            result_id=result_id,
            exact_statement=exact_statement,
            scope_ids=scope_ids,
            claim_ids=claim_ids,
            representation_id=representation_id,
            dependencies=dependencies,
            classification=classification,
            conclusion=conclusion,
            maturity_level=maturity_level,
            proof_refs=proof_refs,
            certificate_refs=certificate_refs,
            source_refs=source_refs,
            audit=audit,
            provenance=provenance,
            supersedes=supersedes,
            source_manifest=relative,
            object_sha256=stable_hash(normalized),
        )

    @property
    def statement_sha256(self) -> str:
        return stable_hash({"exact_statement": self.exact_statement})

    @property
    def audit_identity(self) -> dict[str, Any]:
        return {
            "exact_statement": self.exact_statement,
            "representation_id": self.representation_id,
            "dependencies": sorted(self.dependencies),
            "proof_hashes": sorted(item.sha256 for item in self.proof_refs),
            "certificate_hashes": sorted(item.sha256 for item in self.certificate_refs),
            "source_hashes": sorted(item.sha256 for item in self.source_refs),
            "audit_policy_version": self.audit.policy_version,
            "audit_level": self.audit.audit_level,
        }

    @property
    def audit_key(self) -> str:
        return stable_hash(self.audit_identity)

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return (*self.proof_refs, *self.certificate_refs, *self.source_refs)

    def to_object(self) -> dict[str, Any]:
        return {
            "schema_version": EXTERNAL_RESULT_SCHEMA_VERSION,
            "result_id": self.result_id,
            "exact_statement": self.exact_statement,
            "scope_ids": list(self.scope_ids),
            "claim_ids": list(self.claim_ids),
            "representation_id": self.representation_id,
            "dependencies": list(self.dependencies),
            "classification": self.classification,
            "conclusion": self.conclusion,
            "maturity_level": self.maturity_level,
            "proof_refs": [item.to_dict() for item in self.proof_refs],
            "certificate_refs": [item.to_dict() for item in self.certificate_refs],
            "source_refs": [item.to_dict() for item in self.source_refs],
            "audit": self.audit.to_dict(),
            "provenance": self.provenance,
            "supersedes": list(self.supersedes),
            "source_manifest": self.source_manifest,
        }


@dataclass(frozen=True, slots=True)
class AssetCard:
    asset_id: str
    kind: str
    title: str
    what_it_gives: str
    scope_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    representation_ids: tuple[str, ...]
    preconditions: tuple[str, ...]
    do_not_use: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    proof_refs: tuple[EvidenceRef, ...]
    code_refs: tuple[EvidenceRef, ...]
    certificate_refs: tuple[EvidenceRef, ...]
    source_refs: tuple[EvidenceRef, ...]
    audit: AuditMetadata
    audit_status: str
    known_failure_modes: tuple[str, ...]
    dependencies: tuple[str, ...]
    method_id: str | None
    do_not_repeat: tuple[str, ...]
    reopen_if: tuple[str, ...]
    representation_edge: dict[str, Any] | None
    provenance: dict[str, Any]
    supersedes: tuple[str, ...]
    source_manifest: str
    object_sha256: str

    @classmethod
    def load(cls, project_root: Path, path: Path) -> "AssetCard":
        raw = _require_exact_keys(_load_json(path), _ASSET_KEYS, "asset card")
        if raw["schema_version"] != ASSET_CARD_SCHEMA_VERSION:
            raise ValueError(f"unsupported asset card schema at {path}")
        asset_id = validate_storage_id(str(raw["asset_id"]), "asset_id")
        kind = str(raw["kind"])
        if kind not in ASSET_KINDS:
            raise ValueError(f"invalid asset kind: {kind}")
        audit_raw = raw["audit"]
        if not isinstance(audit_raw, dict) or set(audit_raw) != {
            "status", *_AUDIT_KEYS,
        }:
            raise ValueError("asset audit fields are invalid")
        audit_status = str(audit_raw["status"])
        if audit_status not in ASSET_AUDIT_STATUSES:
            raise ValueError(f"invalid asset audit status: {audit_status}")
        audit = AuditMetadata.from_dict(
            {key: audit_raw[key] for key in _AUDIT_KEYS}, "asset.audit"
        )
        if kind == "RESEARCH_HYPOTHESIS" and audit_status != "UNPROVED":
            raise ValueError("research hypotheses must remain explicitly UNPROVED")
        if audit_status == "AUDITED" and not audit.is_direct_pass and not audit.reuse_audit_key:
            raise ValueError("audited asset requires a direct PASS or exact AuditKey reuse")
        edge_raw = raw["representation_edge"]
        edge: dict[str, Any] | None = None
        if kind == "REPRESENTATION_BRIDGE":
            edge_value = _require_exact_keys(edge_raw, _EDGE_KEYS, "representation_edge")
            edge = {
                "source_representation_id": _normalized_text(
                    edge_value["source_representation_id"],
                    "representation_edge.source_representation_id",
                ),
                "target_representation_id": _normalized_text(
                    edge_value["target_representation_id"],
                    "representation_edge.target_representation_id",
                ),
                "conditions": list(_string_list(
                    edge_value["conditions"], "representation_edge.conditions",
                    normalize_text=True,
                )),
                "localization": _normalized_text(
                    edge_value["localization"], "representation_edge.localization"
                ),
                "saturation": _normalized_text(
                    edge_value["saturation"], "representation_edge.saturation"
                ),
                "content": _normalized_text(
                    edge_value["content"], "representation_edge.content"
                ),
                "exceptional_factors": list(_string_list(
                    edge_value["exceptional_factors"],
                    "representation_edge.exceptional_factors",
                    normalize_text=True,
                )),
            }
            if edge["source_representation_id"] == edge["target_representation_id"]:
                raise ValueError("representation bridge endpoints must differ")
        elif edge_raw is not None:
            raise ValueError("only representation bridge assets may declare an edge")
        method_id = _optional_text(raw["method_id"], "method_id")
        do_not_repeat = _string_list(
            raw["do_not_repeat"], "do_not_repeat", normalize_text=True
        )
        reopen_if = _string_list(raw["reopen_if"], "reopen_if", normalize_text=True)
        if kind in {"NEGATIVE_RESULT", "KILL_GATE"}:
            if method_id is None or not do_not_repeat:
                raise ValueError(
                    "negative result and kill-gate assets require method_id and DO_NOT_REPEAT scope"
                )
        relative = path.resolve().relative_to(project_root.resolve()).as_posix()
        normalized = dict(raw)
        normalized["audit"] = {"status": audit_status, **audit.to_dict()}
        normalized["representation_edge"] = edge
        normalized["source_manifest"] = relative
        return cls(
            asset_id=asset_id,
            kind=kind,
            title=_normalized_text(raw["title"], "title"),
            what_it_gives=_normalized_text(raw["what_it_gives"], "what_it_gives"),
            scope_ids=_string_list(raw["scope_ids"], "scope_ids"),
            claim_ids=_string_list(raw["claim_ids"], "claim_ids"),
            representation_ids=_string_list(
                raw["representation_ids"], "representation_ids"
            ),
            preconditions=_string_list(
                raw["preconditions"], "preconditions", normalize_text=True
            ),
            do_not_use=_string_list(
                raw["do_not_use"], "do_not_use", normalize_text=True
            ),
            inputs=_string_list(raw["inputs"], "inputs", normalize_text=True),
            outputs=_string_list(raw["outputs"], "outputs", normalize_text=True),
            proof_refs=_evidence_refs(raw["proof_refs"], "proof_refs"),
            code_refs=_evidence_refs(raw["code_refs"], "code_refs"),
            certificate_refs=_evidence_refs(
                raw["certificate_refs"], "certificate_refs"
            ),
            source_refs=_evidence_refs(raw["source_refs"], "source_refs"),
            audit=audit,
            audit_status=audit_status,
            known_failure_modes=_string_list(
                raw["known_failure_modes"], "known_failure_modes", normalize_text=True
            ),
            dependencies=_string_list(raw["dependencies"], "dependencies"),
            method_id=method_id,
            do_not_repeat=do_not_repeat,
            reopen_if=reopen_if,
            representation_edge=edge,
            provenance=_provenance(raw["provenance"], "provenance"),
            supersedes=_string_list(raw["supersedes"], "supersedes"),
            source_manifest=relative,
            object_sha256=stable_hash(normalized),
        )

    @property
    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        return (
            *self.proof_refs,
            *self.code_refs,
            *self.certificate_refs,
            *self.source_refs,
        )

    @property
    def audit_identity(self) -> dict[str, Any]:
        return {
            "exact_statement": self.what_it_gives,
            "representation_id": sorted(self.representation_ids),
            "dependencies": sorted(self.dependencies),
            "proof_hashes": sorted(item.sha256 for item in self.proof_refs),
            "certificate_hashes": sorted(
                item.sha256 for item in (*self.code_refs, *self.certificate_refs)
            ),
            "source_hashes": sorted(item.sha256 for item in self.source_refs),
            "audit_policy_version": self.audit.policy_version,
            "audit_level": self.audit.audit_level,
        }

    @property
    def audit_key(self) -> str:
        return stable_hash(self.audit_identity)

    def to_object(self) -> dict[str, Any]:
        return {
            "schema_version": ASSET_CARD_SCHEMA_VERSION,
            "asset_id": self.asset_id,
            "kind": self.kind,
            "title": self.title,
            "what_it_gives": self.what_it_gives,
            "scope_ids": list(self.scope_ids),
            "claim_ids": list(self.claim_ids),
            "representation_ids": list(self.representation_ids),
            "preconditions": list(self.preconditions),
            "do_not_use": list(self.do_not_use),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "proof_refs": [item.to_dict() for item in self.proof_refs],
            "code_refs": [item.to_dict() for item in self.code_refs],
            "certificate_refs": [item.to_dict() for item in self.certificate_refs],
            "source_refs": [item.to_dict() for item in self.source_refs],
            "audit": {"status": self.audit_status, **self.audit.to_dict()},
            "known_failure_modes": list(self.known_failure_modes),
            "dependencies": list(self.dependencies),
            "method_id": self.method_id,
            "do_not_repeat": list(self.do_not_repeat),
            "reopen_if": list(self.reopen_if),
            "representation_edge": self.representation_edge,
            "provenance": self.provenance,
            "supersedes": list(self.supersedes),
            "source_manifest": self.source_manifest,
        }

    def summary(self, *, dependency_closed: bool, evidence_errors: list[str]) -> dict[str, Any]:
        reusable = bool(
            self.audit_status == "AUDITED"
            and dependency_closed
            and not evidence_errors
            and (self.audit.is_direct_pass or self.audit.reuse_audit_key)
        )
        return {
            "asset_id": self.asset_id,
            "kind": self.kind,
            "title": self.title,
            "what_it_gives": self.what_it_gives,
            "scope_ids": list(self.scope_ids),
            "claim_ids": list(self.claim_ids),
            "representation_ids": list(self.representation_ids),
            "preconditions": list(self.preconditions),
            "do_not_use": list(self.do_not_use),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "main_paths": [
                item.path for item in (
                    *self.proof_refs, *self.code_refs, *self.certificate_refs
                )
            ],
            "main_refs": [
                item.to_dict() for item in (
                    *self.proof_refs, *self.code_refs, *self.certificate_refs
                )
            ],
            "audit_status": self.audit_status,
            "audit_key": self.audit_key,
            "audit_identity": self.audit_identity,
            "audit_reused": bool(
                self.audit.reuse_audit_key == self.audit_key and reusable
            ),
            "reusable": reusable,
            "known_failure_modes": list(self.known_failure_modes),
            "dependencies": list(self.dependencies),
            "method_id": self.method_id,
            "do_not_repeat": list(self.do_not_repeat),
            "reopen_if": list(self.reopen_if),
            "representation_edge": self.representation_edge,
            "provenance": self.provenance,
            "source_manifest": self.source_manifest,
            "object_sha256": self.object_sha256,
            "evidence_errors": evidence_errors,
            "supersedes": list(self.supersedes),
        }


@dataclass(frozen=True, slots=True)
class CampaignTheme:
    theme_id: str
    title: str
    objective: str
    include_claim_ids: tuple[str, ...]
    include_scope_ids: tuple[str, ...]
    exclude_claim_ids: tuple[str, ...]
    exclude_scope_ids: tuple[str, ...]
    allowed_method_ids: tuple[str, ...]
    forbidden_method_ids: tuple[str, ...]
    dependency_boundary: tuple[str, ...]
    combination_scope: str
    obligations: tuple[dict[str, Any], ...]
    completion_policy: dict[str, Any] | None = None
    source_path: str | None = None

    @classmethod
    def from_dict(cls, raw: Any, *, source_path: str | None = None) -> "CampaignTheme":
        if not isinstance(raw, dict):
            raise ValueError("campaign theme must be an object")
        schema_version = raw.get("schema_version")
        if schema_version == LEGACY_THEME_SCHEMA_VERSION:
            value = _require_exact_keys(raw, _THEME_V1_KEYS, "campaign theme")
            completion_policy = None
        elif schema_version == THEME_SCHEMA_VERSION:
            value = _require_exact_keys(raw, _THEME_KEYS, "campaign theme")
            policy_raw = value["completion_policy"]
            policy_keys = set(policy_raw) if isinstance(policy_raw, dict) else set()
            if (
                not isinstance(policy_raw, dict)
                or not _COMPLETION_POLICY_KEYS.issubset(policy_keys)
                or policy_keys - (
                    _COMPLETION_POLICY_KEYS | _COMPLETION_POLICY_OPTIONAL_KEYS
                )
            ):
                raise ValueError(
                    "campaign theme completion_policy keys must contain exactly "
                    f"{sorted(_COMPLETION_POLICY_KEYS)} plus optional "
                    "terminal_research_outcomes"
                )
            policy = dict(policy_raw)
            max_candidates = policy["max_accepted_candidates"]
            max_audits = policy["max_valid_audit_attempts_per_candidate"]
            if (
                not isinstance(max_candidates, int) or isinstance(max_candidates, bool)
                or max_candidates < 1
            ):
                raise ValueError("max_accepted_candidates must be a positive integer")
            if (
                not isinstance(max_audits, int) or isinstance(max_audits, bool)
                or max_audits < 1
            ):
                raise ValueError(
                    "max_valid_audit_attempts_per_candidate must be a positive integer"
                )
            if policy["post_candidate_mode"] != "AUDIT_ONLY":
                raise ValueError("post_candidate_mode must be AUDIT_ONLY")
            terminal_verdicts = _string_list(
                policy["terminal_audit_verdicts"],
                "terminal_audit_verdicts",
            )
            if not terminal_verdicts:
                raise ValueError("terminal_audit_verdicts must not be empty")
            unknown_verdicts = set(terminal_verdicts) - _TERMINAL_AUDIT_VERDICTS
            if unknown_verdicts:
                raise ValueError(
                    f"unsupported terminal audit verdicts: {sorted(unknown_verdicts)}"
                )
            terminal_outcomes = _string_list(
                policy.get("terminal_research_outcomes", []),
                "terminal_research_outcomes",
            )
            unknown_outcomes = set(terminal_outcomes) - _TERMINAL_RESEARCH_OUTCOMES
            if unknown_outcomes:
                raise ValueError(
                    "unsupported terminal research outcomes: "
                    f"{sorted(unknown_outcomes)}"
                )
            completion_policy = {
                "max_accepted_candidates": max_candidates,
                "post_candidate_mode": "AUDIT_ONLY",
                "max_valid_audit_attempts_per_candidate": max_audits,
                "terminal_audit_verdicts": list(terminal_verdicts),
            }
            if "terminal_research_outcomes" in policy:
                completion_policy["terminal_research_outcomes"] = list(
                    terminal_outcomes
                )
        else:
            raise ValueError("unsupported campaign theme schema")
        include_claims = _string_list(value["include_claim_ids"], "include_claim_ids")
        include_scopes = _string_list(value["include_scope_ids"], "include_scope_ids")
        exclude_claims = _string_list(value["exclude_claim_ids"], "exclude_claim_ids")
        exclude_scopes = _string_list(value["exclude_scope_ids"], "exclude_scope_ids")
        allowed = _string_list(value["allowed_method_ids"], "allowed_method_ids")
        forbidden = _string_list(value["forbidden_method_ids"], "forbidden_method_ids")
        if set(include_claims) & set(exclude_claims):
            raise ValueError("theme claim include/exclude sets overlap")
        if set(include_scopes) & set(exclude_scopes):
            raise ValueError("theme scope include/exclude sets overlap")
        if set(allowed) & set(forbidden):
            raise ValueError("theme allowed/forbidden method sets overlap")
        if not include_claims and not include_scopes:
            raise ValueError("campaign theme must include at least one claim or scope")
        obligations_raw = value["obligations"]
        if not isinstance(obligations_raw, list):
            raise ValueError("theme obligations must be an array")
        obligations: list[dict[str, Any]] = []
        for index, item in enumerate(obligations_raw):
            obligation = _require_exact_keys(
                item, _OBLIGATION_KEYS, f"obligations[{index}]"
            )
            scope_id = _normalized_text(
                obligation["scope_id"], f"obligations[{index}].scope_id"
            )
            claim_id = _optional_text(
                obligation["claim_id"], f"obligations[{index}].claim_id"
            )
            representation_id = _optional_text(
                obligation["representation_id"],
                f"obligations[{index}].representation_id",
            )
            obligation_allowed = _string_list(
                obligation["allowed_method_ids"],
                f"obligations[{index}].allowed_method_ids",
            )
            obligation_forbidden = _string_list(
                obligation["forbidden_method_ids"],
                f"obligations[{index}].forbidden_method_ids",
            )
            if set(obligation_allowed) & set(obligation_forbidden):
                raise ValueError(f"obligation {scope_id} method sets overlap")
            obligations.append({
                "scope_id": scope_id,
                "claim_id": claim_id,
                "exact_objective": _normalized_text(
                    obligation["exact_objective"],
                    f"obligations[{index}].exact_objective",
                ),
                "representation_id": representation_id,
                "dependencies": list(_string_list(
                    obligation["dependencies"],
                    f"obligations[{index}].dependencies",
                )),
                "allowed_method_ids": list(obligation_allowed),
                "forbidden_method_ids": list(obligation_forbidden),
            })
        scope_ids = [item["scope_id"] for item in obligations]
        if len(scope_ids) != len(set(scope_ids)):
            raise ValueError("theme obligation scope ids must be unique")
        if set(scope_ids) - set(include_scopes):
            raise ValueError("every theme obligation must be named in include_scope_ids")
        return cls(
            theme_id=validate_storage_id(str(value["theme_id"]), "theme_id"),
            title=_normalized_text(value["title"], "title"),
            objective=_normalized_text(value["objective"], "objective"),
            include_claim_ids=include_claims,
            include_scope_ids=include_scopes,
            exclude_claim_ids=exclude_claims,
            exclude_scope_ids=exclude_scopes,
            allowed_method_ids=allowed,
            forbidden_method_ids=forbidden,
            dependency_boundary=_string_list(
                value["dependency_boundary"], "dependency_boundary"
            ),
            combination_scope=_normalized_text(
                value["combination_scope"], "combination_scope"
            ),
            obligations=tuple(obligations),
            completion_policy=completion_policy,
            source_path=source_path,
        )

    @property
    def theme_sha256(self) -> str:
        return stable_hash(self.to_dict(include_source=False))

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": (
                THEME_SCHEMA_VERSION
                if self.completion_policy is not None
                else LEGACY_THEME_SCHEMA_VERSION
            ),
            "theme_id": self.theme_id,
            "title": self.title,
            "objective": self.objective,
            "include_claim_ids": list(self.include_claim_ids),
            "include_scope_ids": list(self.include_scope_ids),
            "exclude_claim_ids": list(self.exclude_claim_ids),
            "exclude_scope_ids": list(self.exclude_scope_ids),
            "allowed_method_ids": list(self.allowed_method_ids),
            "forbidden_method_ids": list(self.forbidden_method_ids),
            "dependency_boundary": list(self.dependency_boundary),
            "combination_scope": self.combination_scope,
            "obligations": [dict(item) for item in self.obligations],
        }
        if self.completion_policy is not None:
            result["completion_policy"] = {
                **self.completion_policy,
                "terminal_audit_verdicts": list(
                    self.completion_policy["terminal_audit_verdicts"]
                ),
            }
            if "terminal_research_outcomes" in self.completion_policy:
                result["completion_policy"]["terminal_research_outcomes"] = list(
                    self.completion_policy["terminal_research_outcomes"]
                )
        if include_source:
            result["source_path"] = self.source_path
            result["theme_sha256"] = self.theme_sha256
        return result

    def task_error(self, task: ResearchTask) -> str | None:
        input_scope = None
        if isinstance(task.input_closure, dict):
            raw_scope = task.input_closure.get("canonical_object_id")
            if isinstance(raw_scope, str) and raw_scope:
                input_scope = raw_scope
        if task.target_claim in self.exclude_claim_ids:
            return f"THEME_EXCLUDED_CLAIM: {task.target_claim}"
        if input_scope in self.exclude_scope_ids or task.route_family in self.exclude_scope_ids:
            return f"THEME_EXCLUDED_SCOPE: {input_scope or task.route_family}"
        included_claim = task.target_claim in self.include_claim_ids
        included_scope = bool(
            input_scope and input_scope in self.include_scope_ids
        )
        if self.include_scope_ids and not input_scope and not included_claim:
            return (
                "THEME_SCOPE_ID_REQUIRED: bind the exact scope through "
                "input_closure.canonical_object_id"
            )
        if not included_claim and not included_scope:
            return (
                f"THEME_SCOPE_VIOLATION: claim={task.target_claim}, "
                f"scope={input_scope}"
            )
        if task.route_family in self.forbidden_method_ids:
            return f"THEME_FORBIDDEN_METHOD: {task.route_family}"
        if self.allowed_method_ids and task.route_family not in self.allowed_method_ids:
            return f"THEME_METHOD_OUTSIDE_ALLOWLIST: {task.route_family}"
        obligation = next(
            (
                item for item in self.obligations
                if input_scope is not None and item["scope_id"] == input_scope
            ),
            None,
        )
        if obligation is not None:
            obligation_forbidden = set(obligation["forbidden_method_ids"])
            obligation_allowed = set(obligation["allowed_method_ids"])
            if task.route_family in obligation_forbidden:
                return f"THEME_OBLIGATION_FORBIDDEN_METHOD: {task.route_family}"
            if obligation_allowed and task.route_family not in obligation_allowed:
                return (
                    f"THEME_OBLIGATION_METHOD_OUTSIDE_ALLOWLIST: {task.route_family}"
                )
        boundary = set(self.dependency_boundary) | set(self.include_claim_ids)
        outside = set(task.dependencies) - boundary
        if outside:
            return f"THEME_DEPENDENCY_BOUNDARY: {sorted(outside)}"
        return None


class ResearchMemoryStore:
    """Rebuildable routing memory that never mutates mathematical authority."""

    def __init__(self, project_root: Path, runtime_root: Path):
        self.project_root = project_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.input_root = self.runtime_root / "research_memory"
        self.external_results_root = self.input_root / "external_results"
        self.assets_root = self.input_root / "assets"
        self.themes_root = self.input_root / "themes"
        self.coordination_root = self.runtime_root / "coordination"
        self.frontier_root = self.coordination_root / "frontier"
        self.objects_root = self.coordination_root / "objects"
        self.audit_receipts_root = self.coordination_root / "audit_receipts"
        self.current_path = self.frontier_root / "CURRENT.json"
        self.history_path = self.frontier_root / "HISTORY.jsonl"
        self.registry_path = self.coordination_root / "ASSET_REGISTRY.json"
        self.representation_graph_path = (
            self.coordination_root / "REPRESENTATION_GRAPH.json"
        )
        self.method_ledger_path = self.coordination_root / "METHOD_LEDGER.json"
        self.audit_index_path = self.coordination_root / "AUDIT_INDEX.json"
        self._asset_cards: dict[str, AssetCard] = {}
        self._last_state: dict[str, Any] | None = None

    def ensure(self) -> None:
        for path in (
            self.external_results_root,
            self.assets_root,
            self.themes_root,
            self.frontier_root,
            self.objects_root,
            self.audit_receipts_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def load_or_pin_theme(
        self,
        campaign_root: Path,
        requested_path: Path | None,
    ) -> CampaignTheme | None:
        pin_path = campaign_root / "THEME.json"
        if pin_path.is_file():
            pinned = _load_json(pin_path)
            if set(pinned) != {"schema_version", "source_path", "theme_sha256", "theme"}:
                raise ValueError("pinned campaign theme fields are invalid")
            theme = CampaignTheme.from_dict(
                pinned["theme"], source_path=pinned["source_path"]
            )
            if theme.theme_sha256 != pinned["theme_sha256"]:
                raise ValueError("pinned campaign theme digest is invalid")
            if requested_path is not None:
                requested = self._load_theme_path(requested_path)
                if requested.theme_sha256 != theme.theme_sha256:
                    raise ValueError("requested theme differs from the pinned campaign theme")
            return theme
        if requested_path is None:
            return None
        if (campaign_root / "CAMPAIGN.json").is_file():
            raise ValueError(
                "a Campaign Theme cannot be added after the campaign has started"
            )
        theme = self._load_theme_path(requested_path)
        atomic_write_json(pin_path, {
            "schema_version": 1,
            "source_path": theme.source_path,
            "theme_sha256": theme.theme_sha256,
            "theme": theme.to_dict(include_source=False),
        })
        return theme

    def _load_theme_path(self, requested_path: Path) -> CampaignTheme:
        path = requested_path
        if not path.is_absolute():
            direct = (self.project_root / path).resolve()
            named = (self.themes_root / path).resolve()
            if direct.is_file():
                path = direct
            elif named.is_file():
                path = named
            elif named.with_suffix(".json").is_file():
                path = named.with_suffix(".json")
            else:
                raise ValueError(f"campaign theme does not exist: {requested_path}")
        else:
            path = path.resolve()
        if not path.is_relative_to(self.project_root):
            raise ValueError("campaign theme must be inside the project root")
        relative = path.relative_to(self.project_root).as_posix()
        return CampaignTheme.from_dict(_load_json(path), source_path=relative)

    def load_theme(self, requested_path: Path) -> CampaignTheme:
        return self._load_theme_path(requested_path)

    def _inventory(self, claim_graph_path: Path, trusted_state_path: Path) -> list[dict[str, Any]]:
        roots = [
            self.project_root / "claims" / "CLAIMS.md",
            self.project_root / "state" / "PROGRESS.md",
            self.project_root / "proofs",
            self.project_root / "audit" / "mathematical",
            self.project_root / "certificates",
            claim_graph_path,
            trusted_state_path,
        ]
        paths: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved.is_file():
                paths.add(resolved)
            elif resolved.is_dir():
                paths.update(item.resolve() for item in resolved.rglob("*") if item.is_file())
        return [
            {
                "path": path.relative_to(self.project_root).as_posix(),
                "sha256": file_digest(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(paths, key=lambda item: item.as_posix())
            if path.is_relative_to(self.project_root)
        ]

    def _write_object(self, kind: str, object_id: str, payload: dict[str, Any]) -> str:
        digest = stable_hash(payload)
        path = self.objects_root / f"{digest}.json"
        wrapper = {
            "schema_version": 1,
            "object_sha256": digest,
            "kind": kind,
            "stable_id": object_id,
            "object": payload,
        }
        if path.is_file():
            if _load_json(path) != wrapper:
                raise ValueError(f"content-addressed object collision: {digest}")
        else:
            atomic_write_json(path, wrapper)
        return digest

    def _validate_refs(self, refs: Iterable[EvidenceRef]) -> list[str]:
        return [error for ref in refs if (error := ref.validate(self.project_root))]

    def _receipt_path(self, audit_key: str) -> Path:
        return self.audit_receipts_root / f"{audit_key}.json"

    def _valid_receipts(self) -> dict[str, dict[str, Any]]:
        receipts: dict[str, dict[str, Any]] = {}
        for path in sorted(self.audit_receipts_root.glob("*.json")):
            raw = _load_json(path)
            expected = {
                "schema_version", "audit_key", "verdict", "independent",
                "auditor", "policy_version", "report_refs", "source_object_id",
                "audit_level", "source_object_sha256", "routing_only", "receipt_sha256",
            }
            if set(raw) != expected:
                raise ValueError(f"routing audit receipt fields are invalid: {path}")
            payload = {key: raw[key] for key in expected - {"receipt_sha256"}}
            if raw["receipt_sha256"] != stable_hash(payload):
                raise ValueError(f"routing audit receipt digest is invalid: {path}")
            refs = _evidence_refs(raw["report_refs"], "routing receipt report_refs")
            if self._validate_refs(refs):
                continue
            audit_key = str(raw["audit_key"])
            source_object_sha256 = str(raw["source_object_sha256"])
            if (
                path.stem != audit_key
                or len(audit_key) != 64
                or any(char not in "0123456789abcdef" for char in audit_key)
                or raw["schema_version"] != ROUTING_AUDIT_RECEIPT_SCHEMA_VERSION
                or raw["verdict"] != "PASS"
                or raw["independent"] is not True
                or not isinstance(raw["auditor"], str)
                or not raw["auditor"].strip()
                or raw["audit_level"] not in {
                    "RESULT", "THEME_INTEGRATION", "GLOBAL",
                }
                or not isinstance(raw["policy_version"], str)
                or not raw["policy_version"].strip()
                or raw["routing_only"] is not True
                or len(source_object_sha256) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in source_object_sha256
                )
            ):
                raise ValueError(f"routing audit receipt identity is invalid: {path}")
            source_path = self.objects_root / f"{source_object_sha256}.json"
            if not source_path.is_file():
                raise ValueError(
                    f"routing audit receipt source object is missing: {path}"
                )
            source = _load_json(source_path)
            if (
                set(source) != {
                    "schema_version", "object_sha256", "kind", "stable_id", "object",
                }
                or source["schema_version"] != 1
                or source["object_sha256"] != source_object_sha256
                or source["stable_id"] != raw["source_object_id"]
                or stable_hash(source["object"]) != source_object_sha256
            ):
                raise ValueError(
                    f"routing audit receipt source object is invalid: {path}"
                )
            receipts[audit_key] = raw
        return receipts

    def _record_direct_pass(
        self,
        *,
        audit_key: str,
        audit: AuditMetadata,
        source_object_id: str,
        source_object_sha256: str,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": ROUTING_AUDIT_RECEIPT_SCHEMA_VERSION,
            "audit_key": audit_key,
            "verdict": "PASS",
            "independent": True,
            "auditor": audit.auditor,
            "audit_level": audit.audit_level,
            "policy_version": audit.policy_version,
            "report_refs": [item.to_dict() for item in audit.report_refs],
            "source_object_id": source_object_id,
            "source_object_sha256": source_object_sha256,
            "routing_only": True,
        }
        receipt = {**payload, "receipt_sha256": stable_hash(payload)}
        path = self._receipt_path(audit_key)
        if path.is_file():
            existing = _load_json(path)
            if existing != receipt:
                raise ValueError(
                    f"conflicting existing PASS receipt for AuditKey {audit_key}"
                )
            return existing
        atomic_write_json(path, receipt)
        return receipt

    @staticmethod
    def _audit_reused(
        audit: AuditMetadata,
        audit_key: str,
        receipts: dict[str, dict[str, Any]],
    ) -> bool:
        return bool(
            audit.reuse_audit_key == audit_key
            and audit_key in receipts
            and receipts[audit_key].get("routing_only") is True
        )

    def _load_results(
        self,
        receipts: dict[str, dict[str, Any]],
    ) -> tuple[list[ExternalResult], dict[str, list[str]], dict[str, bool]]:
        results: list[ExternalResult] = []
        errors: dict[str, list[str]] = {}
        audit_passed: dict[str, bool] = {}
        by_id: dict[str, str] = {}
        for path in sorted(self.external_results_root.rglob("*.json")):
            result = ExternalResult.load(self.project_root, path)
            prior = by_id.get(result.result_id)
            if prior is not None and prior != result.object_sha256:
                raise ValueError(
                    f"external result id has conflicting content: {result.result_id}"
                )
            by_id[result.result_id] = result.object_sha256
            result_errors = self._validate_refs(
                (*result.evidence_refs, *result.audit.report_refs)
            )
            errors[result.result_id] = result_errors
            stored_sha256 = self._write_object(
                "EXTERNAL_RESULT", result.result_id, result.to_object()
            )
            if stored_sha256 != result.object_sha256:
                raise ValueError(
                    f"external result object digest mismatch: {result.result_id}"
                )
            direct = result.audit.is_direct_pass and not result_errors
            if direct:
                receipts[result.audit_key] = self._record_direct_pass(
                    audit_key=result.audit_key,
                    audit=result.audit,
                    source_object_id=result.result_id,
                    source_object_sha256=result.object_sha256,
                )
            reused = self._audit_reused(result.audit, result.audit_key, receipts)
            audit_passed[result.result_id] = direct or reused
            results.append(result)
        return results, errors, audit_passed

    def _load_assets(
        self,
        receipts: dict[str, dict[str, Any]],
    ) -> tuple[list[AssetCard], dict[str, list[str]], dict[str, bool]]:
        assets: list[AssetCard] = []
        errors: dict[str, list[str]] = {}
        audit_passed: dict[str, bool] = {}
        by_id: dict[str, str] = {}
        for path in sorted(self.assets_root.rglob("*.json")):
            asset = AssetCard.load(self.project_root, path)
            prior = by_id.get(asset.asset_id)
            if prior is not None and prior != asset.object_sha256:
                raise ValueError(f"asset id has conflicting content: {asset.asset_id}")
            by_id[asset.asset_id] = asset.object_sha256
            asset_errors = self._validate_refs(
                (*asset.evidence_refs, *asset.audit.report_refs)
            )
            errors[asset.asset_id] = asset_errors
            stored_sha256 = self._write_object(
                "RESEARCH_ASSET", asset.asset_id, asset.to_object()
            )
            if stored_sha256 != asset.object_sha256:
                raise ValueError(f"asset object digest mismatch: {asset.asset_id}")
            direct = asset.audit.is_direct_pass and not asset_errors
            if asset.audit_status == "AUDITED" and direct:
                receipts[asset.audit_key] = self._record_direct_pass(
                    audit_key=asset.audit_key,
                    audit=asset.audit,
                    source_object_id=asset.asset_id,
                    source_object_sha256=asset.object_sha256,
                )
            reused = self._audit_reused(asset.audit, asset.audit_key, receipts)
            audit_passed[asset.asset_id] = bool(
                asset.audit_status == "AUDITED" and (direct or reused)
            )
            assets.append(asset)
        return assets, errors, audit_passed

    @staticmethod
    def _resolve_dependency_closure(
        graph_closed: set[str],
        result_candidates: dict[str, tuple[str, ...]],
        asset_candidates: dict[str, tuple[str, ...]],
    ) -> set[str]:
        closed = set(graph_closed)
        pending = {**result_candidates, **asset_candidates}
        changed = True
        while changed:
            changed = False
            for object_id, dependencies in pending.items():
                if object_id in closed:
                    continue
                if set(dependencies) <= closed:
                    closed.add(object_id)
                    changed = True
        return closed

    def reconcile(
        self,
        *,
        graph: Any,
        claim_graph_path: Path,
        trusted_state_path: Path,
        final_claim_id: str | None,
        theme: CampaignTheme | None,
        phase: str,
        campaign_id: str,
        epoch_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if phase not in {"CAMPAIGN_START", "CAMPAIGN_END", "MANUAL"}:
            raise ValueError(f"invalid frontier reconciliation phase: {phase}")
        self.ensure()
        prior = _load_json(self.current_path) if self.current_path.is_file() else None
        receipts = self._valid_receipts()
        results, result_errors, result_audited = self._load_results(receipts)
        assets, asset_errors, asset_audited = self._load_assets(receipts)

        superseded_results = {
            item for result in results for item in result.supersedes
        }
        superseded_assets = {item for asset in assets for item in asset.supersedes}
        active_results = [
            item for item in results if item.result_id not in superseded_results
        ]
        active_assets = [
            item for item in assets if item.asset_id not in superseded_assets
        ]

        graph_closed = {
            claim_id
            for claim_id, claim in graph.claims.items()
            if claim.trust_status in _TRUSTED_STATUSES
            and claim.research_status in (
                graph.semantics.terminal_positive | graph.semantics.terminal_negative
            )
        }
        result_candidates = {
            result.result_id: result.dependencies
            for result in active_results
            if result.classification == "AUDITED_EXTERNAL_RESULT"
            and result_audited[result.result_id]
            and not result_errors[result.result_id]
        }
        asset_candidates = {
            asset.asset_id: asset.dependencies
            for asset in active_assets
            if asset_audited[asset.asset_id]
            and not asset_errors[asset.asset_id]
        }
        closed = self._resolve_dependency_closure(
            graph_closed, result_candidates, asset_candidates
        )
        closed_scopes = set(graph_closed)
        for result in active_results:
            if result.result_id in closed:
                closed_scopes.update(result.scope_ids)
                closed_scopes.update(result.claim_ids if result.maturity_level != "RESULT" else ())

        result_entries: list[dict[str, Any]] = []
        for result in results:
            errors = result_errors[result.result_id]
            superseded = result.result_id in superseded_results
            dependency_missing = sorted(set(result.dependencies) - closed)
            audit_ok = result_audited[result.result_id]
            if superseded:
                status = "DO_NOT_ROUTE"
                reason = "SUPERSEDED_RESULT"
            elif errors:
                status = "BLOCKED"
                reason = "EVIDENCE_IDENTITY_MISMATCH"
            elif result.classification == "CONFLICTING_RESULT":
                status = "BLOCKED"
                reason = "DECLARED_CONFLICT"
            elif result.classification == "UNAUDITED_EXTERNAL_RESULT":
                status = "BLOCKED"
                reason = "RESULT_AUDIT_REQUIRED"
            elif result.classification == "COMPUTATION_ONLY":
                status = "ROUTE"
                reason = "COMPUTATION_IS_EVIDENCE_NOT_PROOF"
            elif not audit_ok:
                status = "BLOCKED"
                reason = "AUDIT_PASS_RECEIPT_REQUIRED"
            elif dependency_missing:
                status = "WAIT_DEPENDENCY"
                reason = "AUDITED_RESULT_DEPENDENCY_NOT_CLOSED"
            elif result.conclusion == "KILL_GATE":
                status = "KILL_GATED"
                reason = "AUDITED_EXACT_METHOD_FAILURE"
            else:
                status = "DO_NOT_ROUTE"
                reason = "AUDITED_EXTERNAL_RESULT_ROUTING_CLOSED"
            result_entries.append({
                "entry_id": f"external:{result.result_id}",
                "object_kind": "EXTERNAL_RESULT",
                "result_id": result.result_id,
                "exact_statement": result.exact_statement,
                "statement_sha256": result.statement_sha256,
                "scope_ids": list(result.scope_ids),
                "claim_ids": list(result.claim_ids),
                "representation_id": result.representation_id,
                "dependencies": list(result.dependencies),
                "missing_dependencies": dependency_missing,
                "classification": result.classification,
                "conclusion": result.conclusion,
                "maturity_level": result.maturity_level,
                "route_status": status,
                "route_reason": reason,
                "authority_status": (
                    "PENDING_AUTONOMOUS_BINDING"
                    if status in {"DO_NOT_ROUTE", "KILL_GATED"} and not superseded
                    else "NO_AUTHORITY_CHANGE"
                ),
                "audit_key": result.audit_key,
                "audit_identity": result.audit_identity,
                "audit_reused": bool(
                    result.audit.reuse_audit_key == result.audit_key and audit_ok
                ),
                "evidence_errors": errors,
                "superseded": superseded,
                "supersedes": list(result.supersedes),
                "source_manifest": result.source_manifest,
                "object_sha256": result.object_sha256,
                "provenance": result.provenance,
            })

        conflicts: dict[str, set[str]] = {}
        by_scope: dict[str, list[dict[str, Any]]] = {}
        for entry in result_entries:
            if (
                entry["route_status"] not in {"DO_NOT_ROUTE", "KILL_GATED"}
                or entry.get("superseded")
            ):
                continue
            for scope_id in entry["scope_ids"]:
                by_scope.setdefault(scope_id, []).append(entry)
        for scope_id, entries in by_scope.items():
            conclusions = {
                (entry["statement_sha256"], entry["conclusion"])
                for entry in entries
            }
            if len(conclusions) <= 1:
                continue
            ids = {str(entry["result_id"]) for entry in entries}
            for entry in entries:
                conflicts.setdefault(str(entry["result_id"]), set()).update(
                    ids - {str(entry["result_id"])}
                )
        if conflicts:
            for entry in result_entries:
                result_id = str(entry["result_id"])
                if result_id not in conflicts:
                    continue
                entry["route_status"] = "BLOCKED"
                entry["route_reason"] = "CONFLICTING_AUDITED_RESULTS"
                entry["authority_status"] = "NO_AUTHORITY_CHANGE"
                entry["conflicts_with"] = sorted(conflicts[result_id])
                for scope_id in entry["scope_ids"]:
                    closed_scopes.discard(scope_id)

        graph_entries: list[dict[str, Any]] = []
        for claim_id, claim in sorted(graph.claims.items()):
            trusted = claim.trust_status in _TRUSTED_STATUSES
            terminal = claim.research_status in (
                graph.semantics.terminal_positive | graph.semantics.terminal_negative
            )
            if terminal and trusted:
                status = "DO_NOT_ROUTE"
                reason = "CLAIMGRAPH_TRUSTED_TERMINAL"
            elif terminal:
                status = "BLOCKED"
                reason = "CLAIMGRAPH_TERMINAL_AUTHORITY_NOT_TRUSTED"
            else:
                status = "ROUTE"
                reason = "CLAIMGRAPH_OPEN"
            graph_entries.append({
                "entry_id": f"claim:{claim_id}",
                "object_kind": "CLAIMGRAPH_CLAIM",
                "claim_id": claim_id,
                "scope_ids": [claim_id],
                "claim_ids": [claim_id],
                "exact_statement": claim.statement,
                "representation_id": None,
                "dependencies": list(claim.dependencies),
                "maturity_level": "FINAL" if claim_id == final_claim_id else "THEME",
                "route_status": status,
                "route_reason": reason,
                "authority_status": claim.trust_status,
                "claim_status": claim.research_status,
                "evidence_paths": list(claim.evidence_paths),
            })

        asset_summaries: list[dict[str, Any]] = []
        for asset in assets:
            summary = asset.summary(
                dependency_closed=asset.asset_id in closed,
                evidence_errors=asset_errors[asset.asset_id],
            )
            if asset.asset_id in superseded_assets:
                summary["reusable"] = False
                summary["superseded"] = True
            else:
                summary["superseded"] = False
            asset_summaries.append(summary)
        self._asset_cards = {item.asset_id: item for item in active_assets}

        reusable_assets = [item for item in asset_summaries if item["reusable"]]
        bridge_edges = [
            {
                "asset_id": item["asset_id"],
                **item["representation_edge"],
                "audit_key": item["audit_key"],
                "object_sha256": item["object_sha256"],
            }
            for item in reusable_assets
            if item["kind"] == "REPRESENTATION_BRIDGE"
            and item["representation_edge"] is not None
        ]
        method_ledger = [
            {
                "asset_id": item["asset_id"],
                "kind": item["kind"],
                "method_id": item["method_id"],
                "scope_ids": item["scope_ids"],
                "claim_ids": item["claim_ids"],
                "exact_failed_statement": item["what_it_gives"],
                "do_not_repeat": item["do_not_repeat"],
                "reopen_if": item["reopen_if"],
                "known_failure_modes": item["known_failure_modes"],
                "audit_key": item["audit_key"],
            }
            for item in reusable_assets
            if item["kind"] in {"NEGATIVE_RESULT", "KILL_GATE"}
        ]

        theme_entries = self._theme_entries(
            theme=theme,
            closed=closed | closed_scopes,
            result_entries=result_entries,
            method_ledger=method_ledger,
        )
        frontier_entries = [*graph_entries, *result_entries, *theme_entries]
        state_body = {
            "schema_version": FRONTIER_SCHEMA_VERSION,
            "authority": "NONCANONICAL_ROUTING_COORDINATION",
            "routing_authority_separated": True,
            "claim_graph_authority_unchanged": True,
            "campaign_theme": theme.to_dict() if theme is not None else None,
            "frontier_entries": frontier_entries,
            "asset_registry_sha256": stable_hash(asset_summaries),
            "representation_graph_sha256": stable_hash(bridge_edges),
            "method_ledger_sha256": stable_hash(method_ledger),
            "audit_index_sha256": stable_hash(receipts),
            "asset_ids": sorted(item["asset_id"] for item in asset_summaries),
            "reusable_asset_ids": sorted(
                item["asset_id"] for item in reusable_assets
            ),
            "superseded_result_ids": sorted(superseded_results),
            "superseded_asset_ids": sorted(superseded_assets),
            "evidence_inventory": self._inventory(
                claim_graph_path, trusted_state_path
            ),
            "source_counts": {
                "external_results": len(results),
                "assets": len(assets),
                "routing_audit_receipts": len(receipts),
                "representation_edges": len(bridge_edges),
                "kill_gates": len(method_ledger),
            },
        }
        frontier_sha256 = stable_hash(state_body)
        state = {
            **state_body,
            "frontier_sha256": frontier_sha256,
            "generated_at": utc_now(),
            "phase": phase,
            "campaign_id": campaign_id,
            "epoch_id": epoch_id,
        }

        registry_payload = {
            "schema_version": 1,
            "authority": "NONCANONICAL_REUSABLE_RESEARCH_MEMORY",
            "assets": asset_summaries,
            "registry_sha256": stable_hash(asset_summaries),
        }
        representation_payload = {
            "schema_version": 1,
            "authority": "ROUTING_COMPATIBILITY_ONLY",
            "no_bridge_means_incompatible": True,
            "edges": bridge_edges,
            "graph_sha256": stable_hash(bridge_edges),
        }
        method_payload = {
            "schema_version": 1,
            "authority": "EXACT_SCOPE_NEGATIVE_KNOWLEDGE",
            "no_scope_escalation": True,
            "routes": method_ledger,
            "ledger_sha256": stable_hash(method_ledger),
        }
        audit_payload = {
            "schema_version": 1,
            "authority": "ROUTING_AUDIT_DEDUP_ONLY",
            "canonical_promotion_requires_controller_gate": True,
            "receipts": [receipts[key] for key in sorted(receipts)],
            "index_sha256": stable_hash(receipts),
        }
        atomic_write_json(self.registry_path, registry_payload)
        atomic_write_json(self.representation_graph_path, representation_payload)
        atomic_write_json(self.method_ledger_path, method_payload)
        atomic_write_json(self.audit_index_path, audit_payload)

        previous_sha = prior.get("frontier_sha256") if prior else None
        if previous_sha != frontier_sha256:
            atomic_write_json(self.frontier_root / f"{frontier_sha256}.json", state)
            atomic_write_json(self.current_path, state)
            append_jsonl(self.history_path, {
                "schema_version": 1,
                "frontier_sha256": frontier_sha256,
                "previous_frontier_sha256": previous_sha,
                "generated_at": state["generated_at"],
                "phase": phase,
                "campaign_id": campaign_id,
                "epoch_id": epoch_id,
            })
        delta = self._delta(prior, state)
        delta_path = self.frontier_root / "deltas" / f"{epoch_id}-{phase.lower()}.json"
        atomic_write_json(delta_path, delta)
        self._last_state = state
        return state, delta

    @staticmethod
    def _theme_entries(
        *,
        theme: CampaignTheme | None,
        closed: set[str],
        result_entries: list[dict[str, Any]],
        method_ledger: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if theme is None:
            return []
        conflicts = {
            scope
            for item in result_entries
            if item["route_status"] == "BLOCKED"
            and item["route_reason"] in {
                "CONFLICTING_AUDITED_RESULTS", "DECLARED_CONFLICT",
            }
            for scope in item["scope_ids"]
        }
        unaudited = {
            scope
            for item in result_entries
            if item["route_status"] == "BLOCKED"
            and item["route_reason"] in {
                "RESULT_AUDIT_REQUIRED", "AUDIT_PASS_RECEIPT_REQUIRED",
                "EVIDENCE_IDENTITY_MISMATCH",
            }
            for scope in item["scope_ids"]
        }
        entries: list[dict[str, Any]] = []
        for obligation in theme.obligations:
            scope_id = str(obligation["scope_id"])
            dependencies = list(obligation["dependencies"])
            missing = sorted(set(dependencies) - closed)
            allowed = set(obligation["allowed_method_ids"] or theme.allowed_method_ids)
            forbidden = set(obligation["forbidden_method_ids"]) | set(
                theme.forbidden_method_ids
            )
            gates = [
                item for item in method_ledger
                if (
                    scope_id in item["scope_ids"]
                    or obligation["claim_id"] in item["claim_ids"]
                )
                and item["method_id"] not in forbidden
            ]
            gated_methods = {str(item["method_id"]) for item in gates}
            if scope_id in theme.exclude_scope_ids:
                status, reason = "DO_NOT_ROUTE", "THEME_EXCLUDED"
            elif scope_id in conflicts:
                status, reason = "BLOCKED", "CONFLICTING_RESULT"
            elif scope_id in unaudited:
                status, reason = "BLOCKED", "RESULT_AUDIT_REQUIRED"
            elif scope_id in closed:
                status, reason = "DO_NOT_ROUTE", "AUDITED_RESULT_ALREADY_CLOSES_SCOPE"
            elif missing:
                status, reason = "WAIT_DEPENDENCY", "DEPENDENCY_NOT_AUDITED"
            elif allowed and allowed <= gated_methods:
                status, reason = "KILL_GATED", "ALL_ALLOWED_METHODS_EXACTLY_KILL_GATED"
            else:
                status, reason = "ROUTE", "OPEN_THEME_OBLIGATION"
            entries.append({
                "entry_id": f"theme:{theme.theme_id}:{scope_id}",
                "object_kind": "THEME_OBLIGATION",
                "theme_id": theme.theme_id,
                "scope_ids": [scope_id],
                "claim_ids": [obligation["claim_id"]] if obligation["claim_id"] else [],
                "exact_statement": obligation["exact_objective"],
                "representation_id": obligation["representation_id"],
                "dependencies": dependencies,
                "missing_dependencies": missing,
                "allowed_method_ids": sorted(allowed),
                "forbidden_method_ids": sorted(forbidden),
                "route_status": status,
                "route_reason": reason,
                "relevant_kill_gate_asset_ids": sorted(
                    item["asset_id"] for item in gates
                ),
                "maturity_level": "RESULT",
                "authority_status": "NO_AUTHORITY_CHANGE",
            })
        return entries

    @staticmethod
    def _delta(prior: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
        prior_entries = {
            item["entry_id"]: item
            for item in (prior or {}).get("frontier_entries", [])
        }
        current_entries = {
            item["entry_id"]: item for item in current["frontier_entries"]
        }
        new = sorted(set(current_entries) - set(prior_entries))
        removed = sorted(set(prior_entries) - set(current_entries))
        changed = sorted(
            key for key in set(current_entries) & set(prior_entries)
            if stable_hash(current_entries[key]) != stable_hash(prior_entries[key])
        )
        newly_closed = sorted(
            key for key, item in current_entries.items()
            if item.get("route_status") == "DO_NOT_ROUTE"
            and item.get("route_reason") != "SUPERSEDED_RESULT"
            and prior_entries.get(key, {}).get("route_status") != "DO_NOT_ROUTE"
        )
        new_kill_gates = sorted(
            key for key, item in current_entries.items()
            if item.get("route_status") == "KILL_GATED"
            and prior_entries.get(key, {}).get("route_status") != "KILL_GATED"
        )
        new_audited_results = sorted(
            key for key, item in current_entries.items()
            if key not in prior_entries
            and item.get("object_kind") == "EXTERNAL_RESULT"
            and item.get("route_status") in {"DO_NOT_ROUTE", "KILL_GATED"}
        )
        new_external_ingestions = sorted(
            key for key, item in current_entries.items()
            if key not in prior_entries
            and item.get("object_kind") == "EXTERNAL_RESULT"
        )
        new_proved_results = sorted(
            key for key in new_external_ingestions
            if current_entries[key].get("conclusion") == "PROVED"
            and current_entries[key].get("route_status") == "DO_NOT_ROUTE"
        )
        new_falsified_results = sorted(
            key for key in new_external_ingestions
            if current_entries[key].get("conclusion") == "REFUTED"
            and current_entries[key].get("route_status") == "DO_NOT_ROUTE"
        )
        authority_drift = sorted(
            key for key, item in current_entries.items()
            if item.get("authority_status") == "PENDING_AUTONOMOUS_BINDING"
        )
        pending_integration = sorted(
            key for key, item in current_entries.items()
            if item.get("route_status") in {"BLOCKED", "WAIT_DEPENDENCY"}
        )
        prior_reusable_assets = set((prior or {}).get("reusable_asset_ids") or [])
        current_reusable_assets = set(current.get("reusable_asset_ids") or [])
        return {
            "schema_version": 2,
            "previous_frontier_sha256": (
                prior.get("frontier_sha256") if prior else None
            ),
            "frontier_sha256": current["frontier_sha256"],
            "generated_at": current["generated_at"],
            "phase": current["phase"],
            "campaign_id": current["campaign_id"],
            "epoch_id": current["epoch_id"],
            "new_entries": new,
            "removed_entries": removed,
            "changed_entries": changed,
            "newly_closed_obligations": newly_closed,
            "new_audited_results": new_audited_results,
            "new_proved_results": new_proved_results,
            "new_falsified_results": new_falsified_results,
            "new_kill_gates": new_kill_gates,
            "new_falsified_routes": new_kill_gates,
            "new_external_ingestions": new_external_ingestions,
            "new_reusable_assets": sorted(
                current_reusable_assets - prior_reusable_assets
            ),
            "superseded_results": list(current.get("superseded_result_ids") or []),
            "superseded_assets": list(current.get("superseded_asset_ids") or []),
            "authority_drift": authority_drift,
            "pending_human_or_theme_integration": pending_integration,
            "unresolved_integration_items": pending_integration,
        }

    def task_admission_error(
        self,
        task: ResearchTask,
        *,
        theme: CampaignTheme | None,
        state: dict[str, Any] | None = None,
    ) -> str | None:
        if theme is not None:
            error = theme.task_error(task)
            if error:
                return error
        frontier = state or self._last_state
        if frontier is None:
            return "AUDITED_FRONTIER_UNAVAILABLE"
        input_scope = None
        if isinstance(task.input_closure, dict):
            value = task.input_closure.get("canonical_object_id")
            if isinstance(value, str) and value:
                input_scope = value
        if self.method_ledger_path.is_file():
            method_ledger = _load_json(self.method_ledger_path)
            for route in method_ledger.get("routes", []):
                if route.get("method_id") != task.route_family:
                    continue
                scope_match = bool(
                    input_scope and input_scope in set(route.get("scope_ids") or [])
                )
                claim_match = task.target_claim in set(route.get("claim_ids") or [])
                if scope_match or claim_match:
                    return (
                        "AUDITED_FRONTIER_KILL_GATED: "
                        f"{route.get('asset_id')} method={task.route_family} "
                        f"DO_NOT_REPEAT={route.get('do_not_repeat')}"
                    )
        exact_scopes = {item for item in (input_scope, task.route_family) if item}
        blocking: list[dict[str, Any]] = []
        for entry in frontier.get("frontier_entries", []):
            if entry.get("route_status") == "ROUTE":
                continue
            entry_scopes = set(entry.get("scope_ids") or [])
            entry_claims = set(entry.get("claim_ids") or [])
            exact_scope_match = bool(exact_scopes & entry_scopes)
            mature_claim_match = bool(
                task.target_claim in entry_claims
                and entry.get("maturity_level") in {"THEME", "FINAL"}
            )
            graph_claim_match = bool(
                entry.get("object_kind") == "CLAIMGRAPH_CLAIM"
                and task.target_claim in entry_claims
            )
            if exact_scope_match or mature_claim_match or graph_claim_match:
                blocking.append(entry)
        if not blocking:
            return None
        entry = sorted(
            blocking,
            key=lambda item: (
                0 if input_scope in set(item.get("scope_ids") or []) else 1,
                str(item.get("entry_id")),
            ),
        )[0]
        return (
            f"AUDITED_FRONTIER_{entry['route_status']}: "
            f"{entry.get('entry_id')} {entry.get('route_reason')}"
        )

    def relevant_context_bundle(
        self,
        *,
        claim_ids: Iterable[str],
        scope_ids: Iterable[str],
        representation_ids: Iterable[str],
        method_ids: Iterable[str],
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        frontier = state or self._last_state or {}
        claims = set(claim_ids)
        scopes = set(scope_ids)
        representations = set(representation_ids)
        methods = set(method_ids)
        selected: list[dict[str, Any]] = []
        registry = _load_json(self.registry_path) if self.registry_path.is_file() else {
            "assets": []
        }
        for item in registry.get("assets", []):
            asset_scopes = set(item.get("scope_ids") or [])
            if scopes and asset_scopes:
                relevant = bool(scopes & asset_scopes)
            else:
                relevant = bool(
                    claims & set(item.get("claim_ids") or [])
                    or representations & set(item.get("representation_ids") or [])
                    or item.get("method_id") in methods
                )
            if relevant:
                selected.append(item)
        selected.sort(key=lambda item: str(item["asset_id"]))
        frontier_rows = [
            item for item in frontier.get("frontier_entries", [])
            if claims & set(item.get("claim_ids") or [])
            or scopes & set(item.get("scope_ids") or [])
        ]
        return {
            "schema_version": 1,
            "authority": "MINIMAL_RELEVANT_ROUTING_CONTEXT",
            "frontier_sha256": frontier.get("frontier_sha256"),
            "required_audited_theorems": [
                item for item in selected
                if item["kind"] in {"THEOREM", "LEMMA"} and item["reusable"]
            ],
            "representation_bridges": [
                item for item in selected
                if item["kind"] == "REPRESENTATION_BRIDGE" and item["reusable"]
            ],
            "reusable_tools": [
                item for item in selected
                if item["kind"] in {"RESEARCH_TOOL", "CERTIFICATE_VERIFIER"}
                and item["reusable"]
            ],
            "kill_gates": [
                item for item in selected
                if item["kind"] in {"NEGATIVE_RESULT", "KILL_GATE"}
                and item["reusable"]
            ],
            "hypotheses": [
                item for item in selected if item["kind"] == "RESEARCH_HYPOTHESIS"
            ],
            "frontier_entries": frontier_rows,
            "asset_reuse_rule": (
                "Reuse an equivalent audited asset by default. If it is inapplicable, "
                "record the asset id, the violated precondition or do-not-use condition, "
                "and the exact difference of the proposed new asset."
            ),
        }

    def task_context_bundle(self, task: ResearchTask) -> dict[str, Any]:
        input_scope: list[str] = []
        if isinstance(task.input_closure, dict):
            value = task.input_closure.get("canonical_object_id")
            if isinstance(value, str) and value:
                input_scope.append(value)
        return self.relevant_context_bundle(
            claim_ids=[task.target_claim, *task.dependencies],
            scope_ids=[*input_scope, task.route_family],
            representation_ids=[task.representation_id],
            method_ids=[task.route_family],
        )

    def audit_miss_reason(self, item: dict[str, Any]) -> str:
        current = item.get("audit_identity")
        supersedes = set(item.get("supersedes") or [])
        if not isinstance(current, dict) or not supersedes:
            return "NO_PREVIOUS_RECEIPT"
        prior_receipts = [
            receipt for receipt in self._valid_receipts().values()
            if str(receipt.get("source_object_id") or "") in supersedes
        ]
        if not prior_receipts:
            return "NO_PREVIOUS_RECEIPT"
        prior_receipt = sorted(
            prior_receipts,
            key=lambda value: str(value.get("source_object_id") or ""),
        )[0]
        wrapper = _load_json(
            self.objects_root / f"{prior_receipt['source_object_sha256']}.json"
        )
        prior = _audit_identity_from_object(
            str(wrapper["kind"]), dict(wrapper["object"])
        )
        comparisons = (
            ("exact_statement", "STATEMENT_CHANGED"),
            ("representation_id", "REPRESENTATION_CHANGED"),
            ("dependencies", "DEPENDENCY_CHANGED"),
            ("proof_hashes", "PROOF_CHANGED"),
            ("certificate_hashes", "CERTIFICATE_CHANGED"),
            ("source_hashes", "SOURCE_CHANGED"),
            ("audit_policy_version", "AUDIT_POLICY_CHANGED"),
            ("audit_level", "AUDIT_POLICY_CHANGED"),
        )
        for field, reason in comparisons:
            if prior.get(field) != current.get(field):
                return reason
        return "NO_PREVIOUS_RECEIPT"

    def routing_bridge_pairs(self) -> set[tuple[str, str]]:
        if not self.representation_graph_path.is_file():
            return set()
        graph = _load_json(self.representation_graph_path)
        return {
            tuple(sorted((
                str(item["source_representation_id"]),
                str(item["target_representation_id"]),
            )))
            for item in graph.get("edges", [])
        }

    def director_view(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        frontier = state or self._last_state or {}
        theme = frontier.get("campaign_theme")
        include_claims = (theme or {}).get("include_claim_ids") or []
        include_scopes = (theme or {}).get("include_scope_ids") or []
        dependency_boundary = (theme or {}).get("dependency_boundary") or []
        route_entries = list(frontier.get("frontier_entries", []))
        if theme is not None:
            relevant_ids = set(
                [*include_claims, *include_scopes, *dependency_boundary]
            )
            route_entries = [
                item for item in route_entries
                if relevant_ids & set(item.get("claim_ids") or [])
                or relevant_ids & set(item.get("scope_ids") or [])
                or item.get("result_id") in relevant_ids
            ]
        representations = {
            item.get("representation_id")
            for item in route_entries
            if item.get("representation_id")
            and (
                set(item.get("claim_ids") or []) & set(include_claims)
                or set(item.get("scope_ids") or []) & set(include_scopes)
            )
        }
        bundle = self.relevant_context_bundle(
            claim_ids=include_claims,
            scope_ids=include_scopes,
            representation_ids=representations,
            method_ids=(theme or {}).get("allowed_method_ids") or [],
            state=frontier,
        )
        return {
            "schema_version": 1,
            "frontier_sha256": frontier.get("frontier_sha256"),
            "routing_truth": "AUDITED_FRONTIER",
            "authority_truth": "CLAIMGRAPH_AND_CONTROLLER_RECEIPTS",
            "campaign_theme": theme,
            "route_entries": [
                item for item in route_entries
                if item.get("route_status") in ROUTE_STATUSES
            ],
            "relevant_assets": bundle,
            "full_frontier_path": str(self.current_path),
            "asset_registry_path": str(self.registry_path),
            "representation_graph_path": str(self.representation_graph_path),
            "method_ledger_path": str(self.method_ledger_path),
            "audit_index_path": str(self.audit_index_path),
        }
