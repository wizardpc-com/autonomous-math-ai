from __future__ import annotations

from copy import deepcopy
from importlib.metadata import entry_points
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROVIDER_CAPABILITY_VERSION = 1
CANONICAL_EFFORTS = frozenset({
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
})
SUPPORTED_PROVIDER_ADAPTERS = frozenset({
    "codex_app_server", "openai_compatible",
})
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{1,127}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_PLAINTEXT_SECRET_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret",
    "access_token", "refresh_token", "bearer_token", "credential_value",
})
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
_FORBIDDEN_SERVICE_TIERS = frozenset({"priority", "auto", "ultrafast"})


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"[REDACTED]@{host}", parsed.path, parsed.query, parsed.fragment))


def redact_config(value: Any, *, _key: str = "") -> Any:
    """Return a stable explanation-safe copy without resolving credentials."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized in _PLAINTEXT_SECRET_KEYS and item not in {None, ""}:
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact_config(item, _key=normalized)
        return result
    if isinstance(value, list):
        return [redact_config(item, _key=_key) for item in value]
    if isinstance(value, str):
        rendered = _redact_url(value)
        for pattern in _SECRET_VALUE_PATTERNS:
            rendered = pattern.sub("[REDACTED]", rendered)
        return rendered
    return deepcopy(value)


def secret_reference_issues(value: Any, *, path: str = "$") -> list[str]:
    """Reject plaintext-looking credentials while allowing named references."""
    issues: list[str] = []

    def walk(node: Any, current: str) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                child = f"{current}.{key}"
                normalized = str(key).casefold()
                if normalized in _PLAINTEXT_SECRET_KEYS and item not in {None, ""}:
                    issues.append(f"{child} must be a credential reference, not a secret value")
                walk(item, child)
            return
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{current}[{index}]")
            return
        if isinstance(node, str):
            if any(pattern.search(node) for pattern in _SECRET_VALUE_PATTERNS):
                issues.append(f"{current} contains plaintext-looking authentication material")
            try:
                parsed = urlsplit(node)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.scheme and "@" in parsed.netloc:
                userinfo = parsed.netloc.rsplit("@", 1)[0]
                if ":" in userinfo:
                    issues.append(f"{current} embeds credentials in a URL")

    walk(value, path)
    return issues


def validate_credential_reference(value: Any, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"kind", "reference"}:
        raise ValueError(f"{label} must contain exactly kind and reference")
    kind = value.get("kind")
    reference = value.get("reference")
    if kind not in {"environment", "system_credential", "provider_profile", "none"}:
        raise ValueError(f"{label}.kind is unsupported")
    if kind == "none":
        if reference is not None:
            raise ValueError(f"{label}.reference must be null when kind is none")
        return
    if not isinstance(reference, str) or not reference:
        raise ValueError(f"{label}.reference must be a non-empty name")
    if kind == "environment" and not _ENV_NAME_RE.fullmatch(reference):
        raise ValueError(f"{label}.reference must be an uppercase environment variable name")
    if kind != "environment" and not _PROFILE_RE.fullmatch(reference):
        raise ValueError(f"{label}.reference must be a portable credential/profile name")


def _validate_endpoint(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be null or a non-empty URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute http(s) URL")
    if "@" in parsed.netloc:
        raise ValueError(f"{label} must not embed user information")


def _provider_capabilities(provider: dict[str, Any], label: str) -> dict[str, Any]:
    capabilities = provider.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError(f"{label}.capabilities must be an object")
    if capabilities.get("version") != PROVIDER_CAPABILITY_VERSION:
        raise ValueError(f"{label}.capabilities.version is unsupported")
    structured = capabilities.get("structured_outputs")
    if not isinstance(structured, str) or structured not in {
        "native_json_schema", "json_text", "none",
    }:
        raise ValueError(f"{label}.capabilities.structured_outputs is invalid")
    reasoning = capabilities.get("reasoning")
    if not isinstance(reasoning, dict):
        raise ValueError(f"{label}.capabilities.reasoning must be an object")
    supported = reasoning.get("supported_efforts")
    if not isinstance(supported, list) or not supported:
        raise ValueError(f"{label}.capabilities.reasoning.supported_efforts must be non-empty")
    if any(item not in CANONICAL_EFFORTS for item in supported):
        raise ValueError(f"{label}.capabilities.reasoning lists an unknown effort")
    mapping = reasoning.get("mapping", {})
    if not isinstance(mapping, dict):
        raise ValueError(f"{label}.capabilities.reasoning.mapping must be an object")
    if any(key not in CANONICAL_EFFORTS for key in mapping):
        raise ValueError(f"{label}.capabilities.reasoning.mapping has an unknown source effort")
    if any(not isinstance(item, str) or not item for item in mapping.values()):
        raise ValueError(f"{label}.capabilities.reasoning.mapping values must be strings")
    usage = capabilities.get("usage_mapping")
    if not isinstance(usage, dict):
        raise ValueError(f"{label}.capabilities.usage_mapping must be an object")
    for field in (
        "input_tokens", "cached_input_tokens", "uncached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    ):
        paths = usage.get(field)
        if not isinstance(paths, list) or any(not isinstance(item, str) or not item for item in paths):
            raise ValueError(f"{label}.capabilities.usage_mapping.{field} must be a path list")
    if type(capabilities.get("mechanical_one_shot")) is not bool:
        raise ValueError(f"{label}.capabilities.mechanical_one_shot must be boolean")
    tiers = capabilities.get("service_tiers")
    if not isinstance(tiers, list) or any(
        not isinstance(item, str) or not item for item in tiers
    ):
        raise ValueError(f"{label}.capabilities.service_tiers must be a string list")
    if any(item.casefold() in _FORBIDDEN_SERVICE_TIERS for item in tiers):
        raise ValueError(
            f"{label}.capabilities declares a forbidden request tier; "
            "priority is observation-only and auto/ultrafast are not supported"
        )
    if len(tiers) != len(set(tiers)):
        raise ValueError(f"{label}.capabilities.service_tiers contains duplicates")
    tier_parameter = capabilities.get("service_tier_parameter")
    if tiers and (not isinstance(tier_parameter, str) or not tier_parameter):
        raise ValueError(f"{label}.capabilities.service_tier_parameter is required")
    reasoning_parameter = reasoning.get("parameter")
    if reasoning_parameter is not None and (
        not isinstance(reasoning_parameter, str) or not reasoning_parameter
    ):
        raise ValueError(f"{label}.capabilities.reasoning.parameter is invalid")
    cost_path = capabilities.get("cost_path")
    if cost_path is not None and (not isinstance(cost_path, str) or not cost_path):
        raise ValueError(f"{label}.capabilities.cost_path must be null or a path")
    return capabilities


def validate_service_tier(value: Any, capabilities: dict[str, Any], label: str) -> None:
    if value in {None, ""}:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be null or a service tier name")
    if value.casefold() in _FORBIDDEN_SERVICE_TIERS:
        raise ValueError(f"{label} requests a forbidden priority/auto/ultrafast tier")
    if value not in capabilities.get("service_tiers", []):
        raise ValueError(f"{label} is not declared by the provider capability")


def allowed_observed_service_tiers(requested: Any) -> frozenset[str]:
    """Return the exact observation set for a pinned tier request."""
    if requested in {None, ""}:
        return frozenset({"none", "unobservable"})
    normalized = str(requested).casefold()
    if normalized == "fast":
        # OpenAI reports Fast-mode delivery as priority even when the request
        # used the preferred public spelling, fast.
        return frozenset({"fast", "priority"})
    return frozenset({normalized})


def normalize_observed_service_tier(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "none"
    return str(value).strip().casefold()


def validate_provider_and_routes(raw: dict[str, Any]) -> None:
    providers = raw.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ValueError("providers must be a non-empty object")
    for name, provider in providers.items():
        label = f"providers.{name}"
        if not _PROFILE_RE.fullmatch(str(name)):
            raise ValueError(f"{label} has a non-portable provider id")
        if not isinstance(provider, dict):
            raise ValueError(f"{label} must be an object")
        adapter = provider.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise ValueError(f"{label}.adapter must be non-empty")
        _validate_endpoint(provider.get("endpoint"), f"{label}.endpoint")
        profile = provider.get("profile")
        if profile is not None and (
            not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile)
        ):
            raise ValueError(f"{label}.profile must be null or a portable name")
        validate_credential_reference(provider.get("credential"), f"{label}.credential")
        capabilities = _provider_capabilities(provider, label)
        if adapter == "codex_app_server" and set(
            capabilities.get("service_tiers", [])
        ) - {"fast"}:
            raise ValueError(
                f"{label}.capabilities.service_tiers may declare only fast for the "
                "bundled Codex App Server adapter"
            )

    routes = raw.get("models")
    if not isinstance(routes, dict):
        raise ValueError("models must be an object")
    available_adapters = set(SUPPORTED_PROVIDER_ADAPTERS)
    available_adapters.update(
        item.name for item in entry_points(group="autonomous_math_research.providers")
    )
    for role, route in routes.items():
        label = f"models.{role}"
        if not isinstance(route, dict):
            raise ValueError(f"{label} must be an object")
        provider_name = route.get("provider")
        if provider_name not in providers:
            raise ValueError(f"{label}.provider names an unknown provider")
        provider = providers[str(provider_name)]
        if provider.get("adapter") not in available_adapters:
            raise ValueError(
                f"{label}.provider adapter {provider.get('adapter')!r} is not installed"
            )
        capabilities = _provider_capabilities(provider, f"providers.{provider_name}")
        if provider.get("adapter") == "openai_compatible":
            api_style = capabilities.get("api_style", "responses")
            if api_style not in {"responses", "chat_completions"}:
                raise ValueError(f"providers.{provider_name}.capabilities.api_style is invalid")
            if route.get("endpoint") is None and provider.get("endpoint") is None:
                raise ValueError(f"{label} has no OpenAI-compatible endpoint")
        effort = route.get("effort")
        if effort not in CANONICAL_EFFORTS:
            raise ValueError(f"{label}.effort is invalid")
        reasoning = capabilities["reasoning"]
        supported = set(reasoning["supported_efforts"])
        policy = route.get("unsupported_effort", "error")
        if effort not in supported:
            if policy != "map":
                raise ValueError(
                    f"{label}.effort {effort!r} is unsupported by {provider_name}; "
                    "set an explicit capability mapping and unsupported_effort='map'"
                )
            if effort not in reasoning.get("mapping", {}):
                raise ValueError(f"{label}.effort has no explicit provider capability mapping")
        elif policy not in {"error", "map"}:
            raise ValueError(f"{label}.unsupported_effort must be error or map")
        validate_service_tier(route.get("service_tier"), capabilities, f"{label}.service_tier")
        output_mode = route.get("output_mode", "auto")
        if output_mode not in {"auto", "native_json_schema", "json_text"}:
            raise ValueError(f"{label}.output_mode is invalid")
        declared = capabilities["structured_outputs"]
        effective_mode = declared if output_mode == "auto" else output_mode
        if effective_mode == "none" or (
            effective_mode == "native_json_schema" and declared != "native_json_schema"
        ):
            raise ValueError(f"{label} requests an unsupported structured-output mode")
        for key in ("timeout_seconds", "max_concurrency", "token_limit"):
            value = route.get(key)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{label}.{key} must be a positive integer or null")
        for cost_key in ("cost_limit_usd", "estimated_cost_usd"):
            cost_limit = route.get(cost_key)
            if cost_limit is not None and (
                not isinstance(cost_limit, (int, float)) or isinstance(cost_limit, bool)
                or cost_limit <= 0
            ):
                raise ValueError(f"{label}.{cost_key} must be positive or null")
        retries = route.get("retries")
        if not isinstance(retries, dict) or set(retries) != {"transport", "model_protocol"}:
            raise ValueError(f"{label}.retries must contain transport and model_protocol")
        if any(type(item) is not int or item < 0 for item in retries.values()):
            raise ValueError(f"{label}.retries values must be non-negative integers")
        _validate_endpoint(route.get("endpoint"), f"{label}.endpoint")
        profile = route.get("profile")
        if profile is not None and (
            not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile)
        ):
            raise ValueError(f"{label}.profile must be null or a portable name")

    issues = secret_reference_issues(raw)
    if issues:
        raise ValueError("configuration contains forbidden secret material: " + "; ".join(issues))


def mapped_reasoning_effort(provider: dict[str, Any], route: dict[str, Any]) -> str:
    effort = str(route["effort"])
    reasoning = provider["capabilities"]["reasoning"]
    if effort in reasoning["supported_efforts"]:
        return effort
    if route.get("unsupported_effort") == "map" and effort in reasoning.get("mapping", {}):
        return str(reasoning["mapping"][effort])
    raise ValueError(f"reasoning effort {effort!r} is unsupported and has no explicit mapping")
