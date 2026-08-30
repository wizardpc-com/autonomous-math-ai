from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from autonomous_math_research.config import load_config
from autonomous_math_research.domain_semantics import builtin_domain_contract
from autonomous_math_research.initializer import initialize_project
from autonomous_math_research.models import stable_hash
from autonomous_math_research.policy import (
    LEGACY_POLICY_SCHEMA_VERSION,
    POLICY_SCHEMA_VERSION,
    discover_policy_packs,
    domain_contract_for_run,
    domain_contract_from_manifest,
    pin_policy_manifest,
    policy_view_for_role,
)
from autonomous_math_research.storage import file_digest


RUNTIME = Path(__file__).resolve().parent / "_runtime"
DOMAINS = (
    "math-research",
    "certified-computational-research",
    "empirical-research",
)


def _rehash(manifest: dict[str, object]) -> None:
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = stable_hash(body)


class PolicyDomainPackTests(unittest.TestCase):
    def setUp(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.root = RUNTIME / f"amr-policy-domains-{uuid4().hex}"
        self.project = self.root / "neutral-project"
        initialize_project(self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def _config(self, domain: str):  # type: ignore[no-untyped-def]
        source = self.project / "autonomous/config.yaml"
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw["policy"]["pack"] = domain
        path = self.project / f"autonomous/config-{domain}.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return load_config(self.project, path)

    def test_discovery_strictly_loads_all_three_bundled_packs(self) -> None:
        packs = discover_policy_packs()
        self.assertEqual(set(packs), set(DOMAINS))
        for domain, descriptor in packs.items():
            self.assertEqual(descriptor["name"], domain)
            self.assertEqual(
                descriptor["domain_contract"], builtin_domain_contract(domain),
            )
            self.assertIn("director", descriptor["roles"])
            self.assertIn("auditor", descriptor["roles"])

    def test_each_domain_builds_pins_and_injects_role_policy(self) -> None:
        for domain in DOMAINS:
            with self.subTest(domain=domain):
                config = self._config(domain)
                manifest_path = self.root / domain / "policy/MANIFEST.json"
                self.assertEqual(
                    domain_contract_for_run(config, manifest_path, resume=False)["domain"],
                    domain,
                )
                manifest, status = pin_policy_manifest(config, manifest_path)
                self.assertEqual(manifest["schema_version"], POLICY_SCHEMA_VERSION)
                self.assertEqual(manifest["policy_name"], domain)
                self.assertEqual(manifest["domain_contract"]["domain"], domain)
                self.assertEqual(
                    manifest["audit_requirements"],
                    manifest["domain_contract"]["audit_requirements"],
                )
                self.assertFalse(status["source_drift"])
                view = policy_view_for_role(manifest, manifest_path, "director")
                self.assertEqual(view["domain"], domain)
                self.assertEqual(view["domain_contract"]["domain"], domain)
                self.assertTrue(Path(view["descriptor_snapshot"]).is_file())
                self.assertTrue(Path(view["role_prompt_snapshot"]).is_file())
                self.assertTrue(Path(view["skill_snapshot"]).is_file())
                self.assertEqual(
                    domain_contract_for_run(config, manifest_path, resume=True),
                    builtin_domain_contract(domain),
                )

    def test_domain_role_prompts_preserve_domain_trust_boundaries(self) -> None:
        math = self._config("math-research")
        math_path = self.root / "math/policy/MANIFEST.json"
        math_manifest, _ = pin_policy_manifest(math, math_path)
        math_view = policy_view_for_role(math_manifest, math_path, "director")
        math_prompt = Path(math_view["role_prompt_snapshot"]).read_text(
            encoding="utf-8",
        )
        self.assertIn("cheapest exact falsification", math_prompt)
        self.assertIn("Model output alone is never proof", math_prompt)
        self.assertIn("open the exact `semantic_contract`", math_prompt)
        self.assertIn("`representation_compatibility.known_contracts", math_prompt)
        self.assertIn("A `rep:` or `bridge:` identifier is", math_prompt)
        self.assertIn("repair constraint contains `repair_requirements`", math_prompt)
        self.assertIn("copy its `scope_id`", math_prompt)

        certified = self._config("certified-computational-research")
        certified_path = self.root / "certified/policy/MANIFEST.json"
        certified_manifest, _ = pin_policy_manifest(certified, certified_path)
        certified_view = policy_view_for_role(
            certified_manifest, certified_path, "director",
        )
        certified_prompt = Path(certified_view["role_prompt_snapshot"]).read_text(
            encoding="utf-8",
        )
        self.assertIn("`SUPPORTED` is not `CERTIFIED`", certified_prompt)
        self.assertIn("deterministic checker", certified_prompt)

        empirical = self._config("empirical-research")
        empirical_path = self.root / "empirical/policy/MANIFEST.json"
        empirical_manifest, _ = pin_policy_manifest(empirical, empirical_path)
        empirical_view = policy_view_for_role(
            empirical_manifest, empirical_path, "director",
        )
        empirical_prompt = Path(empirical_view["role_prompt_snapshot"]).read_text(
            encoding="utf-8",
        )
        self.assertIn("frozen hypothesis", empirical_prompt)
        self.assertIn("never mathematical `PROVED`", empirical_prompt)

    def test_unknown_domain_fails_closed_at_config_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "policy.pack|enum"):
            self._config("unknown-research")

    def test_pre_pin_crash_uses_legacy_math_semantics_but_not_nonmath(self) -> None:
        missing = self.root / "pre-pin-crash/policy/MANIFEST.json"
        math = self._config("math-research")
        self.assertEqual(
            domain_contract_for_run(math, missing, resume=True),
            builtin_domain_contract("math-research"),
        )
        with self.assertRaisesRegex(ValueError, "without a pinned policy"):
            pin_policy_manifest(math, missing, resume=True)

        empirical = self._config("empirical-research")
        with self.assertRaisesRegex(ValueError, "without a pinned policy"):
            domain_contract_for_run(empirical, missing, resume=True)

    def test_rehashed_invalid_descriptor_snapshot_fails_closed(self) -> None:
        config = self._config("math-research")
        manifest_path = self.root / "invalid-descriptor/policy/MANIFEST.json"
        manifest, _ = pin_policy_manifest(config, manifest_path)
        descriptor_entry = manifest["descriptor"]
        descriptor_path = manifest_path.parent / descriptor_entry["snapshot_path"]
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["hidden_authority"] = "model transcript"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        descriptor_entry["sha256"] = file_digest(descriptor_path)
        _rehash(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "descriptor fields"):
            pin_policy_manifest(config, manifest_path, resume=True)

    def test_rehashed_cross_domain_contract_fails_closed(self) -> None:
        config = self._config("math-research")
        manifest_path = self.root / "rebound-domain/policy/MANIFEST.json"
        manifest, _ = pin_policy_manifest(config, manifest_path)
        manifest["domain_contract"] = builtin_domain_contract("empirical-research")
        manifest["audit_requirements"] = manifest["domain_contract"]["audit_requirements"]
        _rehash(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "domain contract"):
            pin_policy_manifest(config, manifest_path, resume=True)

    def test_rehashed_stable_core_resource_rebinding_fails_closed(self) -> None:
        config = self._config("math-research")
        manifest_path = self.root / "rebound-core-resource/policy/MANIFEST.json"
        manifest, _ = pin_policy_manifest(config, manifest_path)
        descriptor_entry = manifest["descriptor"]
        descriptor_path = manifest_path.parent / descriptor_entry["snapshot_path"]
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["mechanical_resources"]["broker_client"] = "contracts.py"
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        descriptor_entry["sha256"] = file_digest(descriptor_path)
        manifest["one_shot_compute_worker"]["broker_client"] = dict(
            manifest["one_shot_compute_worker"]["contract_definitions"]
        )
        _rehash(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "stable core bindings"):
            pin_policy_manifest(config, manifest_path, resume=True)

    def test_schema_v5_math_manifest_resumes_with_math_semantics(self) -> None:
        config = self._config("math-research")
        manifest_path = self.root / "legacy-v5/policy/MANIFEST.json"
        manifest, _ = pin_policy_manifest(config, manifest_path)
        manifest["schema_version"] = LEGACY_POLICY_SCHEMA_VERSION
        for key in (
            "descriptor", "role_prompts", "domain_contract", "audit_requirements",
        ):
            manifest.pop(key)
        _rehash(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        resumed, status = pin_policy_manifest(config, manifest_path, resume=True)
        self.assertEqual(resumed["schema_version"], LEGACY_POLICY_SCHEMA_VERSION)
        self.assertTrue(status["resume_uses_pinned_snapshot"])
        self.assertEqual(
            domain_contract_from_manifest(resumed),
            builtin_domain_contract("math-research"),
        )
        view = policy_view_for_role(resumed, manifest_path, "director")
        self.assertEqual(view["domain"], "math-research")
        self.assertIsNone(view["role_prompt_snapshot"])
        self.assertEqual(view["domain_contract"]["terminal_positive"], ["PROVED"])


if __name__ == "__main__":
    unittest.main()
