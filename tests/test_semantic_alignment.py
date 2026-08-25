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
from autonomous_math_research.canonical_transition import CanonicalTransitionStore
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import (
    AutonomousController,
    build_mock_full_cycle_backend,
)
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import (
    CandidateEvent,
    EvidenceLevel,
    Impact,
    MathStatus,
    TrustStatus,
    derived_claim_id,
)
from autonomous_math_research.representation import RepresentationContract
from autonomous_math_research.semantic_alignment import (
    SemanticAlignment,
    SemanticPromotionError,
    SemanticStatus,
    SemanticTrustState,
    text_sha256,
)
from autonomous_math_research.storage import file_digest
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
) -> CandidateEvent:
    return CandidateEvent.from_dict({
        "event_id": f"candidate-{uuid4().hex}",
        "producer_thread_id": None,
        "producer_task_id": "test-prover",
        "claim_id": claim_id,
        "type": "THEOREM_CANDIDATE",
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

    def trust_and_receipt(
        self,
        project: Path,
        event: CandidateEvent | None = None,
    ) -> tuple[SemanticAlignment, SemanticTrustState, dict]:
        alignment = SemanticAlignment.load_optional(project)
        trust = alignment.reconcile_trust_state(SemanticTrustState.legacy())
        event = event or candidate()
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
        }
        receipt = alignment.build_verification_receipt(
            trust_state=trust,
            candidate=event,
            artifact_hashes=artifact_hashes,
            evidence_hashes=evidence_hashes,
            domain_evidence_receipt_fingerprints=[],
            audit_receipts=[audit_receipt],
        )
        return alignment, trust.with_receipt(receipt), receipt

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
        decision = alignment.evaluate_claim(
            "C_ROOT", trust_state=trust, claim_statement=GOAL,
        )
        self.assertEqual(decision.status, SemanticStatus.VERIFIED)
        self.assertEqual(receipt["candidate_fingerprint"], event_a.fingerprint)
        self.assertEqual(receipt["representation_id"], event_a.representation_id)
        drifted = alignment.evaluate_claim(
            "C_ROOT",
            trust_state=trust,
            claim_statement=GOAL,
            claim_assumptions=[],
            claim_dependencies=["C_HELPER"],
            parent_claim_id=None,
            check_parent_claim_id=True,
        )
        self.assertEqual(drifted.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("dependencies" in item for item in drifted.issues))

        event_b = candidate(dependencies=["C_HELPER"])
        with self.assertRaisesRegex(SemanticPromotionError, "PASS scope"):
            alignment.build_verification_receipt(
                trust_state=trust,
                candidate=event_b,
                artifact_hashes=receipt["artifact_hashes"],
                evidence_hashes=receipt["evidence_hashes"],
                domain_evidence_receipt_fingerprints=[],
                audit_receipts=receipt["audit_receipts"],
            )

        with self.assertRaises(SemanticPromotionError):
            alignment.build_verification_receipt(
                trust_state=trust,
                candidate=candidate(claim_id="C_OTHER"),
                artifact_hashes=receipt["artifact_hashes"],
                evidence_hashes=receipt["evidence_hashes"],
                domain_evidence_receipt_fingerprints=[],
                audit_receipts=receipt["audit_receipts"],
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

        with self.assertRaisesRegex(ValueError, "committed append-only journal"):
            validate_project(project)

    def test_validator_identity_version_config_and_scope_are_assignment_bound(self) -> None:
        expected = {
            "validator_identity": "controller-independent-auditor",
            "validator_version": "audit-result-v2",
            "validator_config_sha256": "1" * 64,
            "pass_scope_sha256": "2" * 64,
        }
        AutonomousController._require_semantic_audit_context(expected, expected)
        for key in expected:
            with self.subTest(key=key):
                changed = dict(expected)
                changed[key] = "different"
                with self.assertRaisesRegex(ValueError, "changed after assignment"):
                    AutonomousController._require_semantic_audit_context(
                        changed, expected,
                    )

    def test_missing_or_stale_evidence_invalidates_a_trusted_receipt(self) -> None:
        project = self.opted_project()
        alignment, trust, _ = self.trust_and_receipt(project)
        evidence = project / EVIDENCE
        evidence.write_text("changed after verification\n", encoding="utf-8")
        decision = alignment.evaluate_claim(
            "C_ROOT", trust_state=trust, claim_statement=GOAL,
        )
        self.assertEqual(decision.status, SemanticStatus.BRIDGE_OPEN)
        self.assertTrue(any("stale" in item for item in decision.issues))
        evidence.unlink()
        self.assertEqual(
            alignment.evaluate_claim(
                "C_ROOT", trust_state=trust, claim_statement=GOAL,
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
        controller.graph.apply_audit_pass(lemma, 1, 1)
        self.assertEqual(controller.graph.claims[lemma_id].math_status, MathStatus.PROVED)
        self.assertEqual(
            controller.semantic_alignment.evaluate_claim(lemma_id).status,
            SemanticStatus.UNREVIEWED,
        )

        _, trusted, receipt = self.trust_and_receipt(
            project, candidate(dependencies=[lemma_id]),
        )
        controller.semantic_trust = trusted
        controller._commit_claim_state_transition(
            transition_kind="SEMANTIC_VERIFICATION_TRANSITION",
            authorization={
                "candidate_fingerprint": receipt["candidate_fingerprint"],
                "claim_id": receipt["claim_id"],
                "semantic_receipt_fingerprint": receipt["receipt_fingerprint"],
                "bridge_ids": receipt["bridge_ids"],
                "trust_upgrade": False,
            },
            preconditions=controller.semantic_alignment.receipt_file_preconditions(
                receipt,
            ),
        )
        controller.graph.claims["C_ROOT"].dependencies = [lemma_id]
        controller.graph.claims["C_ROOT"].math_status = MathStatus.PROVED
        controller.graph.claims["C_ROOT"].trust_status = TrustStatus.AUDITED_NIGHTLY
        with self.assertRaises(SemanticPromotionError) as caught:
            controller._semantic_final_postcondition()
        self.assertEqual(caught.exception.decision.claim_id, lemma_id)
        self.assertEqual(caught.exception.decision.status, SemanticStatus.UNREVIEWED)

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
        parsed = SemanticTrustState.from_trusted_payload(trusted)
        self.assertEqual(
            parsed.verification_receipts[0]["receipt_fingerprint"],
            receipt["receipt_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
