from __future__ import annotations

import argparse
import asyncio
import json
import os
import runpy
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from ..app_server import AppServerClient
from ..catalog import rebuild_catalog
from ..capabilities import inspect_generated_schema
from ..config import CONFIG_SCHEMA_VERSION, default_max_audit, load_config
from ..controller import (
    AutonomousController, build_mock_full_cycle_backend,
)
from ..eventing import CandidateInbox
from ..initializer import initialize_project
from ..launcher import run_launcher
from ..lifecycle.campaign import (
    CampaignStore, DEFAULT_CAMPAIGN_HOURS, DEFAULT_EPOCH_HOURS,
)
from ..models import CandidateEvent
from ..monitor import build_status, format_status, resolve_run, watch_run
from ..project import ProjectManifest
from ..resources import policy_resource, schema_resource
from ..smoke import (
    FULL_LIFECYCLE_SMOKE_BUDGET,
    SCHEMA_ROLE_SMOKE_BUDGET,
    SCHEMA_ROLES,
    SmokeRunFailed,
    run_real_smoke,
)
from ..schema import load_schema, validate
from ..storage import ProjectLayout, atomic_write_json, file_digest, read_jsonl
from ..storage.artifacts import PORTABLE_SCHEMES, portable_project_uri
from ..storage.steering import append_steering, ingest_asset
from ..validation import validate_project


AUTONOMOUS_REQUIRED_FILES = ("autonomous/project.json",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amr")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a neutral standalone math project")
    init.add_argument("directory", type=Path)
    init.add_argument("--project-id")
    init.add_argument("--final-claim-id", default="C_ROOT")

    check = sub.add_parser("validate", help="validate a project without starting a model")
    check.add_argument("--project", type=Path, required=True)
    check.add_argument("--workspace-root", type=Path)
    check.add_argument("--profile", type=Path)
    check.add_argument("--strict", action="store_true")

    config = sub.add_parser("config", help="validate or explain effective configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name in ("validate", "explain", "summary", "migrate"):
        command = config_sub.add_parser(name)
        command.add_argument("--project", type=Path, required=True)
        command.add_argument("--profile", type=Path)
        command.add_argument("--workspace-root", type=Path)
        if name == "migrate":
            command.add_argument("--write", action="store_true")

    run = sub.add_parser("run", help="run or validate the autonomous controller")
    run.add_argument("--project", type=Path, required=True)
    run.add_argument(
        "--workspace-root", type=Path,
        help="explicit workspace root; otherwise the target project's Git root is discovered",
    )
    run.add_argument(
        "--hours", type=float,
        help=(
            "campaign time limit in hours (default: project campaign.hours, "
            f"built-in {DEFAULT_CAMPAIGN_HOURS:g})"
        ),
    )
    run.add_argument(
        "--epoch-hours", type=float,
        help=(
            "maximum duration of this epoch (default: project campaign.epoch_hours, "
            f"built-in {DEFAULT_EPOCH_HOURS:g})"
        ),
    )
    run.add_argument(
        "--max-director", type=int,
        help="maximum concurrent Director agents; must remain 1",
    )
    run.add_argument(
        "--max-research-workers", "--max-research",
        dest="max_research_workers", type=int,
        help="maximum concurrent research workers (overrides config)",
    )
    run.add_argument(
        "--max-audit", type=int,
        help=(
            "maximum concurrent audit agents; when omitted alongside an explicit "
            "research cap, defaults to the same value as the research cap"
        ),
    )
    run.add_argument(
        "--max-mechanical-subworkers", type=str,
        help=(
            "positive static cap or 'unbounded'; unbounded still obeys broker "
            "budget, resource, rate-limit, queue, timeout, and stop backpressure"
        ),
    )
    run.add_argument(
        "--budget", type=int,
        help="global token budget for this run (overrides config)",
    )
    run.add_argument("--config", type=Path)
    run.add_argument("--profile", type=Path)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--mock", action="store_true")
    run.add_argument("--resume", nargs="?", const="latest")
    run.add_argument("--run-id", help=argparse.SUPPRESS)
    run.add_argument("--campaign-id", help=argparse.SUPPRESS)
    run.add_argument("--previous-epoch-id", help=argparse.SUPPRESS)
    run.add_argument(
        "--recover-candidates-from",
        metavar="RUN_ID",
        help=(
            "start a new run and append-only import candidates rejected by the old "
            "claim-id protocol; cannot be combined with --resume"
        ),
    )

    launcher = sub.add_parser(
        "launcher", help="open the unified interactive project launcher",
    )
    launcher.add_argument(
        "action", nargs="?",
        choices=("validate", "strict", "config", "dry-run", "mock", "real"),
    )
    launcher.add_argument("--workspace-root", type=Path)
    launcher.add_argument("--project", type=Path)
    launcher.add_argument("--profile", type=Path)
    launcher.add_argument("--state-file", type=Path, help=argparse.SUPPRESS)

    emit = sub.add_parser("emit-event", help="append one validated candidate event")
    emit.add_argument("--project", type=Path, required=True)
    emit.add_argument("--file", type=Path, required=True)
    emit.add_argument("--inbox-dir", type=Path)

    steer = sub.add_parser("steer", help="append a bounded human steering record")
    steer.add_argument("--project", type=Path, required=True)
    steer.add_argument("--campaign", required=True)
    steer.add_argument("--kind", required=True)
    steer.add_argument("--note", required=True)
    steer.add_argument("--claim-id")
    steer.add_argument("--route-id")
    steer.add_argument("--audit-kind")

    ingest = sub.add_parser("ingest", help="copy one local asset into campaign storage")
    ingest.add_argument("--project", type=Path, required=True)
    ingest.add_argument("--campaign", required=True)
    ingest.add_argument("--file", type=Path, required=True)
    ingest.add_argument("--description", required=True)

    campaign = sub.add_parser("campaign", help="inspect or continue a sealed campaign")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_continue = campaign_sub.add_parser(
        "continue", help="create a new epoch from the latest sealed checkpoint",
    )
    campaign_continue.add_argument("--project", type=Path, required=True)
    campaign_continue.add_argument("--campaign", required=True)
    campaign_continue.add_argument("--workspace-root", type=Path)
    campaign_continue.add_argument("--profile", type=Path)
    campaign_continue.add_argument("--mock", action="store_true")
    campaign_continue.add_argument("--dry-run", action="store_true")
    campaign_continue.add_argument("--epoch-hours", type=float)

    mechanical_run = sub.add_parser(
        "mechanical-run", help="run the bundled one-shot mechanical worker entry point",
    )
    mechanical_run.add_argument("--project-root", type=Path, required=True)
    mechanical_run.add_argument("worker_args", nargs=argparse.REMAINDER)

    new_experiment = sub.add_parser(
        "new-experiment", help="create a policy-compliant experiment record",
    )
    new_experiment.add_argument("experiment_args", nargs=argparse.REMAINDER)

    detect_tools = sub.add_parser(
        "detect-tools", help="write a project-local mathematical tool inventory",
    )
    detect_tools.add_argument("--project-root", type=Path, required=True)
    detect_tools.add_argument("--output", type=Path)

    probe = sub.add_parser("probe", help="inspect local schema and live App Server capabilities")
    probe.add_argument("--project", type=Path, required=True)
    probe.add_argument("--out", type=Path)

    smoke = sub.add_parser(
        "smoke", help="run the configured minimal real no-fast provider lifecycle"
    )
    smoke.add_argument("--project", type=Path, required=True)
    smoke.add_argument("--config", type=Path)
    smoke.add_argument("--profile", type=Path)
    smoke.add_argument(
        "--budget", type=int,
        help=(
            "soft dispatch budget; an in-flight turn finishes naturally "
            f"(default {SCHEMA_ROLE_SMOKE_BUDGET} for --schema-role, "
            f"{FULL_LIFECYCLE_SMOKE_BUDGET} for the full lifecycle)"
        ),
    )
    smoke.add_argument(
        "--schema-role", choices=SCHEMA_ROLES,
        help="run one protocol-only outputSchema acceptance turn instead of the full toy lifecycle",
    )

    watch = sub.add_parser("watch", help="follow a run's append-only lifecycle events")
    watch.add_argument("--project", type=Path, required=True)
    watch.add_argument("--run", default="latest")
    watch.add_argument(
        "--wait-seconds", type=float, default=0.0,
        help="wait briefly for an explicitly named run to be created",
    )
    watch.add_argument("--tail", type=int, default=20)
    watch.add_argument("--poll-seconds", type=float, default=0.5)
    watch.add_argument(
        "--heartbeat-seconds", type=float, default=30.0,
        help="print a monitor-alive message during quiet periods; 0 disables",
    )
    watch.add_argument(
        "--chat", action="store_true",
        help="merge public agent messages, reasoning summaries, and tool activity",
    )
    watch.add_argument("--chat-tail", type=int, default=40)
    watch.add_argument("--json", action="store_true", help="print raw event JSON")
    watch.add_argument(
        "--ui", choices=("auto", "tui", "plain"), default="auto",
        help="PowerShell monitor UI: auto uses the fixed top panel on an interactive terminal",
    )
    watch.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="ANSI color policy for human-readable watch output",
    )
    watch.add_argument(
        "--no-fold", action="store_true",
        help="do not fold repeated low-information tool activity in the TUI",
    )

    status = sub.add_parser("status", help="show a read-only run status snapshot")
    status.add_argument("--project", type=Path, required=True)
    status.add_argument("--run", default="latest")
    status.add_argument("--json", action="store_true")

    catalog = sub.add_parser(
        "catalog",
        help="rebuild derived cross-run indexes without changing run sources",
    )
    catalog.add_argument("--project", type=Path, required=True)
    catalog.add_argument("--json", action="store_true")

    return parser


def _configure_console_encoding() -> None:
    """Keep Chinese live-monitor output readable in Windows PowerShell."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _latest_run(project: Path) -> str:
    root = ProjectLayout(project.resolve()).runs_root
    candidates = []
    for path in root.iterdir():
        if not path.is_dir() or not (path / "EVENTS.jsonl").exists():
            continue
        events = read_jsonl(path / "EVENTS.jsonl")
        kinds = {event.get("kind") for event in events}
        if "RUN_STARTED" in kinds and "RUN_STOPPED" not in kinds:
            candidates.append(path)
    candidates.sort()
    if not candidates:
        raise ValueError("no incomplete resumable run found")
    return candidates[-1].name


def _resolve_run_project(args: argparse.Namespace) -> Path:
    project = args.project.resolve()
    missing = [
        relative for relative in AUTONOMOUS_REQUIRED_FILES
        if not (project / relative).is_file()
    ]
    if missing:
        raise ValueError(
            f"project {project} is not autonomous-harness enabled: "
            f"missing {missing}"
        )
    return project


def _resolve_max_audit_override(
    max_research_workers: int | None,
    max_audit: int | None,
) -> int | None:
    """Derive audit only when the caller explicitly changes research."""
    if max_audit is not None:
        return int(max_audit)
    if max_research_workers is None:
        return None
    return default_max_audit(max_research_workers)


def _mechanical_cap_override(value: str | int | None) -> int | str | None:
    if value is None:
        return None
    rendered = str(value).strip().casefold()
    if rendered in {"unbounded", "null", "none"}:
        return "unbounded"
    try:
        parsed = int(rendered)
    except ValueError as exc:
        raise ValueError("--max-mechanical-subworkers must be positive or unbounded") from exc
    if parsed < 1:
        raise ValueError("--max-mechanical-subworkers must be positive or unbounded")
    return parsed


async def _run_command(args: argparse.Namespace) -> int:
    project = _resolve_run_project(args)
    run_id = args.run_id
    resume = bool(args.resume)
    if resume and args.run_id:
        raise ValueError("--run-id cannot be combined with --resume")
    if resume and args.recover_candidates_from:
        raise ValueError("--resume cannot be combined with --recover-candidates-from")
    if args.resume:
        run_id = _latest_run(project) if args.resume == "latest" else args.resume
    config_path = args.config
    if resume and run_id:
        if args.dry_run:
            raise ValueError("--resume --dry-run is forbidden; resume must reconcile stale turns and state")
        if args.profile is not None:
            raise ValueError("--resume uses the pinned effective config and forbids --profile")
        run_root = ProjectLayout(project).run_dir(run_id)
        pinned = run_root / "config" / "config.yaml"
        if pinned.exists():
            if args.config and file_digest(args.config.resolve()) != file_digest(pinned):
                raise ValueError("explicit resume config differs from the pinned run config")
            config_path = pinned
        manifest_path = run_root / "RUN_MANIFEST.json"
        if not manifest_path.is_file():
            raise ValueError("cannot resume a legacy run without RUN_MANIFEST.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pinned_mock = manifest.get("execution", {}).get("mode") == "mock"
        if args.mock and not pinned_mock:
            raise ValueError("cannot change a pinned real run into mock mode")
        args.mock = pinned_mock
    config = load_config(
        project, config_path, workspace_root=args.workspace_root,
        require_manifest=True, profile_path=args.profile,
    )
    campaign_hours = (
        float(args.hours) if args.hours is not None else config.campaign_hours
    )
    epoch_hours = (
        float(args.epoch_hours) if args.epoch_hours is not None else config.epoch_hours
    )
    if campaign_hours <= 0 or epoch_hours <= 0:
        raise ValueError("campaign and epoch hours must be positive")
    epoch_hours = min(epoch_hours, campaign_hours)
    max_audit_override = _resolve_max_audit_override(
        args.max_research_workers, args.max_audit,
    )
    effective_research = (
        args.max_research_workers
        if args.max_research_workers is not None else config.max_research_workers
    )
    effective_audit = (
        max_audit_override if max_audit_override is not None else config.max_audit
    )
    audit_ceiling = default_max_audit(int(effective_research))
    if int(effective_audit) > audit_ceiling:
        raise ValueError(
            "max_audit must not exceed max_research_workers: "
            f"got max_audit={effective_audit}, ceiling={audit_ceiling}; "
            "supply a compatible --max-audit explicitly"
        )
    if args.mock:
        graph = ProjectLayout(project).claim_graph_path
        raw_graph = json.loads(graph.read_text(encoding="utf-8"))
        final_claim = next(
            item for item in raw_graph["claims"]
            if item["claim_id"] == config.final_conjecture_claim_id
        )
        backend = build_mock_full_cycle_backend(
            claim_id=str(final_claim["claim_id"]),
            statement=str(final_claim["statement"]),
            assumptions=list(final_claim.get("assumptions") or []),
            dependencies=list(final_claim.get("dependencies") or []),
        )
    else:
        backend = None
    controller = AutonomousController(
        config, backend=backend, run_id=run_id, global_budget=args.budget,
        max_director=args.max_director,
        max_research_workers=args.max_research_workers,
        max_audit=max_audit_override,
        max_mechanical_subworkers=_mechanical_cap_override(args.max_mechanical_subworkers),
        mock=args.mock, resume=resume,
        recover_candidates_from=args.recover_candidates_from,
        campaign_id=args.campaign_id,
        previous_epoch_id=args.previous_epoch_id,
        campaign_hours=campaign_hours,
        epoch_hours=epoch_hours,
    )
    result = await controller.run(epoch_hours, dry_run=args.dry_run)
    print(json.dumps({
        "run_id": result.run_id, "project": config.project_name,
        "report": str(result.report_path),
        "outcome": str(result.outcome_path) if result.outcome_path else None,
        "stopped_reason": result.stopped_reason, "jobs": result.job_count,
        "jobs_started": result.jobs_started,
        "jobs_completed": result.jobs_completed,
        "jobs_cancelled": result.jobs_cancelled,
        "jobs_terminal": result.jobs_terminal,
        "mechanical_subtasks_requested": result.mechanical_subtasks_requested,
        "mechanical_attempts_started": result.mechanical_attempts_started,
        "mechanical_subtasks_terminal": result.mechanical_subtasks_terminal,
        "events": result.event_count, "run_mode": result.run_mode,
        "internal_failure": result.internal_failure,
        "campaign_id": result.campaign_id,
        "epoch_id": result.epoch_id,
    }, ensure_ascii=False, indent=2))
    return 2 if result.internal_failure else 0


async def _continue_campaign(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    layout = ProjectLayout(project)
    campaign = CampaignStore(layout.autonomous_root, args.campaign)
    checkpoint = campaign.load()
    if checkpoint.status in {"COMPLETED", "STOPPED"}:
        raise ValueError(f"{checkpoint.status.lower()} campaign cannot be continued")
    if checkpoint.remaining_seconds <= 0:
        raise ValueError("campaign time budget is exhausted")
    epoch_hours = min(
        float(args.epoch_hours or checkpoint.epoch_hours),
        checkpoint.remaining_seconds / 3600.0,
    )
    forwarded = argparse.Namespace(
        project=project,
        workspace_root=args.workspace_root, hours=checkpoint.campaign_hours,
        epoch_hours=epoch_hours, max_director=None, max_research_workers=None,
        max_audit=None, max_mechanical_subworkers=None, budget=None, config=None,
        profile=args.profile, dry_run=args.dry_run, mock=args.mock, resume=None,
        run_id=None, recover_candidates_from=None, campaign_id=args.campaign,
        previous_epoch_id=(checkpoint.epochs[-1] if checkpoint.epochs else None),
    )
    return await _run_command(forwarded)


async def _probe_command(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    # Schema generation is read-only with respect to the project.  Keep all
    # generated protocol files in an OS temporary directory even when the
    # final redacted capability summary is explicitly written elsewhere.
    # Python 3.14 TemporaryDirectory can install an unusable ACL on Windows;
    # inspect_generated_schema owns and removes its UUID-named child instead.
    result = inspect_generated_schema(work_root=Path(tempfile.gettempdir()))
    client = AppServerClient()
    await client.start()
    try:
        result["live"] = await client.probe_capabilities(project)
    finally:
        await client.close()
    output = args.out or ProjectLayout(project).state_root / "app_server_capabilities.json"
    atomic_write_json(output, result)
    print(json.dumps({"output": str(output), "codex_version": result["codex_version"]}, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console_encoding()
    args = build_parser().parse_args(argv)
    if args.command == "init":
        try:
            root = initialize_project(
                args.directory,
                project_id=args.project_id,
                final_claim_id=args.final_claim_id,
            )
            result = validate_project(root)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "initialized": False, "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"initialized": True, "project": str(root), **result}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "validate":
        try:
            result = validate_project(
                args.project, workspace_root=args.workspace_root,
                strict=args.strict, profile_path=args.profile,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "valid": False, "error_type": type(exc).__name__,
                "error": str(exc), "model_turns_started": 0,
            }, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "config":
        try:
            if args.config_command == "migrate" and args.profile is not None:
                raise ValueError("config migrate cannot persist an explicit user profile")
            config = load_config(
                args.project.resolve(), workspace_root=args.workspace_root,
                require_manifest=True, profile_path=args.profile,
            )
            explanation = config.explained()
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "valid": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "model_turns_started": 0,
            }, ensure_ascii=False, indent=2))
            return 2
        if args.config_command == "migrate":
            changed = bool(config.migrations_applied)
            if args.write and changed:
                atomic_write_json(config.config_path, config.raw)
            payload = {
                "valid": True,
                "project": config.project_name,
                "project_config": str(config.config_path),
                "from_migrations": list(config.migrations_applied),
                "target_schema_version": CONFIG_SCHEMA_VERSION,
                "changed": changed,
                "written": bool(args.write and changed),
                "model_turns_started": 0,
            }
        elif args.config_command == "summary":
            payload = config.summarized()
        elif args.config_command == "explain":
            payload = explanation
        else:
            payload = {
            "valid": True,
            "config_schema_version": explanation["config_schema_version"],
            "profile": explanation["profile"],
            "project_config": explanation["project_config"],
            "user_profile": explanation["user_profile"],
            "migrations_applied": explanation["migrations_applied"],
            "providers": sorted(config.raw["providers"]),
            "roles": sorted(config.raw["models"]),
            "effective_config": explanation["effective_config"],
            "model_turns_started": 0,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "launcher":
        try:
            return run_launcher(
                workspace_root=args.workspace_root,
                project_root=args.project,
                action=args.action,
                profile_path=args.profile,
                state_path=args.state_file,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"launcher error: {exc}")
            return 2
    if args.command == "emit-event":
        data = json.loads(args.file.read_text(encoding="utf-8"))
        project = args.project.resolve()
        with schema_resource("candidate_event.schema.json") as schema_path:
            validate(data, load_schema(schema_path))
            event = CandidateEvent.from_dict(data)
            portable: list[str] = []
            for raw in event.artifact_paths:
                if raw.startswith(PORTABLE_SCHEMES):
                    portable.append(raw)
                    continue
                path = Path(raw)
                resolved = (
                    (args.file.parent / path).resolve()
                    if not path.is_absolute() else path.resolve()
                )
                portable.append(portable_project_uri(project, resolved))
            event.artifact_paths = portable
            layout = ProjectLayout(project)
            layout.ensure()
            inbox = CandidateInbox(layout)
            target = inbox.submit(
                event, schema_path,
                target_root=args.inbox_dir.resolve() if args.inbox_dir else None,
            )
        print(json.dumps({"event_id": event.event_id, "fingerprint": event.fingerprint, "path": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "steer":
        try:
            manifest = ProjectManifest.load(args.project.resolve())
            checkpoint = CampaignStore(
                manifest.resolve(manifest.runtime_root), args.campaign,
            ).load()
            if checkpoint.status not in {"ACTIVE", "PAUSED"}:
                raise ValueError(f"{checkpoint.status.lower()} campaign rejects steering")
            record = append_steering(
                manifest.resolve(manifest.runtime_root), args.campaign,
                kind=args.kind, note=args.note, claim_id=args.claim_id,
                route_id=args.route_id, audit_kind=args.audit_kind,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"appended": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"appended": True, **record}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ingest":
        try:
            manifest = ProjectManifest.load(args.project.resolve())
            checkpoint = CampaignStore(
                manifest.resolve(manifest.runtime_root), args.campaign,
            ).load()
            if checkpoint.status not in {"ACTIVE", "PAUSED"}:
                raise ValueError(f"{checkpoint.status.lower()} campaign rejects assets")
            record = ingest_asset(
                manifest.resolve(manifest.runtime_root), args.campaign,
                args.file, description=args.description,
            )
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ingested": False, "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps({"ingested": True, **record}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "campaign":
        try:
            return asyncio.run(_continue_campaign(args))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "internal_failure": True, "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
            return 2
    if args.command == "mechanical-run":
        if not args.worker_args:
            print("mechanical-run requires the one-shot worker arguments", file=sys.stderr)
            return 2
        with policy_resource("scripts/run_worker.py") as runner:
            previous = sys.argv
            previous_root = os.environ.get("MATH_WORKER_REPOSITORY_ROOT")
            os.environ["MATH_WORKER_REPOSITORY_ROOT"] = str(
                args.project_root.resolve()
            )
            sys.argv = [str(runner), *args.worker_args]
            try:
                runpy.run_path(str(runner), run_name="__main__")
            finally:
                sys.argv = previous
                if previous_root is None:
                    os.environ.pop("MATH_WORKER_REPOSITORY_ROOT", None)
                else:
                    os.environ["MATH_WORKER_REPOSITORY_ROOT"] = previous_root
        return 0
    if args.command == "new-experiment":
        if not args.experiment_args:
            print("new-experiment requires its policy tool arguments", file=sys.stderr)
            return 2
        with policy_resource("scripts/new_experiment.py") as runner:
            previous = sys.argv
            sys.argv = [str(runner), *args.experiment_args]
            try:
                runpy.run_path(str(runner), run_name="__main__")
            finally:
                sys.argv = previous
        return 0
    if args.command == "detect-tools":
        with policy_resource("scripts/detect_math_tools.py") as runner:
            previous_argv = sys.argv
            previous_root = os.environ.get("MATH_WORKER_REPOSITORY_ROOT")
            project_root = args.project_root.resolve()
            output = (
                args.output.resolve()
                if args.output is not None
                else project_root / ".tooling" / "math-tools.json"
            )
            os.environ["MATH_WORKER_REPOSITORY_ROOT"] = str(project_root)
            sys.argv = [str(runner), "--output", str(output)]
            try:
                runpy.run_path(str(runner), run_name="__main__")
            finally:
                sys.argv = previous_argv
                if previous_root is None:
                    os.environ.pop("MATH_WORKER_REPOSITORY_ROOT", None)
                else:
                    os.environ["MATH_WORKER_REPOSITORY_ROOT"] = previous_root
        return 0
    if args.command == "run":
        try:
            return asyncio.run(_run_command(args))
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "internal_failure": True,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
            return 2
    if args.command == "probe":
        return asyncio.run(_probe_command(args))
    if args.command == "smoke":
        config = load_config(
            args.project.resolve(), args.config, profile_path=args.profile,
        )
        smoke_budget = args.budget
        if smoke_budget is None:
            smoke_budget = (
                SCHEMA_ROLE_SMOKE_BUDGET
                if args.schema_role is not None
                else FULL_LIFECYCLE_SMOKE_BUDGET
            )
        try:
            report = asyncio.run(
                run_real_smoke(config, smoke_budget, schema_role=args.schema_role)
            )
        except SmokeRunFailed as exc:
            print(json.dumps({
                "run_id": exc.run_id, "report": str(exc.report_path),
                "internal_failure": True, "error": str(exc.cause),
            }, ensure_ascii=False))
            return 2
        print(json.dumps({
            "report": str(report), "internal_failure": False,
            "schema_role": args.schema_role,
        }, ensure_ascii=False))
        return 0
    if args.command == "watch":
        if args.wait_seconds < 0:
            raise ValueError("--wait-seconds must be non-negative")
        if args.wait_seconds and args.run == "latest":
            raise ValueError("--wait-seconds requires an explicit --run id")
        if args.wait_seconds:
            deadline = time.monotonic() + args.wait_seconds
            run_path = ProjectLayout(args.project.resolve()).run_dir(args.run)
            while not (run_path / "EVENTS.jsonl").is_file():
                if time.monotonic() >= deadline:
                    raise ValueError(f"timed out waiting for run: {args.run}")
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        run_dir = resolve_run(args.project, args.run)
        try:
            return watch_run(
                run_dir, tail=args.tail, poll_seconds=args.poll_seconds,
                heartbeat_seconds=args.heartbeat_seconds, chat=args.chat,
                chat_tail=args.chat_tail, raw_json=args.json,
                ui_mode=args.ui, color_mode=args.color,
                fold_repeats=not args.no_fold,
            )
        except KeyboardInterrupt:
            print("\n监视器已关闭；研究主进程仍会继续运行。")
            return 130
    if args.command == "status":
        run_dir = resolve_run(args.project, args.run)
        status = build_status(run_dir)
        print(
            json.dumps(status, ensure_ascii=False, indent=2)
            if args.json else format_status(status)
        )
        return 0
    if args.command == "catalog":
        try:
            result = rebuild_catalog(args.project)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "catalog_updated": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }, ensure_ascii=False, indent=2))
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"catalog updated: runs={result['runs']}, objects={result['objects']}; "
                f"path={result['catalog_root']}"
            )
        return 0
    raise AssertionError(args.command)
