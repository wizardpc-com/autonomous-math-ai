from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any

from .models import TokenUsage
from .schema import validate_output_schema_compatibility


NotificationHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

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


def app_server_environment() -> dict[str, str]:
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
    # Keep CODEX_HOME so App Server can let Codex read its own existing login.
    return environment


class AppServerError(RuntimeError):
    pass


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
    def __init__(self, phase: str, observed_service_tier: str):
        self.phase = phase
        self.observed_service_tier = observed_service_tier
        super().__init__(
            f"no-fast/no-priority policy violation: {phase} reported non-empty serviceTier "
            f"{observed_service_tier!r}"
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


def attest_no_service_tier(payload: Any, phase: str) -> str:
    """Return an auditable tier observation and fail on every non-empty tier.

    The current App Server schema permits an explicit JSON null. Missing telemetry
    remains unobservable; null or an empty string is recorded as none. Any other
    response means the no-explicit-tier request was not honored and must stop the
    job before further work whenever this check runs before turn/start.
    """
    if not isinstance(payload, dict) or "serviceTier" not in payload:
        return "unobservable"
    raw = payload.get("serviceTier")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "none"
    observed = str(raw)
    raise ServiceTierPolicyError(phase, observed)


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

    def __init__(self, codex_executable: str = "codex", notification_handler: NotificationHandler | None = None):
        self.codex_executable = _resolve_codex(codex_executable)
        self.notification_handler = notification_handler
        self.process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
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
        self.stderr_lines: list[str] = []
        self.initialize_result: dict[str, Any] | None = None

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
        if self.process is not None:
            asyncio.create_task(self._interrupt_unmanaged_turn(thread_id, turn_id))
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
                asyncio.create_task(result)

    def _forward_notification(self, message: dict[str, Any]) -> None:
        if self.notification_handler:
            result = self.notification_handler(redact_auth_material(message))
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    async def __aenter__(self) -> "AppServerClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self.process is not None:
            return
        self._loop = asyncio.get_running_loop()
        self.process = subprocess.Popen(
            [self.codex_executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=app_server_environment(),
        )
        self._reader_thread = threading.Thread(target=self._read_stdout_sync, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr_sync, daemon=True)
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
        except Exception as exc:
            # The reader may observe stdout EOF slightly before the stderr
            # reader drains the actual startup diagnostic.
            await asyncio.sleep(0.05)
            detail = _redacted_stderr_tail(self.stderr_lines)
            await self.close()
            suffix = f"; app-server stderr: {detail}" if detail else ""
            raise AppServerError(f"App Server initialize failed: {exc}{suffix}") from exc

    async def close(self) -> None:
        if self.process is None:
            return
        process = self.process
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
        self.process = None

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> Any:
        if self.process is None:
            raise AppServerError("app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            self.process.stdin.write(payload.encode("utf-8"))
            self.process.stdin.flush()

    def _read_stdout_sync(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := self.process.stdout.readline():
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if self._loop and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._dispatch_message, message)
        finally:
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._fail_pending)

    def _read_stderr_sync(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := self.process.stderr.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            self.stderr_lines.append(text)
            if len(self.stderr_lines) > 500:
                del self.stderr_lines[:100]

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
            asyncio.create_task(self._reject_server_request(message))
            return
        self._handle_notification(message)

    def _fail_pending(self) -> None:
        error = AppServerError("app-server stdout closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for future in self._turn_waiters.values():
            if not future.done():
                future.set_exception(error)
        for future in self._thread_turn_waiters.values():
            if not future.done():
                future.set_exception(error)

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
        sandbox: str = "workspace-write",
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "cwd": str(cwd.resolve()),
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "serviceName": "autonomous_math_research",
            "serviceTier": None,
            "allowProviderModelFallback": False,
        }
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        return await self.request("thread/start", params)

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
    ) -> tuple[dict[str, Any], str, TokenUsage, str]:
        # Last-resort wire boundary. Controller/bootstrap and both backends also
        # call this gate, but no caller can accidentally bypass it here.
        validate_output_schema_compatibility(
            output_schema, schema_path="turn/start.outputSchema",
        )
        inputs: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        if skill_path is not None:
            inputs.append({
                "type": "skill", "name": "math-research",
                "path": str(skill_path.resolve()),
            })
        params = {
            "threadId": thread_id,
            "input": inputs,
            "cwd": str(cwd.resolve()),
            "approvalPolicy": "never",
            "sandboxPolicy": {
                "type": "workspaceWrite",
                "writableRoots": [str(path.resolve()) for path in writable_roots],
                "networkAccess": False,
            },
            "model": model,
            "effort": effort,
            "summary": "concise",
            "serviceTier": None,
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
                    await asyncio.shield(self.interrupt(thread_id, started_turn_id))
                except (asyncio.CancelledError, Exception):
                    pass
            self._retire_turn_ids(thread_id, started_turn_id or "")
            self.turn_ownership.finish_controller_turn(thread_id)
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
        interrupt_error: Exception | None = None
        did_not_stop = False
        try:
            completed = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self.interrupt(thread_id, turn_id))
            except (asyncio.CancelledError, Exception):
                pass
            raise
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
        finally:
            self._turn_waiters.pop(turn_id, None)
            self._thread_turn_waiters.pop(thread_id, None)
            self._turn_started_callbacks.pop(thread_id, None)
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
