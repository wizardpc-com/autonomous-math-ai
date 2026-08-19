#!/usr/bin/env python3
"""Create a minimal, non-overwriting mathematics experiment record."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EVIDENCE_LEVELS = (
    "E0_SPECULATIVE",
    "E1_NUMERIC",
    "E2_EXACT_TESTED",
    "E3_REDUNDANT_EXACT",
    "E4_CERTIFIED",
    "E5_FORMAL",
)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise argparse.ArgumentTypeError("problem-id must contain a letter or digit")
    return slug[:80]


def git_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def parse_parameters(value: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"parameters must be valid JSON: {exc}") from exc


def project_identifier(project_root: Path) -> str:
    manifest = project_root / "autonomous" / "project.json"
    if not manifest.is_file():
        return project_root.name
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid project manifest: {exc}") from exc
    project_id = raw.get("project_id") if isinstance(raw, dict) else None
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9._-]{0,99}", project_id
    ):
        raise ValueError("project manifest has invalid project_id")
    return project_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("problem_id", type=safe_slug)
    parser.add_argument(
        "--project-root", type=Path, default=Path.cwd(),
        help="Target project root containing autonomous/project.json.",
    )
    parser.add_argument("--objective", default="")
    parser.add_argument("--statement", default="")
    parser.add_argument("--tool", default="")
    parser.add_argument("--tool-version", default="")
    parser.add_argument("--command", default="")
    parser.add_argument("--parameters", type=parse_parameters, default={})
    parser.add_argument("--seed", default=None)
    parser.add_argument("--range", dest="exact_range", default="")
    parser.add_argument("--evidence-level", choices=EVIDENCE_LEVELS, default="E0_SPECULATIVE")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S.%fZ")
    project_root = args.project_root.resolve()
    root = args.root.resolve() if args.root is not None else project_root / "experiments"
    experiment_dir = root / args.problem_id / timestamp
    experiment_dir.mkdir(parents=True, exist_ok=False)

    metadata = {
        "schema_version": 1,
        "problem_id": args.problem_id,
        "project_id": project_identifier(project_root),
        "objective": args.objective,
        "exact_statement": args.statement,
        "created_at_utc": now.isoformat(),
        "tool": {"name": args.tool, "version": args.tool_version},
        "command": args.command,
        "source_commit": git_commit(project_root),
        "parameters": args.parameters,
        "random_seed": args.seed,
        "runtime_seconds": None,
        "exact_range_searched": args.exact_range,
        "output": "",
        "output_files": [],
        "status": "UNKNOWN",
        "evidence_level": args.evidence_level,
        "interpretation": "",
        "unresolved_limitations": [],
    }
    (experiment_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    report = f"""# Experiment: {args.problem_id}

## Statement

{args.statement or '<record the exact statement>'}

## Objective

{args.objective or '<record the exact objective>'}

## Status

`UNKNOWN`

## Evidence level

`{args.evidence_level}`

## Mathematical reduction used

<none recorded>

## Falsification attempts

<none recorded>

## Computations

<record exact commands, parameters, ranges, runtime, and outputs>

## Independent checks

<none recorded>

## Counterexamples, if any

<none recorded>

## Remaining gaps

<record limitations and unproved bridges>

## Files/certificates

- `metadata.json`

## Recommended next action

<record one justified next step>
"""
    (experiment_dir / "report.md").write_text(report, encoding="utf-8")
    print(experiment_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
