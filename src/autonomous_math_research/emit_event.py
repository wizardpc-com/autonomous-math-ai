"""Narrow worker-facing candidate event submission helper.

This file is deliberately executable by absolute path from an isolated worker
workspace, where the repository package is otherwise not on ``sys.path``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autonomous_math_research.claim_graph import ClaimGraph
from autonomous_math_research.eventing import CandidateInbox
from autonomous_math_research.models import CandidateEvent
from autonomous_math_research.resources import schema_resource
from autonomous_math_research.schema import load_schema, validate
from autonomous_math_research.storage import ProjectLayout
from autonomous_math_research.storage.artifacts import (
    PORTABLE_SCHEMES, portable_project_uri,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="autonomous-math-emit-event")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--inbox-dir", type=Path, required=True)
    parser.add_argument("--claim-graph", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve()
    payload = json.loads(args.file.read_text(encoding="utf-8"))
    with schema_resource("candidate_event.schema.json") as schema_path:
        validate(payload, load_schema(schema_path))
        event = CandidateEvent.from_dict(payload)
        graph = ClaimGraph.load(args.claim_graph.resolve())
        graph.validate()
        graph.validate_candidate_dependencies(event)
        refs: list[str] = []
        for raw in event.artifact_paths:
            if raw.startswith(PORTABLE_SCHEMES):
                refs.append(raw)
                continue
            path = Path(raw)
            resolved = (
                (args.file.parent / path).resolve()
                if not path.is_absolute() else path.resolve()
            )
            refs.append(portable_project_uri(project, resolved))
        event.artifact_paths = refs
        target = CandidateInbox(ProjectLayout(project)).submit(
            event, schema_path,
            target_root=args.inbox_dir.resolve(),
        )
    print(json.dumps({
        "event_id": event.event_id,
        "fingerprint": event.fingerprint,
        "path": str(target),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
