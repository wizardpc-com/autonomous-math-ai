from __future__ import annotations

import json
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.cli import main as cli_main
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import AutonomousController
from autonomous_math_research.models import ResearchTask
from autonomous_math_research.research_memory import (
    CampaignTheme,
    ExternalResult,
    ResearchMemoryStore,
)
from autonomous_math_research.storage import atomic_write_json, file_digest
from autonomous_math_research.validation import validate_project


ROOT = Path(__file__).resolve().parents[1]


class ResearchMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        shutil.copytree(ROOT / "templates" / "minimal-project", self.project)
        self.runtime = self.project / "autonomous"
        self.store = ResearchMemoryStore(self.project, self.runtime)
        self.graph_path = self.runtime / "state" / "claim_graph.json"
        self.trusted_path = self.runtime / "state" / "nightly_trusted.json"
        self.graph = ClaimGraph.load(self.graph_path)
        self.proof = self._write("proofs/informal/result.md", "exact proof\n")
        self.audit = self._write(
            "audit/mathematical/result-audit.md", "independent audit: PASS\n"
        )
        self.source = self.project / "claims" / "CLAIMS.md"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, text: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _ref(self, path: Path, kind: str) -> dict[str, str]:
        return {
            "kind": kind,
            "path": path.relative_to(self.project).as_posix(),
            "sha256": file_digest(path),
        }

    @staticmethod
    def _provenance(origin: str = "manual") -> dict[str, object]:
        return {
            "producer": "independent-research-thread",
            "origin": origin,
            "produced_at": "2026-08-28T00:00:00Z",
            "lineage": ["source-thread", "independent-audit"],
        }

    def _audit_block(
        self,
        *,
        direct: bool = True,
        reuse_audit_key: str | None = None,
    ) -> dict[str, object]:
        return {
            "verdict": "PASS" if direct else None,
            "independent": direct,
            "auditor": "fresh-independent-auditor" if direct else None,
            "audit_level": "RESULT",
            "policy_version": "result-audit-v1",
            "report_refs": [self._ref(self.audit, "independent_audit")]
            if direct else [],
            "reuse_audit_key": reuse_audit_key,
        }

    def _result(
        self,
        *,
        result_id: str = "result-scope-a",
        scope_id: str = "scope-a",
        statement: str = "The exact scope-a obligation holds.",
        classification: str = "AUDITED_EXTERNAL_RESULT",
        audit: dict[str, object] | None = None,
        dependencies: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "result_id": result_id,
            "exact_statement": statement,
            "scope_ids": [scope_id],
            "claim_ids": ["C_ROOT"],
            "representation_id": "rep:alpha",
            "dependencies": dependencies or [],
            "classification": classification,
            "conclusion": "PROVED",
            "maturity_level": "RESULT",
            "proof_refs": [self._ref(self.proof, "proof")],
            "certificate_refs": [],
            "source_refs": [self._ref(self.source, "canonical_source")],
            "audit": audit or self._audit_block(),
            "provenance": self._provenance(),
            "supersedes": [],
        }

    @staticmethod
    def _theme(
        *,
        scopes: list[str],
        obligations: list[dict[str, object]],
        methods: list[str] | None = None,
    ) -> CampaignTheme:
        return CampaignTheme.from_dict({
            "schema_version": 1,
            "theme_id": "theme-exact-scope",
            "title": "Exact scope campaign",
            "objective": "Resolve only the declared exact scopes.",
            "include_claim_ids": [],
            "include_scope_ids": scopes,
            "exclude_claim_ids": [],
            "exclude_scope_ids": [],
            "allowed_method_ids": methods or [],
            "forbidden_method_ids": [],
            "dependency_boundary": ["C_ROOT"],
            "combination_scope": "No cross-theme union is permitted.",
            "obligations": obligations,
        })

    def test_campaign_theme_v2_completion_policy_round_trips(self) -> None:
        payload = self._theme(scopes=["scope-a"], obligations=[]).to_dict(
            include_source=False
        )
        payload["schema_version"] = 2
        payload["completion_policy"] = {
            "max_accepted_candidates": 1,
            "post_candidate_mode": "AUDIT_ONLY",
            "max_valid_audit_attempts_per_candidate": 1,
            "terminal_audit_verdicts": ["PASS", "REJECT", "UNRESOLVED"],
            "terminal_research_outcomes": [
                "BLOCKED", "FALSIFIED", "OBLIGATION_EXHAUSTED",
            ],
        }

        theme = CampaignTheme.from_dict(payload)

        self.assertEqual(theme.to_dict(include_source=False), payload)

    def test_campaign_theme_v2_rejects_invalid_completion_policy(self) -> None:
        payload = self._theme(scopes=["scope-a"], obligations=[]).to_dict(
            include_source=False
        )
        payload["schema_version"] = 2
        payload["completion_policy"] = {
            "max_accepted_candidates": 1,
            "post_candidate_mode": "RESEARCH",
            "max_valid_audit_attempts_per_candidate": 1,
            "terminal_audit_verdicts": ["PASS"],
        }

        with self.assertRaisesRegex(ValueError, "AUDIT_ONLY"):
            CampaignTheme.from_dict(payload)

    def test_campaign_theme_v2_rejects_unknown_terminal_research_outcome(self) -> None:
        payload = self._theme(scopes=["scope-a"], obligations=[]).to_dict(
            include_source=False
        )
        payload["schema_version"] = 2
        payload["completion_policy"] = {
            "max_accepted_candidates": 1,
            "post_candidate_mode": "AUDIT_ONLY",
            "max_valid_audit_attempts_per_candidate": 1,
            "terminal_audit_verdicts": ["PASS"],
            "terminal_research_outcomes": ["MODEL_SAYS_DONE"],
        }

        with self.assertRaisesRegex(ValueError, "terminal research outcomes"):
            CampaignTheme.from_dict(payload)

    def _obligation(
        self,
        scope_id: str,
        *,
        dependencies: list[str] | None = None,
        methods: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "scope_id": scope_id,
            "claim_id": "C_ROOT",
            "exact_objective": f"Resolve {scope_id} and nothing broader.",
            "representation_id": "rep:alpha",
            "dependencies": dependencies or [],
            "allowed_method_ids": methods or [],
            "forbidden_method_ids": [],
        }

    def _save_result(self, name: str, payload: dict[str, object]) -> Path:
        path = self.runtime / "research_memory" / "external_results" / name
        atomic_write_json(path, payload)
        return path

    def _save_asset(self, name: str, payload: dict[str, object]) -> Path:
        path = self.runtime / "research_memory" / "assets" / name
        atomic_write_json(path, payload)
        return path

    def _reconcile(
        self,
        theme: CampaignTheme | None,
        *,
        phase: str = "MANUAL",
        epoch_id: str = "manual-test",
    ) -> tuple[dict[str, object], dict[str, object]]:
        return self.store.reconcile(
            graph=self.graph,
            claim_graph_path=self.graph_path,
            trusted_state_path=self.trusted_path,
            final_claim_id="C_ROOT",
            theme=theme,
            phase=phase,
            campaign_id="campaign-test",
            epoch_id=epoch_id,
        )

    def _make_strict_ready(self) -> None:
        (self.runtime / "semantics.json").unlink()
        manifest_path = self.runtime / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for paths in manifest["canonical_inputs"].values():
            paths.remove("autonomous/semantics.json")
        atomic_write_json(manifest_path, manifest)
        graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
        graph["claims"][0]["statement"] = "The exact neutral test claim holds."
        graph["claims"][0]["current_gaps"] = [
            "A proof or counterexample remains required."
        ]
        atomic_write_json(self.graph_path, graph)
        self.source.write_text(
            "# Claims\n\n- `C_ROOT`: The exact neutral test claim holds.\n",
            encoding="utf-8",
        )
        self.graph = ClaimGraph.load(self.graph_path)

    @staticmethod
    def _task(scope_id: str, method_id: str = "method-open") -> ResearchTask:
        return ResearchTask(
            task_id=f"task-{scope_id}-{method_id}",
            role="prover",
            target_claim="C_ROOT",
            exact_objective=f"Resolve {scope_id}",
            why_now="theme frontier selects it",
            dependencies=[],
            expected_information_gain="HIGH",
            research_impact="HIGH",
            estimated_cost_tier="LOW",
            required_files=[],
            stop_conditions=["prove, refute, or expose one exact blocker"],
            route_family=method_id,
            metadata={
                "allow_derived_claims": False,
                "independent_exploration": False,
            },
            input_closure={
                "canonical_object_id": scope_id,
                "target_representation_id": "rep:alpha",
                "required_bridge_ids": [],
                "required_source_ids": [],
                "source_bindings": [],
            },
        )

    def test_audited_external_result_closes_routing_without_claimgraph_promotion(self) -> None:
        graph_before = file_digest(self.graph_path)
        trusted_before = file_digest(self.trusted_path)
        self._save_result("scope-a.json", self._result())
        theme = self._theme(
            scopes=["scope-a"], obligations=[self._obligation("scope-a")]
        )

        state, delta = self._reconcile(theme)

        external = next(
            item for item in state["frontier_entries"]
            if item["entry_id"] == "external:result-scope-a"
        )
        obligation = next(
            item for item in state["frontier_entries"]
            if item["object_kind"] == "THEME_OBLIGATION"
        )
        self.assertEqual(external["route_status"], "DO_NOT_ROUTE")
        self.assertEqual(external["authority_status"], "PENDING_AUTONOMOUS_BINDING")
        self.assertEqual(obligation["route_status"], "DO_NOT_ROUTE")
        self.assertEqual(self.graph.claims["C_ROOT"].research_status, "OPEN")
        self.assertEqual(file_digest(self.graph_path), graph_before)
        self.assertEqual(file_digest(self.trusted_path), trusted_before)
        self.assertTrue(delta["newly_closed_obligations"])
        error = self.store.task_admission_error(
            self._task("scope-a"), theme=theme, state=state
        )
        self.assertIn("AUDITED_FRONTIER_DO_NOT_ROUTE", error or "")

    def test_maturity_levels_require_the_matching_audit_level(self) -> None:
        payload = self._result(result_id="theme-result", scope_id="theme-scope")
        payload["maturity_level"] = "THEME"
        path = self._save_result("theme-result.json", payload)

        with self.assertRaisesRegex(
            ValueError, "THEME maturity requires THEME_INTEGRATION audit"
        ):
            ExternalResult.load(self.project, path)

        payload["audit"]["audit_level"] = "THEME_INTEGRATION"
        atomic_write_json(path, payload)
        result = ExternalResult.load(self.project, path)
        self.assertEqual(result.maturity_level, "THEME")
        self.assertEqual(result.audit.audit_level, "THEME_INTEGRATION")

    def test_audit_key_reuses_only_exact_identity(self) -> None:
        first_path = self._save_result("a-first.json", self._result())
        first = ExternalResult.load(self.project, first_path)
        self._reconcile(None, epoch_id="first")
        second = self._result(
            result_id="result-scope-b",
            scope_id="scope-b",
            audit=self._audit_block(
                direct=False, reuse_audit_key=first.audit_key
            ),
        )
        self._save_result("b-reuse.json", second)

        state, _ = self._reconcile(None, epoch_id="second")

        reused = next(
            item for item in state["frontier_entries"]
            if item["entry_id"] == "external:result-scope-b"
        )
        self.assertEqual(reused["route_status"], "DO_NOT_ROUTE")
        self.assertTrue(reused["audit_reused"])
        changed = dict(second)
        changed["exact_statement"] = "A materially different statement."
        changed["result_id"] = "result-scope-c"
        changed["scope_ids"] = ["scope-c"]
        self._save_result("c-wrong-reuse.json", changed)
        state, _ = self._reconcile(None, epoch_id="third")
        rejected = next(
            item for item in state["frontier_entries"]
            if item["entry_id"] == "external:result-scope-c"
        )
        self.assertEqual(rejected["route_status"], "BLOCKED")
        self.assertEqual(rejected["route_reason"], "AUDIT_PASS_RECEIPT_REQUIRED")

        object_path = self.store.objects_root / f"{first.object_sha256}.json"
        object_path.unlink()
        with self.assertRaisesRegex(ValueError, "source object is missing"):
            self._reconcile(None, epoch_id="missing-source-object")

    def _asset_audit(self, status: str = "AUDITED") -> dict[str, object]:
        return {"status": status, **self._audit_block()}

    def _asset(
        self,
        *,
        asset_id: str,
        kind: str,
        scope_id: str,
        method_id: str | None = None,
        edge: dict[str, object] | None = None,
        audit_status: str = "AUDITED",
    ) -> dict[str, object]:
        audit = self._asset_audit(audit_status)
        if audit_status == "UNPROVED":
            audit = {
                "status": "UNPROVED",
                "verdict": None,
                "independent": False,
                "auditor": None,
                "audit_level": "RESULT",
                "policy_version": "hypothesis-v1",
                "report_refs": [],
                "reuse_audit_key": None,
            }
        return {
            "schema_version": 1,
            "asset_id": asset_id,
            "kind": kind,
            "title": f"Asset {asset_id}",
            "what_it_gives": f"Exact reusable content for {asset_id}.",
            "scope_ids": [scope_id],
            "claim_ids": ["C_ROOT"],
            "representation_ids": ["rep:alpha", "rep:beta"]
            if edge else ["rep:alpha"],
            "preconditions": ["Use only on the declared exact scope."],
            "do_not_use": ["Do not broaden to a different representation."],
            "inputs": ["canonical exact-scope object"],
            "outputs": ["auditable result"],
            "proof_refs": [self._ref(self.proof, "proof")],
            "code_refs": [],
            "certificate_refs": [],
            "source_refs": [self._ref(self.source, "canonical_source")],
            "audit": audit,
            "known_failure_modes": ["Fails outside the declared scope."],
            "dependencies": [],
            "method_id": method_id,
            "do_not_repeat": ["Do not rerun this exact method on scope-a."]
            if kind in {"NEGATIVE_RESULT", "KILL_GATE"} else [],
            "reopen_if": ["A new invariant changes the exact premise."]
            if kind in {"NEGATIVE_RESULT", "KILL_GATE"} else [],
            "representation_edge": edge,
            "provenance": self._provenance(),
            "supersedes": [],
        }

    def test_asset_registry_bridge_progressive_context_and_exact_kill_gate(self) -> None:
        edge = {
            "source_representation_id": "rep:alpha",
            "target_representation_id": "rep:beta",
            "conditions": ["nonzero localization factor"],
            "localization": "invert the declared factor only",
            "saturation": "saturate by the declared content",
            "content": "primitive part preserved",
            "exceptional_factors": ["factor-z"],
        }
        self._save_asset("bridge.json", self._asset(
            asset_id="bridge-alpha-beta",
            kind="REPRESENTATION_BRIDGE",
            scope_id="scope-a",
            edge=edge,
        ))
        self._save_asset("kill.json", self._asset(
            asset_id="kill-method-blocked",
            kind="KILL_GATE",
            scope_id="scope-a",
            method_id="method-blocked",
        ))
        self._save_asset("hypothesis.json", self._asset(
            asset_id="hypothesis-unrelated",
            kind="RESEARCH_HYPOTHESIS",
            scope_id="scope-other",
            audit_status="UNPROVED",
        ))
        theme = self._theme(
            scopes=["scope-a"],
            obligations=[self._obligation(
                "scope-a", methods=["method-blocked", "method-open"]
            )],
            methods=["method-blocked", "method-open"],
        )

        state, _ = self._reconcile(theme)

        self.assertIn(
            ("rep:alpha", "rep:beta"), self.store.routing_bridge_pairs()
        )
        blocked = self.store.task_admission_error(
            self._task("scope-a", "method-blocked"), theme=theme, state=state
        )
        self.assertIn("AUDITED_FRONTIER_KILL_GATED", blocked or "")
        self.assertIsNone(self.store.task_admission_error(
            self._task("scope-a", "method-open"), theme=theme, state=state
        ))
        bundle = self.store.relevant_context_bundle(
            claim_ids=["C_ROOT"],
            scope_ids=["scope-a"],
            representation_ids=["rep:alpha"],
            method_ids=["method-open"],
            state=state,
        )
        self.assertEqual(
            [item["asset_id"] for item in bundle["representation_bridges"]],
            ["bridge-alpha-beta"],
        )
        self.assertEqual(
            [item["asset_id"] for item in bundle["kill_gates"]],
            ["kill-method-blocked"],
        )
        self.assertEqual(bundle["hypotheses"], [])

    def test_unaudited_and_digest_mismatch_block_without_supplying_authority(self) -> None:
        unaudited = self._result(
            result_id="unaudited-result",
            scope_id="scope-u",
            classification="UNAUDITED_EXTERNAL_RESULT",
            audit=self._audit_block(direct=False),
        )
        mismatched = self._result(
            result_id="mismatched-result", scope_id="scope-m"
        )
        mismatched["proof_refs"] = [{
            **mismatched["proof_refs"][0], "sha256": "0" * 64,
        }]
        self._save_result("unaudited.json", unaudited)
        self._save_result("mismatched.json", mismatched)

        state, _ = self._reconcile(None)

        status = {
            item["result_id"]: (item["route_status"], item["route_reason"])
            for item in state["frontier_entries"]
            if item["object_kind"] == "EXTERNAL_RESULT"
        }
        self.assertEqual(
            status["unaudited-result"], ("BLOCKED", "RESULT_AUDIT_REQUIRED")
        )
        self.assertEqual(
            status["mismatched-result"],
            ("BLOCKED", "EVIDENCE_IDENTITY_MISMATCH"),
        )
        self.assertEqual(self.graph.claims["C_ROOT"].research_status, "OPEN")

    def test_strict_validation_accepts_fresh_current_without_writes(self) -> None:
        self._make_strict_ready()
        self._save_result("scope-a.json", self._result())
        self._reconcile(None)
        coordination = self.runtime / "coordination"
        before = {
            path.relative_to(coordination): path.read_bytes()
            for path in coordination.rglob("*")
            if path.is_file()
        }

        result = validate_project(self.project, strict=True)

        after = {
            path.relative_to(coordination): path.read_bytes()
            for path in coordination.rglob("*")
            if path.is_file()
        }
        self.assertTrue(result["valid"])
        self.assertEqual(result["audited_frontier"]["status"], "FRESH")
        self.assertEqual(after, before)

    def test_strict_validation_requires_current_when_manifests_exist(self) -> None:
        self._make_strict_ready()
        self._save_result("scope-a.json", self._result())

        with self.assertRaisesRegex(
            ValueError, "CURRENT.json is missing.*frontier rebuild",
        ):
            validate_project(self.project, strict=True)

    def test_strict_validation_requires_current_when_only_theme_exists(self) -> None:
        self._make_strict_ready()
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        atomic_write_json(
            theme_path,
            self._theme(
                scopes=["scope-a"],
                obligations=[self._obligation("scope-a")],
            ).to_dict(include_source=False),
        )

        with self.assertRaisesRegex(
            ValueError, "CURRENT.json is missing.*frontier rebuild",
        ):
            validate_project(self.project, strict=True)

    def test_strict_validation_rejects_changed_live_theme_source(self) -> None:
        self._make_strict_ready()
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        theme_payload = self._theme(
            scopes=["scope-a"],
            obligations=[self._obligation("scope-a")],
        ).to_dict(include_source=False)
        atomic_write_json(theme_path, theme_payload)
        campaign_root = self.runtime / "campaigns" / "campaign-pin"
        theme = self.store.load_or_pin_theme(campaign_root, theme_path)
        pin_path = campaign_root / "THEME.json"
        pin_before = pin_path.read_bytes()
        self._reconcile(theme)
        theme_payload["objective"] = "A changed source-authored objective."
        atomic_write_json(theme_path, theme_payload)

        with self.assertRaisesRegex(
            ValueError, "campaign theme source digest changed",
        ):
            validate_project(self.project, strict=True)
        self.assertEqual(pin_path.read_bytes(), pin_before)

    def test_strict_validation_rejects_missing_live_theme_source(self) -> None:
        self._make_strict_ready()
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        atomic_write_json(
            theme_path,
            self._theme(
                scopes=["scope-a"],
                obligations=[self._obligation("scope-a")],
            ).to_dict(include_source=False),
        )
        self._reconcile(self.store.load_theme(theme_path))
        theme_path.unlink()

        with self.assertRaisesRegex(ValueError, "campaign theme does not exist"):
            validate_project(self.project, strict=True)

    def test_strict_validation_rejects_theme_without_source_path(self) -> None:
        self._make_strict_ready()
        self._reconcile(self._theme(
            scopes=["scope-a"],
            obligations=[self._obligation("scope-a")],
        ))

        with self.assertRaisesRegex(ValueError, "theme source_path is missing"):
            validate_project(self.project, strict=True)

    def test_strict_validation_accepts_fresh_live_theme_without_writes(self) -> None:
        self._make_strict_ready()
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        atomic_write_json(
            theme_path,
            self._theme(
                scopes=["scope-a"],
                obligations=[self._obligation("scope-a")],
            ).to_dict(include_source=False),
        )
        self._reconcile(self.store.load_theme(theme_path))
        coordination = self.runtime / "coordination"
        before = {
            path.relative_to(coordination): path.read_bytes()
            for path in coordination.rglob("*")
            if path.is_file()
        }

        result = validate_project(self.project, strict=True)

        after = {
            path.relative_to(coordination): path.read_bytes()
            for path in coordination.rglob("*")
            if path.is_file()
        }
        self.assertTrue(result["valid"])
        self.assertGreaterEqual(result["audited_frontier"]["source_manifests"], 1)
        self.assertEqual(after, before)

    def test_strict_validation_rejects_stale_current_after_manifest_change(self) -> None:
        self._make_strict_ready()
        path = self._save_result("scope-a.json", self._result())
        self._reconcile(None)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["exact_statement"] = "The revised exact scope-a obligation holds."
        atomic_write_json(path, payload)

        output = io.StringIO()
        with redirect_stdout(output):
            status = cli_main([
                "validate", "--project", str(self.project), "--strict",
            ])
        validation = json.loads(output.getvalue())
        self.assertEqual(status, 2)
        self.assertFalse(validation["valid"])
        self.assertIn("CURRENT.json is stale", validation["error"])

    def test_strict_validation_rejects_current_with_newly_missing_evidence(self) -> None:
        self._make_strict_ready()
        self._save_result("scope-a.json", self._result())
        self._reconcile(None)
        authority_before = (
            file_digest(self.graph_path), file_digest(self.trusted_path),
        )
        self.proof.unlink()

        with self.assertRaisesRegex(
            ValueError, "evidence file is missing: proofs/informal/result.md",
        ):
            validate_project(self.project, strict=True)

        state, _ = self._reconcile(None, epoch_id="missing-evidence-rebuild")
        external = next(
            item for item in state["frontier_entries"]
            if item["entry_id"] == "external:result-scope-a"
        )
        self.assertEqual(
            (external["route_status"], external["route_reason"]),
            ("BLOCKED", "EVIDENCE_IDENTITY_MISMATCH"),
        )
        self.assertTrue(validate_project(self.project, strict=True)["valid"])
        self.assertEqual(
            (file_digest(self.graph_path), file_digest(self.trusted_path)),
            authority_before,
        )

    def test_strict_validation_rejects_current_with_new_digest_mismatch(self) -> None:
        self._make_strict_ready()
        self._save_result("scope-a.json", self._result())
        self._reconcile(None)
        self.proof.write_text("changed proof bytes\n", encoding="utf-8")

        with self.assertRaisesRegex(
            ValueError, "evidence digest changed: proofs/informal/result.md",
        ):
            validate_project(self.project, strict=True)

    def test_theme_dependency_wait_and_campaign_pin_are_fail_closed(self) -> None:
        theme = self._theme(
            scopes=["scope-wait"],
            obligations=[self._obligation(
                "scope-wait", dependencies=["missing-audited-dependency"]
            )],
        )
        theme_path = self.runtime / "research_memory" / "themes" / "wait.json"
        atomic_write_json(theme_path, theme.to_dict(include_source=False))
        campaign_root = self.runtime / "campaigns" / "campaign-pin"
        pinned = self.store.load_or_pin_theme(campaign_root, theme_path)
        self.assertEqual(pinned.theme_sha256, theme.theme_sha256)
        state, _ = self._reconcile(pinned)
        obligation = next(
            item for item in state["frontier_entries"]
            if item["object_kind"] == "THEME_OBLIGATION"
        )
        self.assertEqual(obligation["route_status"], "WAIT_DEPENDENCY")
        self.assertIn("missing-audited-dependency", obligation["missing_dependencies"])
        campaign_root.joinpath("CAMPAIGN.json").write_text("{}", encoding="utf-8")
        different = theme.to_dict(include_source=False)
        different["objective"] = "Different objective must not replace a pin."
        other_path = self.runtime / "research_memory" / "themes" / "other.json"
        atomic_write_json(other_path, different)
        with self.assertRaisesRegex(ValueError, "differs from the pinned"):
            self.store.load_or_pin_theme(campaign_root, other_path)

    def test_controller_admission_uses_pinned_theme_and_audited_frontier(self) -> None:
        self._save_result("scope-a.json", self._result())
        theme = self._theme(
            scopes=["scope-a"], obligations=[self._obligation("scope-a")]
        )
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        atomic_write_json(theme_path, theme.to_dict(include_source=False))
        config = load_config(self.project, require_manifest=True)
        controller = AutonomousController(
            config,
            run_id="controller-memory",
            campaign_id="controller-memory",
            campaign_hours=1,
            epoch_hours=1,
            mock=True,
            theme_path=theme_path,
        )

        controller._reconcile_research_memory("CAMPAIGN_START")

        self.assertEqual(controller.campaign_theme.theme_id, theme.theme_id)
        self.assertIsNotNone(controller.audited_frontier)
        self.assertIn(
            "AUDITED_FRONTIER_DO_NOT_ROUTE",
            controller._validate_director_task(self._task("scope-a")) or "",
        )
        events = controller.store.replay()
        self.assertTrue(any(
            item["kind"] == "AUDITED_FRONTIER_RECONCILED" for item in events
        ))

    def test_theme_scope_is_a_valid_legacy_input_closure_identity(self) -> None:
        (self.runtime / "semantics.json").unlink()
        manifest_path = self.runtime / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for role in manifest["canonical_inputs"].values():
            role.remove("autonomous/semantics.json")
        atomic_write_json(manifest_path, manifest)
        theme = self._theme(
            scopes=["scope-a"], obligations=[self._obligation("scope-a")]
        )
        theme_path = self.runtime / "research_memory" / "themes" / "scope-a.json"
        atomic_write_json(theme_path, theme.to_dict(include_source=False))
        controller = AutonomousController(
            load_config(self.project, require_manifest=True),
            run_id="controller-theme-closure",
            campaign_id="controller-theme-closure",
            campaign_hours=1,
            epoch_hours=1,
            mock=True,
            theme_path=theme_path,
        )
        controller._reconcile_research_memory("CAMPAIGN_START")
        task = self._task("scope-a")
        task.input_closure["target_representation_id"] = task.representation_id

        self.assertEqual(controller._input_closure_missing_ids(task), [])
        task.input_closure["canonical_object_id"] = "scope-outside-theme"
        self.assertEqual(
            controller._input_closure_missing_ids(task), ["scope-outside-theme"]
        )

    def test_frontier_cli_rebuild_inspect_and_context(self) -> None:
        self._save_result("scope-a.json", self._result())
        output = io.StringIO()
        with redirect_stdout(output):
            status = cli_main([
                "frontier", "rebuild", "--project", str(self.project),
            ])
        self.assertEqual(status, 0, output.getvalue())
        rebuilt = json.loads(output.getvalue())
        self.assertTrue(rebuilt["rebuilt"])
        self.assertFalse(rebuilt["canonical_authority_changed"])

        output = io.StringIO()
        with redirect_stdout(output):
            status = cli_main([
                "frontier", "context", "--project", str(self.project),
                "--claim", "C_ROOT", "--scope", "scope-a",
            ])
        self.assertEqual(status, 0, output.getvalue())
        bundle = json.loads(output.getvalue())
        self.assertEqual(bundle["authority"], "MINIMAL_RELEVANT_ROUTING_CONTEXT")
        self.assertTrue(bundle["frontier_entries"])


if __name__ == "__main__":
    unittest.main()
