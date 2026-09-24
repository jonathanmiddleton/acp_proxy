"""Explicit, immutable policy admitted for a logical Bridge session."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Literal

from .json_types import JsonObject, JsonValue


class PermissionAction(StrEnum):
    """Effects whose execution belongs to the workspace tool owner."""

    CREATE = "create"
    EDIT = "edit"
    COMMAND = "command"


@dataclass(frozen=True)
class PermissionPolicy:
    """Version-one allow-all policy; absence is never an implicit policy."""

    version: Literal[1]
    mode: Literal["allow_all"]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise ValueError("permission policy version must be 1")
        if self.mode != "allow_all":
            raise ValueError("permission policy mode must be allow_all")

    @classmethod
    def from_json(cls, value: JsonValue) -> PermissionPolicy:
        """Reject missing, additional, coerced or unsupported policy fields."""
        if not isinstance(value, dict) or set(value) != {"version", "mode"}:
            raise ValueError("permission policy requires exactly version and mode")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("permission policy version must be 1")
        if value["mode"] != "allow_all":
            raise ValueError("permission policy mode must be allow_all")
        return cls(version=1, mode="allow_all")

    def as_json(self) -> JsonObject:
        """Serialize only the supported policy contract."""
        return {"version": self.version, "mode": self.mode}

    @property
    def digest(self) -> str:
        """SHA-256 of the canonical, sorted, compact UTF-8 policy JSON."""
        canonical = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def permits(self, action: PermissionAction) -> bool:
        """Authorize a recognized effect under the explicit current policy."""
        if not isinstance(action, PermissionAction):
            raise ValueError("unrecognized permission action")
        return True
