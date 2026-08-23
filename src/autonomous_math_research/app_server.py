from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4

from .models import TokenUsage
from .provider_config import (
    allowed_observed_service_tiers, normalize_observed_service_tier,
)
from .schema import validate_output_schema_compatibility


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

_DISABLED_APP_SERVER_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
)

_MODEL_SHELL_ENVIRONMENT_FILTERS = (
    "COLORTERM",
    "COMSPEC",
    "FORCE_COLOR",
    "LANG",
    "LC_*",
    "NO_COLOR",
    "OS",
    "PATH",
    "PATHEXT",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "WINDIR",
)
_DEFAULT_APP_SERVER_PERMISSION_PROFILE = "amr-role"

_AUTH_SECRET_KEYS = {
    "accesstoken", "refreshtoken", "idtoken", "authtoken", "sessiontoken",
    "apikey", "authorization", "proxyauthorization", "cookie", "setcookie",
    "secret", "clientsecret", "password", "passwd", "credential", "credentials",
}
_AUTH_SECRET_KEY_MARKERS = (
    "secret", "password", "passwd", "credential", "authorization", "cookie",
    "apikey", "accesskey", "privatekey",
)


def redact_auth_material(value: Any) -> Any:
    """Recursively preserve diagnostics while removing authentication values."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            compact = normalized.replace("_", "")
            if (
                compact in _AUTH_SECRET_KEYS
                or compact.endswith("token")
                or any(marker in compact for marker in _AUTH_SECRET_KEY_MARKERS)
            ):
                safe[str(key)] = "[REDACTED]"
            else:
                safe[str(key)] = redact_auth_material(item)
        return safe
    if isinstance(value, list):
        return [redact_auth_material(item) for item in value]
    if isinstance(value, tuple):
        return [redact_auth_material(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "[REDACTED]", value)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
        return re.sub(
            r'''(?i)((?:["']?(?:api[_-]?key|access[_-]?key(?:[_-]?id)?|'''
            r'''secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|'''
            r'''id[_-]?token|auth[_-]?token|session[_-]?token|client[_-]?secret|'''
            r'''password|passwd|credential|authorization|cookie|private[_-]?key|'''
            r'''secret)["']?\s*[=:]\s*["']?))[^"'\s,;}]+''',
            r"\1[REDACTED]",
            text,
        )
    return value


def _redacted_stderr_tail(lines: list[str], limit: int = 20) -> str:
    """Return a bounded startup diagnostic without persisting auth material."""
    text = " | ".join(str(line) for line in lines[-limit:])
    return str(redact_auth_material(text))[-4000:]


def app_server_environment(
    *, blocked_executable: Path | None = None,
) -> dict[str, str]:
    """Use the existing Codex login path without forwarding ambient secrets."""
    environment = dict(os.environ)
    secret_markers = (
        "TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
        "ACCESS_KEY", "CREDENTIAL", "AUTHORIZATION", "COOKIE",
    )
    credential_helpers = {
        "SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS",
        "GOOGLE_APPLICATION_CREDENTIALS", "AWS_PROFILE", "AZURE_CONFIG_DIR",
        "DOCKER_CONFIG", "KUBECONFIG",
    }
    for key in tuple(environment):
        normalized = key.upper()
        if normalized in credential_helpers or any(
            marker in normalized for marker in secret_markers
        ):
            environment.pop(key, None)
    path_key = next((key for key in environment if key.upper() == "PATH"), "PATH")
    runtime_bin = str(Path(sys.executable).resolve().parent)
    existing = str(environment.get(path_key) or "")
    blocked_directory = (
        os.path.normcase(str(blocked_executable.resolve().parent))
        if blocked_executable is not None else None
    )
    entries: list[str] = []
    for entry in existing.split(os.pathsep):
        if not entry:
            continue
        entry_path = Path(entry.strip().strip('"')).expanduser()
        normalized = os.path.normcase(str(entry_path.resolve()))
        if blocked_directory is not None and normalized == blocked_directory:
            continue
        if any(
            (entry_path / name).is_file()
            for name in ("codex", "codex.exe", "codex.cmd", "codex.bat")
        ):
            continue
        entries.append(entry)
    if os.path.normcase(runtime_bin) not in {
        os.path.normcase(str(Path(entry).expanduser())) for entry in entries
    }:
        entries.insert(0, runtime_bin)
    environment[path_key] = os.pathsep.join(entries)
    # Keep CODEX_HOME so App Server can let Codex read its own existing login.
    return environment


def _toml_inline_string_map(values: dict[str, str]) -> str:
    return "{ " + ", ".join(
        f"{json.dumps(key)} = {json.dumps(value)}"
        for key, value in values.items()
    ) + " }"


def _configured_mcp_server_names(
    codex_executable: str,
    *,
    project_root: Path,
    environment: dict[str, str],
) -> tuple[str, ...]:
    """Return only configured MCP ids so launch can disable each one."""
    command = [codex_executable, "-C", str(project_root.resolve())]
    for feature in _DISABLED_APP_SERVER_FEATURES:
        command.extend(["--disable", feature])
    command.extend(["mcp", "list", "--json"])
    try:
        result = subprocess.run(
            command,
            cwd=project_root.resolve(),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppServerError(
            f"failed to inspect inherited MCP configuration: {type(exc).__name__}"
        ) from exc
    if result.returncode != 0:
        detail = str(redact_auth_material(result.stderr or ""))[-1000:]
        suffix = f": {detail}" if detail else ""
        raise AppServerError(
            f"inherited MCP configuration probe failed with exit {result.returncode}{suffix}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AppServerError("inherited MCP configuration probe returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise AppServerError("inherited MCP configuration probe did not return a list")
    names: list[str] = []
    for item in payload:
        name = item.get("name") if isinstance(item, dict) else None
        if (
            not isinstance(name, str)
            or not name.strip()
            or name != name.strip()
            or len(name) > 256
            or any(ord(character) < 32 for character in name)
            or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None
        ):
            raise AppServerError("inherited MCP configuration contains an invalid server id")
        names.append(name)
    if len(names) != len(set(names)):
        raise AppServerError("inherited MCP configuration contains duplicate server ids")
    return tuple(sorted(names))


def app_server_command(
    codex_executable: str,
    *,
    project_root: Path | None = None,
    permission_profile: str = _DEFAULT_APP_SERVER_PERMISSION_PROFILE,
    mcp_server_names: tuple[str, ...] = (),
    model_shell_path: str | None = None,
    runtime_read_roots: tuple[Path, ...] = (),
    blocked_executable: Path | None = None,
) -> list[str]:
    root = (project_root or Path.cwd()).resolve()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", permission_profile):
        raise AppServerError("invalid App Server permission profile name")
    if model_shell_path is None:
        model_shell_path = app_server_environment().get("PATH", "")
    command = [
        codex_executable, "app-server", "--strict-config",
    ]
    filesystem_entries: dict[str, str] = {
        ":root": "deny",
        ":minimal": "read",
    }
    filesystem_entries[str(root)] = "read"
    for path in runtime_read_roots:
        filesystem_entries[str(path.resolve())] = "read"
    if blocked_executable is not None:
        filesystem_entries[str(blocked_executable.resolve())] = "deny"
    filesystem = _toml_inline_string_map(filesystem_entries)
    filesystem = filesystem[:-2] + (
        ', ":workspace_roots" = { "." = "write" } }'
    )
    config_overrides = (
        "allow_login_shell=false",
        "approval_policy=\"never\"",
        f"default_permissions={json.dumps(permission_profile)}",
        "project_doc_max_bytes=0",
        (
            f"projects={{ {json.dumps(str(root))} = "
            "{ trust_level = \"untrusted\" } }"
        ),
        "web_search=\"disabled\"",
        "tools.view_image=false",
        "shell_environment_policy.inherit=\"core\"",
        "shell_environment_policy.ignore_default_excludes=false",
        "shell_environment_policy.experimental_use_profile=false",
        "shell_environment_policy.filters=" + _toml_inline_string_map({
            pattern: "include" for pattern in _MODEL_SHELL_ENVIRONMENT_FILTERS
        }),
        "shell_environment_policy.set.PATH=" + json.dumps(model_shell_path),
        f"permissions.{permission_profile}.description=\"AMR controller-owned role\"",
        f"permissions.{permission_profile}.filesystem={filesystem}",
        f"permissions.{permission_profile}.network.enabled=false",
    )
    for override in config_overrides:
        command.extend(["-c", override])
    for feature in _DISABLED_APP_SERVER_FEATURES:
        command.extend(["--disable", feature])
    for name in sorted(set(mcp_server_names)):
        if re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
            raise AppServerError("invalid MCP server id for App Server isolation")
        command.extend([
            "-c", f"mcp_servers.{name}.enabled=false",
        ])
    command.append("--stdio")
    return command


class AppServerError(RuntimeError):
    pass


class AppServerTransportClosed(AppServerError):
    """The shared stdio transport is no longer safe for new requests."""


class AppServerRequestTimeout(AppServerTransportClosed):
    """A JSON-RPC request timed out, leaving shared transport health unknown."""

    def __init__(self, method: str, timeout: float):
        self.method = method
        self.timeout = timeout
        super().__init__(f"app-server request {method!r} exceeded {timeout} seconds")


class AppServerTurnTransportLost(AppServerTransportClosed):
    """A turn lost its transport after observable evidence was buffered."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        raw_output: str,
        token_usage: TokenUsage,
        token_telemetry: str,
        cause: Exception,
    ):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.raw_output = str(redact_auth_material(raw_output))
        self.token_usage = token_usage
        self.token_telemetry = token_telemetry
        self.server_error = {
            "code": "app_server_transport_closed",
            "message": str(redact_auth_material(str(cause))),
        }
        super().__init__(
            f"turn {turn_id} lost app-server transport: {self.server_error['message']}"
        )


class StructuredOutputProtocolError(AppServerError):
    """The completed model message was not exactly one JSON object."""


class UnmanagedContinuationError(AppServerError):
    """App Server started a turn that the controller did not request."""


class TurnOwnershipRegistry:
    """Correlate explicit controller turns and expose native continuations."""

    def __init__(self) -> None:
        self._open_threads: set[str] = set()
        self._owned_ids: dict[str, set[str]] = {}
        self._started_count: dict[str, int] = {}
        self.unmanaged_continuations: list[dict[str, str]] = []

    def begin_controller_turn(self, thread_id: str) -> None:
        if any(item["thread_id"] == thread_id for item in self.unmanaged_continuations):
            raise UnmanagedContinuationError(
                f"thread {thread_id} has an unmanaged continuation"
            )
        if thread_id in self._open_threads:
            raise UnmanagedContinuationError(
                f"thread {thread_id} already has a controller-owned turn"
            )
        self._open_threads.add(thread_id)
        self._owned_ids[thread_id] = set()
        self._started_count[thread_id] = 0

    def is_controller_turn_open(self, thread_id: str) -> bool:
        return thread_id in self._open_threads

    def observe_started(self, thread_id: str, turn_id: str) -> bool:
        if thread_id in self._open_threads:
            owned = self._owned_ids.setdefault(thread_id, set())
            if self._started_count.get(thread_id, 0) == 0:
                self._started_count[thread_id] = 1
                if turn_id:
                    owned.add(turn_id)
                return True
            # App Server notifications are at-least-once telemetry. Replaying
            # the same owned start is harmless; only a distinct turn is an
            # unmanaged continuation.
            if turn_id and turn_id in owned:
                return True
        self.unmanaged_continuations.append({
            "thread_id": thread_id,
            "turn_id": turn_id,
        })
        return False

    def bind_response(self, thread_id: str, turn_id: str) -> None:
        if thread_id not in self._open_threads:
            raise UnmanagedContinuationError(
                f"turn/start response for unowned thread {thread_id}"
            )
        if turn_id:
            self._owned_ids.setdefault(thread_id, set()).add(turn_id)

    def observe_completed(self, thread_id: str, turn_id: str) -> bool:
        if thread_id not in self._open_threads:
            return False
        owned = self._owned_ids.setdefault(thread_id, set())
        if (
            turn_id
            and self._started_count.get(thread_id, 0) > 0
            and turn_id not in owned
        ):
            self.unmanaged_continuations.append({
                "thread_id": thread_id,
                "turn_id": turn_id,
            })
            return False
        if turn_id:
            owned.add(turn_id)
        return True

    def finish_controller_turn(self, thread_id: str) -> None:
        self._open_threads.discard(thread_id)
        self._owned_ids.pop(thread_id, None)
        self._started_count.pop(thread_id, None)


class AppServerRequestError(AppServerError):
    """A JSON-RPC request was rejected by App Server.

    Keep the structured server payload available to the backend so a 4xx
    schema/protocol rejection cannot be flattened into a retryable transport
    error or replaced by a later parsing exception.
    """

    def __init__(self, server_error: Any):
        raw = (
            dict(server_error) if isinstance(server_error, dict)
            else {"message": str(server_error)}
        )
        self.server_error = redact_auth_material(raw)
        super().__init__(json.dumps(self.server_error, ensure_ascii=False))


class AppServerTurnFailed(AppServerError):
    def __init__(
        self,
        turn: dict[str, Any],
        *,
        raw_output: str | None = None,
        token_usage: TokenUsage | None = None,
        token_telemetry: str | None = None,
    ):
        # Keep the complete terminal stream payload.  The parsed server_error
        # below is convenient for classification, but it must not replace the
        # original event when the controller persists failure evidence.
        self.turn = redact_auth_material(dict(turn))
        self.turn_id = str(turn.get("id") or "")
        self.status = str(turn.get("status") or "failed")
        raw_error = self.turn.get("error")
        self.server_error = (
            dict(raw_error) if isinstance(raw_error, dict)
            else {"message": str(raw_error or "turn failed without an error message")}
        )
        self.raw_output = (
            str(redact_auth_material(raw_output)) if raw_output is not None else None
        )
        self.token_usage = token_usage
        self.token_telemetry = token_telemetry
        message = str(self.server_error.get("message") or "turn failed without an error message")
        super().__init__(
            f"turn/completed.status={self.status}: {message}"
        )


class AppServerTurnTimeout(TimeoutError):
    """A local turn deadline with all stream evidence observed before drain."""

    def __init__(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout: float,
        turn: dict[str, Any] | None,
        raw_output: str,
        token_usage: TokenUsage,
        token_telemetry: str,
        interrupt_error: Exception | None = None,
        did_not_stop: bool = False,
    ):
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.turn = redact_auth_material(dict(turn or {}))
        self.raw_output = str(redact_auth_material(raw_output))
        self.token_usage = token_usage
        self.token_telemetry = token_telemetry
        details: dict[str, Any] = {
            "code": "turn_timeout",
            "message": f"turn {turn_id} exceeded {timeout} seconds",
            "timeout_seconds": timeout,
            "turn_status": str(self.turn.get("status") or "unknown"),
            "did_not_stop_after_interrupt": did_not_stop,
        }
        if interrupt_error is not None:
            details["interrupt_error"] = str(redact_auth_material(str(interrupt_error)))
        self.server_error = redact_auth_material(details)
        message = str(self.server_error["message"])
        if did_not_stop:
            message += "; turn did not stop within the interrupt grace period"
        if interrupt_error is not None:
            message += f"; interrupt failed: {self.server_error['interrupt_error']}"
        super().__init__(message)


class ServiceTierPolicyError(AppServerError):
    def __init__(
        self,
        phase: str,
        observed_service_tier: str,
        requested_service_tier: str | None = None,
    ):
        self.phase = phase
        self.observed_service_tier = observed_service_tier
        self.requested_service_tier = requested_service_tier
        super().__init__(
            "service tier policy violation: "
            f"{phase} requested {requested_service_tier!r} but reported "
            f"serviceTier {observed_service_tier!r}"
        )


class ModelRoutePolicyError(AppServerError):
    """The server reported a route other than the exact requested model."""

    def __init__(
        self,
        phase: str,
        requested_model: str,
        observed_model: str,
        *,
        route_event: dict[str, Any] | None = None,
        turn: dict[str, Any] | None = None,
        raw_output: str | None = None,
        token_usage: TokenUsage | None = None,
        token_telemetry: str | None = None,
    ):
        self.phase = phase
        self.requested_model = requested_model
        self.observed_model = observed_model
        self.route_event = redact_auth_material(dict(route_event or {}))
        self.turn = redact_auth_material(dict(turn or {}))
        self.turn_id = str(self.turn.get("id") or "")
        self.raw_output = (
            str(redact_auth_material(raw_output)) if raw_output is not None else None
        )
        self.token_usage = token_usage
        self.token_telemetry = token_telemetry
        super().__init__(
            "model route policy violation: "
            f"{phase} requested {requested_model!r} but observed {observed_model!r}"
        )


def attest_service_tier(
    payload: Any,
    phase: str,
    requested_service_tier: str | None,
) -> str:
    """Attest an App Server tier response against the pinned request."""
    if not isinstance(payload, dict) or "serviceTier" not in payload:
        observed = "unobservable"
    else:
        observed = normalize_observed_service_tier(payload.get("serviceTier"))
    if observed not in allowed_observed_service_tiers(requested_service_tier):
        raise ServiceTierPolicyError(
            phase, observed, requested_service_tier=requested_service_tier,
        )
    return observed


def attest_no_service_tier(payload: Any, phase: str) -> str:
    """Backward-compatible strict attestation for a null tier request."""
    return attest_service_tier(payload, phase, None)


def attest_model_route(payload: Any, phase: str, requested_model: str) -> str:
    """Return the observed model or fail closed on an explicit mismatch."""
    if not isinstance(payload, dict):
        return "unobservable"
    raw = None
    for key in ("model", "modelId", "resolvedModel", "actualModel"):
        if isinstance(payload.get(key), str) and str(payload[key]).strip():
            raw = str(payload[key]).strip()
            break
    if raw is None:
        return "unobservable"
    if raw != requested_model:
        raise ModelRoutePolicyError(phase, requested_model, raw)
    return raw


class AppServerClient:
    """Thin JSONL client for the local Codex App Server stdio protocol."""

    def __init__(
        self,
        codex_executable: str = "codex",
        notification_handler: NotificationHandler | None = None,
        *,
        project_root: Path | None = None,
    ):
        self.codex_executable = _resolve_codex(codex_executable)
        self.notification_handler = notification_handler
        self.project_root = project_root.resolve() if project_root is not None else None
        self.permission_profile = f"amr-role-{uuid4().hex[:16]}"
        self.process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transport_generation = 0
        self._transport_alive = False
        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._turn_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._thread_turn_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._completed_thread_turns: dict[str, dict[str, Any]] = {}
        self._notification_turn_by_thread: dict[str, str] = {}
        self._response_turn_by_thread: dict[str, str] = {}
        self._turn_aliases: dict[str, str] = {}
        self._turn_started_callbacks: dict[str, Callable[[str], None]] = {}
        self._retired_turn_ids_by_thread: dict[str, set[str]] = {}
        self._messages: dict[str, list[str]] = {}
        self._token_usage: dict[str, TokenUsage] = {}
        self._thread_token_usage: dict[str, TokenUsage] = {}
        self._model_reroutes_by_thread: dict[str, list[dict[str, Any]]] = {}
        self.turn_ownership = TurnOwnershipRegistry()
        self._background_tasks: set[asyncio.Future[Any]] = set()
        self.stderr_lines: list[str] = []
        self.initialize_result: dict[str, Any] | None = None

    @property
    def transport_available(self) -> bool:
        process = self.process
        return bool(
            self._transport_alive
            and process is not None
            and process.poll() is None
            and process.stdin is not None
        )

    @staticmethod
    def _consume_future_exception(future: asyncio.Future[Any]) -> None:
        """Mark a terminal Future observed without changing its outcome."""
        if not future.done() or future.cancelled():
            return
        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _track_background(self, awaitable: Awaitable[Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(awaitable)
        self._background_tasks.add(task)

        def finished(completed: asyncio.Future[Any]) -> None:
            self._background_tasks.discard(completed)
            self._consume_future_exception(completed)

        task.add_done_callback(finished)
        return task

    def _discard_buffered_completion_for_thread(self, thread_id: str) -> None:
        buffered = self._completed_thread_turns.pop(thread_id, None)
        if buffered is None:
            return
        for turn_id, candidate in list(self._completed_turns.items()):
            if candidate is buffered:
                self._completed_turns.pop(turn_id, None)

    def _pop_buffered_completion(
        self, thread_id: str, turn_ids: tuple[str, ...],
    ) -> dict[str, Any] | None:
        buffered: dict[str, Any] | None = None
        for turn_id in turn_ids:
            if not turn_id:
                continue
            candidate = self._completed_turns.pop(turn_id, None)
            if buffered is None and candidate is not None:
                buffered = candidate
        thread_candidate = self._completed_thread_turns.pop(thread_id, None)
        if buffered is None:
            buffered = thread_candidate
        if buffered is None:
            return None
        # A completion received before turn/start's response is indexed by
        # both turn and thread because the two ids may differ on Windows.
        # Consuming either index must remove every alias of that same event.
        for turn_id, candidate in list(self._completed_turns.items()):
            if candidate is buffered:
                self._completed_turns.pop(turn_id, None)
        for candidate_thread, candidate in list(self._completed_thread_turns.items()):
            if candidate is buffered:
                self._completed_thread_turns.pop(candidate_thread, None)
        return buffered

    def _retire_turn_ids(self, thread_id: str, *turn_ids: str) -> None:
        retired = self._retired_turn_ids_by_thread.setdefault(thread_id, set())
        retired.update(turn_id for turn_id in turn_ids if turn_id)
        # App Server clients can survive many jobs in one campaign. Bound this
        # late-notification guard without affecting any realistically active
        # same-thread continuation.
        while len(self._retired_turn_ids_by_thread) > 4096:
            oldest_thread_id = next(iter(self._retired_turn_ids_by_thread))
            if oldest_thread_id == thread_id and len(self._retired_turn_ids_by_thread) > 1:
                oldest_thread_id = next(
                    item for item in self._retired_turn_ids_by_thread if item != thread_id
                )
            self._retired_turn_ids_by_thread.pop(oldest_thread_id, None)

    async def _interrupt_unmanaged_turn(self, thread_id: str, turn_id: str) -> None:
        """Best-effort containment after a native turn escapes ownership."""
        try:
            await self.interrupt(thread_id, turn_id)
        except Exception as exc:  # The controller has already failed closed.
            if self.notification_handler:
                result = self.notification_handler({
                    "method": "amr/unmanagedContinuationInterruptFailed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "errorType": type(exc).__name__,
                    },
                })
                if asyncio.iscoroutine(result):
                    await result

    def _report_unmanaged_turn(self, thread_id: str, turn_id: str) -> None:
        if self.transport_available:
            self._track_background(self._interrupt_unmanaged_turn(thread_id, turn_id))
        if self.notification_handler:
            result = self.notification_handler({
                "method": "amr/unmanagedContinuation",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "action": "interrupt_and_fail_closed",
                },
            })
            if asyncio.iscoroutine(result):
                self._track_background(result)

    def _forward_notification(self, message: dict[str, Any]) -> None:
        if self.notification_handler:
            result = self.notification_handler(redact_auth_material(message))
            if asyncio.iscoroutine(result):
                self._track_background(result)

    async def __aenter__(self) -> "AppServerClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self.process is not None:
            if self.transport_available:
                return
            await self.close()
        self._loop = asyncio.get_running_loop()
        self._transport_generation += 1
        generation = self._transport_generation
        if self.project_root is None or not self.project_root.is_dir():
            raise AppServerError("App Server launch requires an existing project root")
        environment = app_server_environment(
            blocked_executable=Path(self.codex_executable),
        )
        mcp_server_names = _configured_mcp_server_names(
            self.codex_executable,
            project_root=self.project_root,
            environment=environment,
        )
        path_key = next(
            (key for key in environment if key.upper() == "PATH"), "PATH",
        )
        process = subprocess.Popen(
            app_server_command(
                self.codex_executable,
                project_root=self.project_root,
                permission_profile=self.permission_profile,
                mcp_server_names=mcp_server_names,
                model_shell_path=str(environment.get(path_key) or ""),
                runtime_read_roots=(Path(sys.executable).resolve().parent,),
                blocked_executable=Path(self.codex_executable),
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=self.project_root,
            env=environment,
        )
        self.process = process
        self._transport_alive = True
        self.stderr_lines.clear()
        self._reader_thread = threading.Thread(
            target=self._read_stdout_sync, args=(process, generation), daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_sync, args=(process,), daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()
        try:
            self.initialize_result = await self.request("initialize", {
                "clientInfo": {
                    "name": "autonomous_math_research",
                    "title": "Autonomous Math AI",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            })
            await self.notify("initialized", {})
            await self._attest_permission_profile_available()
            await self._attest_no_mcp_servers()
        except Exception as exc:
            # The reader may observe stdout EOF slightly before the stderr
            # reader drains the actual startup diagnostic.
            await asyncio.sleep(0.05)
            detail = _redacted_stderr_tail(self.stderr_lines)
            await self.close()
            suffix = f"; app-server stderr: {detail}" if detail else ""
            raise AppServerError(f"App Server initialize failed: {exc}{suffix}") from exc

    async def _attest_permission_profile_available(self) -> None:
        cursor: str | None = None
        while True:
            response = await self.request("permissionProfile/list", {
                "cursor": cursor,
                "limit": 100,
                "cwd": str(self.project_root),
            })
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise AppServerError("App Server permission profile response is invalid")
            for item in response["data"]:
                if not isinstance(item, dict):
                    raise AppServerError("App Server permission profile entry is invalid")
                if item.get("id") == self.permission_profile:
                    if item.get("allowed") is not True:
                        raise AppServerError(
                            "controller-owned App Server permission profile is not allowed"
                        )
                    return
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                raise AppServerError(
                    "controller-owned App Server permission profile is unavailable"
                )
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppServerError("App Server permission profile cursor is invalid")
            cursor = next_cursor

    async def _attest_no_mcp_servers(self) -> None:
        cursor: str | None = None
        while True:
            response = await self.request("mcpServerStatus/list", {
                "cursor": cursor,
                "limit": 100,
                "detail": "full",
                "threadId": None,
            })
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise AppServerError("App Server MCP inventory response is invalid")
            for server in response["data"]:
                if not isinstance(server, dict):
                    raise AppServerError("App Server MCP inventory entry is invalid")
                if (
                    not isinstance(server.get("name"), str)
                    or not server["name"]
                    or server.get("authStatus") not in {
                        "unknown", "unsupported", "notLoggedIn",
                        "bearerToken", "oAuth",
                    }
                    or not isinstance(server.get("tools"), dict)
                    or not isinstance(server.get("resources"), list)
                    or not isinstance(server.get("resourceTemplates"), list)
                    or (
                        "serverInfo" in server
                        and server["serverInfo"] is not None
                        and not isinstance(server["serverInfo"], dict)
                    )
                ):
                    raise AppServerError("App Server MCP inventory entry is invalid")
                if (
                    server.get("tools")
                    or server.get("resources")
                    or server.get("resourceTemplates")
                    or server.get("serverInfo") is not None
                ):
                    raise AppServerError("App Server exposed an inherited MCP server")
            next_cursor = response.get("nextCursor")
            if next_cursor is None:
                return
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AppServerError("App Server MCP inventory cursor is invalid")
            cursor = next_cursor

    async def close(self) -> None:
        process = self.process
        generation = self._transport_generation
        if process is None:
            await self._drain_background_tasks()
            return
        self._transport_alive = False
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if process.poll() is None:
                process.kill()
                deadline = time.monotonic() + 2
                while process.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
        for stream in (process.stdout, process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
        if self.process is process:
            self.process = None
        self._fail_pending(generation, message="app-server closed")
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                await asyncio.to_thread(thread.join, 1.0)
        self._reader_thread = None
        self._stderr_thread = None
        await self._drain_background_tasks()

    async def _drain_background_tasks(self) -> None:
        tasks = list(self._background_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> Any:
        if not self.transport_available:
            raise AppServerTransportClosed("app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                # JSON-RPC ids remain correlated, but after one request stalls
                # the shared stdio server can no longer be assumed healthy.
                # Fail every co-tenant promptly so the controller contains the
                # provider once instead of dispatching a wave of 60s timeouts.
                self._fail_pending(
                    self._transport_generation,
                    message=f"app-server request {method!r} timed out",
                )
                raise AppServerRequestTimeout(method, timeout) from exc
        finally:
            self._pending.pop(request_id, None)
            self._consume_future_exception(future)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.transport_available:
            raise AppServerTransportClosed("app-server is not running")
        process = self.process
        assert process is not None and process.stdin is not None
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(payload.encode("utf-8"))
                process.stdin.flush()
        except OSError as exc:
            self._fail_pending(
                self._transport_generation,
                message=f"app-server write failed: {type(exc).__name__}",
            )
            raise AppServerTransportClosed("app-server stdin closed") from exc

    def _read_stdout_sync(
        self, process: subprocess.Popen[bytes], generation: int,
    ) -> None:
        assert process.stdout is not None
        try:
            while line := process.stdout.readline():
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._dispatch_message, message)
        except (OSError, ValueError):
            pass
        finally:
            if self._loop and not self._loop.is_closed():
                try:
                    self._loop.call_soon_threadsafe(
                        self._fail_pending, generation,
                    )
                except RuntimeError:
                    pass

    def _read_stderr_sync(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        try:
            while line := process.stderr.readline():
                text = line.decode("utf-8", errors="replace").rstrip()
                self.stderr_lines.append(text)
                if len(self.stderr_lines) > 500:
                    del self.stderr_lines[:100]
        except (OSError, ValueError):
            pass

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            future = self._pending.get(int(message["id"]))
            if future and not future.done():
                if "error" in message:
                    future.set_exception(AppServerRequestError(message["error"]))
                else:
                    future.set_result(message.get("result"))
            return
        if "id" in message and "method" in message:
            self._track_background(self._reject_server_request(message))
            return
        self._handle_notification(message)

    def _fail_pending(
        self, generation: int | None = None, *, message: str = "app-server stdout closed",
    ) -> None:
        if generation is not None and generation != self._transport_generation:
            return
        self._transport_alive = False
        futures: dict[int, asyncio.Future[Any]] = {}
        for future in (
            *self._pending.values(),
            *self._turn_waiters.values(),
            *self._thread_turn_waiters.values(),
        ):
            futures[id(future)] = future
        for future in futures.values():
            if not future.done():
                future.set_exception(AppServerTransportClosed(message))

    async def _reject_server_request(self, message: dict[str, Any]) -> None:
        # Autonomous runs use approvalPolicy=never and prompts forbid interactive
        # questions. Fail closed if a server-initiated request still appears.
        await self._send({
            "id": message["id"],
            "error": {"code": -32000, "message": "non-interactive autonomous client declined request"},
        })

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = str(params.get("threadId") or "")
        turn_payload = params.get("turn") or {}
        event_turn_id = str(
            params.get("turnId")
            or (turn_payload.get("id") if isinstance(turn_payload, dict) else "")
            or ""
        )
        if event_turn_id in self._retired_turn_ids_by_thread.get(thread_id, set()):
            # Late telemetry from a completed turn remains observable but must
            # not mutate buffers, usage, aliases, or ownership for its successor.
            self._forward_notification(message)
            return
        if method == "turn/started":
            turn = params.get("turn") or {}
            notification_turn_id = str(turn.get("id") or "")
            if thread_id and notification_turn_id and not self.turn_ownership.observe_started(
                thread_id, notification_turn_id,
            ):
                self._report_unmanaged_turn(thread_id, notification_turn_id)
                return
            if thread_id and notification_turn_id:
                self._notification_turn_by_thread[thread_id] = notification_turn_id
                response_turn_id = self._response_turn_by_thread.get(thread_id)
                if response_turn_id:
                    # Some Codex 0.147.0 Windows traces deliver turn/start's
                    # response before turn/started and use different ids.  Keep
                    # the late notification as the active cancellation target.
                    self._turn_aliases[response_turn_id] = notification_turn_id
                callback = self._turn_started_callbacks.get(thread_id)
                if callback:
                    callback(notification_turn_id)
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                self._messages.setdefault(params.get("turnId", ""), []).append(str(item.get("text", "")))
        elif method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            usage = TokenUsage.from_app_server(total)
            self._token_usage[params.get("turnId", "")] = usage
            if thread_id:
                self._thread_token_usage[thread_id] = usage
        elif method == "model/rerouted" and thread_id:
            self._model_reroutes_by_thread.setdefault(thread_id, []).append(
                redact_auth_material(dict(params))
            )
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = str(turn.get("id", ""))
            if thread_id:
                was_controller_owned = self.turn_ownership.is_controller_turn_open(thread_id)
                if not self.turn_ownership.observe_completed(thread_id, turn_id):
                    if was_controller_owned and turn_id:
                        self._report_unmanaged_turn(thread_id, turn_id)
                    return
            if thread_id and turn_id:
                self._notification_turn_by_thread[thread_id] = turn_id
            # A tool-using turn can emit several completed agentMessage items:
            # intermediate structured progress updates followed by the actual
            # final answer.  When the completion payload carries agentMessage
            # items, it is the authoritative final-turn snapshot and must
            # replace (not be appended to) the streamed history.
            completed_messages = [
                str(item.get("text", ""))
                for item in turn.get("items") or []
                if item.get("type") == "agentMessage"
            ]
            if completed_messages:
                self._messages[turn_id] = completed_messages
            waiter = self._turn_waiters.get(turn_id)
            thread_waiter = self._thread_turn_waiters.get(thread_id)
            matched_waiter = waiter is not None or thread_waiter is not None
            notified_waiters: set[int] = set()
            for candidate_waiter in (waiter, thread_waiter):
                if candidate_waiter is None or id(candidate_waiter) in notified_waiters:
                    continue
                notified_waiters.add(id(candidate_waiter))
                if not candidate_waiter.done():
                    candidate_waiter.set_result(params)
            if not matched_waiter:
                if turn_id:
                    self._completed_turns[turn_id] = params
                if thread_id:
                    self._completed_thread_turns[thread_id] = params
        self._forward_notification(message)

    async def start_thread(
        self,
        *,
        model: str,
        cwd: Path,
        writable_roots: list[Path],
        developer_instructions: str | None = None,
        service_tier: str | None = None,
    ) -> dict[str, Any]:
        roots = list(dict.fromkeys(
            str(path.resolve()) for path in [cwd, *writable_roots]
        ))
        params: dict[str, Any] = {
            "model": model,
            "cwd": str(cwd.resolve()),
            "approvalPolicy": "never",
            "permissions": self.permission_profile,
            "runtimeWorkspaceRoots": roots,
            "serviceName": "autonomous_math_research",
            "serviceTier": service_tier,
            "allowProviderModelFallback": False,
        }
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        response = await self.request("thread/start", params)
        active = response.get("activePermissionProfile") if isinstance(response, dict) else None
        if not isinstance(active, dict) or active.get("id") != self.permission_profile:
            raise AppServerError(
                "App Server did not attest the controller-owned permission profile"
            )
        return response

    async def set_goal(self, thread_id: str, objective: str, token_budget: int | None) -> dict[str, Any]:
        params: dict[str, Any] = {"threadId": thread_id, "objective": objective, "status": "active"}
        if token_budget is not None:
            params["tokenBudget"] = token_budget
        return await self.request("thread/goal/set", params)

    async def start_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        cwd: Path,
        model: str,
        effort: str,
        output_schema: dict[str, Any],
        writable_roots: list[Path],
        timeout: float,
        skill_path: Path | None = None,
        on_started: Callable[[str], None] | None = None,
        service_tier: str | None = None,
    ) -> tuple[dict[str, Any], str, TokenUsage, str]:
        # Last-resort wire boundary. Controller/bootstrap and both backends also
        # call this gate, but no caller can accidentally bypass it here.
        validate_output_schema_compatibility(
            output_schema, schema_path="turn/start.outputSchema",
        )
        inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if skill_path is not None:
            inputs.append({
                "type": "skill", "name": skill_path.parent.name,
                "path": str(skill_path.resolve()),
            })
        params = {
            "threadId": thread_id,
            "input": inputs,
            "cwd": str(cwd.resolve()),
            "approvalPolicy": "never",
            "permissions": self.permission_profile,
            "runtimeWorkspaceRoots": list(dict.fromkeys(
                str(path.resolve()) for path in [cwd, *writable_roots]
            )),
            "model": model,
            "effort": effort,
            "summary": "concise",
            "serviceTier": service_tier,
            "outputSchema": output_schema,
        }
        self._notification_turn_by_thread.pop(thread_id, None)
        self._response_turn_by_thread.pop(thread_id, None)
        self._discard_buffered_completion_for_thread(thread_id)
        self._model_reroutes_by_thread.pop(thread_id, None)
        self.turn_ownership.begin_controller_turn(thread_id)
        try:
            response = await self.request("turn/start", params)
        except asyncio.CancelledError:
            started_turn_id = self._notification_turn_by_thread.get(thread_id)
            if started_turn_id:
                try:
                    # Do not shield this coroutine. A second cancellation of
                    # the owning job must cancel and reap the interrupt request
                    # too, rather than leaving an unowned request Future behind.
                    await self.interrupt(thread_id, started_turn_id)
                except (asyncio.CancelledError, Exception):
                    pass
            self._retire_turn_ids(thread_id, started_turn_id or "")
            self.turn_ownership.finish_controller_turn(thread_id)
            raise
        except AppServerTransportClosed as start_error:
            # A turn/started notification can win the race with the JSON-RPC
            # turn/start response.  If stdout then closes, interrupting through
            # the same dead transport cannot establish containment and must not
            # be relabelled as a native/unmanaged continuation.  The controller
            # will close the local App Server process and restart in a fresh
            # epoch; retain any output/usage already observed on the wire.
            started_turn_id = self._notification_turn_by_thread.get(thread_id)
            self._retire_turn_ids(thread_id, started_turn_id or "")
            self.turn_ownership.finish_controller_turn(thread_id)
            self._discard_buffered_completion_for_thread(thread_id)
            self._model_reroutes_by_thread.pop(thread_id, None)
            messages = (
                self._messages.pop(started_turn_id, [])
                if started_turn_id else []
            )
            usage = self._thread_token_usage.pop(thread_id, None)
            turn_usage = (
                self._token_usage.pop(started_turn_id, None)
                if started_turn_id else None
            )
            usage = usage or turn_usage
            if started_turn_id:
                raise AppServerTurnTransportLost(
                    thread_id=thread_id,
                    turn_id=started_turn_id,
                    raw_output=messages[-1] if messages else "",
                    token_usage=usage or TokenUsage(),
                    token_telemetry="observed" if usage is not None else "unknown",
                    cause=start_error,
                ) from start_error
            raise
        except Exception as start_error:
            started_turn_id = self._notification_turn_by_thread.get(thread_id)
            containment_error: Exception | None = None
            if started_turn_id:
                try:
                    await self.interrupt(thread_id, started_turn_id)
                except Exception as exc:
                    containment_error = exc
            self._retire_turn_ids(thread_id, started_turn_id or "")
            self.turn_ownership.finish_controller_turn(thread_id)
            self._discard_buffered_completion_for_thread(thread_id)
            if containment_error is not None:
                raise UnmanagedContinuationError(
                    "turn/start failed after the remote turn began and containment failed"
                ) from start_error
            raise
        turn = response["turn"]
        response_turn_id = str(turn["id"])
        self.turn_ownership.bind_response(thread_id, response_turn_id)
        self._response_turn_by_thread[thread_id] = response_turn_id
        # Officially the turn/start response id and streamed turn id are the
        # same.  Codex 0.147.0 on Windows has been observed returning a rollout
        # id in the response while turn/started and turn/completed use another
        # id.  A thread can have only one active turn, so correlate completion
        # by thread and retain an alias for steering/interruption.
        turn_id = self._notification_turn_by_thread.get(thread_id, response_turn_id)
        self._turn_aliases[response_turn_id] = turn_id
        waiter = asyncio.get_running_loop().create_future()
        self._turn_waiters[turn_id] = waiter
        self._thread_turn_waiters[thread_id] = waiter
        buffered_completion = self._pop_buffered_completion(
            thread_id, (turn_id, response_turn_id),
        )
        if str(turn.get("status") or "").lower() in {"completed", "failed", "interrupted"}:
            waiter.set_result({"threadId": thread_id, "turn": turn})
        elif buffered_completion is not None:
            waiter.set_result(buffered_completion)
        if on_started:
            self._turn_started_callbacks[thread_id] = on_started
            on_started(turn_id)
        completed: dict[str, Any] | None = None
        timed_out = False
        transport_error: AppServerTransportClosed | None = None
        interrupt_error: Exception | None = None
        did_not_stop = False
        try:
            completed = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.CancelledError:
            try:
                # The caller owns containment through completion. Shielding
                # here can orphan the interrupt task if shutdown cancels the
                # caller a second time while App Server is closing.
                await self.interrupt(thread_id, turn_id)
            except (asyncio.CancelledError, Exception):
                pass
            raise
        except AppServerTransportClosed as exc:
            transport_error = exc
        except asyncio.TimeoutError:
            timed_out = True
            try:
                await self.interrupt(thread_id, turn_id)
            except Exception as exc:  # Preserve the transport failure with stream evidence.
                interrupt_error = exc
            try:
                completed = await asyncio.wait_for(asyncio.shield(waiter), timeout=15)
            except asyncio.TimeoutError:
                did_not_stop = True
            except AppServerTransportClosed as exc:
                transport_error = exc
                interrupt_error = interrupt_error or exc
                did_not_stop = True
        finally:
            self._turn_waiters.pop(turn_id, None)
            self._thread_turn_waiters.pop(thread_id, None)
            self._turn_started_callbacks.pop(thread_id, None)
            if not waiter.done():
                waiter.cancel()
            else:
                # asyncio.shield leaves the underlying waiter alive when the
                # owner is cancelled.  EOF can then complete it with an
                # exception that no coroutine awaits; retrieve it explicitly.
                self._consume_future_exception(waiter)
            self.turn_ownership.finish_controller_turn(thread_id)
        terminal_turn = ((completed or {}).get("turn") or {})
        completed_turn_id = str(
            terminal_turn.get("id")
            or self._notification_turn_by_thread.get(thread_id)
            or turn_id
        )
        self._retire_turn_ids(
            thread_id, response_turn_id, turn_id, completed_turn_id,
        )
        self._turn_aliases[response_turn_id] = completed_turn_id
        # Structured output is one JSON document.  Do not concatenate every
        # agentMessage emitted during a tool-using turn: each message can be a
        # separately valid JSON document, and joining them produces an invalid
        # `JSON\nJSON` payload.  Prefer the completed turn id, retain the
        # response/stream-id fallbacks, and discard all alias buffers.
        messages: list[str] | None = None
        seen_message_ids: set[str] = set()
        for candidate_turn_id in (completed_turn_id, turn_id, response_turn_id):
            if not candidate_turn_id or candidate_turn_id in seen_message_ids:
                continue
            seen_message_ids.add(candidate_turn_id)
            candidate_messages = self._messages.pop(candidate_turn_id, [])
            if messages is None and candidate_messages:
                messages = candidate_messages
        text = messages[-1] if messages else ""
        usage: TokenUsage | None = self._thread_token_usage.pop(thread_id, None)
        seen_usage_ids: set[str] = set()
        for candidate_turn_id in (completed_turn_id, turn_id, response_turn_id):
            if not candidate_turn_id or candidate_turn_id in seen_usage_ids:
                continue
            seen_usage_ids.add(candidate_turn_id)
            candidate_usage = self._token_usage.pop(candidate_turn_id, None)
            if usage is None and candidate_usage is not None:
                usage = candidate_usage
        telemetry = "observed" if usage is not None else "unknown"
        usage = usage or TokenUsage()
        reroutes = self._model_reroutes_by_thread.pop(thread_id, [])
        if reroutes:
            route_event = reroutes[-1]
            observed_model = next(
                (
                    str(route_event[key])
                    for key in ("toModel", "model", "newModel", "resolvedModel")
                    if isinstance(route_event.get(key), str)
                    and str(route_event[key]).strip()
                ),
                "<model/rerouted event without target model>",
            )
            raise ModelRoutePolicyError(
                "model/rerouted",
                model,
                observed_model,
                route_event=route_event,
                turn=terminal_turn,
                raw_output=text,
                token_usage=usage,
                token_telemetry=telemetry,
            )
        if transport_error is not None:
            raise AppServerTurnTransportLost(
                thread_id=thread_id,
                turn_id=completed_turn_id,
                raw_output=text,
                token_usage=usage,
                token_telemetry=telemetry,
                cause=transport_error,
            ) from transport_error
        if timed_out:
            if str(terminal_turn.get("status") or "").lower() == "failed":
                raise AppServerTurnFailed(
                    terminal_turn,
                    raw_output=text,
                    token_usage=usage,
                    token_telemetry=telemetry,
                )
            raise AppServerTurnTimeout(
                thread_id=thread_id,
                turn_id=completed_turn_id,
                timeout=timeout,
                turn=terminal_turn or None,
                raw_output=text,
                token_usage=usage,
                token_telemetry=telemetry,
                interrupt_error=interrupt_error,
                did_not_stop=did_not_stop,
            )
        assert completed is not None
        return completed, text, usage, telemetry

    async def steer(self, thread_id: str, turn_id: str, text: str) -> Any:
        turn_id = self._turn_aliases.get(turn_id, turn_id)
        return await self.request("turn/steer", {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text}],
        })

    async def interrupt(self, thread_id: str, turn_id: str) -> Any:
        turn_id = self._turn_aliases.get(turn_id, turn_id)
        # Prefer the newest streamed id for the thread.  This closes the race
        # where turn/start responds before a mismatched turn/started id arrives.
        turn_id = self._notification_turn_by_thread.get(thread_id, turn_id)
        return await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def probe_capabilities(self, cwd: Path) -> dict[str, Any]:
        calls = {
            "account": ("account/read", {"refreshToken": False}),
            "rate_limits": ("account/rateLimits/read", None),
            "usage": ("account/usage/read", None),
            "permission_profiles": ("permissionProfile/list", {"cwd": str(cwd.resolve())}),
            "requirements": ("configRequirements/read", {}),
            "models": ("model/list", {"limit": 100}),
        }
        result: dict[str, Any] = {}
        for key, (method, params) in calls.items():
            try:
                value = await self.request(method, params, timeout=30)
                if key == "account":
                    value = _redact_account(value)
                result[key] = {
                    "supported": True,
                    "result": redact_auth_material(value),
                }
            except Exception as exc:
                result[key] = {
                    "supported": False,
                    "error": str(redact_auth_material(str(exc))),
                }
        result["initialize"] = redact_auth_material(self.initialize_result)
        return result


def _redact_account(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    safe = dict(value)
    account = safe.get("account")
    if isinstance(account, dict):
        safe["account"] = {
            key: val for key, val in account.items()
            if key in {"type", "planType", "requiresOpenaiAuth", "isTeam", "workspaceType"}
        }
    for secret in ("accessToken", "refreshToken", "apiKey", "token"):
        safe.pop(secret, None)
    return safe


def _resolve_codex(value: str) -> str:
    if os.name == "nt" and value == "codex":
        npm_shim = shutil.which("codex.cmd")
        if npm_shim:
            native = (
                Path(npm_shim).parent
                / "node_modules" / "@openai" / "codex" / "node_modules"
                / "@openai" / "codex-win32-x64" / "vendor"
                / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
            )
            if native.exists():
                return str(native)
            return npm_shim
        return shutil.which("codex.exe") or value
    return shutil.which(value) or value


def parse_structured_message(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise StructuredOutputProtocolError("agent returned an empty structured output")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputProtocolError(
            f"agent output is not exactly one JSON value: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise StructuredOutputProtocolError("agent output must be a JSON object")
    return value
