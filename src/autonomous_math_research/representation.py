from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any


LEGACY_REPRESENTATION_VALUE = "LEGACY_UNSPECIFIED"
REPRESENTATION_FIELDS = frozenset({
    "branch", "localization", "saturation", "normalization", "content",
    "exceptional_factors", "combination_scope",
})


@dataclass(frozen=True, slots=True)
class RepresentationContract:
    branch: str
    localization: str
    saturation: str
    normalization: str
    content: str
    exceptional_factors: tuple[str, ...]
    combination_scope: str

    @classmethod
    def legacy(cls) -> "RepresentationContract":
        return cls(
            branch=LEGACY_REPRESENTATION_VALUE,
            localization=LEGACY_REPRESENTATION_VALUE,
            saturation=LEGACY_REPRESENTATION_VALUE,
            normalization=LEGACY_REPRESENTATION_VALUE,
            content=LEGACY_REPRESENTATION_VALUE,
            exceptional_factors=(),
            combination_scope=LEGACY_REPRESENTATION_VALUE,
        )

    @classmethod
    def from_dict(cls, value: Any) -> "RepresentationContract":
        if value is None:
            return cls.legacy()
        if not isinstance(value, dict) or set(value) != REPRESENTATION_FIELDS:
            raise ValueError(
                "representation must contain exactly "
                f"{sorted(REPRESENTATION_FIELDS)}"
            )
        strings = {}
        for key in REPRESENTATION_FIELDS - {"exceptional_factors"}:
            item = value[key]
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"representation.{key} must be non-empty")
            strings[key] = item
        factors = value["exceptional_factors"]
        if not isinstance(factors, list) or any(
            not isinstance(item, str) or not item.strip() for item in factors
        ):
            raise ValueError("representation.exceptional_factors must be strings")
        if len(factors) != len(set(factors)):
            raise ValueError("representation.exceptional_factors contains duplicates")
        return cls(exceptional_factors=tuple(sorted(factors)), **strings)

    @property
    def representation_id(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "rep:" + sha256(payload.encode("utf-8")).hexdigest()

    @property
    def is_legacy(self) -> bool:
        return self.branch == LEGACY_REPRESENTATION_VALUE

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["exceptional_factors"] = list(self.exceptional_factors)
        return result


def require_compatible_representations(
    left: RepresentationContract,
    right: RepresentationContract,
    *,
    audited_bridge_ids: set[tuple[str, str]] | None = None,
) -> None:
    if left.representation_id == right.representation_id:
        return
    pair = tuple(sorted((left.representation_id, right.representation_id)))
    if audited_bridge_ids and pair in audited_bridge_ids:
        return
    raise ValueError(
        "representation mismatch requires an independently PASSed "
        f"REPRESENTATION_BRIDGE: {pair[0]} != {pair[1]}"
    )
