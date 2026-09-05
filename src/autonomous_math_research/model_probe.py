"""Explicit, bounded App Server compatibility check without a research campaign."""
from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import secrets
import tempfile
from typing import Any

from .app_server import (
    AppServerClient, attest_model_route, attest_reasoning_effort,
    attest_service_tier, parse_structured_message, redact_auth_material,
)
from .config import HarnessConfig
from .schema import validate, validate_output_schema_compatibility
from .storage import atomic_write_json


PROBE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["OK"]},
                   "sha256": {"type": "string"}},
    "required": ["status", "sha256"],
}


async def probe_model(config: HarnessConfig, *, live: bool = False, timeout: int = 90, budget: int = 4000) -> dict[str, Any]:
    if not 1 <= timeout <= 180 or not 1 <= budget <= 20000:
        raise ValueError("probe requires timeout 1..180 seconds and token budget 1..20000")
    validate_output_schema_compatibility(PROBE_SCHEMA, schema_path="probe")
    route = config.route_for("smoke")
    if config.raw["providers"][route["provider"]]["adapter"] != "codex_app_server":
        raise ValueError("compatibility probe requires a Codex App Server smoke route")
    report: dict[str, Any] = {
        "status": "LIVE_NOT_RUN", "requested_model": route["model"],
        "requested_effort": route["mapped_effort"], "requested_service_tier": route.get("service_tier"),
        "observed_model": None, "observed_effort": None, "route_status": "UNKNOWN",
        "model_turns_started": 0, "model_turns_requested": 0, "turn_limit": 2, "timeout_seconds": timeout,
        "token_budget": budget, "token_budget_hard_cap": False,
        "budget_semantics": "observed usage stops subsequent dispatch; an in-flight turn can overshoot",
        "turns": [], "canonical_authority_changed": False,
    }
    if not live:
        return report
    tool_events: list[dict[str, Any]] = []

    def observe(message: dict[str, Any]) -> None:
        item = (message.get("params") or {}).get("item") or {}
        if message.get("method") == "item/completed" and item.get("type") == "commandExecution":
            tool_events.append({"exit_code": item.get("exitCode"), "status": item.get("status")})

    # Retained under the selected project's runtime, outside its canonical state.
    from .project import ProjectManifest
    manifest = ProjectManifest.load(config.project_root)
    probe_root = manifest.resolve(manifest.runtime_root) / "compatibility_probes"
    probe_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="probe-", dir=probe_root))
    report["evidence_directory"] = str(work)
    client = AppServerClient(notification_handler=observe, project_root=work, read_roots=(work,))
    try:
        async with asyncio.timeout(timeout):
            await client.start()
            # Check every selected role before spending a model turn, including audit efforts.
            for role in config.raw["models"]:
                selected = config.route_for(role)
                if selected["provider"] == route["provider"]:
                    await client.validate_model_effort(selected["model"], selected["mapped_effort"])
            observed_total = 0
            for index in range(2):
                workspace = work / f"turn-{index + 1}"
                workspace.mkdir()
                data = secrets.token_bytes(32)
                (workspace / "input.bin").write_bytes(data)
                expected = sha256(data).hexdigest()
                started = await client.start_thread(
                    model=route["model"], cwd=workspace, writable_roots=[workspace],
                    service_tier=route.get("service_tier"),
                    developer_instructions="Execute only the bounded local file hashing task. Do not spawn agents or change model routes.",
                )
                thread_model = attest_model_route(started, "thread/start", route["model"])
                tier = attest_service_tier(started, "thread/start", route.get("service_tier"))
                report["model_turns_requested"] += 1
                turn_observed = False

                def on_started(_turn_id: str) -> None:
                    nonlocal turn_observed
                    if not turn_observed:
                        report["model_turns_started"] += 1
                        turn_observed = True

                before = len(tool_events)
                completed, text, usage, telemetry = await client.start_turn(
                    thread_id=started["thread"]["id"], cwd=workspace,
                    prompt="Run a local shell command to compute the SHA-256 of input.bin and write only its hexadecimal digest to output.txt. Return JSON with status OK and that sha256. Do not infer or invent the digest.",
                    model=route["model"], effort=route["mapped_effort"], output_schema=PROBE_SCHEMA,
                    writable_roots=[workspace], timeout=timeout, service_tier=route.get("service_tier"),
                    on_started=on_started,
                )
                turn = completed.get("turn") or {}
                actual = attest_model_route(turn, "turn/completed", route["model"])
                effort = attest_reasoning_effort(turn, route["mapped_effort"])
                parsed = parse_structured_message(text)
                validate(parsed, PROBE_SCHEMA)
                if turn.get("status") != "completed" or parsed["sha256"] != expected:
                    raise ValueError("probe structured output or deterministic result failed")
                if not (workspace / "output.txt").is_file() or (workspace / "output.txt").read_text(encoding="utf-8-sig").strip() != expected:
                    raise ValueError("probe tool output missing or incorrect")
                executed = any(item["exit_code"] == 0 for item in tool_events[before:])
                row = {"thread_id": started["thread"]["id"], "turn_id": turn.get("id"),
                       "thread_configured_model": None if thread_model == "unobservable" else thread_model,
                       "observed_model": None if actual == "unobservable" else actual,
                       "observed_effort": effort, "observed_service_tier": tier,
                       "tool_execution_observed": executed, "token_usage": usage.to_dict(),
                       "token_telemetry": telemetry}
                report["turns"].append(row)
                atomic_write_json(workspace / "RESULT.json", {"result": parsed, **row})
                observed_total += usage.total_tokens
                if telemetry != "observed" or not executed or observed_total >= budget:
                    break
            rows = report["turns"]
            report["observed_model"] = rows[-1]["observed_model"] if rows else None
            report["observed_effort"] = rows[-1]["observed_effort"] if rows else None
            report["route_status"] = "OBSERVED" if rows and all(row["observed_model"] and row["observed_effort"] for row in rows) else "UNKNOWN"
            report["status"] = "PASS" if len(rows) == 2 and report["route_status"] == "OBSERVED" and all(row["tool_execution_observed"] and row["token_telemetry"] == "observed" for row in rows) else "INCOMPLETE_TELEMETRY_OR_BUDGET"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = str(redact_auth_material(str(exc)))
    finally:
        try:
            await client.close()
        finally:
            atomic_write_json(work / "PROBE.json", report)
    return report
