from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.metadata import entry_points
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .app_server import (
    ModelRoutePolicyError, ServiceTierPolicyError, parse_structured_message,
    redact_auth_material,
)
from .backend import (
    AppServerBackend, CandidateSink, CodexBackend, TurnController,
    _classify_failure, _provider_quota_details,
)
from .config import HarnessConfig
from .models import JobOutcome, ResearchTask, TokenUsage
from .provider_config import (
    allowed_observed_service_tiers, normalize_observed_service_tier,
)
from .schema import validate, validate_output_schema_compatibility


class ProviderTransportError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, payload: Any = None):
        super().__init__(message)
        self.status = int(status)
        self.payload = redact_auth_material(payload)


class HttpTransport(Protocol):
    async def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


CredentialResolver = Callable[[dict[str, Any], dict[str, Any]], str | None]


def _path_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def normalize_usage(raw: Any, mapping: dict[str, list[str]]) -> tuple[TokenUsage, str]:
    usage = TokenUsage()
    observed = 0
    for field_name, paths in mapping.items():
        for path in paths:
            value = _path_value(raw, path)
            if type(value) is int and value >= 0:
                setattr(usage, field_name, int(value))
                observed += 1
                break
    if usage.uncached_input_tokens == 0 and (
        usage.input_tokens or usage.cached_input_tokens
    ):
        usage.uncached_input_tokens = max(
            0, usage.input_tokens - usage.cached_input_tokens,
        )
    if usage.total_tokens == 0 and (usage.input_tokens or usage.output_tokens):
        usage.total_tokens = usage.input_tokens + usage.output_tokens
    return usage, "observed" if observed else "unknown"


def _set_nested(target: dict[str, Any], dotted: str | None, value: Any) -> None:
    if not dotted:
        return
    parts = dotted.split(".")
    current = target
    for part in parts[:-1]:
        child = current.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"provider parameter path collides at {dotted!r}")
        current = child
    current[parts[-1]] = value


def _response_text(response: dict[str, Any], api_style: str) -> str:
    if isinstance(response.get("output_text"), str):
        return str(response["output_text"])
    if api_style == "chat_completions":
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = (choices[0].get("message") or {}).get("content")
            if isinstance(content, str):
                return content
    pieces: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                pieces.append(text)
    if pieces:
        return "\n".join(pieces)
    raise ValueError("provider response did not contain normalized text output")


async def _stdlib_http_transport(
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    def send() -> dict[str, Any]:
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                body = exc.read(65536).decode("utf-8", errors="replace")
                decoded: Any = json.loads(body)
            except (OSError, json.JSONDecodeError):
                decoded = {"message": "provider returned a non-JSON HTTP error"}
            raise ProviderTransportError(
                f"provider HTTP request failed with status {exc.code}",
                status=exc.code,
                payload=decoded,
            ) from exc
        except URLError as exc:
            raise ProviderTransportError("provider transport failed", payload={"reason": str(exc.reason)}) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderTransportError("provider returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ProviderTransportError("provider response must be a JSON object")
        return decoded

    return await asyncio.to_thread(send)


def _default_credential_resolver(
    credential: dict[str, Any], _provider: dict[str, Any],
) -> str | None:
    kind = credential.get("kind")
    reference = credential.get("reference")
    if kind == "none":
        return None
    if kind == "environment":
        return os.environ.get(str(reference))
    raise ValueError(
        f"credential reference kind {kind!r} requires a provider-specific adapter/profile resolver"
    )


class OpenAICompatibleBackend:
    """OpenAI-compatible HTTP adapter normalized to the common role contract."""

    def __init__(
        self,
        config: HarnessConfig,
        provider_name: str,
        *,
        transport: HttpTransport | None = None,
        credential_resolver: CredentialResolver | None = None,
    ):
        self.config = config
        self.provider_name = provider_name
        self.provider = dict(config.raw["providers"][provider_name])
        self.transport = transport or _stdlib_http_transport
        self.credential_resolver = credential_resolver or _default_credential_resolver
        self.active: set[str] = set()

    def _model_for(self, role: str) -> tuple[str, str]:
        route = self.config.route_for(role)
        return str(route["model"]), str(route["mapped_effort"])

    def set_economy_mode(self, enabled: bool) -> None:
        del enabled

    def supports_same_thread_continuation(self, role: str) -> bool:
        del role
        return False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.active.clear()

    def _request(self, route: dict[str, Any], prompt: str, schema: dict[str, Any]) -> tuple[str, dict[str, str], dict[str, Any], str]:
        capabilities = self.provider["capabilities"]
        api_style = str(capabilities.get("api_style") or "responses")
        endpoint = str(route.get("endpoint") or self.provider.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise ValueError(f"provider {self.provider_name} has no endpoint")
        suffix = "/chat/completions" if api_style == "chat_completions" else "/responses"
        if not endpoint.endswith(suffix):
            endpoint += suffix
        token = self.credential_resolver(self.provider["credential"], self.provider)
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif self.provider["credential"].get("kind") != "none":
            raise ValueError(
                f"credential reference {self.provider['credential'].get('reference')!r} is unavailable"
            )
        payload: dict[str, Any] = {"model": route["model"]}
        if api_style == "chat_completions":
            payload["messages"] = [{"role": "user", "content": prompt}]
        else:
            payload["input"] = prompt
        reasoning = capabilities["reasoning"]
        _set_nested(payload, reasoning.get("parameter"), route["mapped_effort"])
        if route.get("service_tier") not in {None, ""}:
            _set_nested(
                payload, capabilities.get("service_tier_parameter"),
                route["service_tier"],
            )
        output_mode = route.get("output_mode", "auto")
        if output_mode == "auto":
            output_mode = capabilities["structured_outputs"]
        if output_mode == "native_json_schema":
            name = re.sub(r"[^A-Za-z0-9_-]", "_", str(route["model"]))[:64] or "amr_output"
            if api_style == "chat_completions":
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": name, "strict": True, "schema": schema},
                }
            else:
                payload["text"] = {
                    "format": {
                        "type": "json_schema", "name": name,
                        "strict": True, "schema": schema,
                    }
                }
        elif output_mode == "json_text":
            payload["input" if api_style != "chat_completions" else "messages"] = (
                f"{prompt}\nReturn exactly one JSON object matching the supplied local schema."
                if api_style != "chat_completions"
                else [{
                    "role": "user",
                    "content": f"{prompt}\nReturn exactly one JSON object matching the supplied local schema.",
                }]
            )
        else:
            raise ValueError("provider has no configured structured-output normalization mode")
        return endpoint, headers, payload, api_style

    async def run_job(
        self,
        *,
        job_id: str,
        task: ResearchTask,
        prompt: str,
        output_schema: dict[str, Any],
        workspace: Path,
        writable_roots: list[Path],
        timeout: float,
        token_budget: int | None,
        candidate_sink: CandidateSink,
        skill_path: Path | None = None,
        turn_controller: TurnController | None = None,
    ) -> JobOutcome:
        del workspace, writable_roots, token_budget, candidate_sink, skill_path, turn_controller
        route = self.config.route_for(task.role)
        model, effort = self._model_for(task.role)
        usage = TokenUsage()
        token_telemetry = "unknown"
        raw_output = ""
        cost_usd: float | None = None
        cost_telemetry = "unknown"
        response: dict[str, Any] = {}
        self.active.add(job_id)
        try:
            if route["provider"] != self.provider_name:
                raise ValueError("provider router dispatched a role to the wrong adapter")
            validate_output_schema_compatibility(
                output_schema, schema_path=f"{task.output_contract} ({task.role})",
            )
            endpoint, headers, payload, api_style = self._request(route, prompt, output_schema)
            response = await self.transport(endpoint, headers, payload, timeout)
            observed_model = response.get("model")
            if isinstance(observed_model, str) and observed_model and observed_model != model:
                raise ModelRoutePolicyError("provider_response", model, observed_model)
            observed_tier = response.get("service_tier", response.get("serviceTier"))
            requested_tier = route.get("service_tier")
            observed_tier_label = (
                "unobservable"
                if "service_tier" not in response and "serviceTier" not in response
                else normalize_observed_service_tier(observed_tier)
            )
            if observed_tier_label not in allowed_observed_service_tiers(requested_tier):
                raise ServiceTierPolicyError(
                    "provider_response", observed_tier_label,
                    requested_service_tier=requested_tier,
                )
            raw_output = _response_text(response, api_style)
            parsed = parse_structured_message(raw_output)
            validate(parsed, output_schema)
            usage, token_telemetry = normalize_usage(
                response.get("usage") or {},
                self.provider["capabilities"]["usage_mapping"],
            )
            cost_path = self.provider["capabilities"].get("cost_path")
            raw_cost = _path_value(response, str(cost_path)) if cost_path else None
            if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and raw_cost >= 0:
                cost_usd = float(raw_cost)
                cost_telemetry = "observed"
            return JobOutcome(
                job_id=job_id, task_id=task.task_id, role=task.role,
                claim_id=task.target_claim, status="completed", result=parsed,
                thread_id=(str(response.get("id")) if response.get("id") else None),
                turn_id=(str(response.get("id")) if response.get("id") else None),
                model=model, reasoning_effort=effort,
                provider=self.provider_name, provider_profile=route.get("profile"),
                requested_service_tier=requested_tier,
                observed_service_tier=observed_tier_label,
                token_usage=usage, token_telemetry=token_telemetry,
                cost_usd=cost_usd, cost_telemetry=cost_telemetry,
                artifact_paths=list(parsed.get("artifact_paths", [])),
            )
        except Exception as exc:
            if isinstance(exc, ProviderTransportError):
                details = {"http_status": exc.status, "error": exc.payload}
                quota_details = _provider_quota_details(details)
                if quota_details is not None:
                    failure_kind, retryable = "provider_quota_exhausted", False
                    details = quota_details
                elif exc.status == 429:
                    failure_kind, retryable = "rate_limit", True
                elif exc.status in {401, 403}:
                    failure_kind, retryable = "access_denied", False
                elif exc.status == 404:
                    failure_kind, retryable = "model_or_endpoint_unavailable", False
                elif exc.status >= 500 or exc.status == 0:
                    failure_kind, retryable = "transport_transient", True
                else:
                    failure_kind, retryable = "provider_request", False
            else:
                failure_kind, retryable, details = _classify_failure(exc)
            return JobOutcome(
                job_id=job_id, task_id=task.task_id, role=task.role,
                claim_id=task.target_claim, status="ERROR", result={},
                model=model, reasoning_effort=effort,
                provider=self.provider_name, provider_profile=route.get("profile"),
                requested_service_tier=route.get("service_tier"),
                observed_service_tier=(
                    str(response.get("service_tier", response.get("serviceTier")))
                    if response.get("service_tier", response.get("serviceTier")) else None
                ),
                token_usage=usage, token_telemetry=token_telemetry,
                cost_usd=cost_usd, cost_telemetry=cost_telemetry,
                error=str(redact_auth_material(str(exc))),
                failure_kind=failure_kind, retryable=retryable,
                server_error=redact_auth_material(details),
                raw_output=str(redact_auth_material(raw_output)) or None,
            )
        finally:
            self.active.discard(job_id)

    async def cancel(self, job_id: str) -> bool:
        return job_id in self.active

    async def rate_limits(self) -> dict[str, Any] | None:
        return None


class ProviderRouterBackend:
    """Dispatch each role through its declared provider adapter."""

    def __init__(
        self,
        config: HarnessConfig,
        trace_notification: Callable[[dict[str, Any]], Any] | None = None,
        *,
        adapter_overrides: dict[str, CodexBackend] | None = None,
        roles: set[str] | None = None,
    ):
        self.config = config
        self.adapters: dict[str, CodexBackend] = dict(adapter_overrides or {})
        active_providers = {
            str(route["provider"])
            for role, route in config.raw["models"].items()
            if roles is None or role in roles
        }
        factories: dict[str, Callable[..., CodexBackend]] = {}
        for item in entry_points(group="autonomous_math_research.providers"):
            factories[item.name] = item.load()
        for provider_name in sorted(active_providers):
            if provider_name in self.adapters:
                continue
            provider = config.raw["providers"][provider_name]
            adapter = provider["adapter"]
            if adapter == "codex_app_server":
                self.adapters[provider_name] = AppServerBackend(
                    config, trace_notification, provider_name=provider_name,
                )
            elif adapter == "openai_compatible":
                self.adapters[provider_name] = OpenAICompatibleBackend(
                    config, provider_name,
                )
            elif adapter in factories:
                self.adapters[provider_name] = factories[adapter](
                    config=config,
                    provider_name=provider_name,
                    trace_notification=trace_notification,
                )
            else:
                raise ValueError(f"provider adapter {adapter!r} is not installed")
        self._active_provider: dict[str, str] = {}

    def _model_for(self, role: str) -> tuple[str, str]:
        route = self.config.route_for(role)
        return str(route["model"]), str(route["mapped_effort"])

    @property
    def active(self) -> dict[str, tuple[str, str]]:
        """Expose App Server bindings without coupling the controller to adapters."""
        combined: dict[str, tuple[str, str]] = {}
        for adapter in self.adapters.values():
            active = getattr(adapter, "active", {})
            if not isinstance(active, dict):
                continue
            for job_id, binding in active.items():
                if (
                    isinstance(binding, tuple) and len(binding) == 2
                    and all(isinstance(item, str) for item in binding)
                ):
                    combined[str(job_id)] = binding
        return combined

    def set_economy_mode(self, enabled: bool) -> None:
        for adapter in self.adapters.values():
            adapter.set_economy_mode(enabled)

    def supports_same_thread_continuation(self, role: str) -> bool:
        provider_name = str(self.config.route_for(role)["provider"])
        adapter = self.adapters[provider_name]
        checker = getattr(adapter, "supports_same_thread_continuation", None)
        return bool(callable(checker) and checker(role))

    async def start(self) -> None:
        await asyncio.gather(*(adapter.start() for adapter in self.adapters.values()))

    async def close(self) -> None:
        await asyncio.gather(
            *(adapter.close() for adapter in self.adapters.values()),
            return_exceptions=True,
        )

    async def run_job(self, *, job_id: str, task: ResearchTask, **kwargs: Any) -> JobOutcome:
        provider_name = str(self.config.route_for(task.role)["provider"])
        adapter = self.adapters[provider_name]
        self._active_provider[job_id] = provider_name
        try:
            return await adapter.run_job(job_id=job_id, task=task, **kwargs)
        finally:
            self._active_provider.pop(job_id, None)

    async def cancel(self, job_id: str) -> bool:
        provider = self._active_provider.get(job_id)
        if provider is None:
            return False
        return await self.adapters[provider].cancel(job_id)

    async def rate_limits(self) -> dict[str, Any] | None:
        provider_names = list(self.adapters)
        results = await asyncio.gather(
            *(self.adapters[name].rate_limits() for name in provider_names),
            return_exceptions=True,
        )
        normalized = {
            name: result
            for name, result in zip(provider_names, results, strict=True)
            if isinstance(result, dict)
        }
        return {"providers": normalized} if normalized else None

    def app_server_backend(self) -> AppServerBackend | None:
        return next(
            (item for item in self.adapters.values() if isinstance(item, AppServerBackend)),
            None,
        )

    async def interrupt_remote(self, thread_id: str, turn_id: str) -> None:
        backend = self.app_server_backend()
        if backend is None:
            raise ValueError("no Codex App Server provider is active")
        await backend.client.interrupt(thread_id, turn_id)

    async def probe_capabilities(self, project_root: Path) -> dict[str, Any] | None:
        backend = self.app_server_backend()
        if backend is None:
            return None
        return await backend.client.probe_capabilities(project_root)
