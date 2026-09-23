"""Diagnostic stream admission preserves evidence and rejects wrong payload shapes."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from hypothesis import given
from hypothesis import strategies as st

import acp_validate


@given(text=st.text())
def test_diagnostic_reader_preserves_full_stream_and_correlates_messages(text: str) -> None:
    response = {"jsonrpc": "2.0", "id": 7, "result": {"sessionId": "session"}}
    notification = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"update": {"content": {"type": "text", "text": text}}},
    }
    source = StringIO(
        "\n".join(
            (json.dumps(response), "", json.dumps(notification), "raw diagnostic")
        )
    )
    messages: acp_validate.MessageLog = []

    acp_validate.read_ndjson(source, messages)

    assert messages == [
        ("out", response),
        ("out", notification),
        ("out", {"_raw": "raw diagnostic"}),
    ]
    assert acp_validate.find_response(messages, 7) == response
    assert acp_validate.find_response(messages, 8) is None
    assert acp_validate.find_notifications(messages, "session/update") == [notification]


def test_diagnostic_result_rejects_nonobject_payload_without_losing_raw_evidence() -> None:
    messages: acp_validate.MessageLog = []
    acp_validate.read_ndjson(StringIO('{"id":7,"result":["unexpected"]}\n'), messages)
    response = acp_validate.find_response(messages, 7)
    assert response is not None

    with pytest.raises(ValueError, match="Expected a JSON object, received list"):
        acp_validate._as_object(response["result"])

    assert response == {"id": 7, "result": ["unexpected"]}


def test_diagnostic_reader_retains_deep_json_and_continues_to_following_response() -> None:
    nested_json = "[" * 260 + '"evidence"' + "]" * 260
    response = {"jsonrpc": "2.0", "id": 7, "result": {"sessionId": "session"}}
    source = StringIO(nested_json + "\n" + json.dumps(response) + "\n")
    messages: acp_validate.MessageLog = []

    acp_validate.read_ndjson(source, messages)

    assert messages == [("out", json.loads(nested_json)), ("out", response)]
    assert acp_validate.find_response(messages, 7) == response
