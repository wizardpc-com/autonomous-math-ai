from __future__ import annotations

import unittest

from autonomous_math_research.mechanical import attest_mechanical_host_capability


class PreflightGateTests(unittest.TestCase):
    def test_windows_mechanical_sandbox_fails_closed(self) -> None:
        result = attest_mechanical_host_capability(
            declared=True,
            selection_mode="preferred",
            host_system="Windows",
        )
        self.assertFalse(result["runtime_available"])
        self.assertEqual(
            result["reason"],
            "windows_split_filesystem_sandbox_unenforceable",
        )

    def test_injected_test_runner_does_not_weaken_production_attestation(self) -> None:
        result = attest_mechanical_host_capability(
            declared=True,
            selection_mode="preferred",
            host_system="Windows",
            test_or_injected_runner=True,
        )
        self.assertTrue(result["runtime_available"])
        self.assertEqual(result["reason"], "injected_or_mock_runner")


if __name__ == "__main__":
    unittest.main()
