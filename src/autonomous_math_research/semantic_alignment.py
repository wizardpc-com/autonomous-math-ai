from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable

from .representation import RepresentationContract
from .storage import file_digest
from .storage.artifacts import PORTABLE_SCHEMES, resolve_portable_uri


SEMANTICS_FILENAME = "semantics.json"
SEMANTICS_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "research_contract", "registry", "bridges", "claims",
})
_RESEARCH_CONTRACT_KEYS = frozenset({"active_version", "versions"})
_CONTRACT_VERSION_KEYS = frozenset({
    "version", "final_claim_id", "canonical_text", "sha256",
    "supersedes_sha256",
})
_REGISTRY_KEYS = frozenset({"entries"})
_ENTRY_KEYS = frozenset({
    "id", "kind", "canonical_name", "definition", "canonical_source",
    "aliases", "forbidden_confusions", "allowed_representations",
})
_BRIDGE_KEYS = frozenset({
    "id", "source", "target", "justification", "evidence",
})
_CLAIM_BINDING_KEYS = frozenset({
    "claim_id", "canonical_object", "core_terms", "required_bridges",
    "representation_id",
})
_TRUST_STATE_KEYS = frozenset({
    "schema_version", "opted_in", "source_path", "contract_history",
    "contract_head", "verification_receipts",
})
_VERIFICATION_RECEIPT_KEYS = frozenset({
    "receipt_fingerprint", "candidate_fingerprint", "claim_id",
    "candidate_type", "exact_statement_sha256", "claim_assumptions",
    "claim_dependencies", "parent_claim_id", "representation_id",
    "representation_contract",
    "artifact_hashes", "evidence_hashes",
    "domain_evidence_receipt_fingerprints", "audit_receipts", "bridge_ids",
    "contract_head", "semantic_head",
})
_AUDIT_RECEIPT_KEYS = frozenset({
    "audit_id", "audit_kind", "auditor_thread_id", "result_path",
    "result_sha256", "statement_checked_sha256", "verified_evidence_level",
    "validator_identity", "validator_version", "validator_config_sha256",
    "pass_scope_sha256",
})
_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPRESENTATION_ID_RE = re.compile(r"^rep:[0-9a-f]{64}$")
_NODE_TRANSITIONS = {
    "object": frozenset({"representation"}),
    "representation": frozenset({"representation", "evidence"}),
    "evidence": frozenset({"validator"}),
    "validator": frozenset({"claim"}),
    "claim": frozenset(),
}


class SemanticStatus(StrEnum):
    VERIFIED = "VERIFIED"
    BRIDGE_OPEN = "BRIDGE_OPEN"
    TERM_AMBIGUOUS = "TERM_AMBIGUOUS"
    UNREVIEWED = "UNREVIEWED"


def text_sha256(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _payload_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _exact_dict(value: Any, keys: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        actual = set(value) if isinstance(value, dict) else set()
        raise ValueError(
            f"{label} fields differ; missing={sorted(keys - actual)}, "
            f"extra={sorted(actual - keys)}"
        )
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return tuple(value)


def _semantic_id(value: Any, label: str, *, prefix: str | None = None) -> str:
    result = _string(value, label)
    if not _ID_RE.fullmatch(result):
        raise ValueError(f"{label} must be a portable semantic id")
    if prefix is not None and not result.startswith(f"{prefix}:"):
        raise ValueError(f"{label} must start with {prefix}:")
    return result


def _node_kind(node_id: str) -> str:
    return node_id.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class ResearchContractVersion:
    version: int
    final_claim_id: str
    canonical_text: str
    sha256: str
    supersedes_sha256: str | None

    @classmethod
    def from_dict(cls, value: Any) -> "ResearchContractVersion":
        raw = _exact_dict(value, _CONTRACT_VERSION_KEYS, "research contract version")
        version = raw["version"]
        if type(version) is not int or version < 1:
            raise ValueError("research contract version must be a positive integer")
        canonical_text = _string(raw["canonical_text"], "research contract canonical_text")
        digest = _string(raw["sha256"], "research contract sha256")
        if not _SHA256_RE.fullmatch(digest) or digest != text_sha256(canonical_text):
            raise ValueError("research contract canonical_text SHA256 mismatch")
        supersedes = raw["supersedes_sha256"]
        if supersedes is not None and (
            not isinstance(supersedes, str) or not _SHA256_RE.fullmatch(supersedes)
        ):
            raise ValueError("research contract supersedes_sha256 is invalid")
        return cls(
            version=version,
            final_claim_id=_string(raw["final_claim_id"], "research contract final_claim_id"),
            canonical_text=canonical_text,
            sha256=digest,
            supersedes_sha256=supersedes,
        )


@dataclass(frozen=True, slots=True)
class SemanticEntry:
    id: str
    kind: str
    canonical_name: str
    definition: str
    canonical_source: str
    aliases: tuple[str, ...]
    forbidden_confusions: tuple[str, ...]
    allowed_representations: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "SemanticEntry":
        raw = _exact_dict(value, _ENTRY_KEYS, "semantic registry entry")
        kind = _string(raw["kind"], "semantic registry kind")
        if kind not in {"TERM", "OBJECT"}:
            raise ValueError("semantic registry kind must be TERM or OBJECT")
        identifier = _semantic_id(
            raw["id"], "semantic registry id",
            prefix="term" if kind == "TERM" else "object",
        )
        aliases = _strings(raw["aliases"], f"semantic registry {identifier} aliases")
        forbidden = _strings(
            raw["forbidden_confusions"],
            f"semantic registry {identifier} forbidden_confusions",
        )
        canonical_name = _string(
            raw["canonical_name"], f"semantic registry {identifier} canonical_name",
        )
        own_names = {canonical_name.casefold(), *(item.casefold() for item in aliases)}
        collision = own_names & {item.casefold() for item in forbidden}
        if collision:
            raise ValueError(
                f"semantic registry {identifier} treats its own name as a forbidden confusion"
            )
        allowed = _strings(
            raw["allowed_representations"],
            f"semantic registry {identifier} allowed_representations",
        )
        for representation in allowed:
            _semantic_id(
                representation,
                f"semantic registry {identifier} allowed representation",
                prefix="representation",
            )
        return cls(
            id=identifier,
            kind=kind,
            canonical_name=canonical_name,
            definition=_string(
                raw["definition"], f"semantic registry {identifier} definition",
            ),
            canonical_source=_string(
                raw["canonical_source"],
                f"semantic registry {identifier} canonical_source",
            ),
            aliases=aliases,
            forbidden_confusions=forbidden,
            allowed_representations=allowed,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("aliases", "forbidden_confusions", "allowed_representations"):
            result[key] = list(result[key])
        return result


@dataclass(frozen=True, slots=True)
class RepresentationBridge:
    id: str
    source: str
    target: str
    justification: str
    evidence: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "RepresentationBridge":
        raw = _exact_dict(value, _BRIDGE_KEYS, "representation bridge")
        identifier = _semantic_id(raw["id"], "representation bridge id", prefix="bridge")
        source = _semantic_id(raw["source"], f"{identifier} source")
        target = _semantic_id(raw["target"], f"{identifier} target")
        if _node_kind(source) not in _NODE_TRANSITIONS:
            raise ValueError(f"{identifier} source has an unsupported semantic node type")
        if _node_kind(target) not in _NODE_TRANSITIONS:
            raise ValueError(f"{identifier} target has an unsupported semantic node type")
        evidence = _strings(raw["evidence"], f"{identifier} evidence")
        if not evidence:
            raise ValueError(f"{identifier} evidence must not be empty")
        for path in evidence:
            relative = PurePosixPath(path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != path
                or (relative.parts and relative.parts[0].endswith(":"))
            ):
                raise ValueError(
                    f"{identifier} evidence must contain project-relative POSIX paths"
                )
        return cls(
            id=identifier,
            source=source,
            target=target,
            justification=_string(raw["justification"], f"{identifier} justification"),
            evidence=evidence,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence"] = list(self.evidence)
        return result


@dataclass(frozen=True, slots=True)
class ClaimSemanticBinding:
    claim_id: str
    canonical_object: str
    core_terms: tuple[str, ...]
    required_bridges: tuple[str, ...]
    representation_id: str

    @classmethod
    def from_dict(cls, value: Any) -> "ClaimSemanticBinding":
        raw = _exact_dict(value, _CLAIM_BINDING_KEYS, "semantic claim binding")
        representation_id = _string(
            raw["representation_id"], "semantic claim representation_id",
        )
        if not _REPRESENTATION_ID_RE.fullmatch(representation_id):
            raise ValueError("semantic claim representation_id must be a content hash")
        return cls(
            claim_id=_string(raw["claim_id"], "semantic claim_id"),
            canonical_object=_semantic_id(
                raw["canonical_object"], "semantic claim canonical_object", prefix="object",
            ),
            core_terms=_strings(raw["core_terms"], "semantic claim core_terms"),
            required_bridges=_strings(
                raw["required_bridges"], "semantic claim required_bridges",
            ),
            representation_id=representation_id,
        )


@dataclass(frozen=True, slots=True)
class SemanticTrustState:
    opted_in: bool
    source_path: str | None
    contract_history: tuple[str, ...]
    contract_head: str | None
    verification_receipts: tuple[dict[str, Any], ...]

    @classmethod
    def legacy(cls) -> "SemanticTrustState":
        return cls(False, None, (), None, ())

    @classmethod
    def from_trusted_payload(cls, trusted: Any) -> "SemanticTrustState":
        if not isinstance(trusted, dict):
            raise ValueError("canonical trusted state must be a JSON object")
        raw = trusted.get("semantic_alignment")
        if raw is None:
            return cls.legacy()
        value = _exact_dict(raw, _TRUST_STATE_KEYS, "semantic trusted state")
        if value["schema_version"] != 1 or value["opted_in"] is not True:
            raise ValueError("semantic trusted state must be an opted-in schema v1 state")
        source_path = _string(value["source_path"], "semantic trusted source_path")
        history = _strings(value["contract_history"], "semantic contract history")
        if not history or any(not _SHA256_RE.fullmatch(item) for item in history):
            raise ValueError("semantic trusted contract history is invalid")
        head = _string(value["contract_head"], "semantic trusted contract_head")
        if head != history[-1]:
            raise ValueError("semantic trusted contract_head does not match its history")
        receipts_raw = value["verification_receipts"]
        if not isinstance(receipts_raw, list):
            raise ValueError("semantic verification receipts must be an array")
        receipts = tuple(_validate_verification_receipt(item) for item in receipts_raw)
        fingerprints = [item["receipt_fingerprint"] for item in receipts]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("semantic verification receipts contain duplicate fingerprints")
        return cls(True, source_path, history, head, receipts)

    def to_payload(self) -> dict[str, Any]:
        if not self.opted_in:
            raise ValueError("legacy semantic state has no trusted payload")
        return {
            "schema_version": 1,
            "opted_in": True,
            "source_path": self.source_path,
            "contract_history": list(self.contract_history),
            "contract_head": self.contract_head,
            "verification_receipts": [dict(item) for item in self.verification_receipts],
        }

    def with_receipt(self, receipt: dict[str, Any]) -> "SemanticTrustState":
        normalized = _validate_verification_receipt(receipt)
        if any(
            item["receipt_fingerprint"] == normalized["receipt_fingerprint"]
            for item in self.verification_receipts
        ):
            return self
        return SemanticTrustState(
            self.opted_in,
            self.source_path,
            self.contract_history,
            self.contract_head,
            (*self.verification_receipts, normalized),
        )

    def require_committed_journal(
        self, authorizations: Iterable[dict[str, Any]],
    ) -> None:
        """Require trusted semantic state to be derived from canonical commits."""
        if not self.opted_in:
            return
        committed = [dict(item) for item in authorizations]
        contract_heads = [
            item.get("contract_head")
            for item in committed
            if item.get("kind") in {"SEMANTIC_OPT_IN", "SEMANTIC_CONTRACT_APPENDED"}
        ]
        if self.contract_head not in contract_heads:
            raise ValueError(
                "semantic opt-in or contract head lacks a committed canonical transition"
            )
        receipt_authorizations = [
            item for item in committed
            if item.get("kind") == "SEMANTIC_VERIFICATION_TRANSITION"
        ]
        journal_fingerprints = tuple(
            str(item.get("semantic_receipt_fingerprint") or "")
            for item in receipt_authorizations
        )
        state_fingerprints = tuple(
            item["receipt_fingerprint"] for item in self.verification_receipts
        )
        if journal_fingerprints != state_fingerprints:
            raise ValueError(
                "semantic verification receipt history is not the committed append-only journal"
            )
        for receipt, authorization in zip(
            self.verification_receipts, receipt_authorizations, strict=True,
        ):
            if (
                authorization.get("candidate_fingerprint")
                != receipt["candidate_fingerprint"]
                or authorization.get("claim_id") != receipt["claim_id"]
                or tuple(authorization.get("bridge_ids") or ())
                != tuple(receipt["bridge_ids"])
            ):
                raise ValueError(
                    "semantic verification receipt disagrees with its canonical authorization"
                )


def _hash_mapping(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        for key, digest in value.items()
    ):
        raise ValueError(f"{label} must map non-empty paths to SHA256 digests")
    return dict(sorted(value.items()))


def _validate_audit_receipt(value: Any) -> dict[str, Any]:
    raw = _exact_dict(value, _AUDIT_RECEIPT_KEYS, "semantic audit receipt")
    normalized = dict(raw)
    for key in (
        "audit_id", "audit_kind", "result_path", "verified_evidence_level",
        "validator_identity", "validator_version",
    ):
        normalized[key] = _string(raw[key], f"semantic audit receipt {key}")
    auditor = raw["auditor_thread_id"]
    if auditor is not None and (not isinstance(auditor, str) or not auditor.strip()):
        raise ValueError("semantic audit receipt auditor_thread_id is invalid")
    normalized["auditor_thread_id"] = auditor
    for key in (
        "result_sha256", "statement_checked_sha256", "validator_config_sha256",
        "pass_scope_sha256",
    ):
        digest = _string(raw[key], f"semantic audit receipt {key}")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"semantic audit receipt {key} is not a SHA256 digest")
        normalized[key] = digest
    return normalized


def _validate_verification_receipt(value: Any) -> dict[str, Any]:
    raw = _exact_dict(
        value, _VERIFICATION_RECEIPT_KEYS, "semantic verification receipt",
    )
    normalized = dict(raw)
    for key in (
        "candidate_fingerprint", "claim_id", "candidate_type", "representation_id",
        "contract_head", "semantic_head",
    ):
        normalized[key] = _string(raw[key], f"semantic verification receipt {key}")
    if not _SHA256_RE.fullmatch(normalized["candidate_fingerprint"]):
        raise ValueError("semantic receipt candidate_fingerprint is invalid")
    if not _REPRESENTATION_ID_RE.fullmatch(normalized["representation_id"]):
        raise ValueError("semantic receipt representation_id is invalid")
    for key in ("exact_statement_sha256", "contract_head", "semantic_head"):
        if not _SHA256_RE.fullmatch(str(normalized[key])):
            raise ValueError(f"semantic verification receipt {key} is invalid")
    contract = RepresentationContract.from_dict(raw["representation_contract"])
    if contract.representation_id != normalized["representation_id"]:
        raise ValueError("semantic receipt representation id does not match its content")
    normalized["representation_contract"] = contract.to_dict()
    normalized["claim_assumptions"] = list(_strings(
        raw["claim_assumptions"], "semantic receipt claim_assumptions",
    ))
    normalized["claim_dependencies"] = list(_strings(
        raw["claim_dependencies"], "semantic receipt claim_dependencies",
    ))
    parent_claim_id = raw["parent_claim_id"]
    if parent_claim_id is not None and (
        not isinstance(parent_claim_id, str) or not parent_claim_id.strip()
    ):
        raise ValueError("semantic receipt parent_claim_id is invalid")
    normalized["parent_claim_id"] = parent_claim_id
    normalized["artifact_hashes"] = _hash_mapping(
        raw["artifact_hashes"], "semantic receipt artifact_hashes",
    )
    normalized["evidence_hashes"] = _hash_mapping(
        raw["evidence_hashes"], "semantic receipt evidence_hashes",
    )
    if not normalized["artifact_hashes"] or not normalized["evidence_hashes"]:
        raise ValueError("semantic receipt requires sealed artifact and bridge evidence hashes")
    domain_receipts = _strings(
        raw["domain_evidence_receipt_fingerprints"],
        "semantic domain evidence receipt fingerprints",
    )
    if any(not _SHA256_RE.fullmatch(item) for item in domain_receipts):
        raise ValueError("semantic domain evidence receipt fingerprint is invalid")
    normalized["domain_evidence_receipt_fingerprints"] = list(domain_receipts)
    bridge_ids = _strings(raw["bridge_ids"], "semantic receipt bridge_ids")
    if not bridge_ids:
        raise ValueError("semantic receipt bridge_ids must not be empty")
    for bridge_id in bridge_ids:
        _semantic_id(bridge_id, "semantic receipt bridge id", prefix="bridge")
    normalized["bridge_ids"] = list(bridge_ids)
    audit_receipts_raw = raw["audit_receipts"]
    if not isinstance(audit_receipts_raw, list) or not audit_receipts_raw:
        raise ValueError("semantic receipt requires at least one audit receipt")
    normalized["audit_receipts"] = [
        _validate_audit_receipt(item) for item in audit_receipts_raw
    ]
    fingerprint = _string(
        raw["receipt_fingerprint"], "semantic receipt fingerprint",
    )
    if not _SHA256_RE.fullmatch(fingerprint):
        raise ValueError("semantic receipt fingerprint is invalid")
    payload = dict(normalized)
    payload.pop("receipt_fingerprint", None)
    if _payload_sha256(payload) != fingerprint:
        raise ValueError("semantic receipt fingerprint does not match its content")
    normalized["receipt_fingerprint"] = fingerprint
    return normalized


@dataclass(frozen=True, slots=True)
class ClaimSemanticDecision:
    claim_id: str
    status: str
    issues: tuple[str, ...]
    required_bridges: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "status": self.status,
            "issues": list(self.issues),
            "required_bridges": list(self.required_bridges),
        }


class SemanticPromotionError(ValueError):
    def __init__(self, decision: ClaimSemanticDecision):
        self.decision = decision
        detail = "; ".join(decision.issues) or decision.status
        super().__init__(
            f"semantic gate blocks terminal claim {decision.claim_id}: "
            f"{decision.status}: {detail}"
        )


class SemanticAlignment:
    """Project-level research contract, term registry, and bridge graph.

    Absence is a legacy compatibility state only until the controller persists
    semantic opt-in. The file is declaration-only; VERIFIED is derived solely
    from controller-owned receipts in canonical trusted state.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        path: Path | None,
        source_sha256: str | None,
        contract_versions: tuple[ResearchContractVersion, ...],
        entries: dict[str, SemanticEntry],
        bridges: dict[str, RepresentationBridge],
        claims: dict[str, ClaimSemanticBinding],
    ):
        self.project_root = project_root.resolve()
        self.path = path.resolve() if path is not None else None
        self.source_sha256 = source_sha256
        self.contract_versions = contract_versions
        self.entries = entries
        self.bridges = bridges
        self.claims = claims
        self._term_lookup: dict[str, set[str]] = {}
        for entry in entries.values():
            for token in (entry.id, entry.canonical_name, *entry.aliases):
                self._term_lookup.setdefault(token.casefold(), set()).add(entry.id)

    @classmethod
    def legacy(cls, project_root: Path) -> "SemanticAlignment":
        return cls(
            project_root=project_root,
            path=None,
            source_sha256=None,
            contract_versions=(),
            entries={},
            bridges={},
            claims={},
        )

    @classmethod
    def load_optional(
        cls,
        project_root: Path,
        path: Path | None = None,
        *,
        required: bool = False,
    ) -> "SemanticAlignment":
        root = project_root.resolve()
        source = (path or root / "autonomous" / SEMANTICS_FILENAME).resolve()
        if not source.exists():
            if required:
                raise ValueError(
                    "semantic alignment metadata is missing after persistent opt-in"
                )
            return cls.legacy(root)
        if not source.is_file():
            raise ValueError("semantic alignment path must be a file")
        if not source.is_relative_to(root):
            raise ValueError("semantic alignment file must be inside the project root")
        raw_bytes = source.read_bytes()
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("semantic alignment file must be valid UTF-8 JSON") from exc
        document = _exact_dict(raw, _TOP_LEVEL_KEYS, "semantic alignment")
        if (
            type(document["schema_version"]) is not int
            or document["schema_version"] != SEMANTICS_SCHEMA_VERSION
        ):
            raise ValueError("unsupported semantic alignment schema_version")

        contract = _exact_dict(
            document["research_contract"],
            _RESEARCH_CONTRACT_KEYS,
            "research contract",
        )
        if not isinstance(contract["versions"], list) or not contract["versions"]:
            raise ValueError("research contract versions must be a non-empty array")
        versions = tuple(
            ResearchContractVersion.from_dict(item) for item in contract["versions"]
        )
        for index, version in enumerate(versions, 1):
            if version.version != index:
                raise ValueError("research contract versions must be consecutive from 1")
            expected = None if index == 1 else versions[index - 2].sha256
            if version.supersedes_sha256 != expected:
                raise ValueError(
                    "research contract versions must form an explicit SHA256 chain"
                )
        active_version = contract["active_version"]
        if type(active_version) is not int or active_version != versions[-1].version:
            raise ValueError("research contract active_version must select the latest version")

        registry = _exact_dict(document["registry"], _REGISTRY_KEYS, "semantic registry")
        if not isinstance(registry["entries"], list):
            raise ValueError("semantic registry entries must be an array")
        entry_values = tuple(SemanticEntry.from_dict(item) for item in registry["entries"])
        entries = {item.id: item for item in entry_values}
        if len(entries) != len(entry_values):
            raise ValueError("semantic registry contains duplicate ids")

        if not isinstance(document["bridges"], list):
            raise ValueError("representation bridges must be an array")
        bridge_values = tuple(
            RepresentationBridge.from_dict(item) for item in document["bridges"]
        )
        bridges = {item.id: item for item in bridge_values}
        if len(bridges) != len(bridge_values):
            raise ValueError("representation bridges contain duplicate ids")

        if not isinstance(document["claims"], list):
            raise ValueError("semantic claims must be an array")
        claim_values = tuple(
            ClaimSemanticBinding.from_dict(item) for item in document["claims"]
        )
        claims = {item.claim_id: item for item in claim_values}
        if len(claims) != len(claim_values):
            raise ValueError("semantic claims contain duplicate claim ids")

        return cls(
            project_root=root,
            path=source,
            source_sha256=sha256(raw_bytes).hexdigest(),
            contract_versions=versions,
            entries=entries,
            bridges=bridges,
            claims=claims,
        )

    @property
    def present(self) -> bool:
        return self.path is not None

    @property
    def active_contract(self) -> ResearchContractVersion | None:
        return self.contract_versions[-1] if self.contract_versions else None

    @property
    def contract_history(self) -> tuple[str, ...]:
        return tuple(
            _payload_sha256({
                "version": item.version,
                "final_claim_id": item.final_claim_id,
                "canonical_text_sha256": item.sha256,
                "supersedes_sha256": item.supersedes_sha256,
            })
            for item in self.contract_versions
        )

    @property
    def contract_head(self) -> str | None:
        history = self.contract_history
        return history[-1] if history else None

    @property
    def source_path(self) -> str | None:
        return (
            self.path.relative_to(self.project_root).as_posix()
            if self.path is not None else None
        )

    def reconcile_trust_state(
        self, state: SemanticTrustState,
    ) -> SemanticTrustState:
        if not state.opted_in:
            if not self.present:
                return state
            return SemanticTrustState(
                True,
                self.source_path,
                self.contract_history,
                self.contract_head,
                (),
            )
        if not self.present:
            raise ValueError("semantic alignment metadata is missing after persistent opt-in")
        if state.source_path != self.source_path:
            raise ValueError("semantic alignment source path changed after persistent opt-in")
        current = self.contract_history
        trusted = state.contract_history
        if len(current) < len(trusted) or current[:len(trusted)] != trusted:
            raise ValueError(
                "research contract history rewrote or removed a trusted version"
            )
        return SemanticTrustState(
            True,
            self.source_path,
            current,
            self.contract_head,
            state.verification_receipts,
        )

    def assert_unchanged(self) -> None:
        if self.path is None:
            return
        if not self.path.is_file() or sha256(self.path.read_bytes()).hexdigest() != self.source_sha256:
            raise ValueError("semantic alignment file changed after the research contract was frozen")

    def _term_issues(self, values: Iterable[str]) -> list[str]:
        issues: list[str] = []
        for value in values:
            matches = self._term_lookup.get(value.casefold(), set())
            if not matches:
                issues.append(f"unregistered core term: {value}")
            elif len(matches) > 1:
                issues.append(f"ambiguous core term {value}: {sorted(matches)}")
        return issues

    def _declared_evidence_paths(
        self, binding: ClaimSemanticBinding,
    ) -> tuple[str, ...]:
        paths: list[str] = []
        for bridge_id in binding.required_bridges:
            bridge = self.bridges.get(bridge_id)
            if bridge is not None:
                paths.extend(bridge.evidence)
        return tuple(dict.fromkeys(paths))

    def _matching_receipt(
        self,
        binding: ClaimSemanticBinding,
        *,
        trust_state: SemanticTrustState,
        claim_statement: str | None,
        claim_assumptions: Iterable[str] | None = None,
        claim_dependencies: Iterable[str] | None = None,
        parent_claim_id: str | None = None,
        check_parent_claim_id: bool = False,
        validate_files: bool,
    ) -> tuple[dict[str, Any] | None, str | None]:
        stale_reason: str | None = None
        for receipt in reversed(trust_state.verification_receipts):
            if receipt["claim_id"] != binding.claim_id:
                continue
            checks = {
                "semantic head": receipt["semantic_head"] == self.source_sha256,
                "contract head": receipt["contract_head"] == self.contract_head,
                "bridge path": tuple(receipt["bridge_ids"]) == binding.required_bridges,
                "representation": receipt["representation_id"] == binding.representation_id,
                "claim statement": (
                    claim_statement is None
                    or receipt["exact_statement_sha256"] == text_sha256(claim_statement)
                ),
                "claim assumptions": (
                    claim_assumptions is None
                    or sorted(" ".join(item.split()) for item in claim_assumptions)
                    == sorted(
                        " ".join(item.split())
                        for item in receipt["claim_assumptions"]
                    )
                ),
                "claim dependencies": (
                    claim_dependencies is None
                    or sorted(claim_dependencies)
                    == sorted(receipt["claim_dependencies"])
                ),
                "parent claim": (
                    not check_parent_claim_id
                    or receipt["parent_claim_id"] == parent_claim_id
                ),
                "declared evidence": set(receipt["evidence_hashes"])
                == set(self._declared_evidence_paths(binding)),
            }
            failed = [name for name, passed in checks.items() if not passed]
            if failed:
                stale_reason = "stale semantic receipt mismatch: " + ", ".join(failed)
                continue
            if validate_files:
                try:
                    self.receipt_file_preconditions(receipt)
                except ValueError as exc:
                    stale_reason = str(exc)
                    continue
            return receipt, None
        return None, stale_reason

    def evaluate_claim(
        self,
        claim_id: str,
        *,
        trust_state: SemanticTrustState | None = None,
        claim_statement: str | None = None,
        claim_assumptions: Iterable[str] | None = None,
        claim_dependencies: Iterable[str] | None = None,
        parent_claim_id: str | None = None,
        check_parent_claim_id: bool = False,
        validate_files: bool = True,
    ) -> ClaimSemanticDecision:
        if not self.present:
            return ClaimSemanticDecision(
                claim_id=claim_id,
                status=SemanticStatus.UNREVIEWED,
                issues=("legacy project has no semantic alignment metadata",),
                required_bridges=(),
            )
        binding = self.claims.get(claim_id)
        if binding is None:
            return ClaimSemanticDecision(
                claim_id=claim_id,
                status=SemanticStatus.UNREVIEWED,
                issues=("claim has no semantic binding",),
                required_bridges=(),
            )

        issues: list[str] = []
        term_issues: list[str] = []
        canonical = self.entries.get(binding.canonical_object)
        if canonical is None or canonical.kind != "OBJECT":
            term_issues.append(
                f"canonical object is not registered: {binding.canonical_object}"
            )
        if not binding.core_terms:
            term_issues.append("claim declares no core terms")
        term_issues.extend(self._term_issues(binding.core_terms))
        if not binding.required_bridges:
            issues.append("claim declares no representation bridge path")

        current = binding.canonical_object
        node_kinds = [_node_kind(current)]
        for bridge_id in binding.required_bridges:
            bridge = self.bridges.get(bridge_id)
            if bridge is None:
                issues.append(f"required bridge is not registered: {bridge_id}")
                continue
            if bridge.source != current:
                issues.append(
                    f"bridge path is discontinuous at {bridge_id}: "
                    f"expected source {current}, got {bridge.source}"
                )
            source_kind = _node_kind(bridge.source)
            target_kind = _node_kind(bridge.target)
            if target_kind not in _NODE_TRANSITIONS[source_kind]:
                issues.append(
                    f"bridge {bridge_id} has invalid layer transition "
                    f"{source_kind}->{target_kind}"
                )
            current = bridge.target
            node_kinds.append(target_kind)

        expected_target = f"claim:{claim_id}"
        if current != expected_target:
            issues.append(
                f"bridge path does not terminate at {expected_target}: {current}"
            )
        required_layers = ("object", "representation", "evidence", "validator", "claim")
        positions: list[int] = []
        for layer in required_layers:
            try:
                positions.append(node_kinds.index(layer))
            except ValueError:
                issues.append(f"bridge path is missing the {layer} layer")
        if len(positions) == len(required_layers) and positions != sorted(positions):
            issues.append("bridge layers are out of order")

        if canonical is not None and canonical.kind == "OBJECT":
            representations = [
                bridge.target
                for bridge_id in binding.required_bridges
                if (bridge := self.bridges.get(bridge_id)) is not None
                and _node_kind(bridge.target) == "representation"
            ]
            first_representation = representations[0] if representations else None
            if first_representation not in canonical.allowed_representations:
                issues.append(
                    "bridge path uses a representation not allowed by the canonical object: "
                    f"{first_representation or 'missing'}"
                )

        if term_issues:
            status = SemanticStatus.TERM_AMBIGUOUS
            issues = [*term_issues, *issues]
        elif issues:
            status = SemanticStatus.BRIDGE_OPEN
        else:
            state = trust_state or SemanticTrustState.legacy()
            receipt, stale_reason = self._matching_receipt(
                binding,
                trust_state=state,
                claim_statement=claim_statement,
                claim_assumptions=claim_assumptions,
                claim_dependencies=claim_dependencies,
                parent_claim_id=parent_claim_id,
                check_parent_claim_id=check_parent_claim_id,
                validate_files=validate_files,
            )
            if receipt is None:
                status = SemanticStatus.BRIDGE_OPEN
                issues.append(
                    stale_reason
                    or "no controller-owned candidate verification receipt covers this bridge path"
                )
            else:
                status = SemanticStatus.VERIFIED
        return ClaimSemanticDecision(
            claim_id=claim_id,
            status=status,
            issues=tuple(dict.fromkeys(issues)),
            required_bridges=binding.required_bridges,
        )

    def validate_project(
        self,
        *,
        final_claim_id: str,
        final_claim_statement: str,
        claim_ids: Iterable[str],
        trust_state: SemanticTrustState | None = None,
        claim_statements: dict[str, str] | None = None,
        claim_objects: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.present:
            return self.summary(
                claim_ids,
                trust_state=trust_state,
                claim_statements=claim_statements,
                claim_objects=claim_objects,
            )
        self.assert_unchanged()
        contract = self.active_contract
        if contract is None:
            raise ValueError("semantic alignment research contract is missing")
        if contract.final_claim_id != final_claim_id:
            raise ValueError(
                "research contract final_claim_id does not match the project manifest"
            )
        if contract.canonical_text != final_claim_statement:
            raise ValueError(
                "research contract canonical_text does not match the final ClaimGraph statement"
            )
        if trust_state is not None:
            self.reconcile_trust_state(trust_state)
        return self.summary(
            claim_ids,
            trust_state=trust_state,
            claim_statements=claim_statements,
            claim_objects=claim_objects,
        )

    def build_verification_receipt(
        self,
        *,
        trust_state: SemanticTrustState,
        candidate: Any,
        artifact_hashes: dict[str, str],
        evidence_hashes: dict[str, str],
        domain_evidence_receipt_fingerprints: Iterable[str],
        audit_receipts: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        self.assert_unchanged()
        if not self.present or not trust_state.opted_in:
            raise ValueError("semantic verification requires persistent project opt-in")
        binding = self.claims.get(str(candidate.claim_id))
        if binding is None:
            raise SemanticPromotionError(self.evaluate_claim(str(candidate.claim_id)))
        declared_bridge_ids = tuple(getattr(candidate, "semantic_bridge_ids", ()))
        issues: list[str] = []
        if declared_bridge_ids != binding.required_bridges:
            issues.append("candidate semantic_bridge_ids do not match the declared bridge path")
        if candidate.representation_id != binding.representation_id:
            issues.append("candidate RepresentationContract does not match the semantic binding")
        declared_evidence = set(self._declared_evidence_paths(binding))
        if set(evidence_hashes) != declared_evidence:
            issues.append("candidate evidence hashes do not cover the declared bridge evidence")
        if not artifact_hashes or not evidence_hashes:
            issues.append("candidate lacks sealed artifact or semantic evidence hashes")
        if not set(evidence_hashes.values()).issubset(set(artifact_hashes.values())):
            issues.append("declared bridge evidence is not contained in the sealed candidate")
        normalized_audits = [_validate_audit_receipt(item) for item in audit_receipts]
        if not normalized_audits:
            issues.append("candidate lacks an independent PASS audit receipt")
        statement_digest = text_sha256(candidate.exact_statement)
        expected_pass_scope = _payload_sha256({
            "candidate_fingerprint": candidate.fingerprint,
            "claim_id": candidate.claim_id,
            "exact_statement": " ".join(candidate.exact_statement.split()),
            "representation_id": candidate.representation_id,
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "semantic_evidence_hashes": dict(sorted(evidence_hashes.items())),
            "semantic_bridge_ids": list(candidate.semantic_bridge_ids),
        })
        if any(
            item["statement_checked_sha256"] != statement_digest
            for item in normalized_audits
        ):
            issues.append("audit PASS scope checked a different exact statement")
        if any(
            item["pass_scope_sha256"] != expected_pass_scope
            for item in normalized_audits
        ):
            issues.append("audit validator PASS scope does not match this sealed candidate")
        if issues:
            raise SemanticPromotionError(ClaimSemanticDecision(
                claim_id=str(candidate.claim_id),
                status=SemanticStatus.BRIDGE_OPEN,
                issues=tuple(issues),
                required_bridges=binding.required_bridges,
            ))
        payload: dict[str, Any] = {
            "candidate_fingerprint": candidate.fingerprint,
            "claim_id": candidate.claim_id,
            "candidate_type": candidate.type,
            "exact_statement_sha256": statement_digest,
            "claim_assumptions": list(candidate.assumptions),
            "claim_dependencies": list(candidate.dependencies),
            "parent_claim_id": candidate.parent_claim_id,
            "representation_id": candidate.representation_id,
            "representation_contract": candidate.representation_contract.to_dict(),
            "artifact_hashes": dict(sorted(artifact_hashes.items())),
            "evidence_hashes": dict(sorted(evidence_hashes.items())),
            "domain_evidence_receipt_fingerprints": sorted(set(
                domain_evidence_receipt_fingerprints
            )),
            "audit_receipts": normalized_audits,
            "bridge_ids": list(binding.required_bridges),
            "contract_head": self.contract_head,
            "semantic_head": self.source_sha256,
        }
        payload["receipt_fingerprint"] = _payload_sha256(payload)
        return _validate_verification_receipt(payload)

    def require_claim_verified(
        self,
        claim_id: str,
        *,
        trust_state: SemanticTrustState,
        claim_statement: str,
        claim_assumptions: Iterable[str] | None = None,
        claim_dependencies: Iterable[str] | None = None,
        parent_claim_id: str | None = None,
        check_parent_claim_id: bool = False,
    ) -> ClaimSemanticDecision:
        decision = self.evaluate_claim(
            claim_id,
            trust_state=trust_state,
            claim_statement=claim_statement,
            claim_assumptions=claim_assumptions,
            claim_dependencies=claim_dependencies,
            parent_claim_id=parent_claim_id,
            check_parent_claim_id=check_parent_claim_id,
        )
        if self.present and decision.status != SemanticStatus.VERIFIED:
            raise SemanticPromotionError(decision)
        return decision

    def receipt_file_preconditions(
        self, receipt: dict[str, Any],
    ) -> dict[Path, str]:
        normalized = _validate_verification_receipt(receipt)
        runtime_root = self.project_root / "autonomous"
        preconditions: dict[Path, str] = {}
        for uri, digest in normalized["artifact_hashes"].items():
            if not uri.startswith(PORTABLE_SCHEMES):
                raise ValueError("semantic receipt artifact path is not a durable URI")
            path = resolve_portable_uri(self.project_root, runtime_root, uri)
            if file_digest(path) != digest:
                raise ValueError(f"semantic receipt artifact is missing or stale: {uri}")
            preconditions[path] = digest
        for relative, digest in normalized["evidence_hashes"].items():
            path = (self.project_root / relative).resolve()
            if not path.is_relative_to(self.project_root) or not path.is_file():
                raise ValueError(
                    f"semantic bridge evidence is missing or outside the project: {relative}"
                )
            if file_digest(path) != digest:
                raise ValueError(f"semantic bridge evidence is stale: {relative}")
            preconditions[path] = digest
        for audit in normalized["audit_receipts"]:
            path = resolve_portable_uri(
                self.project_root, runtime_root, audit["result_path"],
            )
            if file_digest(path) != audit["result_sha256"]:
                raise ValueError(
                    f"semantic audit receipt is missing or stale: {audit['result_path']}"
                )
            preconditions[path] = audit["result_sha256"]
        return preconditions

    def require_final_claim_acceptance(
        self,
        final_claim_id: str,
        *,
        claims: dict[str, Any],
        trust_state: SemanticTrustState,
    ) -> dict[Path, str]:
        if not self.present:
            return {}
        closure: list[str] = []
        pending = [final_claim_id]
        seen: set[str] = set()
        while pending:
            claim_id = pending.pop()
            if claim_id in seen:
                continue
            claim = claims.get(claim_id)
            if claim is None:
                raise ValueError(
                    f"trusted final claim dependency is absent: {claim_id}"
                )
            seen.add(claim_id)
            closure.append(claim_id)
            pending.extend(reversed(list(claim.dependencies)))

        preconditions: dict[Path, str] = {}
        for claim_id in closure:
            claim = claims[claim_id]
            decision = self.require_claim_verified(
                claim_id,
                trust_state=trust_state,
                claim_statement=claim.statement,
                claim_assumptions=claim.assumptions,
                claim_dependencies=claim.dependencies,
                parent_claim_id=claim.parent_claim_id,
                check_parent_claim_id=True,
            )
            binding = self.claims[claim_id]
            receipt, _ = self._matching_receipt(
                binding,
                trust_state=trust_state,
                claim_statement=claim.statement,
                claim_assumptions=claim.assumptions,
                claim_dependencies=claim.dependencies,
                parent_claim_id=claim.parent_claim_id,
                check_parent_claim_id=True,
                validate_files=True,
            )
            if receipt is None:
                raise SemanticPromotionError(decision)
            for path, digest in self.receipt_file_preconditions(receipt).items():
                prior = preconditions.get(path)
                if prior is not None and prior != digest:
                    raise ValueError(
                        f"semantic receipts disagree on evidence digest: {path}"
                    )
                preconditions[path] = digest
        return preconditions

    def summary(
        self,
        claim_ids: Iterable[str],
        *,
        trust_state: SemanticTrustState | None = None,
        claim_statements: dict[str, str] | None = None,
        claim_objects: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        contract = self.active_contract
        state = trust_state or SemanticTrustState.legacy()
        return {
            "schema_version": SEMANTICS_SCHEMA_VERSION,
            "present": self.present,
            "path": self.source_path,
            "source_sha256": self.source_sha256,
            "legacy_default": SemanticStatus.UNREVIEWED,
            "research_contract": (
                {
                    "active_version": contract.version,
                    "final_claim_id": contract.final_claim_id,
                    "canonical_text": contract.canonical_text,
                    "sha256": contract.sha256,
                }
                if contract is not None else None
            ),
            "registry": [
                self.entries[key].to_dict() for key in sorted(self.entries)
            ],
            "bridges": [
                self.bridges[key].to_dict() for key in sorted(self.bridges)
            ],
            "claims": {
                claim_id: self.evaluate_claim(
                    claim_id,
                    trust_state=state,
                    claim_statement=(claim_statements or {}).get(claim_id),
                    claim_assumptions=(
                        claim_objects[claim_id].assumptions
                        if claim_objects is not None and claim_id in claim_objects
                        else None
                    ),
                    claim_dependencies=(
                        claim_objects[claim_id].dependencies
                        if claim_objects is not None and claim_id in claim_objects
                        else None
                    ),
                    parent_claim_id=(
                        claim_objects[claim_id].parent_claim_id
                        if claim_objects is not None and claim_id in claim_objects
                        else None
                    ),
                    check_parent_claim_id=(
                        claim_objects is not None and claim_id in claim_objects
                    ),
                ).to_dict()
                for claim_id in sorted(set(claim_ids))
            },
            "trusted_state": {
                "opted_in": state.opted_in,
                "contract_head": state.contract_head,
                "verification_receipt_count": len(state.verification_receipts),
            },
            "terminal_gate": (
                "No unverified bridge into trusted final claims."
                if self.present else
                "Legacy compatibility: no semantic metadata, report UNREVIEWED without failing."
            ),
        }
