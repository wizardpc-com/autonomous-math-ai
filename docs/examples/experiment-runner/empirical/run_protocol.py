from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
from typing import Any


PROTOCOL_FIELDS = {
    "schema_version",
    "protocol_id",
    "metric",
    "comparison",
    "threshold",
    "missing_values",
}


def stop(message: str) -> int:
    print(json.dumps({"protocol_completed": False, "error": message}), file=sys.stderr)
    return 1


def validate_protocol(value: Any) -> tuple[str, Decimal]:
    if not isinstance(value, dict) or set(value) != PROTOCOL_FIELDS:
        raise ValueError("protocol fields are invalid")
    if value["schema_version"] != 1:
        raise ValueError("unsupported protocol schema")
    if value["metric"] != "arithmetic_mean":
        raise ValueError("unsupported metric")
    if value["comparison"] != "greater_than_or_equal":
        raise ValueError("unsupported comparison")
    if value["missing_values"] != "reject":
        raise ValueError("unsupported missing-value rule")
    protocol_id = value["protocol_id"]
    if not isinstance(protocol_id, str) or not protocol_id:
        raise ValueError("protocol_id must be non-empty")
    if not isinstance(value["threshold"], str):
        raise ValueError("threshold must be a decimal string")
    try:
        threshold = Decimal(value["threshold"])
    except InvalidOperation as exc:
        raise ValueError("threshold must be a decimal string") from exc
    if not threshold.is_finite():
        raise ValueError("threshold must be finite")
    return protocol_id, threshold


def load_observations(path: Path) -> list[Decimal]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["observation_id", "value"]:
            raise ValueError("CSV header is invalid")
        seen: set[str] = set()
        values: list[Decimal] = []
        for row in reader:
            observation_id = row["observation_id"]
            if not observation_id or observation_id in seen:
                raise ValueError("observation IDs must be non-empty and unique")
            seen.add(observation_id)
            try:
                number = Decimal(row["value"])
            except (InvalidOperation, TypeError) as exc:
                raise ValueError(f"invalid value for {observation_id}") from exc
            if not number.is_finite():
                raise ValueError(f"non-finite value for {observation_id}")
            values.append(number)
    if not values:
        raise ValueError("at least one observation is required")
    return values


def main() -> int:
    if len(sys.argv) != 3:
        return stop("usage: run_protocol.py PROTOCOL.json OBSERVATIONS.csv")
    try:
        protocol = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        protocol_id, threshold = validate_protocol(protocol)
        values = load_observations(Path(sys.argv[2]))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return stop(str(exc))
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    print(json.dumps({
        "comparison": "greater_than_or_equal",
        "mean": str(mean),
        "observation_count": len(values),
        "protocol_completed": True,
        "protocol_id": protocol_id,
        "supported": mean >= threshold,
        "threshold": str(threshold),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
