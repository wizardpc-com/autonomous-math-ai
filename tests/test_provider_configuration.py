from __future__ import annotations

import asyncio
import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from autonomous_math_research.cli import main as cli_main
from autonomous_math_research.backend import MockCodexBackend
from autonomous_math_research.config import load_config
from autonomous_math_research.controller import AutonomousController
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import ResearchTask, TokenUsage, stable_hash
from autonomous_math_research.provider_backend import (
    OpenAICompatibleBackend, ProviderRouterBackend, ProviderTransportError,
)
from autonomous_math_research.provider_config import redact_config
from autonomous_math_research.resources import schema_resource
from autonomous_math_research.schema import load_schema
from autonomous_math_research.validation import validate_project
from autonomous_math_research.token_governor import TokenGovernor
from autonomous_math_research.storage import file_digest


RUNTIME = Path(__file__).resolve().parent / "_runtime"
REPO = Path(__file__).resolve().parents[1]


class ProviderConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME / f"provider-config-{uuid4().hex}"
        self.project = self.root / "neutral-project"
        initialize_project(
            self.project, project_id="neutral-project", final_claim_id="C_FINAL",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _profile(self, value: dict, name: str = "profile.json") -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _cli(self, args: list[str]) -> tuple[int, dict]:
        output = StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_main(args)
        return code, json.loads(output.getvalue())

    def test_default_profile_is_codex_and_new_budgets_are_separate(self) -> None:
        config = load_config(self.project)
        self.assertEqual(config.raw["schema_version"], 12)
        self.assertEqual(config.raw["campaign"], {"hours": 5.0, "epoch_hours": 2.0})
        self.assertEqual(config.raw["execution"], {"fast_mode": False})
        self.assertEqual(config.profile_name, "codex-app-server-default")
        self.assertTrue(all(
            route["provider"] == "codex" for route in config.raw["models"].values()
        ))
        self.assertEqual(config.raw["budgets"]["global_tokens"], 500_000_000)
        self.assertEqual(config.raw["budgets"]["mechanical_tokens"], 1_500_000_000)
        self.assertIsNone(config.max_mechanical_subworkers)
        self.assertFalse(
            config.raw["policy"]["one_shot_compute_worker"]["recursive_spawn_allowed"]
        )
        self.assertEqual(
            config.raw["policy"]["one_shot_compute_worker"]["fallback_condition"],
            "provider_execution_failure",
        )
        self.assertEqual(config.research_max_turns("prover"), 4)
        self.assertEqual(config.research_max_turns("falsifier"), 3)
        self.assertEqual(config.research_max_turns("explorer"), 3)
        self.assertIn(
            "uncached_input_tokens",
            config.raw["providers"]["codex"]["capabilities"]["usage_mapping"],
        )
        self.assertTrue(all(
            route["service_tier"] is None
            for route in config.raw["models"].values()
        ))

    def test_init_cli_accepts_explicit_ids_and_strict_cli_stays_zero_model(self) -> None:
        explicit = self.root / "explicit-project"
        code, payload = self._cli([
            "init", str(explicit), "--project-id", "declared-project",
            "--final-claim-id", "FINAL.CLAIM",
        ])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["project_id"], "declared-project")
        self.assertEqual(payload["final_claim_id"], "FINAL.CLAIM")
        code, payload = self._cli([
            "validate", "--project", str(explicit), "--strict",
        ])
        self.assertEqual(code, 2, payload)
        self.assertEqual(payload["model_turns_started"], 0)
        self.assertIn("placeholder", payload["error"])

    def test_custom_compatible_provider_and_explicit_effort_mapping_validate(self) -> None:
        profile = REPO / "docs" / "examples" / "custom-provider-profile.json"
        config = load_config(self.project, profile_path=profile)
        route = config.route_for("explorer")
        self.assertEqual(route["provider"], "research-gateway")
        self.assertEqual(route["mapped_effort"], "high")
        self.assertEqual(route["service_tier"], "default")
        self.assertEqual(
            config.raw["providers"]["research-gateway"]["credential"]["reference"],
            "RESEARCH_GATEWAY_API_KEY",
        )

    def test_unsupported_effort_cannot_silently_downgrade(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        route = raw["overrides"]["models"]["explorer"]
        route["unsupported_effort"] = "error"
        with self.assertRaisesRegex(ValueError, "unsupported"):
            load_config(self.project, profile_path=self._profile(raw))

    def test_plaintext_secret_is_rejected_and_explanation_is_redacted(self) -> None:
        secret_profile = {
            "profile_schema_version": 1,
            "name": "bad-secret",
            "extends": "codex-app-server-default",
            "overrides": {
                "providers": {
                    "openai-compatible": {"api_key": "plaintext-test-credential"}
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "secret"):
            load_config(self.project, profile_path=self._profile(secret_profile))
        rendered = json.dumps(
            redact_config({"api_key": "plaintext-test-credential"}),
            sort_keys=True,
        )
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("plaintext-test-credential", rendered)

    def test_config_cli_does_not_resolve_environment_secret_or_start_model(self) -> None:
        profile = REPO / "docs" / "examples" / "per-role-api-profile.json"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret-never-read"}):
            code, payload = self._cli([
                "config", "explain", "--project", str(self.project),
                "--profile", str(profile),
            ])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["model_turns_started"], 0)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("test-secret-never-read", serialized)
        self.assertIn("OPENAI_API_KEY", serialized)

    def test_config_summary_and_v8_campaign_migration_are_zero_model(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 8
        raw.pop("campaign", None)
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        config = load_config(self.project)
        self.assertEqual(
            config.migrations_applied,
            ("8->9", "9->10", "10->11", "11->12"),
        )
        self.assertEqual(config.campaign_hours, 5.0)
        self.assertEqual(config.epoch_hours, 2.0)

        code, payload = self._cli([
            "config", "summary", "--project", str(self.project),
        ])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["config_schema_version"], 12)
        self.assertEqual(payload["execution"], {"fast_mode": False})
        self.assertEqual(payload["campaign"]["epoch_hours"], 2.0)
        self.assertEqual(
            payload["mechanical"]["fallback_condition"],
            "provider_execution_failure",
        )
        self.assertEqual(payload["model_turns_started"], 0)

        code, payload = self._cli([
            "config", "migrate", "--project", str(self.project), "--write",
        ])
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["written"])
        persisted = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 12)
        self.assertEqual(persisted["campaign"], {"hours": 5.0, "epoch_hours": 2.0})
        self.assertEqual(payload["model_turns_started"], 0)

    def test_campaign_epoch_must_not_exceed_campaign_hours(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "bad-campaign",
            "extends": "codex-app-server-default",
            "overrides": {"campaign": {"hours": 1, "epoch_hours": 2}},
        }
        with self.assertRaisesRegex(ValueError, "must not exceed"):
            load_config(self.project, profile_path=self._profile(profile))

    def test_v10_mechanical_fallback_policy_migrates_without_changing_routes(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 10
        worker = raw["policy"]["one_shot_compute_worker"]
        worker["fallback_condition"] = "permanent_unavailable_or_access_denied"
        before_routes = (worker["primary_route"], worker["fallback_route"])
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        migrated = load_config(self.project)

        effective_worker = migrated.raw["policy"]["one_shot_compute_worker"]
        self.assertEqual(migrated.migrations_applied, ("10->11", "11->12"))
        self.assertEqual(
            effective_worker["fallback_condition"], "provider_execution_failure"
        )
        self.assertEqual(
            (effective_worker["primary_route"], effective_worker["fallback_route"]),
            before_routes,
        )

    def test_v9_scalar_turn_limit_migrates_and_per_role_override_validates(self) -> None:
        config_path = self.project / "autonomous" / "config.yaml"
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["schema_version"] = 9
        raw["engine"] = {"research_max_turns": 7}
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        migrated = load_config(self.project)
        self.assertEqual(
            migrated.migrations_applied, ("9->10", "10->11", "11->12")
        )
        self.assertEqual(migrated.raw["engine"]["research_max_turns"], {
            "prover": 7, "falsifier": 7, "explorer": 7,
        })

        profile = {
            "profile_schema_version": 1,
            "name": "deeper-prover",
            "extends": "codex-app-server-default",
            "overrides": {
                "engine": {"research_max_turns": {"prover": 16}},
            },
        }
        overridden = load_config(
            self.project, profile_path=self._profile(profile, "turn-profile.json"),
        )
        self.assertEqual(overridden.research_max_turns("prover"), 16)
        self.assertEqual(overridden.research_max_turns("falsifier"), 7)

    def test_provider_capabilities_reject_unsupported_structured_mode(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        provider = raw["overrides"]["providers"]["research-gateway"]
        provider["capabilities"]["structured_outputs"] = []
        with self.assertRaisesRegex(ValueError, "structured_outputs"):
            load_config(self.project, profile_path=self._profile(raw))

    def test_fast_service_tier_requires_explicit_global_opt_in(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        raw["overrides"]["models"]["explorer"]["service_tier"] = "fast"
        raw["overrides"]["providers"]["research-gateway"]["capabilities"][
            "service_tiers"
        ].append("fast")
        with self.assertRaisesRegex(ValueError, "requires execution.fast_mode=true"):
            load_config(self.project, profile_path=self._profile(raw))

    def test_explicit_fast_mode_derives_every_main_route_only(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "explicit-fast-main-roles",
            "extends": "codex-app-server-default",
            "overrides": {"execution": {"fast_mode": True}},
        }
        config = load_config(self.project, profile_path=self._profile(profile))

        self.assertTrue(config.fast_mode)
        self.assertEqual(config.requested_service_tier, "fast")
        self.assertTrue(all(
            route["service_tier"] == "fast"
            for route in config.raw["models"].values()
        ))
        worker = config.raw["policy"]["one_shot_compute_worker"]
        self.assertIsNone(worker["service_tier"])
        self.assertIsNone(worker["primary_route"]["service_tier"])
        self.assertIsNone(worker["fallback_route"]["service_tier"])

    def test_explicit_fast_mode_rejects_priority_request_value(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "invalid-priority-request",
            "extends": "codex-app-server-default",
            "overrides": {
                "execution": {"fast_mode": True},
                "models": {"explorer": {"service_tier": "priority"}},
            },
        }
        with self.assertRaisesRegex(ValueError, "conflicts"):
            load_config(self.project, profile_path=self._profile(profile))

    def test_ultrafast_cannot_bypass_explicit_fast_mode(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        raw["overrides"]["models"]["explorer"]["service_tier"] = "ultrafast"
        raw["overrides"]["providers"]["research-gateway"]["capabilities"][
            "service_tiers"
        ].append("ultrafast")

        with self.assertRaisesRegex(ValueError, "forbidden request tier"):
            load_config(self.project, profile_path=self._profile(raw))

    def test_fast_mode_fails_closed_for_provider_without_fast_capability(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        raw["overrides"]["execution"] = {"fast_mode": True}
        raw["overrides"]["models"]["explorer"]["service_tier"] = None
        with self.assertRaisesRegex(ValueError, "not declared"):
            load_config(self.project, profile_path=self._profile(raw))

    def test_fast_dry_run_pins_routes_without_rewriting_canonical_files(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "fast-dry-run",
            "extends": "codex-app-server-default",
            "overrides": {"execution": {"fast_mode": True}},
        }
        config = load_config(self.project, profile_path=self._profile(profile))
        canonical = [
            self.project / "claims" / "CLAIMS.md",
            self.project / "state" / "PROGRESS.md",
            self.project / "autonomous" / "state" / "claim_graph.json",
            self.project / "autonomous" / "state" / "nightly_trusted.json",
        ]
        before = {path: path.read_bytes() for path in canonical}
        controller = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="fast-dry-run", campaign_id="fast-dry-run",
        )

        result = asyncio.run(controller.run(0.01, dry_run=True))

        self.assertFalse(result.internal_failure, result.stopped_reason)
        manifest = json.loads(
            (controller.run_dir / "RUN_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["requested_service_tier"], "fast")
        self.assertTrue(all(
            route["service_tier"] == "fast"
            for route in manifest["requested_routes"].values()
        ))
        self.assertEqual({path: path.read_bytes() for path in canonical}, before)

    def test_v11_pinned_config_is_resume_equivalent_without_rewrite(self) -> None:
        config = load_config(self.project)
        first = AutonomousController(
            config, backend=MockCodexBackend(), mock=True,
            run_id="v11-pinned-resume", campaign_id="v11-pinned-resume",
        )
        first._pin_run_inputs(0.01, False)
        snapshot = first.run_dir / "config" / "config.yaml"
        pinned = json.loads(snapshot.read_text(encoding="utf-8"))
        pinned["schema_version"] = 11
        pinned.pop("execution")
        snapshot.write_text(
            json.dumps(pinned, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path = first.run_dir / "RUN_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["config"]["sha256"] = file_digest(snapshot)
        unsigned = dict(manifest)
        unsigned.pop("manifest_sha256", None)
        manifest["manifest_sha256"] = stable_hash(unsigned)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        before = snapshot.read_bytes()
        resumed = AutonomousController(
            load_config(self.project), backend=MockCodexBackend(), mock=True,
            run_id="v11-pinned-resume", campaign_id="v11-pinned-resume",
            resume=True,
        )

        resumed._pin_run_inputs(0.01, False)

        self.assertEqual(snapshot.read_bytes(), before)

    def test_deep_config_schema_rejects_unknown_nested_fields(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "unknown-field",
            "extends": "codex-app-server-default",
            "overrides": {"engine": {"undeclared_switch": True}},
        }
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            load_config(self.project, profile_path=self._profile(profile))

    def test_installed_external_mechanical_runner_can_be_selected(self) -> None:
        raw = json.loads(
            (REPO / "docs" / "examples" / "custom-provider-profile.json")
            .read_text(encoding="utf-8")
        )
        provider = raw["overrides"]["providers"]["research-gateway"]
        provider["adapter"] = "test_external_adapter"
        provider["capabilities"]["mechanical_one_shot"] = True
        mechanical = raw["overrides"].setdefault("policy", {}).setdefault(
            "one_shot_compute_worker", {}
        )
        route = {
            "provider": "research-gateway", "model": "mechanical-model",
            "endpoint": None, "profile": None, "effort": "high",
            "unsupported_effort": "error", "service_tier": None,
        }
        mechanical["primary_route"] = dict(route)
        mechanical["fallback_route"] = {**route, "model": "mechanical-fallback"}

        def provider_entries(*, group):  # type: ignore[no-untyped-def]
            return (
                [SimpleNamespace(name="test_external_adapter")]
                if group == "autonomous_math_research.providers" else []
            )

        def runner_entries(*, group):  # type: ignore[no-untyped-def]
            return (
                [SimpleNamespace(name="test_external_adapter")]
                if group == "autonomous_math_research.mechanical_runners" else []
            )

        with (
            patch("autonomous_math_research.provider_config.entry_points", provider_entries),
            patch("autonomous_math_research.mechanical.entry_points", runner_entries),
        ):
            config = load_config(self.project, profile_path=self._profile(raw))
        configured = config.raw["policy"]["one_shot_compute_worker"]
        self.assertEqual(configured["primary_route"]["provider"], "research-gateway")

    def test_legacy_run_limits_receive_new_budget_defaults(self) -> None:
        normalized = AutonomousController._normalized_manifest_limits({
            "schema_version": 7,
            "execution": {"limits": {
                "global_tokens": 1000, "max_director": 1,
                "max_research_workers": 2, "max_audit": 2,
                "max_total_model_concurrency": 5,
                "duration_seconds": 60, "deadline_epoch": 100,
            }},
        })
        self.assertIsNone(normalized["mechanical_tokens"])
        self.assertIsNone(normalized["global_cost_usd"])
        self.assertIsNone(normalized["mechanical_cost_usd"])
        self.assertEqual(normalized["max_mechanical_subworkers"], 0)

    def test_cost_accounting_preserves_observation_across_unknown_update(self) -> None:
        governor = TokenGovernor(global_budget=1000, configured_max_research=1)
        governor.record("job", "explorer", TokenUsage(total_tokens=10), cost_usd=0.25)
        governor.record("job", "explorer", TokenUsage(total_tokens=12), cost_usd=None)
        governor.record("job", "explorer", TokenUsage(total_tokens=12), cost_usd=0.30)
        self.assertAlmostEqual(governor.total_cost_usd, 0.30)

    def test_token_accounting_separates_cached_uncached_output_and_reasoning(self) -> None:
        usage = TokenUsage.from_app_server({
            "inputTokens": 1000,
            "cachedInputTokens": 800,
            "outputTokens": 120,
            "reasoningOutputTokens": 75,
            "totalTokens": 1120,
        })
        self.assertEqual(usage.cached_input_tokens, 800)
        self.assertEqual(usage.uncached_input_tokens, 200)
        self.assertEqual(usage.output_tokens, 120)
        self.assertEqual(usage.reasoning_output_tokens, 75)
        governor = TokenGovernor(global_budget=10_000, configured_max_research=1)
        governor.record("job", "prover", usage)
        snapshot = governor.snapshot()["total"]
        self.assertEqual(snapshot["uncached_input_tokens"], 200)
        self.assertEqual(snapshot["cached_input_tokens"], 800)

    def test_strict_init_detects_placeholders_then_accepts_consistent_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "placeholder"):
            validate_project(self.project, strict=True)
        graph_path = self.project / "autonomous" / "state" / "claim_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["claims"][0]["statement"] = "For every object in the declared domain, property P holds."
        graph["claims"][0]["current_gaps"] = ["A proof or counterexample is still required."]
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (self.project / "claims" / "CLAIMS.md").write_text(
            "# Claims\n\n- `C_FINAL`: For every object in the declared domain, property P holds.\n",
            encoding="utf-8",
        )
        result = validate_project(self.project, strict=True)
        self.assertTrue(result["valid"])
        self.assertTrue(result["strict"])
        self.assertEqual(result["model_turns_started"], 0)

    def test_strict_init_rejects_claim_id_mismatch(self) -> None:
        (self.project / "claims" / "CLAIMS.md").write_text(
            "# Claims\n\n- `C_OTHER`: Exact neutral test statement.\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "final_claim_id|Claim IDs|claim IDs"):
            validate_project(self.project, strict=True)

    def test_strict_init_accepts_rich_markdown_without_mirroring_dynamic_ids(self) -> None:
        graph_path = self.project / "autonomous" / "state" / "claim_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["claims"][0]["statement"] = "Every object has property P."
        graph["claims"][0]["current_gaps"] = ["A proof remains open."]
        dynamic = dict(graph["claims"][0])
        dynamic.update({
            "claim_id": "C_DYNAMIC",
            "statement": "A controller-derived research claim.",
            "current_gaps": ["Independent audit remains open."],
        })
        graph["claims"].append(dynamic)
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (self.project / "claims" / "CLAIMS.md").write_text(
            "# Claims\n\n"
            "| Claim | Status | Evidence |\n"
            "|---|---|---|\n"
            "| C_FINAL | `OPEN` | `E0_SPECULATIVE` |\n\n"
            "Inline notation such as `D_T` is contextual, not a claim declaration.\n",
            encoding="utf-8",
        )

        result = validate_project(self.project, strict=True)

        self.assertTrue(result["valid"])
        self.assertTrue(result["strict"])

    def test_strict_init_rejects_final_id_only_mentioned_in_prose(self) -> None:
        graph_path = self.project / "autonomous" / "state" / "claim_graph.json"
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph["claims"][0]["statement"] = "Every object has property P."
        graph["claims"][0]["current_gaps"] = ["A proof remains open."]
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (self.project / "claims" / "CLAIMS.md").write_text(
            "# Claims\n\nThe final target C_FINAL is discussed here but not declared.\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "explicitly declared"):
            validate_project(self.project, strict=True)

    def test_project_or_profile_cannot_remove_core_protected_paths(self) -> None:
        profile = {
            "profile_schema_version": 1,
            "name": "unsafe-boundary",
            "extends": "codex-app-server-default",
            "overrides": {"workspace": {"protected_paths": ["claims"]}},
        }
        with self.assertRaisesRegex(ValueError, "protected paths"):
            load_config(self.project, profile_path=self._profile(profile))

    def test_unbounded_mechanical_cap_still_has_resource_and_budget_backpressure(self) -> None:
        controller = AutonomousController(load_config(self.project), mock=True)
        self.assertIsNone(controller.max_mechanical_subworkers)
        self.assertGreaterEqual(controller._mechanical_resource_capacity(), 1)
        self.assertLessEqual(controller._mechanical_resource_capacity(), os.cpu_count() or 1)
        self.assertEqual(controller.mechanical_governor.global_budget, 1_500_000_000)
        self.assertEqual(
            controller.config.raw["policy"]["one_shot_compute_worker"]
            ["backpressure"]["max_queue_depth"],
            256,
        )


class OpenAICompatibleAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME / f"provider-adapter-{uuid4().hex}"
        self.project = self.root / "neutral-project"
        initialize_project(self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    async def test_transport_normalizes_schema_usage_cost_and_tier_without_network(self) -> None:
        profile = self.root / "api-profile.json"
        profile.write_text(json.dumps({
            "profile_schema_version": 1,
            "name": "mock-transport",
            "extends": "codex-app-server-default",
            "overrides": {
                "providers": {
                    "openai-compatible": {
                        "credential": {"kind": "none", "reference": None},
                        "capabilities": {"cost_path": "billing.cost_usd"},
                    }
                },
                "models": {
                    "explorer": {
                        "provider": "openai-compatible",
                        "model": "test-model",
                        "effort": "high",
                        "service_tier": "default",
                    }
                },
            },
        }, indent=2) + "\n", encoding="utf-8")
        config = load_config(self.project, profile_path=profile)
        captured: dict = {}

        async def transport(endpoint, headers, payload, timeout):  # type: ignore[no-untyped-def]
            captured.update({
                "endpoint": endpoint, "headers": headers,
                "payload": payload, "timeout": timeout,
            })
            result = {
                "result_type": "NO_PROGRESS",
                "main_finding": "Deterministic adapter test.",
                "status": "COMPLETED",
                "artifact_paths": [],
                "next_suggested_question": "None.",
                "evidence_level": "E0_SPECULATIVE",
                "asset_usage": [],
            }
            return {
                "id": "response-test", "model": "test-model",
                "service_tier": "default", "output_text": json.dumps(result),
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 80},
                    "output_tokens": 7,
                    "output_tokens_details": {"reasoning_tokens": 3},
                    "total_tokens": 107,
                },
                "billing": {"cost_usd": 0.25},
            }

        backend = OpenAICompatibleBackend(
            config, "openai-compatible", transport=transport,
        )
        task = ResearchTask(
            task_id="adapter-test", role="explorer", target_claim="C_ROOT",
            exact_objective="Return a schema-valid deterministic test result.",
            why_now="adapter test", dependencies=[], expected_information_gain="LOW",
            mathematical_impact="LOW", estimated_cost_tier="LOW", required_files=[],
            stop_conditions=["return"], output_contract="worker_result.schema.json",
        )
        with schema_resource("worker_result.schema.json") as path:
            schema = load_schema(path)
        outcome = await backend.run_job(
            job_id="job-adapter", task=task, prompt="test", output_schema=schema,
            workspace=self.project, writable_roots=[self.project], timeout=3,
            token_budget=100, candidate_sink=lambda _event: asyncio.sleep(0),
        )
        self.assertTrue(outcome.succeeded, outcome.error)
        self.assertEqual(outcome.provider, "openai-compatible")
        self.assertEqual(outcome.requested_service_tier, "default")
        self.assertEqual(outcome.observed_service_tier, "default")
        self.assertEqual(outcome.token_usage.total_tokens, 107)
        self.assertEqual(outcome.token_usage.cached_input_tokens, 80)
        self.assertEqual(outcome.token_usage.uncached_input_tokens, 20)
        self.assertEqual(outcome.token_usage.output_tokens, 7)
        self.assertEqual(outcome.token_usage.reasoning_output_tokens, 3)
        self.assertEqual(outcome.cost_usd, 0.25)
        self.assertTrue(captured["endpoint"].endswith("/responses"))
        self.assertNotIn("Authorization", captured["headers"])
        self.assertEqual(captured["payload"]["reasoning"]["effort"], "high")
        self.assertEqual(captured["payload"]["service_tier"], "default")
        self.assertEqual(captured["payload"]["text"]["format"]["type"], "json_schema")

    async def test_router_preserves_bindings_and_aggregates_provider_rate_limits(self) -> None:
        config = load_config(
            self.project,
            profile_path=REPO / "docs" / "examples" / "per-role-api-profile.json",
        )

        class Adapter:
            def __init__(self, active, rates):  # type: ignore[no-untyped-def]
                self.active = active
                self.rates = rates

            async def rate_limits(self):  # type: ignore[no-untyped-def]
                return self.rates

        codex = Adapter({"job-codex": ("thread-1", "turn-1")}, {"used": 15})
        api = Adapter(set(), {"used": 72})
        router = ProviderRouterBackend(config, adapter_overrides={
            "codex": codex,  # type: ignore[dict-item]
            "openai-compatible": api,  # type: ignore[dict-item]
        })
        self.assertEqual(router.active["job-codex"], ("thread-1", "turn-1"))
        rates = await router.rate_limits()
        self.assertEqual(rates["providers"]["codex"]["used"], 15)  # type: ignore[index]
        self.assertEqual(
            rates["providers"]["openai-compatible"]["used"], 72,  # type: ignore[index]
        )

    async def test_transport_usage_limit_preserves_official_reset_hint(self) -> None:
        profile = self.root / "quota-profile.json"
        profile.write_text(json.dumps({
            "profile_schema_version": 1,
            "name": "quota-transport",
            "extends": "codex-app-server-default",
            "overrides": {
                "providers": {
                    "openai-compatible": {
                        "credential": {"kind": "none", "reference": None},
                    }
                },
                "models": {
                    "explorer": {
                        "provider": "openai-compatible",
                        "model": "test-model",
                        "effort": "high",
                        "service_tier": "default",
                    }
                },
            },
        }, indent=2) + "\n", encoding="utf-8")
        config = load_config(self.project, profile_path=profile)

        async def transport(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProviderTransportError(
                "provider HTTP request failed with status 429",
                status=429,
                payload={
                    "error": {
                        "code": "usage_limit_reached",
                        "message": "You've hit your usage limit",
                        "reset_at": "2026-08-22T00:00:00Z",
                    }
                },
            )

        backend = OpenAICompatibleBackend(
            config, "openai-compatible", transport=transport,
        )
        task = ResearchTask(
            task_id="quota-test", role="explorer", target_claim="C_ROOT",
            exact_objective="Return a schema-valid deterministic test result.",
            why_now="quota test", dependencies=[], expected_information_gain="LOW",
            mathematical_impact="LOW", estimated_cost_tier="LOW", required_files=[],
            stop_conditions=["return"], output_contract="worker_result.schema.json",
        )
        with schema_resource("worker_result.schema.json") as path:
            schema = load_schema(path)
        outcome = await backend.run_job(
            job_id="job-quota", task=task, prompt="test", output_schema=schema,
            workspace=self.project, writable_roots=[self.project], timeout=3,
            token_budget=100, candidate_sink=lambda _event: asyncio.sleep(0),
        )
        self.assertEqual(outcome.failure_kind, "provider_quota_exhausted")
        self.assertFalse(outcome.retryable)
        self.assertEqual(
            outcome.server_error["provider_reset_at"], "2026-08-22T00:00:00Z",
        )


if __name__ == "__main__":
    unittest.main()
