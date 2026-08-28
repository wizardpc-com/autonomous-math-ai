from __future__ import annotations

import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.canonical_transition import (
    CanonicalTransitionStore,
    bytes_sha256,
    json_bytes,
)
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import (
    AutonomousController,
    build_mock_full_cycle_backend,
)
from autonomous_math_research.director_context import load_full_context_archive
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import (
    CandidateEvent,
    EvidenceLevel,
    Impact,
    LifecyclePhase,
    MathStatus,
    TrustStatus,
    ResearchTask,
    derived_claim_id,
    stable_hash,
)
from autonomous_math_research.policy import build_policy_manifest
from autonomous_math_research.representation import RepresentationContract
from autonomous_math_research.reconciliation import ReconciliationStore
from autonomous_math_research.semantic_alignment import (
    SEMANTIC_AUDIT_AUTHORITY_CONTEXT_SCHEMA_VERSION,
    SemanticAlignment,
    SemanticPromotionError,
    SemanticStatus,
    SemanticTrustState,
    build_validation_authority_head,
    text_sha256,
)
from autonomous_math_research.storage import EventStore, append_jsonl, file_digest
from autonomous_math_research.validation import validate_project


GOAL = "Every accepted input has the declared canonical result."
EVIDENCE = "audit/semantic-evidence.txt"
BRIDGES = [
    "bridge:object-to-representation",
    "bridge:representation-to-evidence",
    "bridge:evidence-to-validator",
    "bridge:validator-to-claim",
]
LEGACY_REPRESENTATION = RepresentationContract.legacy()


def payload_sha256(value: object) -> str:
    return sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def semantic_document(
    *,
    bridge_status: str | None = None,
    core_terms: list[str] | None = None,
    required_bridges: list[str] | None = None,
    endpoint: str = "claim:C_ROOT",
) -> dict:
    del bridge_status
    bridge_ids = BRIDGES
    return {
        "schema_version": 1,
        "research_contract": {
            "active_version": 1,
            "versions": [{
                "version": 1,
                "final_claim_id": "C_ROOT",
                "canonical_text": GOAL,
                "sha256": text_sha256(GOAL),
                "supersedes_sha256": None,
            }],
        },
        "registry": {
            "entries": [{
                "id": "object:accepted-input",
                "kind": "OBJECT",
                "canonical_name": "accepted input",
                "definition": "An input satisfying the frozen acceptance predicate.",
                "canonical_source": "claims/CLAIMS.md#C_ROOT",
                "aliases": ["admissible input"],
                "forbidden_confusions": ["syntactically valid input"],
                "allowed_representations": ["representation:normalized-record"],
            }],
        },
        "bridges": [
            {
                "id": bridge_ids[0],
                "source": "object:accepted-input",
                "target": "representation:normalized-record",
                "justification": "The encoding preserves the acceptance predicate.",
                "evidence": [EVIDENCE],
            },
            {
                "id": bridge_ids[1],
                "source": "representation:normalized-record",
                "target": "evidence:deterministic-run",
                "justification": "The run consumes exactly the normalized record.",
                "evidence": [EVIDENCE],
            },
            {
                "id": bridge_ids[2],
                "source": "evidence:deterministic-run",
                "target": "validator:reference-checker",
                "justification": "The checker validates the complete run receipt.",
                "evidence": [EVIDENCE],
            },
            {
                "id": bridge_ids[3],
                "source": "validator:reference-checker",
                "target": endpoint,
                "justification": "The checker contract has the same quantifiers as C_ROOT.",
                "evidence": [EVIDENCE],
            },
        ],
        "claims": [{
            "claim_id": "C_ROOT",
            "canonical_object": "object:accepted-input",
            "core_terms": core_terms if core_terms is not None else ["accepted input"],
            "required_bridges": (
                required_bridges if required_bridges is not None else bridge_ids
            ),
            "representation_id": LEGACY_REPRESENTATION.representation_id,
        }],
    }


def candidate(
    *,
    claim_id: str = "C_ROOT",
    statement: str = GOAL,
    dependencies: list[str] | None = None,
    bridge_ids: list[str] | None = None,
    candidate_type: str = "THEOREM_CANDIDATE",
    evidence_attempt_id: str | None = None,
) -> CandidateEvent:
    return CandidateEvent.from_dict({
        "event_id": f"candidate-{uuid4().hex}",
        "producer_thread_id": "producer-thread",
        "producer_task_id": "test-prover",
        "claim_id": claim_id,
        "type": candidate_type,
        "impact": Impact.CRITICAL,
        "concise_summary": "sealed test candidate",
        "exact_statement": statement,
        "artifact_paths": [],
        "reproduction_commands": [],
        "dependency_impact": [],
        "assumptions": [],
        "dependencies": list(dependencies or []),
        "parent_claim_id": None,
        "representation": LEGACY_REPRESENTATION.to_dict(),
        "bridge_representation_ids": [],
        "semantic_bridge_ids": list(BRIDGES if bridge_ids is None else bridge_ids),
        "evidence_receipts": [],
        "fingerprint_version": 2 if evidence_attempt_id is not None else 1,
        "evidence_attempt_id": evidence_attempt_id,
        "proposed_evidence_level": EvidenceLevel.E0_SPECULATIVE,
    })


class SemanticAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        runtime = Path(__file__).resolve().parent / "_runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.root = runtime / f"semantic-{uuid4().hex}"
        (self.root / "autonomous").mkdir(parents=True)
        self.path = self.root / "autonomous" / "semantics.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def write(self, document: dict) -> None:
        self.path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def opted_project(self) -> Path:
        project = initialize_project(self.root / f"project-{uuid4().hex}")
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        graph = ClaimGraph.load(graph_path)
        graph.claims["C_ROOT"].statement = GOAL
        graph.save()
        evidence = project / EVIDENCE
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("semantic bridge evidence\n", encoding="utf-8")
        semantic_path = project / "autonomous" / "semantics.json"
        semantic_path.write_text(
            json.dumps(semantic_document(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = project / "autonomous" / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for role in ("director", "research", "audit"):
            values = manifest["canonical_inputs"][role]
            if "autonomous/semantics.json" not in values:
                values.append("autonomous/semantics.json")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return project

    def install_legacy_transition(
        self, project: Path, *, committed: bool = True,
    ) -> tuple[CanonicalTransitionStore, str]:
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        trusted_path = project / "autonomous" / "state" / "nightly_trusted.json"
        store = CanonicalTransitionStore(
            project_root=project,
            runtime_root=project / "autonomous",
        )
        authorization = {
            "kind": "CANDIDATE_REGISTERED",
            "claim_id": "C_ROOT",
            "candidate_fingerprint": "legacy-fixture",
            "trust_upgrade": False,
        }
        current_id = store.commit(
            targets={
                graph_path: graph_path.read_bytes(),
                trusted_path: trusted_path.read_bytes(),
            },
            authorization=authorization,
            trusted_state_path=trusted_path,
            claim_graph_sha256=file_digest(graph_path),
        )
        prepared, terminal = deepcopy(store.records())
        trusted_uri = "project://autonomous/state/nightly_trusted.json"
        legacy_seed = {
            "authorization": authorization,
            "before": {
                item["path"]: item["before_sha256"]
                for item in prepared["targets"]
            },
            "after": {
                item["path"]: item["after_sha256"]
                for item in prepared["targets"]
                if item["path"] != trusted_uri
            },
            "claim_graph_sha256": file_digest(graph_path),
        }
        legacy_id = f"transition-{stable_hash(legacy_seed)[:24]}"
        (store.root / current_id).rename(store.root / legacy_id)

        prepared["schema_version"] = 1
        prepared["transition_id"] = legacy_id
        prepared.pop("preconditions")
        trusted_target = next(
            item for item in prepared["targets"] if item["path"] == trusted_uri
        )
        trusted_after = store.root / legacy_id / trusted_target["after_snapshot"]
        trusted_payload = json.loads(trusted_after.read_text(encoding="utf-8"))
        trusted_payload["last_transition_id"] = legacy_id
        trusted_bytes = json_bytes(trusted_payload)
        trusted_after.write_bytes(trusted_bytes)
        trusted_target["after_sha256"] = bytes_sha256(trusted_bytes)
        trusted_path.write_bytes(trusted_bytes)

        terminal["schema_version"] = 1
        terminal["transition_id"] = legacy_id
        records = [prepared, *([terminal] if committed else [])]
        store.ledger_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )
        return store, legacy_id

    def validation_authority_head(self, project: Path) -> str:
        config = load_config(project)
        return build_validation_authority_head(
            audit_config=dict(config.raw["audit"]),
            policy_manifest_sha256=str(
                build_policy_manifest(config)["manifest_sha256"]
            ),
        )

    def trust_and_receipt(
        self,
        project: Path,
        event: CandidateEvent | None = None,
        *,
        claim_graph: ClaimGraph | None = None,
        base_trust: SemanticTrustState | None = None,
    ) -> tuple[SemanticAlignment, SemanticTrustState, dict]:
        alignment = SemanticAlignment.load_optional(project)
        trust = alignment.reconcile_trust_state(
            base_trust or SemanticTrustState.legacy()
        )
        event = event or candidate()
        graph = claim_graph or ClaimGraph.load(
            project / "autonomous" / "state" / "claim_graph.json"
        )
        authority_head = self.validation_authority_head(project)
        config = load_config(project)
        policy_manifest_sha256 = str(
            build_policy_manifest(config)["manifest_sha256"]
        )
        dependency_shape_sha256 = payload_sha256(
            graph.canonical_dependency_shape(event.claim_id)
        )
        candidate_scope_sha256 = payload_sha256({
            "candidate_fingerprint": event.fingerprint,
            "dependency_shape_sha256": dependency_shape_sha256,
            "validation_authority_head": authority_head,
        })
        evidence_path = project / EVIDENCE
        digest = file_digest(evidence_path)
        audit_path = project / "autonomous" / "state" / "test-audit.json"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text('{"verdict":"PASS"}\n', encoding="utf-8")
        artifact_hashes = {f"project://{EVIDENCE}": digest}
        evidence_hashes = {EVIDENCE: digest}
        pass_scope = payload_sha256({
            "candidate_fingerprint": event.fingerprint,
            "claim_id": event.claim_id,
            "exact_statement": " ".join(event.exact_statement.split()),
            "representation_id": event.representation_id,
            "artifact_hashes": artifact_hashes,
            "semantic_evidence_hashes": evidence_hashes,
            "semantic_bridge_ids": list(event.semantic_bridge_ids),
            "candidate_scope_sha256": candidate_scope_sha256,
        })
        audit_receipt = {
            "audit_id": "audit-test",
            "audit_kind": "independent_auditor",
            "auditor_thread_id": "thread-test",
            "result_path": "project://autonomous/state/test-audit.json",
            "result_sha256": file_digest(audit_path),
            "statement_checked_sha256": text_sha256(event.exact_statement),
            "verified_evidence_level": EvidenceLevel.E0_SPECULATIVE,
            "validator_identity": "controller-independent-auditor",
            "validator_version": "audit-result-v2",
            "validator_config_sha256": "1" * 64,
            "pass_scope_sha256": pass_scope,
            "authority_context": {
                "schema_version": (
                    SEMANTIC_AUDIT_AUTHORITY_CONTEXT_SCHEMA_VERSION
                ),
                "validation_authority_head": authority_head,
                "validator_identity": "controller-independent-auditor",
                "validator_version": "audit-result-v2",
                "audit_config_sha256": payload_sha256(
                    dict(config.raw["audit"])
                ),
                "policy_manifest_sha256": policy_manifest_sha256,
                "validator_config_sha256": "1" * 64,
                "pass_scope_sha256": pass_scope,
            },
        }
        receipt = alignment.build_verification_receipt(
            trust_state=trust,
            candidate=event,
            artifact_hashes=artifact_hashes,
            evidence_hashes=evidence_hashes,
            domain_evidence_receipt_fingerprints=[],
            audit_receipts=[audit_receipt],
            producer_identity={
                "run_id": "run-test",
                "job_id": "job-test",
                "task_id": event.producer_task_id,
                "thread_id": str(event.producer_thread_id),
                "role": "prover",
            },
            claim_graph=graph,
            validation_authority_head=authority_head,
        )
        trust = trust.with_receipt(receipt)
        trust, _ = trust.with_terminal_binding(receipt, MathStatus.PROVED)
        return alignment, trust, receipt

    def persistent_controller(self, project: Path) -> AutonomousController:
        return AutonomousController(
            load_config(project),
            backend=build_mock_full_cycle_backend(
                statement=GOAL,
                evidence_path=str(project / EVIDENCE),
                semantic_bridge_ids=BRIDGES,
            ),
            mock=False,
        )

    def run_controller(self, controller: AutonomousController):
        controller.mock = True
        with patch(
            "autonomous_math_research.provenance._codex_capability",
            return_value={},
        ), patch(
            "autonomous_math_research.provenance._codex_identity",
            return_value={
                "codex_cli_version": "test-codex",
                "app_server_schema_sha256": "1" * 64,
                "app_server_required_protocol_sha256": "2" * 64,
            },
        ):
            return asyncio.run(controller.run(0.02))

    def corrupt_canonical_journal(
        self,
        project: Path,
        controller: AutonomousController,
        mode: str,
    ) -> None:
        ledger = controller.canonical_transitions.ledger_path
        if mode == "prepared_committed_mismatch":
            records = [json.loads(line) for line in ledger.read_text(
                encoding="utf-8",
            ).splitlines() if line.strip()]
            committed = next(
                item for item in reversed(records) if item.get("kind") == "COMMITTED"
            )
            committed["authorization"] = {
                **committed["authorization"], "kind": "FORGED_AUTHORIZATION",
            }
            ledger.write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True)
                    for item in records
                ) + "\n",
                encoding="utf-8",
            )
            return
        if mode == "forged_receipt_committed_only":
            _, forged_trust, _ = self.trust_and_receipt(project)
            trusted_path = project / "autonomous" / "state" / "nightly_trusted.json"
            trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
            trusted["semantic_alignment"] = forged_trust.to_payload()
            trusted_path.write_text(
                json.dumps(trusted, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            controller.semantic_trust = forged_trust
        append_jsonl(ledger, {
            "schema_version": 1,
            "kind": "COMMITTED",
            "transition_id": f"forged-{mode}",
            "timestamp": "2026-08-25T00:00:00Z",
            "recovered": False,
            "authorization": {
                "kind": "SEMANTIC_VERIFICATION_TRANSITION",
                "candidate_fingerprint": "f" * 64,
                "claim_id": "C_ROOT",
                "semantic_receipt_fingerprint": "e" * 64,
                "bridge_ids": BRIDGES,
            },
        })

    def test_goal_text_cannot_be_silently_modified(self) -> None:
        document = semantic_document()
        self.write(document)
        alignment = SemanticAlignment.load_optional(self.root)
        document["research_contract"]["versions"][0]["canonical_text"] += " Changed."
        document["research_contract"]["versions"][0]["sha256"] = text_sha256(
            document["research_contract"]["versions"][0]["canonical_text"]
        )
        self.write(document)

        with self.assertRaisesRegex(ValueError, "changed after.*frozen"):
            alignment.assert_unchanged()

    def test_legacy_journal_supports_first_strict_opt_in_and_zero_model_dry_run(
        self,
    ) -> None:
        project = self.opted_project()
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        graph_path.write_text(
            graph_path.read_text(encoding="utf-8").replace(
                "AMR_PLACEHOLDER: mathematical content is not configured.",
                "The semantic lifecycle fixture remains open.",
            ).replace(
                "AMR_PLACEHOLDER: research protocol and evidence contract are not configured.",
                "Use the deterministic fixture evidence and controller-owned receipt.",
            ),
            encoding="utf-8",
        )
        (project / "claims" / "CLAIMS.md").write_text(
            f"# Claims\n\n- `C_ROOT`: {GOAL}\n",
            encoding="utf-8",
        )
        store, transition_id = self.install_legacy_transition(project)
        canonical_paths = (
            project / "autonomous" / "state" / "claim_graph.json",
            project / "autonomous" / "state" / "nightly_trusted.json",
        )
        canonical_before = {path: path.read_bytes() for path in canonical_paths}
        journal_before = store.ledger_path.read_bytes()

        transactions = store.verified_committed_transactions(recover=False)
        self.assertEqual([item["transition_id"] for item in transactions], [transition_id])
        self.assertEqual(
            store.verified_prepared_record(transition_id, recover=False)["preconditions"],
            [],
        )
        result = validate_project(project, strict=True)
        self.assertTrue(result["valid"])
        self.assertEqual(result["model_turns_started"], 0)

        controller = self.persistent_controller(project)
        with patch(
            "autonomous_math_research.provenance._codex_capability",
            return_value={},
        ), patch(
            "autonomous_math_research.provenance._codex_identity",
            return_value={
                "codex_cli_version": "test-codex",
                "app_server_schema_sha256": "1" * 64,
                "app_server_required_protocol_sha256": "2" * 64,
            },
        ):
            dry_run = asyncio.run(controller.run(0.001, dry_run=True))
        self.assertEqual(dry_run.jobs_started, 0)
        self.assertEqual(store.recover(), [])
        self.assertEqual(
            {path: path.read_bytes() for path in canonical_paths},
            canonical_before,
        )
        self.assertEqual(store.ledger_path.read_bytes(), journal_before)

    def test_legacy_prepared_recovery_preserves_canonical_state(self) -> None:
        project = self.opted_project()
        store, transition_id = self.install_legacy_transition(
            project, committed=False,
        )
        canonical_paths = (
            project / "autonomous" / "state" / "claim_graph.json",
            project / "autonomous" / "state" / "nightly_trusted.json",
        )
        before = {path: path.read_bytes() for path in canonical_paths}

        self.assertEqual(store.recover(), [transition_id])
        self.assertEqual(
            {path: path.read_bytes() for path in canonical_paths},
            before,
        )
        self.assertEqual(store.records()[-1]["schema_version"], 1)

    def test_legacy_journal_rejects_incomplete_or_contradictory_history(self) -> None:
        project = self.opted_project()
        store, _ = self.install_legacy_transition(project)
        records = store.records()
        records[0]["targets"] = [
            item for item in records[0]["targets"]
            if not item["path"].endswith("nightly_trusted.json")
        ]
        store.ledger_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "exactly one trusted-state target"):
            store.verified_committed_transactions(recover=False)

    def test_legacy_journal_cannot_carry_semantic_trust_state(self) -> None:
        project = self.opted_project()
        store, transition_id = self.install_legacy_transition(project)
        records = store.records()
        trusted_target = next(
            item for item in records[0]["targets"]
            if item["path"].endswith("nightly_trusted.json")
        )
        trusted_after = (
            store.root / transition_id / trusted_target["after_snapshot"]
        )
        payload = json.loads(trusted_after.read_text(encoding="utf-8"))
        payload["semantic_alignment"] = {}
        changed = json_bytes(payload)
        trusted_after.write_bytes(changed)
        trusted_target["after_sha256"] = bytes_sha256(changed)
        (project / "autonomous" / "state" / "nightly_trusted.json").write_bytes(
            changed
        )
        store.ledger_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                for item in records
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "cannot authorize semantic trust"):
            store.verified_committed_transactions(recover=False)

    def test_v11_and_current_records_cannot_drop_preconditions(self) -> None:
        for schema_version in (1, 2):
            with self.subTest(schema_version=schema_version):
                project = self.opted_project()
                graph_path = project / "autonomous" / "state" / "claim_graph.json"
                trusted_path = (
                    project / "autonomous" / "state" / "nightly_trusted.json"
                )
                store = CanonicalTransitionStore(
                    project_root=project,
                    runtime_root=project / "autonomous",
                )
                store.commit(
                    targets={
                        graph_path: graph_path.read_bytes(),
                        trusted_path: trusted_path.read_bytes(),
                    },
                    authorization={
                        "kind": "CURRENT_FORMAT_FIXTURE",
                        "trust_upgrade": False,
                    },
                    trusted_state_path=trusted_path,
                    claim_graph_sha256=file_digest(graph_path),
                    preconditions={
                        project / "autonomous" / "semantics.json": file_digest(
                            project / "autonomous" / "semantics.json"
                        ),
                    },
                )
                records = store.records()
                for record in records:
                    record["schema_version"] = schema_version
                records[0].pop("preconditions")
                store.ledger_path.write_text(
                    "".join(
                        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                        for item in records
                    ),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    ValueError, "fields are invalid|identity does not match",
                ):
                    store.verified_committed_transactions(recover=False)

    def test_goal_change_requires_a_new_chained_version(self) -> None:
        document = semantic_document()
        previous = document["research_contract"]["versions"][0]["sha256"]
        changed_goal = GOAL + " Version two narrows the input domain explicitly."
        document["research_contract"]["active_version"] = 2
        document["research_contract"]["versions"].append({
            "version": 2,
            "final_claim_id": "C_ROOT",
            "canonical_text": changed_goal,
            "sha256": text_sha256(changed_goal),
            "supersedes_sha256": previous,
        })
        self.write(document)

        alignment = SemanticAlignment.load_optional(self.root)

        self.assertEqual(alignment.active_contract.version, 2)
        self.assertEqual(alignment.active_contract.supersedes_sha256, previous)

    def test_unregistered_core_term_is_flagged(self) -> None:
        self.write(semantic_document(core_terms=["new unregistered invariant"]))

        decision = SemanticAlignment.load_optional(self.root).evaluate_claim("C_ROOT")

        self.assertEqual(decision.status, SemanticStatus.TERM_AMBIGUOUS)
        self.assertTrue(any("unregistered core term" in item for item in decision.issues))

    def test_complete_declaration_does_not_create_trusted_verification(self) -> None:
        self.write(semantic_document())
        alignment = SemanticAlignment.load_optional(self.root)

        decision = alignment.evaluate_claim("C_ROOT", claim_statement=GOAL)

        self.assertEqual(decision.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("controller-owned" in item for item in decision.issues))

    def test_hand_filled_verified_is_not_a_declaration_field(self) -> None:
        document = semantic_document()
        document["bridges"][0]["status"] = "VERIFIED"
        self.write(document)

        with self.assertRaisesRegex(ValueError, "extra=.*status"):
            SemanticAlignment.load_optional(self.root)

    def test_validator_pass_does_not_replace_missing_claim_bridge(self) -> None:
        self.write(semantic_document(required_bridges=[
            "bridge:object-to-representation",
            "bridge:representation-to-evidence",
            "bridge:evidence-to-validator",
        ]))
        alignment = SemanticAlignment.load_optional(self.root)

        decision = alignment.evaluate_claim("C_ROOT", claim_statement=GOAL)

        self.assertEqual(decision.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("does not terminate" in item for item in decision.issues))

    def test_legacy_project_without_semantics_still_validates(self) -> None:
        legacy = initialize_project(self.root / "legacy")

        result = validate_project(legacy)

        self.assertTrue(result["valid"])
        semantic = result["semantic_alignment"]
        self.assertFalse(semantic["present"])
        self.assertEqual(semantic["claims"]["C_ROOT"]["status"], "UNREVIEWED")

    def test_controller_blocks_audited_proved_transition_with_open_bridge(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)

        with self.assertRaisesRegex(ValueError, "exactly match"):
            controller._validate_final_target_candidate(candidate(
                bridge_ids=BRIDGES[:-1],
            ))

    def test_candidate_receipt_binds_claim_representation_evidence_and_scope(self) -> None:
        project = self.opted_project()
        event_a = candidate()
        alignment, trust, receipt = self.trust_and_receipt(project, event_a)
        graph = ClaimGraph.load(project / "autonomous" / "state" / "claim_graph.json")
        authority_head = self.validation_authority_head(project)
        decision = alignment.evaluate_claim(
            "C_ROOT", trust_state=trust, claim_graph=graph,
            validation_authority_head=authority_head,
        )
        self.assertEqual(decision.status, SemanticStatus.VERIFIED)
        self.assertEqual(receipt["candidate_fingerprint"], event_a.fingerprint)
        self.assertEqual(receipt["representation_id"], event_a.representation_id)
        helper = deepcopy(graph.claims["C_ROOT"])
        helper.claim_id = "C_HELPER"
        helper.proof_obligations = []
        graph.claims["C_HELPER"] = helper
        graph.claims["C_ROOT"].dependencies = ["C_HELPER"]
        drifted = alignment.evaluate_claim(
            "C_ROOT",
            trust_state=trust,
            claim_graph=graph,
            validation_authority_head=authority_head,
        )
        self.assertEqual(drifted.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("dependency" in item for item in drifted.issues))

        event_b = candidate(evidence_attempt_id="attempt-" + "b" * 64)
        with self.assertRaisesRegex(SemanticPromotionError, "PASS scope"):
            alignment.build_verification_receipt(
                trust_state=trust,
                candidate=event_b,
                artifact_hashes=receipt["artifact_hashes"],
                evidence_hashes=receipt["evidence_hashes"],
                domain_evidence_receipt_fingerprints=[],
                audit_receipts=receipt["audit_receipts"],
                producer_identity=receipt["producer_identity"],
                claim_graph=ClaimGraph.load(
                    project / "autonomous" / "state" / "claim_graph.json"
                ),
                validation_authority_head=authority_head,
            )

    def test_candidate_a_receipt_cannot_authorize_candidate_b_terminal_state(self) -> None:
        project = self.opted_project()
        event_a = candidate()
        event_b = candidate(evidence_attempt_id="attempt-" + "b" * 64)
        alignment, trust, _ = self.trust_and_receipt(project, event_a)
        payload = trust.to_payload()
        terminal = payload["terminal_bindings"][-1]
        terminal["candidate_fingerprint"] = event_b.fingerprint
        unsigned = dict(terminal)
        unsigned.pop("transition_authorization_fingerprint")
        terminal["transition_authorization_fingerprint"] = payload_sha256(unsigned)
        mismatched = SemanticTrustState.from_trusted_payload({
            "semantic_alignment": payload,
        })
        graph = ClaimGraph.load(project / "autonomous" / "state" / "claim_graph.json")
        graph.claims["C_ROOT"].math_status = MathStatus.PROVED

        with self.assertRaisesRegex(
            SemanticPromotionError, "exact audited candidate|terminal candidate",
        ):
            alignment.require_final_claim_acceptance(
                "C_ROOT", claim_graph=graph, trust_state=mismatched,
                validation_authority_head=self.validation_authority_head(project),
            )

    def test_terminal_binding_rejects_a_different_receipt_fingerprint(self) -> None:
        project = self.opted_project()
        alignment, trust, _ = self.trust_and_receipt(project)
        payload = trust.to_payload()
        terminal = payload["terminal_bindings"][-1]
        terminal["semantic_receipt_fingerprint"] = "f" * 64
        unsigned = dict(terminal)
        unsigned.pop("transition_authorization_fingerprint")
        terminal["transition_authorization_fingerprint"] = payload_sha256(unsigned)
        mismatched = SemanticTrustState.from_trusted_payload({
            "semantic_alignment": payload,
        })
        graph = ClaimGraph.load(project / "autonomous" / "state" / "claim_graph.json")
        graph.claims["C_ROOT"].math_status = MathStatus.PROVED

        with self.assertRaises(SemanticPromotionError):
            alignment.require_final_claim_acceptance(
                "C_ROOT", claim_graph=graph, trust_state=mismatched,
                validation_authority_head=self.validation_authority_head(project),
            )

    def test_semantically_bound_subclaim_requires_complete_bridge_ids(self) -> None:
        project = self.opted_project()
        statement = "Every normalized helper record is stable."
        lemma_id = derived_claim_id("C_ROOT", statement, [], [])
        semantic_path = project / "autonomous" / "semantics.json"
        document = json.loads(semantic_path.read_text(encoding="utf-8"))
        subclaim_bridge = "bridge:validator-to-subclaim"
        document["bridges"].append({
            "id": subclaim_bridge,
            "source": "validator:reference-checker",
            "target": f"claim:{lemma_id}",
            "justification": "The same checker contract covers the registered helper claim.",
            "evidence": [EVIDENCE],
        })
        document["claims"].append({
            "claim_id": lemma_id,
            "canonical_object": "object:accepted-input",
            "core_terms": ["accepted input"],
            "required_bridges": [*BRIDGES[:-1], subclaim_bridge],
            "representation_id": LEGACY_REPRESENTATION.representation_id,
        })
        semantic_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        controller = self.persistent_controller(project)
        lemma = candidate(
            claim_id=lemma_id,
            statement=statement,
            bridge_ids=[],
            candidate_type="KEY_LEMMA",
        )
        lemma.parent_claim_id = "C_ROOT"

        with self.assertRaisesRegex(ValueError, "exactly match"):
            controller._validate_final_target_candidate(lemma)

    def test_semantic_receipt_requires_controller_owned_distinct_identities(self) -> None:
        project = self.opted_project()
        event = candidate()
        alignment, trust, receipt = self.trust_and_receipt(project, event)

        def rebuild(
            producer_identity: object,
            audit_receipts: list[dict],
        ) -> None:
            alignment.build_verification_receipt(
                trust_state=trust,
                candidate=event,
                artifact_hashes=receipt["artifact_hashes"],
                evidence_hashes=receipt["evidence_hashes"],
                domain_evidence_receipt_fingerprints=[],
                audit_receipts=audit_receipts,
                producer_identity=producer_identity,  # type: ignore[arg-type]
                claim_graph=ClaimGraph.load(
                    project / "autonomous" / "state" / "claim_graph.json"
                ),
                validation_authority_head=self.validation_authority_head(project),
            )

        cases: list[tuple[str, object, list[dict], str]] = []
        cases.append(("producer null", None, receipt["audit_receipts"], "producer"))
        auditor_null = deepcopy(receipt["audit_receipts"])
        auditor_null[0]["auditor_thread_id"] = None
        cases.append(("auditor null", receipt["producer_identity"], auditor_null, "auditor"))
        same_as_producer = deepcopy(receipt["audit_receipts"])
        same_as_producer[0]["auditor_thread_id"] = receipt["producer_identity"][
            "thread_id"
        ]
        cases.append((
            "auditor equals producer", receipt["producer_identity"],
            same_as_producer, "differ",
        ))
        duplicate_auditors = deepcopy(receipt["audit_receipts"])
        second = deepcopy(duplicate_auditors[0])
        second["audit_id"] = "audit-test-second"
        duplicate_auditors.append(second)
        cases.append((
            "duplicate auditors", receipt["producer_identity"],
            duplicate_auditors, "pairwise distinct",
        ))
        for label, producer, audits, message in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, message):
                    rebuild(producer, audits)

        with self.assertRaises(SemanticPromotionError):
            alignment.build_verification_receipt(
                trust_state=trust,
                candidate=candidate(claim_id="C_OTHER"),
                artifact_hashes=receipt["artifact_hashes"],
                evidence_hashes=receipt["evidence_hashes"],
                domain_evidence_receipt_fingerprints=[],
                audit_receipts=receipt["audit_receipts"],
                producer_identity=receipt["producer_identity"],
                claim_graph=ClaimGraph.load(
                    project / "autonomous" / "state" / "claim_graph.json"
                ),
                validation_authority_head=self.validation_authority_head(project),
            )

    def test_hand_written_trusted_receipt_lacks_canonical_transition_authority(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        _, forged_trust, _ = self.trust_and_receipt(project)
        trusted_path = project / "autonomous" / "state" / "nightly_trusted.json"
        trusted = json.loads(trusted_path.read_text(encoding="utf-8"))
        trusted["semantic_alignment"] = forged_trust.to_payload()
        trusted_path.write_text(
            json.dumps(trusted, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ValueError, "committed append-only journal|latest committed transition",
        ):
            validate_project(project)

    def test_journal_corruption_fails_validation_guard_and_controller_bootstrap(self) -> None:
        corruption_modes = (
            "forged_receipt_committed_only",
            "committed_without_prepared",
            "prepared_committed_mismatch",
        )
        entrypoints = ("validation", "direct_guard", "controller_bootstrap")
        for mode in corruption_modes:
            for entrypoint in entrypoints:
                with self.subTest(mode=mode, entrypoint=entrypoint):
                    project = self.opted_project()
                    controller = self.persistent_controller(project)
                    controller._refresh_canonical_state()
                    self.corrupt_canonical_journal(project, controller, mode)
                    with self.assertRaisesRegex(
                        ValueError,
                        "canonical transition|PREPARED|prepared|authorization",
                    ):
                        if entrypoint == "validation":
                            validate_project(project)
                        elif entrypoint == "controller_bootstrap":
                            self.persistent_controller(project)._refresh_canonical_state()
                        else:
                            event = candidate()
                            controller.graph.mark_candidate(event)
                            controller.graph.apply_audit_pass(event, 2, 2)

    def test_validator_identity_version_config_and_scope_are_assignment_bound(self) -> None:
        expected = {
            "schema_version": SEMANTIC_AUDIT_AUTHORITY_CONTEXT_SCHEMA_VERSION,
            "validation_authority_head": "0" * 64,
            "validator_identity": "controller-independent-auditor",
            "validator_version": "audit-result-v2",
            "audit_config_sha256": "3" * 64,
            "policy_manifest_sha256": "4" * 64,
            "validator_config_sha256": "1" * 64,
            "pass_scope_sha256": "2" * 64,
        }
        AutonomousController._require_semantic_audit_context(expected, expected)
        for key in expected:
            with self.subTest(key=key):
                changed = dict(expected)
                changed[key] = (
                    2 if key == "schema_version" else "different"
                )
                with self.assertRaisesRegex(ValueError, "changed after assignment"):
                    AutonomousController._require_semantic_audit_context(
                        changed, expected,
                    )

    def test_missing_or_stale_evidence_invalidates_a_trusted_receipt(self) -> None:
        project = self.opted_project()
        alignment, trust, _ = self.trust_and_receipt(project)
        graph = ClaimGraph.load(project / "autonomous" / "state" / "claim_graph.json")
        authority_head = self.validation_authority_head(project)
        evidence = project / EVIDENCE
        evidence.write_text("changed after verification\n", encoding="utf-8")
        decision = alignment.evaluate_claim(
            "C_ROOT", trust_state=trust, claim_graph=graph,
            validation_authority_head=authority_head,
        )
        self.assertEqual(decision.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("stale" in item for item in decision.issues))
        evidence.unlink()
        self.assertEqual(
            alignment.evaluate_claim(
                "C_ROOT", trust_state=trust, claim_graph=graph,
                validation_authority_head=authority_head,
            ).status,
            SemanticStatus.BRIDGE_OPEN,
        )

    def test_bridge_wrong_endpoint_discontinuity_and_order_fail_closed(self) -> None:
        project = self.opted_project()
        cases = [semantic_document(endpoint="claim:C_OTHER")]
        broken = semantic_document()
        broken["bridges"][1]["source"] = "representation:other"
        cases.append(broken)
        cases.append(semantic_document(
            required_bridges=[BRIDGES[1], BRIDGES[0], *BRIDGES[2:]],
        ))
        semantic_path = project / "autonomous" / "semantics.json"
        for index, document in enumerate(cases):
            with self.subTest(index=index):
                semantic_path.write_text(
                    json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                decision = SemanticAlignment.load_optional(project).evaluate_claim("C_ROOT")
                self.assertEqual(decision.status, SemanticStatus.BRIDGE_OPEN)

    def test_persistent_opt_in_blocks_rewrite_missing_and_corrupt_semantics(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        with patch(
            "autonomous_math_research.provenance._codex_capability",
            return_value={},
        ), patch(
            "autonomous_math_research.provenance._codex_identity",
            return_value={
                "codex_cli_version": "test-codex",
                "app_server_schema_sha256": "1" * 64,
                "app_server_required_protocol_sha256": "2" * 64,
            },
        ):
            controller._pin_run_inputs(0.01, True)

        original = semantic_document()
        rewritten = deepcopy(original)
        changed = GOAL + " Rewritten history."
        rewritten["research_contract"]["versions"][0]["canonical_text"] = changed
        rewritten["research_contract"]["versions"][0]["sha256"] = text_sha256(changed)
        semantic_path = project / "autonomous" / "semantics.json"
        semantic_path.write_text(
            json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "rewrote or removed"):
            self.persistent_controller(project)

        semantic_path.write_text(
            json.dumps(original, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        semantic_path.unlink()
        modes = [
            {"run_id": "fresh-missing"},
            {"run_id": controller.run_id, "resume": True},
            {
                "run_id": "new-epoch-missing",
                "campaign_id": controller.campaign_id,
                "previous_epoch_id": controller.run_id,
            },
        ]
        for mode in modes:
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(
                    ValueError,
                    "missing after persistent opt-in|manifest path does not exist",
                ):
                    AutonomousController(
                        load_config(project),
                        backend=build_mock_full_cycle_backend(),
                        mock=False,
                        **mode,
                    )

        semantic_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
            self.persistent_controller(project)

    def test_persistent_contract_history_accepts_only_a_new_tail(self) -> None:
        project = self.opted_project()
        first = self.persistent_controller(project)
        first._refresh_canonical_state()
        document = semantic_document()
        previous = document["research_contract"]["versions"][0]["sha256"]
        document["research_contract"]["active_version"] = 2
        document["research_contract"]["versions"].append({
            "version": 2,
            "final_claim_id": "C_ROOT",
            "canonical_text": GOAL,
            "sha256": text_sha256(GOAL),
            "supersedes_sha256": previous,
        })
        semantic_path = project / "autonomous" / "semantics.json"
        semantic_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        second = self.persistent_controller(project)
        second._refresh_canonical_state()
        self.assertEqual(len(second.semantic_trust.contract_history), 2)
        authorizations = [
            record["authorization"]
            for record in second.canonical_transitions.records()
            if record.get("kind") == "COMMITTED"
        ]
        self.assertEqual(authorizations[-1]["kind"], "SEMANTIC_CONTRACT_APPENDED")
        self.assertEqual(
            authorizations[-1]["contract_head"], second.semantic_trust.contract_head,
        )

    def test_startup_recovery_import_and_finalization_share_the_postcondition(self) -> None:
        project = self.opted_project()
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        graph = ClaimGraph.load(graph_path)
        graph.claims["C_ROOT"].math_status = MathStatus.PROVED
        graph.claims["C_ROOT"].trust_status = TrustStatus.AUDITED_NIGHTLY
        graph.save()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        with self.assertRaises(SemanticPromotionError):
            controller._write_compact_snapshot()
        self.assertFalse(controller._begin_finalization_if_resolved("startup"))
        with self.assertRaises(SemanticPromotionError):
            controller._commit_claim_state_transition(
                transition_kind="RECOVERY_OR_IMPORT_TEST",
                authorization={"trust_upgrade": False},
            )

    def test_direct_claim_graph_terminal_transition_is_guarded(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        event = candidate()
        controller.graph.mark_candidate(event)
        with self.assertRaises(SemanticPromotionError):
            controller.graph.apply_audit_pass(event, 2, 2)
        self.assertNotEqual(
            controller.graph.claims["C_ROOT"].math_status, MathStatus.PROVED,
        )

    def test_negative_terminal_does_not_claim_positive_semantic_entailment(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        refutation = candidate(
            bridge_ids=[], candidate_type="COUNTEREXAMPLE",
        )
        controller._validate_final_target_candidate(refutation)
        controller.graph.mark_candidate(refutation)

        controller.graph.apply_audit_pass(refutation, 2, 2)

        self.assertEqual(
            controller.graph.claims["C_ROOT"].math_status, MathStatus.FAILED,
        )
        self.assertEqual(
            controller.semantic_alignment.evaluate_claim(
                "C_ROOT", trust_state=controller.semantic_trust, claim_statement=GOAL,
            ).status,
            SemanticStatus.BRIDGE_OPEN,
        )

    def test_cross_domain_candidate_is_rejected_without_controller_failure(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        cross_domain = candidate(candidate_type="CERTIFICATE")
        target = controller._task_inbox(cross_domain.producer_task_id)
        source = controller.inbox.submit(cross_domain, target_root=target)

        asyncio.run(controller._poll_filesystem_candidates())
        with patch.object(
            controller, "_validate_candidate_provenance", return_value=None,
        ):
            asyncio.run(controller._process_candidate_queue())

        rejected = [
            item for item in controller.store.replay()
            if item["kind"] == "CANDIDATE_REJECTED"
            and item["payload"].get("event_id") == cross_domain.event_id
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn(
            "unsupported event type for math-research: CERTIFICATE",
            rejected[0]["payload"]["reason"],
        )
        self.assertFalse(source.exists())
        self.assertTrue(
            controller.inbox.processed_root.joinpath(
                f"{cross_domain.event_id}.{cross_domain.fingerprint[:12]}.json"
            ).is_file()
        )
        self.assertEqual(controller.graph.claims["C_ROOT"].math_status, MathStatus.OPEN)

    def test_cross_domain_candidate_recovery_scan_fails_closed(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        source_run_id = f"recovery-source-{uuid4().hex}"
        source_store = EventStore(
            controller.layout.run_dir(source_run_id) / "EVENTS.jsonl",
            source_run_id,
        )
        cross_domain = candidate(candidate_type="CERTIFICATE")
        controller.inbox.submit(cross_domain)
        controller.inbox.mark_processed(cross_domain, source_run_id)
        source_store.append("CANDIDATE_REJECTED", {
            "event_id": cross_domain.event_id,
            "fingerprint": cross_domain.fingerprint,
            "producer_task_id": cross_domain.producer_task_id,
            "claim_id": cross_domain.claim_id,
            "reason": "candidate statement does not match existing claim",
        })
        source_store.append("RUN_STOPPED", {"reason": "fixture terminal run"})

        with self.assertRaisesRegex(
            ValueError, "unsupported event type for math-research: CERTIFICATE",
        ):
            controller._load_recoverable_candidates(source_run_id)
        self.assertEqual(
            controller.graph.claims["C_ROOT"].math_status, MathStatus.OPEN,
        )

    def test_cross_domain_candidate_previous_epoch_import_fails_closed(self) -> None:
        project = self.opted_project()
        first = self.persistent_controller(project)
        original_queue = first._queue_next_audit

        def stop_after_first_pass(event: CandidateEvent) -> None:
            if first.audit_gate.pass_count(event.fingerprint) == 1:
                first.lifecycle.transition(
                    LifecyclePhase.DRAINING_EPOCH,
                    reason="test checkpoint before corrupted import",
                )
                first.scheduler_stop_reason = (
                    "test checkpoint before corrupted import"
                )
                return
            original_queue(event)

        first._queue_next_audit = stop_after_first_pass
        first_result = self.run_controller(first)
        self.assertFalse(first_result.internal_failure, first_result.stopped_reason)
        checkpoint_path = first.run_dir / "state" / "compact_snapshot.json"
        compact = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        corrupted = load_full_context_archive(checkpoint_path, compact)
        frontier = corrupted["candidate_audit_frontier"]
        self.assertEqual(len(frontier), 1)
        event_payload = dict(frontier[0]["event"])
        event_payload.pop("fingerprint", None)
        event_payload["type"] = "CERTIFICATE"
        cross_domain = CandidateEvent.from_dict(event_payload)
        frontier[0]["event"] = cross_domain.to_dict()

        second = AutonomousController(
            load_config(project),
            backend=build_mock_full_cycle_backend(
                statement=GOAL,
                evidence_path=str(project / EVIDENCE),
                semantic_bridge_ids=BRIDGES,
            ),
            mock=False,
            run_id=f"corrupted-import-{uuid4().hex}",
            campaign_id=first.campaign_id,
            previous_epoch_id=first.run_id,
            campaign_hours=first.campaign_hours,
            epoch_hours=first.epoch_hours,
        )
        with patch(
            "autonomous_math_research.controller.load_full_context_archive",
            return_value=corrupted,
        ):
            second_result = self.run_controller(second)

        self.assertTrue(second_result.internal_failure)
        self.assertIn(
            "unsupported event type for math-research: CERTIFICATE",
            second_result.stopped_reason,
        )
        self.assertFalse(any(
            item["kind"] == "EPOCH_CHECKPOINT_IMPORTED"
            for item in second.store.replay()
        ))
        self.assertEqual(
            second.graph.claims["C_ROOT"].math_status, MathStatus.OPEN,
        )

    def test_dynamic_subclaim_can_close_but_cannot_support_final_unreviewed(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        statement = "Every normalized helper record is stable."
        lemma_id = derived_claim_id("C_ROOT", statement, [], [])
        lemma = candidate(
            claim_id=lemma_id,
            statement=statement,
            bridge_ids=[],
        )
        lemma.parent_claim_id = "C_ROOT"
        controller.graph.mark_candidate(lemma)
        controller._commit_claim_state_transition(
            transition_kind="CANDIDATE_REGISTERED",
            authorization={
                "candidate_fingerprint": lemma.fingerprint,
                "claim_id": lemma.claim_id,
                "trust_upgrade": False,
            },
        )
        controller.graph.apply_audit_pass(lemma, 2, 2)
        controller._commit_claim_state_transition(
            transition_kind="AUDITED_CLAIM_TRANSITION",
            authorization={
                "candidate_fingerprint": lemma.fingerprint,
                "claim_id": lemma.claim_id,
                "terminal_status": MathStatus.PROVED,
                "semantic_receipt_fingerprint": None,
                "semantic_terminal_binding": None,
                "trust_upgrade": False,
            },
        )
        self.assertEqual(controller.graph.claims[lemma_id].math_status, MathStatus.PROVED)
        self.assertEqual(
            controller.semantic_alignment.evaluate_claim(lemma_id).status,
            SemanticStatus.UNREVIEWED,
        )
        lemma_obligation_id = controller.graph.claims[lemma_id].proof_obligations[
            0
        ].obligation_id
        controller.graph.claims["C_ROOT"].proof_obligations[0].dependencies = [
            lemma_obligation_id
        ]
        self.assertEqual(
            controller.graph.resolved_dependency_claim_ids("C_ROOT"),
            (lemma_id,),
        )
        controller._commit_claim_state_transition(
            transition_kind="PROOF_OBLIGATION_DEPENDENCY_UPDATED",
            authorization={"claim_id": "C_ROOT", "trust_upgrade": False},
        )

        result = self.run_controller(controller)

        self.assertFalse(result.internal_failure, result.stopped_reason)
        self.assertFalse(controller.final_conjecture_proved)
        persisted = ClaimGraph.load(
            project / "autonomous" / "state" / "claim_graph.json"
        )
        self.assertEqual(persisted.claims[lemma_id].math_status, MathStatus.PROVED)
        self.assertNotEqual(persisted.claims["C_ROOT"].math_status, MathStatus.PROVED)
        validation = validate_project(project)
        self.assertTrue(validation["valid"])
        self.assertEqual(
            validation["semantic_alignment"]["claims"][lemma_id]["status"],
            SemanticStatus.UNREVIEWED,
        )

    def test_claim_and_obligation_ids_are_globally_disjoint_before_lifecycle(self) -> None:
        project = self.opted_project()
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        payload = json.loads(graph_path.read_text(encoding="utf-8"))
        root = next(
            item for item in payload["claims"] if item["claim_id"] == "C_ROOT"
        )
        self.assertEqual(root["math_status"], MathStatus.OPEN)
        root["proof_obligations"][0]["obligation_id"] = "C_ROOT"
        graph_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "globally disjoint"):
            ClaimGraph.load(graph_path)
        with self.assertRaisesRegex(ValueError, "globally disjoint"):
            self.persistent_controller(project)
        with self.assertRaisesRegex(ValueError, "globally disjoint"):
            validate_project(project)

        persisted = json.loads(graph_path.read_text(encoding="utf-8"))
        persisted_root = next(
            item for item in persisted["claims"]
            if item["claim_id"] == "C_ROOT"
        )
        self.assertEqual(persisted_root["math_status"], MathStatus.OPEN)

    def test_semantic_head_change_blocks_commit(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller._refresh_canonical_state()
        semantic_path = project / "autonomous" / "semantics.json"
        original_commit = controller.canonical_transitions.commit

        def mutate_after_controller_check(**kwargs):
            semantic_path.write_text(
                semantic_path.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            return original_commit(**kwargs)

        with patch.object(
            controller.canonical_transitions,
            "commit",
            side_effect=mutate_after_controller_check,
        ):
            with self.assertRaisesRegex(ValueError, "precondition changed before prepare"):
                controller._commit_claim_state_transition(
                    transition_kind="TOCTOU_TEST",
                    authorization={"trust_upgrade": False},
                )

    def test_crash_recovery_rechecks_semantic_preconditions(self) -> None:
        project = initialize_project(self.root / "recovery-precondition")
        semantic_path = project / "autonomous" / "semantics.json"
        semantic_path.write_text("{}\n", encoding="utf-8")
        graph_path = project / "autonomous" / "state" / "claim_graph.json"
        trusted_path = project / "autonomous" / "state" / "nightly_trusted.json"
        store = CanonicalTransitionStore(
            project_root=project,
            runtime_root=project / "autonomous",
        )
        digest = file_digest(semantic_path)
        with patch.object(store, "recover", return_value=[]):
            store.commit(
                targets={
                    graph_path: graph_path.read_bytes(),
                    trusted_path: trusted_path.read_bytes(),
                },
                authorization={"kind": "TOCTOU_RECOVERY_TEST"},
                trusted_state_path=trusted_path,
                claim_graph_sha256=file_digest(graph_path),
                preconditions={semantic_path: digest},
            )
        semantic_path.write_text('{"changed":true}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "precondition changed before commit"):
            store.recover()

    def test_controller_creates_persistent_candidate_bound_verification(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        controller.mock = True
        with patch(
            "autonomous_math_research.provenance._codex_capability",
            return_value={},
        ), patch(
            "autonomous_math_research.provenance._codex_identity",
            return_value={
                "codex_cli_version": "test-codex",
                "app_server_schema_sha256": "1" * 64,
                "app_server_required_protocol_sha256": "2" * 64,
            },
        ):
            result = asyncio.run(controller.run(0.02))
        self.assertFalse(result.internal_failure, result.stopped_reason)
        self.assertTrue(controller.final_conjecture_proved)
        trusted = json.loads(
            (project / "autonomous" / "state" / "nightly_trusted.json").read_text(
                encoding="utf-8",
            )
        )
        receipts = trusted["semantic_alignment"]["verification_receipts"]
        self.assertEqual(len(receipts), 1)
        receipt = receipts[0]
        self.assertEqual(receipt["claim_id"], "C_ROOT")
        self.assertEqual(receipt["bridge_ids"], BRIDGES)
        self.assertEqual(len(receipt["audit_receipts"]), 2)
        self.assertEqual(
            receipt["dependency_shape"],
            controller.graph.canonical_dependency_shape("C_ROOT"),
        )
        self.assertEqual(
            receipt["validation_authority_head"],
            controller._current_validation_authority_head(),
        )
        parsed = SemanticTrustState.from_trusted_payload(trusted)
        self.assertEqual(
            parsed.verification_receipts[0]["receipt_fingerprint"],
            receipt["receipt_fingerprint"],
        )
        terminal = parsed.terminal_bindings[-1]
        self.assertEqual(terminal["claim_id"], "C_ROOT")
        self.assertEqual(terminal["terminal_status"], MathStatus.PROVED)
        self.assertEqual(
            terminal["candidate_fingerprint"], receipt["candidate_fingerprint"],
        )
        self.assertEqual(
            terminal["semantic_receipt_fingerprint"], receipt["receipt_fingerprint"],
        )
        self.assertEqual(terminal["representation_id"], receipt["representation_id"])
        self.assertEqual(
            terminal["representation_content_sha256"],
            receipt["representation_id"].removeprefix("rep:"),
        )
        self.assertEqual(
            terminal["validation_authority_head"],
            receipt["validation_authority_head"],
        )
        self.assertEqual(
            terminal["dependency_shape_sha256"],
            payload_sha256(receipt["dependency_shape"]),
        )
        authorizations = controller.canonical_transitions.verified_committed_authorizations()
        terminal_authorization = next(
            item for item in reversed(authorizations)
            if item.get("semantic_terminal_binding") is not None
        )
        self.assertEqual(terminal_authorization["kind"], "AUDITED_CLAIM_TRANSITION")
        self.assertEqual(terminal_authorization["semantic_terminal_binding"], terminal)
        self.assertEqual(
            terminal_authorization["candidate_fingerprint"],
            receipt["candidate_fingerprint"],
        )
        self.assertEqual(
            terminal_authorization["semantic_receipt_fingerprint"],
            receipt["receipt_fingerprint"],
        )
        self.assertEqual(
            terminal_authorization["transition_authorization_fingerprint"],
            terminal["transition_authorization_fingerprint"],
        )
        self.assertTrue(receipt["producer_identity"]["thread_id"])
        auditor_ids = [
            item["auditor_thread_id"] for item in receipt["audit_receipts"]
        ]
        self.assertEqual(len(auditor_ids), len(set(auditor_ids)))
        self.assertNotIn(receipt["producer_identity"]["thread_id"], auditor_ids)

    def test_authority_change_mid_audit_requires_two_fresh_passes(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        original_queue = controller._queue_next_audit
        switched = False
        checked_partial_v3_frontier = False
        semantic_version = patch(
            "autonomous_math_research.semantic_alignment."
            "SEMANTIC_VALIDATOR_VERSION",
            "audit-result-v3",
        )
        controller_version = patch(
            "autonomous_math_research.controller.SEMANTIC_VALIDATOR_VERSION",
            "audit-result-v3",
        )

        def queue_with_authority_change(event: CandidateEvent) -> None:
            nonlocal switched, checked_partial_v3_frontier
            pass_events = [
                item for item in controller.store.replay()
                if item["kind"] == "AUDIT_RECORDED"
                and item["payload"].get("verdict") == "PASS"
            ]
            if len(pass_events) == 1 and not switched:
                semantic_version.start()
                controller_version.start()
                switched = True
            elif len(pass_events) == 2 and switched:
                checked_partial_v3_frontier = True
                self.assertEqual(
                    controller.audit_gate.pass_count(event.fingerprint), 1,
                )
                self.assertNotEqual(
                    controller.graph.claims["C_ROOT"].math_status,
                    MathStatus.PROVED,
                )
                self.assertEqual(
                    controller.semantic_trust.verification_receipts, (),
                )
            original_queue(event)

        controller._queue_next_audit = queue_with_authority_change
        try:
            result = self.run_controller(controller)
            self.assertFalse(result.internal_failure, result.stopped_reason)
            self.assertTrue(controller.final_conjecture_proved)
            self.assertTrue(switched)
            self.assertTrue(checked_partial_v3_frontier)

            pass_events = [
                item for item in controller.store.replay()
                if item["kind"] == "AUDIT_RECORDED"
                and item["payload"].get("verdict") == "PASS"
            ]
            self.assertEqual(len(pass_events), 3)
            versions = [
                item["payload"]["semantic_authority_context"][
                    "validator_version"
                ]
                for item in pass_events
            ]
            self.assertEqual(versions, [
                "audit-result-v2", "audit-result-v3", "audit-result-v3",
            ])
            old_audit_id = pass_events[0]["payload"]["audit_id"]
            current_head = controller._current_validation_authority_head()
            receipt = controller.semantic_trust.verification_receipts[-1]
            self.assertEqual(len(receipt["audit_receipts"]), 2)
            self.assertNotIn(
                old_audit_id,
                {item["audit_id"] for item in receipt["audit_receipts"]},
            )
            for audit_receipt in receipt["audit_receipts"]:
                context = audit_receipt["authority_context"]
                self.assertEqual(
                    context["validator_version"], "audit-result-v3",
                )
                self.assertEqual(
                    context["validation_authority_head"], current_head,
                )
            self.assertTrue(any(
                item["kind"] == "SEMANTIC_AUDIT_PASSES_STALE"
                for item in controller.store.replay()
            ))

            persisted = ClaimGraph.load(
                project / "autonomous" / "state" / "claim_graph.json"
            )
            self.assertEqual(
                persisted.claims["C_ROOT"].math_status, MathStatus.PROVED,
            )
            trusted = json.loads(
                (
                    project / "autonomous" / "state" / "nightly_trusted.json"
                ).read_text(encoding="utf-8")
            )
            parsed = SemanticTrustState.from_trusted_payload(trusted)
            self.assertEqual(
                parsed.terminal_bindings[-1]["semantic_receipt_fingerprint"],
                receipt["receipt_fingerprint"],
            )
            self.assertTrue(validate_project(project)["valid"])
        finally:
            if switched:
                controller_version.stop()
                semantic_version.stop()

    def test_checkpoint_import_preserves_old_audit_authority_context(self) -> None:
        project = self.opted_project()
        first = self.persistent_controller(project)
        original_queue = first._queue_next_audit
        checkpoint_requested = False

        def stop_after_first_pass(event: CandidateEvent) -> None:
            nonlocal checkpoint_requested
            if (
                not checkpoint_requested
                and first.audit_gate.pass_count(event.fingerprint) == 1
            ):
                checkpoint_requested = True
                first.lifecycle.transition(
                    LifecyclePhase.DRAINING_EPOCH,
                    reason="test checkpoint after authority-v2 PASS",
                )
                first.scheduler_stop_reason = (
                    "test checkpoint after authority-v2 PASS"
                )
                return
            original_queue(event)

        first._queue_next_audit = stop_after_first_pass
        first_result = self.run_controller(first)
        self.assertFalse(first_result.internal_failure, first_result.stopped_reason)
        self.assertFalse(first.final_conjecture_proved)
        self.assertTrue(checkpoint_requested)
        self.assertTrue(validate_project(project)["valid"])

        checkpoint_path = first.run_dir / "state" / "compact_snapshot.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        frontier = checkpoint["candidate_audit_frontier"]
        self.assertEqual(len(frontier), 1)
        old_result = frontier[0]["audit_results"][0]
        old_audit_id = old_result["audit_id"]
        self.assertEqual(
            old_result["semantic_authority_context"]["validator_version"],
            "audit-result-v2",
        )
        old_head = old_result["semantic_authority_context"][
            "validation_authority_head"
        ]

        with patch(
            "autonomous_math_research.semantic_alignment."
            "SEMANTIC_VALIDATOR_VERSION",
            "audit-result-v3",
        ), patch(
            "autonomous_math_research.controller.SEMANTIC_VALIDATOR_VERSION",
            "audit-result-v3",
        ):
            second = AutonomousController(
                load_config(project),
                backend=build_mock_full_cycle_backend(
                    statement=GOAL,
                    evidence_path=str(project / EVIDENCE),
                    semantic_bridge_ids=BRIDGES,
                ),
                mock=False,
                run_id=f"authority-v3-{uuid4().hex}",
                campaign_id=first.campaign_id,
                previous_epoch_id=first.run_id,
                campaign_hours=first.campaign_hours,
                epoch_hours=first.epoch_hours,
            )
            second_result = self.run_controller(second)
            self.assertFalse(
                second_result.internal_failure, second_result.stopped_reason,
            )
            self.assertTrue(second.final_conjecture_proved)
            self.assertTrue(any(
                item["kind"] == "EPOCH_CHECKPOINT_IMPORTED"
                for item in second.store.replay()
            ))
            current_head = second._current_validation_authority_head()
            self.assertNotEqual(old_head, current_head)
            receipt = second.semantic_trust.verification_receipts[-1]
            self.assertEqual(len(receipt["audit_receipts"]), 2)
            self.assertNotIn(
                old_audit_id,
                {item["audit_id"] for item in receipt["audit_receipts"]},
            )
            for audit_receipt in receipt["audit_receipts"]:
                self.assertEqual(
                    audit_receipt["authority_context"]["validator_version"],
                    "audit-result-v3",
                )
                self.assertEqual(
                    audit_receipt["authority_context"][
                        "validation_authority_head"
                    ],
                    current_head,
                )
            persisted = ClaimGraph.load(
                project / "autonomous" / "state" / "claim_graph.json"
            )
            self.assertEqual(
                persisted.claims["C_ROOT"].math_status, MathStatus.PROVED,
            )
            trusted = json.loads(
                (
                    project / "autonomous" / "state" / "nightly_trusted.json"
                ).read_text(encoding="utf-8")
            )
            parsed = SemanticTrustState.from_trusted_payload(trusted)
            self.assertEqual(
                parsed.terminal_bindings[-1]["semantic_receipt_fingerprint"],
                receipt["receipt_fingerprint"],
            )
            self.assertTrue(validate_project(project)["valid"])

        preserved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(
            preserved["candidate_audit_frontier"][0]["audit_results"][0][
                "semantic_authority_context"
            ]["validation_authority_head"],
            old_head,
        )

    def test_repromotion_requires_a_new_audited_transition(self) -> None:
        project = self.opted_project()
        first = self.persistent_controller(project)
        first_result = self.run_controller(first)
        self.assertFalse(first_result.internal_failure, first_result.stopped_reason)
        self.assertTrue(first.final_conjecture_proved)
        first_receipt = first.semantic_trust.verification_receipts[-1]

        refutation = candidate(
            bridge_ids=[], candidate_type="COUNTEREXAMPLE",
            evidence_attempt_id="attempt-" + "f" * 64,
        )
        first.graph.mark_candidate(refutation)
        first._commit_claim_state_transition(
            transition_kind="CANDIDATE_REGISTERED",
            authorization={
                "candidate_fingerprint": refutation.fingerprint,
                "claim_id": refutation.claim_id,
                "trust_upgrade": False,
            },
        )
        first.graph.apply_audit_pass(refutation, 2, 2)
        first._commit_claim_state_transition(
            transition_kind="AUDITED_CLAIM_TRANSITION",
            authorization={
                "candidate_fingerprint": refutation.fingerprint,
                "claim_id": refutation.claim_id,
                "terminal_status": MathStatus.FAILED,
                "semantic_receipt_fingerprint": None,
                "semantic_terminal_binding": None,
                "trust_upgrade": False,
            },
        )
        self.assertEqual(first.graph.claims["C_ROOT"].math_status, MathStatus.FAILED)

        first.graph.claims["C_ROOT"].math_status = MathStatus.PROVED
        with self.assertRaisesRegex(
            ValueError, "current audited canonical transition",
        ):
            first._commit_claim_state_transition(
                transition_kind="MANUAL_REPROMOTION",
                authorization={"claim_id": "C_ROOT", "trust_upgrade": False},
            )
        persisted = ClaimGraph.load(
            project / "autonomous" / "state" / "claim_graph.json"
        )
        self.assertEqual(persisted.claims["C_ROOT"].math_status, MathStatus.FAILED)
        self.assertTrue(validate_project(project)["valid"])

        (project / EVIDENCE).write_text(
            "semantic bridge evidence for a fresh audited attempt\n",
            encoding="utf-8",
        )
        second = self.persistent_controller(project)
        second._refresh_canonical_state()
        fresh_candidate = candidate(
            evidence_attempt_id="attempt-" + "2" * 64,
        )
        second.graph.mark_candidate(fresh_candidate)
        _, updated_trust, second_receipt = self.trust_and_receipt(
            project,
            fresh_candidate,
            claim_graph=second.graph,
            base_trust=second.semantic_trust,
        )
        producer_identity = second_receipt["producer_identity"]
        second._commit_claim_state_transition(
            transition_kind="CANDIDATE_REGISTERED",
            authorization={
                "candidate_fingerprint": fresh_candidate.fingerprint,
                "claim_id": fresh_candidate.claim_id,
                "producer_identity": producer_identity,
                "trust_upgrade": False,
            },
        )
        second.semantic_trust = updated_trust
        terminal_binding = updated_trust.terminal_bindings[-1]
        authorization = {
            "candidate_fingerprint": fresh_candidate.fingerprint,
            "claim_id": fresh_candidate.claim_id,
            "terminal_status": MathStatus.PROVED,
            "semantic_receipt_fingerprint": second_receipt["receipt_fingerprint"],
            "representation_id": second_receipt["representation_id"],
            "representation_content_sha256": terminal_binding[
                "representation_content_sha256"
            ],
            "bridge_ids": second_receipt["bridge_ids"],
            "semantic_terminal_binding": terminal_binding,
            "candidate_scope_sha256": terminal_binding["candidate_scope_sha256"],
            "dependency_shape_sha256": terminal_binding[
                "dependency_shape_sha256"
            ],
            "transition_authorization_fingerprint": terminal_binding[
                "transition_authorization_fingerprint"
            ],
            "trust_upgrade": False,
        }
        second._pending_semantic_authorization = (
            second._canonical_transition_authorization(
                "AUDITED_CLAIM_TRANSITION", authorization,
            )
        )
        try:
            second.graph.apply_audit_pass(fresh_candidate, 2, 2)
            second._commit_claim_state_transition(
                transition_kind="AUDITED_CLAIM_TRANSITION",
                authorization=authorization,
                preconditions=second.semantic_alignment.receipt_file_preconditions(
                    second_receipt
                ),
            )
        finally:
            second._pending_semantic_authorization = None
        self.assertEqual(
            second.graph.claims["C_ROOT"].math_status, MathStatus.PROVED,
        )
        self.assertEqual(len(second.semantic_trust.verification_receipts), 2)
        self.assertNotEqual(
            first_receipt["candidate_fingerprint"],
            second_receipt["candidate_fingerprint"],
        )
        self.assertEqual(
            second.semantic_trust.terminal_bindings[-1][
                "semantic_receipt_fingerprint"
            ],
            second_receipt["receipt_fingerprint"],
        )
        self.assertTrue(validate_project(project)["valid"])

    def test_historical_reconciliation_is_atomic_claim_local_and_idempotent(self) -> None:
        project = self.opted_project()
        child_id = "C_COMPONENT"
        child_statement = "The audited component holds under the frozen assumptions."
        child_bridges = [
            "bridge:component-object-to-representation",
            "bridge:component-representation-to-evidence",
            "bridge:component-evidence-to-validator",
            "bridge:component-validator-to-claim",
        ]
        graph = ClaimGraph.load(project / "autonomous" / "state" / "claim_graph.json")
        child = CandidateEvent.from_dict({
            "event_id": "historical-component",
            "producer_thread_id": None,
            "producer_task_id": "historical-reconciliation",
            "claim_id": child_id,
            "type": "THEOREM_CANDIDATE",
            "impact": "HIGH",
            "concise_summary": "historically proved component",
            "exact_statement": child_statement,
            "artifact_paths": [f"project://{EVIDENCE}"],
            "reproduction_commands": [],
            "dependency_impact": [],
            "parent_claim_id": "C_ROOT",
            "assumptions": [],
            "dependencies": [],
            "representation": LEGACY_REPRESENTATION.to_dict(),
            "bridge_representation_ids": [],
            "semantic_bridge_ids": child_bridges,
            "evidence_receipts": [],
            "proposed_evidence_level": EvidenceLevel.E0_SPECULATIVE,
        })
        graph.mark_candidate(child)
        graph.claims[child_id].math_status = MathStatus.OPEN
        graph.claims[child_id].current_gaps = ["historical authority drift"]
        graph.save()

        document = semantic_document()
        document["registry"]["entries"].append({
            "id": "object:component",
            "kind": "OBJECT",
            "canonical_name": "component",
            "definition": "The exact component in the frozen decomposition.",
            "canonical_source": "claims/CLAIMS.md#C_ROOT",
            "aliases": [],
            "forbidden_confusions": ["the full root claim"],
            "allowed_representations": ["representation:component-record"],
        })
        nodes = [
            ("object:component", "representation:component-record"),
            ("representation:component-record", "evidence:component-proof"),
            ("evidence:component-proof", "validator:component-audit"),
            ("validator:component-audit", f"claim:{child_id}"),
        ]
        document["bridges"].extend({
            "id": bridge_id,
            "source": source,
            "target": target,
            "justification": "The frozen component mapping preserves the exact statement.",
            "evidence": [EVIDENCE],
        } for bridge_id, (source, target) in zip(child_bridges, nodes))
        document["claims"].append({
            "claim_id": child_id,
            "canonical_object": "object:component",
            "core_terms": ["component"],
            "required_bridges": child_bridges,
            "representation_id": LEGACY_REPRESENTATION.representation_id,
        })
        semantic_path = project / "autonomous" / "semantics.json"
        semantic_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        historical_audit = project / "audit" / "historical-component-audit.md"
        historical_audit.write_text(
            "Independent historical audit: PASS for the exact component.\n",
            encoding="utf-8",
        )
        alignment = SemanticAlignment.load_optional(project)
        store = ReconciliationStore(
            project_root=project, runtime_root=project / "autonomous",
        )
        stage, appended = store.stage({
            "schema_version": 1,
            "kind": "NARROW_DERIVED_SUBCLAIM",
            "target_claim_id": "C_ROOT",
            "target_obligation_id": None,
            "candidate": child.to_dict(),
            "historical_proof_paths": [EVIDENCE],
            "historical_audit_paths": [
                "audit/historical-component-audit.md"
            ],
        }, claim_graph=graph, semantic_alignment=alignment)
        self.assertTrue(appended)

        config = load_config(project)
        controller = AutonomousController(
            config,
            backend=MockCodexBackend(),
            reconciliation_id=stage.reconciliation_id,
            mock=False,
        )
        pending = ResearchTask(
            task_id="ordinary-component-research",
            role="prover",
            target_claim=child_id,
            exact_objective="Re-prove the component.",
            why_now="should be gated",
            dependencies=[],
            expected_information_gain="HIGH",
            research_impact="HIGH",
            estimated_cost_tier="LOW",
            required_files=[],
            stop_conditions=["return"],
            metadata={
                "allow_derived_claims": False,
                "independent_exploration": False,
            },
        )
        self.assertIn(
            "authority drift",
            controller._validate_director_task(pending),
        )
        self.assertEqual(controller._authority_sync_status("C_ROOT"), "IN_SYNC")
        with patch.object(controller, "_queue_next_audit", return_value=None):
            interrupted = self.run_controller(controller)
        self.assertTrue(interrupted.internal_failure)
        self.assertIsNone(store.applied_marker(stage.reconciliation_id))

        controller = AutonomousController(
            config,
            backend=MockCodexBackend(),
            reconciliation_id=stage.reconciliation_id,
            mock=False,
        )
        result = self.run_controller(controller)
        self.assertFalse(result.internal_failure, result.stopped_reason)
        self.assertEqual(result.stopped_reason, "historical reconciliation applied")

        committed = ClaimGraph.load(
            project / "autonomous" / "state" / "claim_graph.json"
        )
        self.assertEqual(committed.claims[child_id].math_status, MathStatus.PROVED)
        self.assertEqual(committed.claims["C_ROOT"].math_status, MathStatus.OPEN)
        self.assertTrue(any(
            item["claim_id"] == child_id
            for item in controller.semantic_trust.terminal_bindings
        ))
        summary = store.summary(transition_store=controller.canonical_transitions)
        self.assertNotIn(child_id, summary["claim_status"])
        authorizations = controller.canonical_transitions.verified_committed_authorizations()
        self.assertEqual(1, sum(
            item.get("reconciliation_id") == stage.reconciliation_id
            and item.get("kind") == "AUDITED_CLAIM_TRANSITION"
            for item in authorizations
        ))

        second = AutonomousController(
            config,
            backend=MockCodexBackend(),
            reconciliation_id=stage.reconciliation_id,
            mock=False,
        )
        second_result = self.run_controller(second)
        self.assertFalse(second_result.internal_failure, second_result.stopped_reason)
        self.assertEqual(second_result.jobs_started, 0)
        self.assertEqual(
            second_result.stopped_reason,
            "historical reconciliation already in sync",
        )
        self.assertIn(
            "CLAIM_ALREADY_TERMINAL",
            second._validate_director_task(pending),
        )
        latest_graph = ClaimGraph.load(
            project / "autonomous" / "state" / "claim_graph.json"
        )
        terminal_stage, _ = store.stage({
            "schema_version": 1,
            "kind": "TERMINAL_CLAIM",
            "target_claim_id": "C_ROOT",
            "target_obligation_id": None,
            "candidate": candidate().to_dict(),
            "historical_proof_paths": [EVIDENCE],
            "historical_audit_paths": [
                "audit/historical-component-audit.md"
            ],
        }, claim_graph=latest_graph, semantic_alignment=alignment)
        partial_stage, _ = store.stage({
            "schema_version": 1,
            "kind": "PARTIAL_OBLIGATION",
            "target_claim_id": "C_ROOT",
            "target_obligation_id": latest_graph.claims[
                "C_ROOT"
            ].proof_obligations[0].obligation_id,
            "candidate": child.to_dict(),
            "historical_proof_paths": [EVIDENCE],
            "historical_audit_paths": [
                "audit/historical-component-audit.md"
            ],
        }, claim_graph=latest_graph, semantic_alignment=alignment)
        self.assertEqual(terminal_stage.reconciliation_kind, "TERMINAL_CLAIM")
        self.assertEqual(partial_stage.reconciliation_kind, "PARTIAL_OBLIGATION")

    def test_validation_authority_change_invalidates_authoritative_receipt(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        result = self.run_controller(controller)
        self.assertFalse(result.internal_failure, result.stopped_reason)
        self.assertTrue(controller.final_conjecture_proved)
        self.assertTrue(validate_project(project)["valid"])

        authority_patches = (
            (
                "autonomous_math_research.semantic_alignment."
                "SEMANTIC_AUDITOR_IDENTITY",
                "controller-independent-auditor-v2",
            ),
            (
                "autonomous_math_research.semantic_alignment."
                "SEMANTIC_VALIDATOR_VERSION",
                "audit-result-v3",
            ),
        )
        for target, replacement in authority_patches:
            with self.subTest(target=target), patch(target, replacement):
                with self.assertRaisesRegex(ValueError, "authority|validator"):
                    validate_project(project)
                with self.assertRaisesRegex(
                    (ValueError, SemanticPromotionError), "authority|validator",
                ):
                    controller._semantic_final_postcondition()

        original_threshold = controller.config.raw["audit"]["immediate_threshold"]
        controller.config.raw["audit"]["immediate_threshold"] = "MEDIUM"
        try:
            with self.assertRaisesRegex(SemanticPromotionError, "authority"):
                controller._semantic_final_postcondition()
        finally:
            controller.config.raw["audit"]["immediate_threshold"] = original_threshold

        config_path = project / "autonomous" / "config.yaml"
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        changed_config = deepcopy(raw_config)
        changed_config["audit"]["immediate_threshold"] = "MEDIUM"
        config_path.write_text(
            json.dumps(changed_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SemanticPromotionError, "authority"):
            validate_project(project)

        changed_policy = deepcopy(raw_config)
        worker = changed_policy["policy"]["one_shot_compute_worker"]
        worker["estimated_tokens"] = int(worker["estimated_tokens"]) + 1
        config_path.write_text(
            json.dumps(changed_policy, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SemanticPromotionError, "authority"):
            validate_project(project)

        config_path.write_text(
            json.dumps(raw_config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.assertTrue(validate_project(project)["valid"])

    def test_predispatch_input_closure_reports_exact_missing_ids(self) -> None:
        project = self.opted_project()
        controller = self.persistent_controller(project)
        task = ResearchTask(
            task_id="closure-check",
            role="prover",
            target_claim="C_ROOT",
            exact_objective="Use the frozen canonical inputs.",
            why_now="pre-dispatch regression",
            dependencies=[],
            expected_information_gain="HIGH",
            research_impact="HIGH",
            estimated_cost_tier="LOW",
            required_files=[],
            stop_conditions=["return"],
            metadata={
                "allow_derived_claims": False,
                "independent_exploration": False,
            },
        )
        self.assertEqual(
            controller._input_closure_missing_ids(task),
            sorted(["object:accepted-input", *BRIDGES]),
        )
        task.required_files = ["claims/CLAIMS.md", EVIDENCE]
        self.assertEqual(controller._input_closure_missing_ids(task), [])
        task.input_closure = {
            "canonical_object_id": "object:accepted-input",
            "target_representation_id": LEGACY_REPRESENTATION.representation_id,
            "required_bridge_ids": BRIDGES,
            "required_source_ids": ["source:branch", "source:localizer"],
            "source_bindings": [
                {"source_id": "source:branch", "path": EVIDENCE},
                {
                    "source_id": "source:localizer",
                    "path": "sources/missing-localizer.json",
                },
            ],
        }
        self.assertEqual(
            controller._input_closure_missing_ids(task),
            ["source:localizer"],
        )


if __name__ == "__main__":
    unittest.main()
