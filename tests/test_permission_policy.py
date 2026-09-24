"""The first permission policy is explicit, immutable and unambiguous."""

from dataclasses import FrozenInstanceError
import hashlib

import pytest

from meadow_bridge.json_types import JsonValue
from meadow_bridge.permission_handler import PermissionHandler
from meadow_bridge.permission_policy import PermissionAction, PermissionPolicy


def test_policy_acknowledges_canonical_digest_and_all_supported_effects() -> None:
    policy = PermissionPolicy.from_json({"mode": "allow_all", "version": 1})
    expected = hashlib.sha256(b'{"mode":"allow_all","version":1}').hexdigest()
    assert policy.digest == expected
    assert policy.as_json() == {"version": 1, "mode": "allow_all"}
    handler = PermissionHandler(policy)
    for action in PermissionAction:
        decision = handler.confirm(action)
        assert decision.allowed and decision.action is action
        assert decision.policy_digest == expected
        assert handler.authorize(action) == decision
    with pytest.raises(FrozenInstanceError):
        setattr(policy, "mode", "deny_all")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"version": 1},
        {"mode": "allow_all"},
        {"version": True, "mode": "allow_all"},
        {"version": 2, "mode": "allow_all"},
        {"version": 1, "mode": "ask"},
        {"version": 1, "mode": "allow_all", "rules": []},
    ],
)
def test_unsupported_or_implicit_policy_is_rejected(value: JsonValue) -> None:
    with pytest.raises(ValueError):
        PermissionPolicy.from_json(value)
