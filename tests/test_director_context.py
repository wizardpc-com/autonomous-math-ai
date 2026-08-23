from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from autonomous_math_research.director_context import (
    COMPACT_SNAPSHOT_HARD_LIMIT_BYTES,
    DIRECTOR_PROMPT_HARD_LIMIT_BYTES,
    DIRECTOR_PROMPT_TARGET_BYTES,
    DirectorPromptTooLarge,
    build_compact_snapshot,
    enforce_director_prompt_limit,
    utf8_size,
)
from autonomous_math_research.prompts import director_prompt


class DirectorContextBoundsTests(unittest.TestCase):
    def test_nonmath_domain_and_negative_frontier_survive_compaction(self) -> None:
        compact = build_compact_snapshot(
            {
                "domain": "empirical-research",
                "strictly_trusted": [],
                "strictly_negative": [{"claim_id": "C_NEG"}],
                "open_frontier": [],
                "active_tasks": [],
                "recent_changes": [],
            },
            full_context_reference={
                "relative_path": "director_context_archive/context.json",
                "sha256": "0" * 64,
                "bytes": 3,
                "generation": 1,
            },
            history_archive={"events_path": "EVENTS.jsonl"},
        )

        self.assertEqual(compact["domain"], "empirical-research")
        self.assertEqual(compact["strictly_negative"], [{"claim_id": "C_NEG"}])
        self.assertNotIn("strictly_refuted", compact)
        self.assertEqual(compact["summary_counts"]["strictly_negative"], 1)

    def test_many_director_rounds_do_not_grow_the_first_user_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            snapshot_path = root / "compact_snapshot.json"
            packet_path = root / "task_packet.json"
            archive_path = root / "director_context_archive" / "context.json"
            archive_path.parent.mkdir()
            archive_path.write_text("{}\n", encoding="utf-8")
            packet_path.write_text("{}\n", encoding="utf-8")
            marker = "HISTORY-MUST-NOT-BE-IN-THE-FIRST-MESSAGE"
            sizes: list[int] = []

            for rounds in (1, 10, 100, 1_000):
                full = {
                    "snapshot_provenance": {
                        "epoch_id": "bounded-run",
                        "campaign_id": "bounded-campaign",
                        "attempt_id": "attempt-1",
                        "generation": rounds,
                    },
                    "controller_watermark": {"state_version": rounds},
                    "recent_changes": [
                        {
                            "round": index,
                            "transcript": marker + ("x" * 20_000),
                            "compact_snapshot": {"prior": "y" * 20_000},
                        }
                        for index in range(rounds)
                    ],
                    "open_frontier": [],
                    "strictly_trusted": [],
                    "strictly_refuted": [],
                    "active_tasks": [],
                    "candidate_audit_frontier": [],
                    "pending_research": [],
                    "pending_audits": [],
                    "deferred_research_continuation_ids": [],
                    "research_continuation_checkpoints": [],
                    "route_state": [],
                }
                compact = build_compact_snapshot(
                    full,
                    full_context_reference={
                        "relative_path": "director_context_archive/context.json",
                        "sha256": "0" * 64,
                        "bytes": 3,
                        "generation": rounds,
                    },
                    history_archive={"events_path": str(root / "EVENTS.jsonl")},
                )
                snapshot_path.write_text(
                    json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                prompt = director_prompt(
                    root,
                    snapshot_path,
                    [{"note": marker + ("z" * 20_000)}] * rounds,
                    {"manifest_sha256": "1" * 64},
                    task_packet_path=packet_path,
                    full_context_path=archive_path,
                )
                sizes.append(utf8_size(prompt))
                self.assertNotIn(marker, prompt)
                self.assertLess(snapshot_path.stat().st_size, COMPACT_SNAPSHOT_HARD_LIMIT_BYTES)
                self.assertLess(sizes[-1], DIRECTOR_PROMPT_TARGET_BYTES)

            self.assertLessEqual(max(sizes) - min(sizes), 16)

    def test_hard_limit_is_strict_and_explicit(self) -> None:
        with self.assertRaisesRegex(
            DirectorPromptTooLarge, "rejected before thread creation"
        ):
            enforce_director_prompt_limit("x" * DIRECTOR_PROMPT_HARD_LIMIT_BYTES)
        self.assertEqual(
            enforce_director_prompt_limit(
                "x" * (DIRECTOR_PROMPT_HARD_LIMIT_BYTES - 1)
            ),
            DIRECTOR_PROMPT_HARD_LIMIT_BYTES - 1,
        )


if __name__ == "__main__":
    unittest.main()


