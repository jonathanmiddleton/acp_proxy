"""State-machine and property tests for the direct request ledger."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from acp_proxy.direct_state import (
    DirectConflict,
    DirectLedger,
    DirectLimitExceeded,
    OperationKind,
    OperationState,
)

IDS = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")),
    min_size=1,
    max_size=32,
)
PATH_SAFE_IDS = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    st.text(
        alphabet=(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"
        ),
        min_size=0,
        max_size=16,
    ),
)
DIRECT_ACTION_LABELS = (
    "create",
    "initial",
    "invocation",
    "correction",
    "continuation",
    "cancel",
    "retire",
)


@given(operation_id=IDS, digest=IDS)
def test_identical_operation_is_admitted_at_most_once(
    operation_id: str, digest: str
) -> None:
    """ADI-04/11: an identical duplicate observes one ledger record."""
    ledger = DirectLedger(max_operations=4)
    first, created = ledger.admit(
        operation_id=operation_id,
        digest=digest,
        kind=OperationKind.PROMPT,
        logical_session_id="session",
    )
    duplicate, duplicate_created = ledger.admit(
        operation_id=operation_id,
        digest=digest,
        kind=OperationKind.PROMPT,
        logical_session_id="session",
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate is first
    assert duplicate.state is OperationState.ACCEPTED


@given(operation_id=IDS, left=IDS, right=IDS.filter(bool))
def test_conflicting_operation_reuse_fails_pre_effect(
    operation_id: str, left: str, right: str
) -> None:
    """ADI-04: one operation identity cannot name different content."""
    if left == right:
        right += "x"
    ledger = DirectLedger(max_operations=4)
    ledger.admit(operation_id, left, OperationKind.PROMPT, "session")

    with pytest.raises(DirectConflict):
        ledger.admit(operation_id, right, OperationKind.PROMPT, "session")


def test_ledger_never_evicts_replay_protection() -> None:
    """ADI-04/15: capacity exhaustion rejects rather than evicting history."""
    ledger = DirectLedger(max_operations=1)
    first, _ = ledger.admit("one", "digest-one", OperationKind.CREATE, "session")
    first.set_terminal(OperationState.COMPLETED, result={"ok": True})

    with pytest.raises(DirectLimitExceeded):
        ledger.admit("two", "digest-two", OperationKind.CREATE, "other-session")
    assert ledger.get("one") is first


@given(
    operation_suffixes=st.lists(
        PATH_SAFE_IDS,
        min_size=len(DIRECT_ACTION_LABELS),
        max_size=len(DIRECT_ACTION_LABELS),
    ),
    digests=st.lists(
        IDS,
        min_size=len(DIRECT_ACTION_LABELS),
        max_size=len(DIRECT_ACTION_LABELS),
    ),
)
def test_every_direct_action_has_generated_join_and_conflict_reuse(
    operation_suffixes: list[str], digests: list[str]
) -> None:
    """ADI-04: every mutation family has arbitrary ID/digest replay proof."""

    ledger = DirectLedger(max_operations=len(DIRECT_ACTION_LABELS))
    for action, suffix, digest in zip(
        DIRECT_ACTION_LABELS,
        operation_suffixes,
        digests,
        strict=True,
    ):
        operation_id = f"{action}-{suffix}"
        kind = {
            "create": OperationKind.CREATE,
            "cancel": OperationKind.CANCEL,
            "retire": OperationKind.RETIRE,
        }.get(action, OperationKind.PROMPT)
        invocation_id = (
            f"invocation-{action}-{suffix}"
            if kind is OperationKind.PROMPT
            else None
        )
        target_operation_id = (
            f"target-{suffix}" if kind is OperationKind.CANCEL else None
        )
        logical_session_id = f"session-{action}"

        record, created = ledger.admit(
            operation_id,
            digest,
            kind,
            logical_session_id,
            invocation_id=invocation_id,
            target_operation_id=target_operation_id,
        )
        duplicate, duplicate_created = ledger.admit(
            operation_id,
            digest,
            kind,
            logical_session_id,
            invocation_id=invocation_id,
            target_operation_id=target_operation_id,
        )
        assert created is True
        assert duplicate_created is False
        assert duplicate is record

        with pytest.raises(DirectConflict, match="reused with different content"):
            ledger.admit(
                operation_id,
                digest + "x",
                kind,
                logical_session_id,
                invocation_id=invocation_id,
                target_operation_id=target_operation_id,
            )
