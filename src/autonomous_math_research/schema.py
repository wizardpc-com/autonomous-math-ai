from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import ipaddress
import json
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import UUID

from .contracts import OUTPUT_CONTRACT_KEYS, OUTPUT_PROTOCOL_VERSION, contract_name


class SchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SchemaCompatibilityIssue:
    schema_path: str
    json_path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.schema_path}:{self.json_path}: {self.reason}"


class OutputSchemaCompatibilityError(SchemaError):
    """One or more schemas cannot be sent as strict App Server outputSchema."""

    def __init__(self, issues: Iterable[SchemaCompatibilityIssue]):
        self.issues = tuple(issues)
        if not self.issues:
            raise ValueError("OutputSchemaCompatibilityError requires at least one issue")
        super().__init__("invalid App Server output schema: " + "; ".join(map(str, self.issues)))


# Current non-fine-tuned Structured Outputs subset documented by OpenAI on
# 2026-08-18. Accepting arbitrary JSON Schema keywords is exactly how mock
# validation drifted away from the real App Server protocol.
_ALLOWED_KEYWORDS = {
    "$schema", "$defs", "$ref",
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "anyOf",
    "title", "description",
    "pattern", "format",
    "multipleOf", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems",
}
_UNSUPPORTED_KEYWORDS = {
    "allOf", "oneOf", "not", "dependentRequired", "dependentSchemas",
    "if", "then", "else", "patternProperties", "propertyNames",
    "contains", "minContains", "maxContains", "uniqueItems",
    "minProperties", "maxProperties", "unevaluatedProperties",
    "unevaluatedItems", "prefixItems", "examples", "default",
    "minLength", "maxLength",
}
_ALLOWED_TYPES = {"string", "number", "boolean", "integer", "object", "array", "null"}
_SUPPORTED_FORMATS = {
    "date-time", "time", "date", "duration", "email", "hostname",
    "ipv4", "ipv6", "uuid",
}


def load_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError(f"{path}:$: cannot load JSON schema: {exc}") from exc
    if not isinstance(value, dict):
        raise SchemaError(f"{path}:$: schema root must be an object")
    return value


def validate_output_schema_compatibility(
    schema: dict[str, Any], *, schema_path: str | Path = "<memory>",
) -> None:
    """Fail fast on the strict JSON-Schema subset accepted by outputSchema.

    This is a protocol compatibility check, not an instance validator. It is
    deliberately shared by real and mock backends and runs before turn/start.
    """
    source = str(schema_path)
    issues: list[SchemaCompatibilityIssue] = []
    property_count = 0
    enum_count = 0
    schema_string_chars = 0

    def issue(path: str, reason: str) -> None:
        issues.append(SchemaCompatibilityIssue(source, path, reason))

    def declared_types(node: dict[str, Any], path: str) -> set[str]:
        raw = node.get("type")
        if raw is None:
            return set()
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, list) or not values:
            issue(path + ".type", "type must be a non-empty string or array of strings")
            return set()
        result: set[str] = set()
        for index, value in enumerate(values):
            if not isinstance(value, str) or value not in _ALLOWED_TYPES:
                issue(f"{path}.type[{index}]", f"unsupported type {value!r}")
            else:
                result.add(value)
        if len(result) != len(values):
            issue(path + ".type", "type entries must be unique")
        return result

    def value_matches_type(value: Any, allowed: set[str]) -> bool:
        if value is None:
            return "null" in allowed
        if isinstance(value, bool):
            return "boolean" in allowed
        if isinstance(value, int):
            return "integer" in allowed or "number" in allowed
        if isinstance(value, float):
            return "number" in allowed
        if isinstance(value, str):
            return "string" in allowed
        if isinstance(value, list):
            return "array" in allowed
        if isinstance(value, dict):
            return "object" in allowed
        return False

    def resolve_local_ref(ref: str) -> Any:
        target: Any = schema
        if ref == "#":
            return target
        if not ref.startswith("#/"):
            raise KeyError(ref)
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                raise KeyError(ref)
            target = target[part]
        return target

    def walk(node: Any, path: str, depth: int, *, root: bool = False) -> None:
        nonlocal property_count, enum_count, schema_string_chars
        if not isinstance(node, dict):
            issue(path, "schema node must be an object")
            return
        if depth > 10:
            issue(path, "object/schema nesting exceeds the Structured Outputs limit of 10")
        for key in node:
            if key in _UNSUPPORTED_KEYWORDS or key not in _ALLOWED_KEYWORDS:
                issue(f"{path}.{key}", f"unsupported Structured Outputs keyword {key!r}")
        if root and (node.get("type") != "object" or "anyOf" in node):
            issue(path, "root output schema must have type 'object' and must not use anyOf")

        is_ref = isinstance(node.get("$ref"), str)
        if "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#"):
                issue(path + ".$ref", "$ref must be a local JSON pointer beginning with '#'")
            else:
                try:
                    target = resolve_local_ref(ref)
                    if not isinstance(target, dict):
                        issue(path + ".$ref", "$ref must resolve to a schema object")
                except KeyError:
                    issue(path + ".$ref", f"unresolved local JSON Schema reference {ref!r}")
        types = declared_types(node, path)
        if not types and not is_ref and "anyOf" not in node:
            issue(path, "schema must have an explicit type key")

        if "enum" in node:
            enum = node["enum"]
            if not types:
                issue(path + ".enum", "enum must be accompanied by an explicit type")
            if not isinstance(enum, list) or not enum:
                issue(path + ".enum", "enum must be a non-empty array")
            else:
                enum_count += len(enum)
                if len(enum) != len({json.dumps(item, sort_keys=True) for item in enum}):
                    issue(path + ".enum", "enum values must be unique")
                for index, value in enumerate(enum):
                    if types and not value_matches_type(value, types):
                        issue(f"{path}.enum[{index}]", "enum value does not match the declared type")
                    if isinstance(value, str):
                        schema_string_chars += len(value)
                if len(enum) > 250:
                    enum_string_chars = sum(
                        len(value) for value in enum if isinstance(value, str)
                    )
                    if enum_string_chars > 15_000:
                        issue(
                            path + ".enum",
                            "a string enum with more than 250 values may contain at most "
                            "15000 characters",
                        )
        if "const" in node:
            if not types:
                issue(path + ".const", "const must be accompanied by an explicit type")
            elif not value_matches_type(node["const"], types):
                issue(path + ".const", "const value does not match the declared type")
            if isinstance(node["const"], str):
                schema_string_chars += len(node["const"])

        if "format" in node and node["format"] not in _SUPPORTED_FORMATS:
            issue(path + ".format", f"unsupported string format {node['format']!r}")
        string_keywords = {"pattern", "format"} & set(node)
        if string_keywords and "string" not in types:
            issue(path, f"string constraints require type 'string': {sorted(string_keywords)}")
        if "pattern" in node:
            if not isinstance(node["pattern"], str):
                issue(path + ".pattern", "pattern must be a string")
            else:
                try:
                    re.compile(node["pattern"])
                except re.error as exc:
                    issue(path + ".pattern", f"invalid regular expression: {exc}")
        number_keywords = {
            "multipleOf", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        } & set(node)
        if number_keywords and not ({"number", "integer"} & types):
            issue(path, f"number constraints require type 'number' or 'integer': {sorted(number_keywords)}")
        for keyword in number_keywords:
            value = node[keyword]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                issue(f"{path}.{keyword}", f"{keyword} must be a number")
        if "multipleOf" in node and isinstance(node["multipleOf"], (int, float)):
            if isinstance(node["multipleOf"], bool) or node["multipleOf"] <= 0:
                issue(path + ".multipleOf", "multipleOf must be greater than zero")
        lower = node.get("minimum", node.get("exclusiveMinimum"))
        upper = node.get("maximum", node.get("exclusiveMaximum"))
        if (
            isinstance(lower, (int, float)) and not isinstance(lower, bool)
            and isinstance(upper, (int, float)) and not isinstance(upper, bool)
            and lower > upper
        ):
            issue(path, "numeric lower bound cannot exceed upper bound")

        array_limit_keywords = {"minItems", "maxItems"} & set(node)
        if array_limit_keywords and "array" not in types:
            issue(path, f"array constraints require type 'array': {sorted(array_limit_keywords)}")
        for keyword in array_limit_keywords:
            if (
                not isinstance(node[keyword], int) or isinstance(node[keyword], bool)
                or node[keyword] < 0
            ):
                issue(f"{path}.{keyword}", f"{keyword} must be a non-negative integer")
        if (
            isinstance(node.get("minItems"), int)
            and isinstance(node.get("maxItems"), int)
            and node["minItems"] > node["maxItems"]
        ):
            issue(path, "minItems cannot exceed maxItems")

        if "object" in types:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                issue(path + ".properties", "object schemas must declare a properties object")
                properties = {}
            if node.get("additionalProperties") is not False:
                issue(path + ".additionalProperties", "object schemas must set additionalProperties to false")
            required = node.get("required")
            if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                issue(path + ".required", "object schemas must declare required as an array of property names")
                required_set: set[str] = set()
            else:
                required_set = set(required)
                if len(required_set) != len(required):
                    issue(path + ".required", "required entries must be unique")
            property_names = set(properties)
            if required_set != property_names:
                missing = sorted(property_names - required_set)
                unknown = sorted(required_set - property_names)
                detail = []
                if missing:
                    detail.append(f"not required: {missing}")
                if unknown:
                    detail.append(f"not properties: {unknown}")
                issue(path + ".required", "all and only object properties must be required (" + ", ".join(detail) + ")")
            property_count += len(properties)
            for name, child in properties.items():
                schema_string_chars += len(str(name))
                walk(child, f"{path}.properties.{name}", depth + 1)
        elif "properties" in node or "required" in node or "additionalProperties" in node:
            issue(path, "object keywords require type 'object'")

        if "array" in types:
            items = node.get("items")
            if not isinstance(items, dict):
                issue(path + ".items", "array schemas must declare one complete items schema")
            else:
                walk(items, path + ".items", depth + 1)
        elif "items" in node:
            issue(path + ".items", "items requires type 'array'")

        if "anyOf" in node:
            options = node["anyOf"]
            if not isinstance(options, list) or not options:
                issue(path + ".anyOf", "anyOf must be a non-empty array")
            else:
                for index, option in enumerate(options):
                    walk(option, f"{path}.anyOf[{index}]", depth + 1)

        defs = node.get("$defs")
        if defs is not None:
            if not isinstance(defs, dict):
                issue(path + ".$defs", "$defs must be an object")
            else:
                for name, definition in defs.items():
                    schema_string_chars += len(str(name))
                    walk(definition, f"{path}.$defs.{name}", depth + 1)

    walk(schema, "$", 0, root=True)
    known_contract = contract_name(source)
    if known_contract:
        expected = set(OUTPUT_CONTRACT_KEYS[known_contract])
        properties = schema.get("properties")
        actual_properties = set(properties) if isinstance(properties, dict) else set()
        required = schema.get("required")
        actual_required = set(required) if isinstance(required, list) else set()
        if actual_properties != expected:
            issue(
                "$.properties",
                f"protocol v{OUTPUT_PROTOCOL_VERSION} {known_contract} keys must be exactly "
                f"{sorted(expected)}; got {sorted(actual_properties)}",
            )
        if actual_required != expected:
            issue(
                "$.required",
                f"protocol v{OUTPUT_PROTOCOL_VERSION} {known_contract} required keys must be exactly "
                f"{sorted(expected)}; got {sorted(actual_required)}",
            )
    if property_count > 5000:
        issue("$", f"schema declares {property_count} properties; maximum is 5000")
    if enum_count > 1000:
        issue("$", f"schema declares {enum_count} enum values; maximum is 1000")
    if schema_string_chars > 120_000:
        issue("$", f"schema names/enum/const strings total {schema_string_chars} characters; maximum is 120000")
    if issues:
        raise OutputSchemaCompatibilityError(issues)


def preflight_output_schema_files(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    issues: list[SchemaCompatibilityIssue] = []
    for path in paths:
        try:
            schema = load_schema(path)
            validate_output_schema_compatibility(schema, schema_path=path)
            loaded[path.name] = schema
        except OutputSchemaCompatibilityError as exc:
            issues.extend(exc.issues)
        except SchemaError as exc:
            issues.append(SchemaCompatibilityIssue(str(path), "$", str(exc)))
    if issues:
        raise OutputSchemaCompatibilityError(issues)
    return loaded


def validate(
    instance: Any, schema: dict[str, Any], path: str = "$", *,
    _root_schema: dict[str, Any] | None = None,
) -> None:
    """Validate the deliberately small JSON-Schema subset used by this MVP."""
    root_schema = schema if _root_schema is None else _root_schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise SchemaError(f"{path}: only local JSON Schema references are supported")
        target: Any = root_schema
        if ref != "#":
            if not ref.startswith("#/"):
                raise SchemaError(f"{path}: invalid local JSON Schema reference {ref!r}")
            for raw_part in ref[2:].split("/"):
                part = raw_part.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or part not in target:
                    raise SchemaError(f"{path}: unresolved JSON Schema reference {ref!r}")
                target = target[part]
        if not isinstance(target, dict):
            raise SchemaError(f"{path}: JSON Schema reference {ref!r} does not target an object")
        validate(instance, target, path, _root_schema=root_schema)
        return
    if "const" in schema and instance != schema["const"]:
        raise SchemaError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(f"{path}: {instance!r} not in enum")
    if "anyOf" in schema:
        errors = 0
        for option in schema["anyOf"]:
            try:
                validate(instance, option, path, _root_schema=root_schema)
                return
            except SchemaError:
                errors += 1
        raise SchemaError(f"{path}: no anyOf schema matched ({errors} failures)")
    if "oneOf" in schema:
        passes = 0
        for option in schema["oneOf"]:
            try:
                validate(instance, option, path, _root_schema=root_schema)
                passes += 1
            except SchemaError:
                pass
        if passes != 1:
            raise SchemaError(f"{path}: expected exactly one matching schema, got {passes}")
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        errors: list[str] = []
        for one in expected:
            try:
                validate(instance, {**schema, "type": one}, path, _root_schema=root_schema)
                return
            except SchemaError as exc:
                errors.append(str(exc))
        raise SchemaError(f"{path}: no allowed type matched")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if expected:
        pytype = type_map[expected]
        if (
            not isinstance(instance, pytype)
            or expected in {"integer", "number"} and isinstance(instance, bool)
        ):
            raise SchemaError(f"{path}: expected {expected}, got {type(instance).__name__}")
    if expected == "object":
        props = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(instance)
        if missing:
            raise SchemaError(f"{path}: missing required fields {sorted(missing)}")
        if schema.get("additionalProperties") is False:
            unknown = set(instance) - set(props)
            if unknown:
                raise SchemaError(f"{path}: unknown fields {sorted(unknown)}")
        for key, value in instance.items():
            if key in props:
                validate(value, props[key], f"{path}.{key}", _root_schema=root_schema)
    elif expected == "array":
        if len(instance) < schema.get("minItems", 0):
            raise SchemaError(f"{path}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaError(f"{path}: too many items")
        item_schema = schema.get("items")
        if item_schema:
            for index, value in enumerate(instance):
                validate(value, item_schema, f"{path}[{index}]", _root_schema=root_schema)
    elif expected == "string":
        if len(instance) < schema.get("minLength", 0):
            raise SchemaError(f"{path}: string too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaError(f"{path}: string too long")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise SchemaError(f"{path}: string does not match pattern")
        if "format" in schema and not _matches_format(instance, str(schema["format"])):
            raise SchemaError(f"{path}: string does not match format {schema['format']!r}")
    elif expected in {"integer", "number"}:
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaError(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaError(f"{path}: above maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise SchemaError(f"{path}: not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            raise SchemaError(f"{path}: not below exclusiveMaximum")
        if "multipleOf" in schema:
            try:
                if Decimal(str(instance)) % Decimal(str(schema["multipleOf"])) != 0:
                    raise SchemaError(f"{path}: not a multipleOf {schema['multipleOf']}")
            except InvalidOperation as exc:
                raise SchemaError(f"{path}: invalid multipleOf comparison") from exc


_DURATION_RE = re.compile(
    r"^P(?=\d|T\d)(?:\d+Y)?(?:\d+M)?(?:\d+D)?"
    r"(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$"
)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


def _matches_format(value: str, schema_format: str) -> bool:
    try:
        if schema_format == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif schema_format == "time":
            time.fromisoformat(value.replace("Z", "+00:00"))
        elif schema_format == "date":
            date.fromisoformat(value)
        elif schema_format == "duration":
            return _DURATION_RE.fullmatch(value) is not None
        elif schema_format == "email":
            return _EMAIL_RE.fullmatch(value) is not None
        elif schema_format == "hostname":
            return _HOSTNAME_RE.fullmatch(value) is not None
        elif schema_format == "ipv4":
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
        elif schema_format == "ipv6":
            return isinstance(ipaddress.ip_address(value), ipaddress.IPv6Address)
        elif schema_format == "uuid":
            UUID(value)
        else:
            return False
    except (ValueError, TypeError):
        return False
    return True
