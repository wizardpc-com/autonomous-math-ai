from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


def fail(message: str) -> int:
    print(json.dumps({"certificate_valid": False, "error": message}), file=sys.stderr)
    return 1


def check(value: Any) -> tuple[bool, str, int]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "items"}:
        return False, "certificate fields are invalid", 0
    if value["schema_version"] != 1:
        return False, "unsupported certificate schema", 0
    items = value["items"]
    if not isinstance(items, list) or not items:
        return False, "items must be a non-empty array", 0
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {"value", "square"}:
            return False, f"item {index} fields are invalid", index
        number, reported = item["value"], item["square"]
        if type(number) is not int or type(reported) is not int:
            return False, f"item {index} values must be integers", index
        if number * number != reported:
            return False, f"item {index} does not verify", index
    return True, "", len(items)


def main() -> int:
    if len(sys.argv) != 2:
        return fail("usage: checker.py CERTIFICATE.json")
    try:
        value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(str(exc))
    valid, message, count = check(value)
    if not valid:
        return fail(message)
    print(json.dumps({"certificate_valid": True, "items_checked": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
