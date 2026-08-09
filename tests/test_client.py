"""Unit tests for the ACP client layer.

Covers message extraction (ADR-002, ADR-004), agent callback handlers
(ADR-007), model management, and notification routing. These tests
exercise client.py in isolation — no subprocess, no transport.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from acp_proxy.client import (
    AcpClient,
    CallbackPolicy,
    ModelInfo,
    SessionState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(*roles_and_contents: tuple[str, str | list | None]) -> list[dict]:
    """Build an OpenAI-format messages array from (role, content) pairs."""
    return [{"role": r, "content": c} for r, c in roles_and_contents]


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    """AcpClient._extract_text handles the three content shapes OpenCode sends."""

    def test_string_content(self):
        assert AcpClient._extract_text("hello world") == "hello world"

    def test_none_content(self):
        assert AcpClient._extract_text(None) == ""

    def test_list_with_text_blocks(self):
        content = [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ]
        assert AcpClient._extract_text(content) == "part one\npart two"

    def test_list_with_mixed_block_types(self):
        """Non-text blocks (images, resources) are skipped."""
        content = [
            {"type": "image", "url": "http://example.com/img.png"},
            {"type": "text", "text": "the text"},
        ]
        assert AcpClient._extract_text(content) == "the text"

    def test_empty_list(self):
        assert AcpClient._extract_text([]) == ""

    def test_empty_string(self):
        assert AcpClient._extract_text("") == ""


# ---------------------------------------------------------------------------
# extract_last_user_message (ADR-004)
# ---------------------------------------------------------------------------


class TestExtractLastUserMessage:
    """ADR-004: only the last user message is forwarded to the ACP session."""

    def test_single_user_message(self):
        msgs = _make_messages(("user", "hello"))
        assert AcpClient.extract_last_user_message(msgs) == "hello"

    def test_multi_turn_returns_last_user(self):
        """With full history replay, returns only the newest user message."""
        msgs = _make_messages(
            ("system", "You are helpful."),
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
        )
        assert AcpClient.extract_last_user_message(msgs) == "second question"

    def test_system_messages_stripped(self):
        """System messages (OpenCode's prompt) are never returned."""
        msgs = _make_messages(
            ("system", "You are a coding assistant with tools..."),
            ("user", "help me"),
        )
        assert AcpClient.extract_last_user_message(msgs) == "help me"

    def test_assistant_messages_stripped(self):
        """Assistant messages from prior turns are not included."""
        msgs = _make_messages(
            ("user", "question"),
            ("assistant", "answer"),
            ("user", "follow-up"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert result == "follow-up"
        assert "answer" not in result

    def test_system_reminder_in_earlier_user_message_stripped(self):
        """<system-reminder> tags in earlier user messages don't leak through."""
        msgs = _make_messages(
            ("user", "<system-reminder>build mode</system-reminder>\nfirst msg"),
            ("assistant", "ok"),
            ("user", "second msg"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert result == "second msg"
        assert "system-reminder" not in result

    def test_list_content_in_last_user_message(self):
        """Content blocks (list format) are extracted correctly."""
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "from blocks"}]},
        ]
        assert AcpClient.extract_last_user_message(msgs) == "from blocks"

    def test_no_user_messages_fallback(self):
        """If no user message exists, concatenate all non-empty content."""
        msgs = _make_messages(
            ("system", "system prompt"),
            ("assistant", "stray assistant"),
        )
        result = AcpClient.extract_last_user_message(msgs)
        assert "system prompt" in result
        assert "stray assistant" in result

    def test_none_content_user_message_skipped(self):
        """A user message with None content is skipped in favor of earlier ones."""
        msgs = _make_messages(
            ("user", "real content"),
            ("assistant", "reply"),
            ("user", None),
        )
        # The last user message has None content, _extract_text returns "",
        # but the method still returns it (empty string). The fallback only
        # triggers when there are NO user messages at all.
        result = AcpClient.extract_last_user_message(msgs)
        # It returns "" because the last user message has None content
        assert result == ""


# ---------------------------------------------------------------------------
# extract_first_user_message (ADR-002)
# ---------------------------------------------------------------------------


class TestExtractFirstUserMessage:
    """ADR-002: first user message is the stable conversation anchor for session ID."""

    def test_returns_first_user_message(self):
        msgs = _make_messages(
            ("system", "system prompt"),
            ("user", "first question"),
            ("assistant", "answer"),
            ("user", "second question"),
        )
        assert AcpClient.extract_first_user_message(msgs) == "first question"

    def test_stable_across_turns(self):
        """Simulates OpenCode's full-replay: first user message is the same."""
        turn_1 = _make_messages(("user", "hello agent"))
        turn_2 = _make_messages(
            ("user", "hello agent"),
            ("assistant", "hi"),
            ("user", "follow up"),
        )
        assert AcpClient.extract_first_user_message(
            turn_1
        ) == AcpClient.extract_first_user_message(turn_2)

    def test_no_user_messages_returns_empty(self):
        msgs = _make_messages(("system", "only system"))
        assert AcpClient.extract_first_user_message(msgs) == ""

    def test_system_message_not_returned(self):
        """System messages are not user messages even though they come first."""
        msgs = _make_messages(
            ("system", "I am a system prompt"),
            ("user", "I am the user"),
        )
        assert AcpClient.extract_first_user_message(msgs) == "I am the user"

    def test_title_generator_different_anchor(self):
        """Title generator messages differ from conversation messages."""
        conversation = _make_messages(("user", "help me refactor this function"))
        title_gen = _make_messages(
            ("user", "You are a title generator. Summarize: help me refactor...")
        )
        assert AcpClient.extract_first_user_message(
            conversation
        ) != AcpClient.extract_first_user_message(title_gen)


# ---------------------------------------------------------------------------
# _messages_to_prompt (ADR-004)
# ---------------------------------------------------------------------------


class TestMessagesToPrompt:
    """ADR-004: messages are converted to a single ACP text content block."""

    def test_returns_single_text_block(self):
        client = AcpClient.__new__(AcpClient)
        msgs = _make_messages(
            ("system", "ignored system prompt"),
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
        )
        result = client._messages_to_prompt(msgs)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "second question"

    def test_opencode_system_prompt_not_in_output(self):
        """OpenCode's ~15K system prompt must never reach the ACP session."""
        client = AcpClient.__new__(AcpClient)
        long_system = "You are OpenCode. " * 1000
        msgs = _make_messages(
            ("system", long_system),
            ("user", "actual question"),
        )
        result = client._messages_to_prompt(msgs)
        assert "OpenCode" not in result[0]["text"]
        assert result[0]["text"] == "actual question"


# ---------------------------------------------------------------------------
# _handle_permission_request (ADR-007)
# ---------------------------------------------------------------------------


class TestHandlePermissionRequest:
    """ADR-007: auto-approve with priority allow_always > allow_once > first."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        client._direct_prompt_phases = {}
        client._direct_update_budgets = {}
        client._expected_model_updates = {}
        client._available_commands_by_session = {}
        client._provisional_session_ids = set()
        return client

    def test_prefers_allow_always(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "allow_once", "name": "Once"},
                {"optionId": "2", "kind": "allow_always", "name": "Always"},
                {"optionId": "3", "kind": "deny", "name": "Deny"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "2"
        assert result["outcome"]["outcome"] == "selected"

    def test_falls_back_to_allow_once(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "deny", "name": "Deny"},
                {"optionId": "2", "kind": "allow_once", "name": "Once"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "2"

    def test_falls_back_to_first_option(self):
        client = self._make_client()
        params = {
            "options": [
                {"optionId": "1", "kind": "deny", "name": "Deny"},
            ]
        }
        result = client._handle_permission_request(params)
        assert result["outcome"]["optionId"] == "1"

    def test_empty_options_returns_cancelled(self):
        client = self._make_client()
        params = {"options": []}
        result = client._handle_permission_request(params)
        assert result["outcome"]["outcome"] == "cancelled"


# ---------------------------------------------------------------------------
# _handle_read_file (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleReadFile:
    """ADR-007: fs/read_text_file callback reads from disk."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        return client

    def test_read_full_file(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("line one\nline two\nline three\n")
        result = client._handle_read_file({"path": str(f)})
        assert result["content"] == "line one\nline two\nline three\n"

    def test_read_with_line_and_limit(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("line1\nline2\nline3\nline4\nline5\n")
        result = client._handle_read_file({"path": str(f), "line": 2, "limit": 2})
        assert result["content"] == "line2\nline3\n"

    def test_read_with_line_no_limit(self, tmp_path):
        client = self._make_client()
        f = tmp_path / "test.txt"
        f.write_text("a\nb\nc\nd\n")
        result = client._handle_read_file({"path": str(f), "line": 3})
        assert result["content"] == "c\nd\n"

    def test_read_nonexistent_file_raises(self):
        client = self._make_client()
        with pytest.raises(FileNotFoundError):
            client._handle_read_file({"path": "/nonexistent/path/file.txt"})


# ---------------------------------------------------------------------------
# _handle_write_file (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleWriteFile:
    """ADR-007: fs/write_text_file callback writes to disk."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        return client

    def test_write_creates_file(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "output.txt"
        client._handle_write_file({"path": str(target), "content": "hello"})
        assert target.read_text() == "hello"

    def test_write_creates_intermediate_directories(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "a" / "b" / "c" / "file.txt"
        client._handle_write_file({"path": str(target), "content": "nested"})
        assert target.read_text() == "nested"

    def test_write_overwrites_existing(self, tmp_path):
        client = self._make_client()
        target = tmp_path / "existing.txt"
        target.write_text("old content")
        client._handle_write_file({"path": str(target), "content": "new content"})
        assert target.read_text() == "new content"


# ---------------------------------------------------------------------------
# _handle_agent_request dispatch (ADR-007)
# ---------------------------------------------------------------------------


class TestHandleAgentRequest:
    """ADR-007: incoming agent requests are dispatched to the correct handler."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        client._direct_prompt_phases = {}
        client._direct_update_budgets = {}
        client._expected_model_updates = {}
        client._available_commands_by_session = {}
        return client

    def test_unknown_method_returns_none(self):
        client = self._make_client()
        result = client._handle_agent_request(
            {"method": "unknown/method", "params": {}}
        )
        assert result is None

    def test_permission_request_dispatched(self):
        client = self._make_client()
        result = client._handle_agent_request(
            {
                "method": "session/request_permission",
                "params": {
                    "options": [
                        {"optionId": "1", "kind": "allow_always", "name": "Allow"}
                    ]
                },
            }
        )
        assert result["outcome"]["outcome"] == "selected"

    def test_handler_exception_propagates(self, tmp_path):
        client = self._make_client()
        with pytest.raises(FileNotFoundError):
            client._handle_agent_request(
                {
                    "method": "fs/read_text_file",
                    "params": {"path": "/nonexistent/file.txt"},
                }
            )


# ---------------------------------------------------------------------------
# _handle_notification routing
# ---------------------------------------------------------------------------


class TestHandleNotification:
    """Notifications are routed to the correct session's update queue."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}
        client._direct_prompt_phases = {}
        client._direct_update_budgets = {}
        client._expected_model_updates = {}
        client._available_commands_by_session = {}
        client._provisional_session_ids = set()
        return client

    def test_session_update_routed_to_queue(self):
        client = self._make_client()
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues["session-1"] = queue

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session-1",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    },
                },
            }
        )
        assert not queue.empty()
        update = queue.get_nowait()
        assert update["sessionUpdate"] == "agent_message_chunk"

    def test_unknown_session_id_silently_dropped(self):
        """Updates for sessions we're not tracking are dropped without error."""
        client = self._make_client()
        # No queues registered — should not raise
        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "unknown-session",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            }
        )

    def test_non_session_update_notification_ignored(self):
        """Notifications that aren't session/update are handled gracefully."""
        client = self._make_client()
        # Should not raise even though no handler exists for this method
        client._handle_notification(
            {
                "method": "some/other_notification",
                "params": {},
            }
        )

    @pytest.mark.parametrize(
        "message",
        [
            {
                "method": "session/update",
                "params": {
                    "sessionId": "unknown-session",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionId": "known-but-not-prompting",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active",
                    "update": {"sessionUpdate": "future_unknown_update"},
                },
            },
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active",
                    "update": {
                        "content": {"type": "text", "text": "missing kind"}
                    },
                },
            },
            {"method": "unknown/notification", "params": {}},
        ],
    )
    def test_direct_unknown_late_or_malformed_update_fails_continuity(
        self, message
    ) -> None:
        """ADI-08/10: direct mode never silently drops ambiguous evidence."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._direct_prompt_phases = {"active": "active"}
        client._sessions = {
            "known-but-not-prompting": SessionState(
                session_id="known-but-not-prompting"
            ),
            "active": SessionState(session_id="active"),
        }
        client._update_queues["active"] = asyncio.Queue()
        transport = MagicMock()
        client._transport = transport

        client._handle_notification(message)

        transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )

    def test_direct_known_update_is_routed_without_failure(self) -> None:
        """A well-formed active direct update remains ordered in its sole queue."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._direct_prompt_phases = {"active": "active"}
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues["active"] = queue
        client._direct_update_budgets["active"] = {
            "bytes": 0,
            "count": 0,
            "byte_limit": 10_000,
            "count_limit": 10,
        }
        transport = MagicMock()
        client._transport = transport
        update = {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hello"},
        }

        client._handle_notification(
            {
                "method": "session/update",
                "params": {"sessionId": "active", "update": update},
            }
        )

        assert queue.get_nowait() == update
        transport.fail_closed.assert_not_called()

    @pytest.mark.parametrize(
        "update",
        [
            {"sessionUpdate": "usage_update", "inputTokens": True},
            {"sessionUpdate": "usage_update", "outputTokens": -1},
            {"sessionUpdate": "usage_update", "totalTokens": "malformed"},
            {
                "sessionUpdate": "session_info_update",
                "sessionInfo": [False, -2],
            },
        ],
    )
    def test_direct_unproven_usage_and_session_info_are_bounded_raw_updates(
        self, update: dict[str, Any]
    ) -> None:
        """ADI-08: agent-defined diagnostics are retained but not interpreted."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._direct_prompt_phases = {"active": "active"}
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues["active"] = queue
        client._direct_update_budgets["active"] = {
            "bytes": 0,
            "count": 0,
            "byte_limit": 10_000,
            "count_limit": 10,
        }
        client._transport = MagicMock()

        client._handle_notification(
            {
                "method": "session/update",
                "params": {"sessionId": "active", "update": update},
            }
        )

        assert queue.get_nowait() == update
        client._transport.fail_closed.assert_not_called()

    def test_direct_out_of_prompt_model_drift_fails_continuity(self) -> None:
        """ADI-03/08: a config notification cannot silently change the model."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {
            "session": SessionState(
                session_id="session", model_id="gpt-5.3-codex"
            )
        }
        client._transport = MagicMock()

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {
                                "id": "model",
                                "category": "model",
                                "currentValue": "different-model",
                            }
                        ],
                    },
                },
            }
        )

        client._transport.fail_closed.assert_called_once_with(
            "direct ACP selected model drifted"
        )

    def test_direct_active_prompt_model_drift_fails_before_evidence_retention(
        self,
    ) -> None:
        """ADI-03/08: active prompt evidence cannot normalize a model change."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {
            "session": SessionState(
                session_id="session", model_id="gpt-5.3-codex"
            )
        }
        client._direct_prompt_phases = {"session": "active"}
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues = {"session": queue}
        client._direct_update_budgets = {
            "session": {
                "bytes": 0,
                "count": 0,
                "byte_limit": 10_000,
                "count_limit": 10,
            }
        }
        client._transport = MagicMock()

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {
                                "id": "model",
                                "currentValue": "different-model",
                            }
                        ],
                    },
                },
            }
        )

        assert queue.empty()
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP selected model drifted"
        )

    def test_direct_known_control_update_is_retained_outside_prompt(self) -> None:
        """ADI-08: legitimate control-plane updates are validated, not dropped."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {"session": SessionState(session_id="session")}
        client._transport = MagicMock()
        commands = [{"name": "test", "description": "command"}]

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session",
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": commands,
                    },
                },
            }
        )

        assert client._available_commands_by_session == {"session": commands}
        client._transport.fail_closed.assert_not_called()

    def test_direct_provisional_session_new_command_update_is_retained(self) -> None:
        """Observed ACP ordering: commands may precede the session/new response."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1
        commands = [{"name": "test", "description": "command"}]

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "provisional-session",
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": commands,
                    },
                },
            }
        )

        assert client._available_commands_by_session == {
            "provisional-session": commands
        }
        client._transport.fail_closed.assert_not_called()

    def test_direct_provisional_session_ids_are_bounded_to_pending_creates(
        self,
    ) -> None:
        """ADI-08/15: unknown pre-response IDs cannot flood retained state."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1

        for session_id in ("provisional-one", "provisional-two"):
            client._handle_notification(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": [],
                        },
                    },
                }
            )

        assert client._provisional_session_ids == {"provisional-one"}
        assert set(client._available_commands_by_session) == {"provisional-one"}
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )

    def test_direct_provisional_session_response_mismatch_fails_and_clears(
        self,
    ) -> None:
        """ADI-03/08: a returned session ID must match its provisional stream."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._provisional_session_ids = {"provisional"}
        client._available_commands_by_session = {"provisional": []}
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 0

        with pytest.raises(ConnectionError, match="provisional session identity"):
            client._bind_provisional_session("different")

        assert client._provisional_session_ids == set()
        assert client._available_commands_by_session == {}
        client._transport.fail_closed.assert_called_once()

    @pytest.mark.parametrize(
        ("byte_limit", "count_limit", "updates"),
        [
            (10_000, 1, ["one", "two"]),
            (80, 10, ["x" * 200]),
        ],
    )
    def test_direct_reader_side_evidence_limits_fail_before_queue_growth(
        self,
        byte_limit: int,
        count_limit: int,
        updates: list[str],
    ) -> None:
        """ADI-08/15: a fast child cannot outpace bounded evidence retention."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {"session": SessionState(session_id="session")}
        client._direct_prompt_phases = {"session": "active"}
        queue: asyncio.Queue = asyncio.Queue(maxsize=count_limit + 1)
        client._update_queues = {"session": queue}
        client._direct_update_budgets = {
            "session": {
                "bytes": 0,
                "count": 0,
                "byte_limit": byte_limit,
                "count_limit": count_limit,
            }
        }
        client._transport = MagicMock()

        for text in updates:
            client._handle_notification(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )

        assert queue.qsize() <= count_limit
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP evidence stream exceeded reader-side limits"
        )

    def test_direct_active_config_update_rejects_malformed_items(self) -> None:
        """ADI-03/08: every complete config option item is structurally checked."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {
            "session": SessionState(
                session_id="session", model_id="gpt-5.3-codex"
            )
        }
        client._direct_prompt_phases = {"session": "active"}
        client._update_queues = {"session": asyncio.Queue()}
        client._direct_update_budgets = {
            "session": {
                "bytes": 0,
                "count": 0,
                "byte_limit": 10_000,
                "count_limit": 10,
            }
        }
        client._transport = MagicMock()

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "session",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [42],
                    },
                },
            }
        )

        assert client._update_queues["session"].empty()
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP config update malformed"
        )

    @pytest.mark.parametrize("next_phase", ["terminal", "preparing"])
    def test_direct_post_terminal_update_fails_before_or_during_next_prompt(
        self, next_phase: str
    ) -> None:
        """ADI-08/10: terminal response is an ordered, closed evidence boundary."""

        client = self._make_client()
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue = asyncio.Queue()
        client._update_queues["active"] = queue
        client._direct_prompt_phases = {"active": "active"}
        transport = MagicMock()
        transport.has_pending_incoming_requests.return_value = False
        client._transport = transport

        client._observe_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}},
            "session/prompt",
            {"sessionId": "active", "prompt": []},
        )
        assert queue.get_nowait() == {"__acp_prompt_terminal__": True}
        client._direct_prompt_phases["active"] = next_phase

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "late"},
                    },
                },
            }
        )

        transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )


# ---------------------------------------------------------------------------
# _extract_models
# ---------------------------------------------------------------------------


class TestExtractModels:
    """Model catalog parsing from ACP session/new response."""

    def _make_client(self) -> AcpClient:
        client = AcpClient.__new__(AcpClient)
        client._models = []
        client._default_model = None
        return client

    def test_typical_response(self):
        client = self._make_client()
        client._extract_models(
            {
                "availableModels": [
                    {"modelId": "gpt-4.1", "name": "GPT 4.1"},
                    {"modelId": "gpt-4o", "name": "GPT 4o", "_meta": {"tier": "free"}},
                ],
                "currentModelId": "gpt-4.1",
            }
        )
        assert len(client._models) == 2
        assert client._models[0].model_id == "gpt-4.1"
        assert client._models[1].meta == {"tier": "free"}
        assert client._default_model == "gpt-4.1"

    def test_empty_models_list(self):
        client = self._make_client()
        client._extract_models({"availableModels": [], "currentModelId": None})
        assert client._models == []
        assert client._default_model is None

    def test_missing_name_uses_model_id(self):
        client = self._make_client()
        client._extract_models(
            {
                "availableModels": [{"modelId": "auto"}],
                "currentModelId": "auto",
            }
        )
        assert client._models[0].name == "auto"

    def test_missing_available_models_key(self):
        client = self._make_client()
        client._extract_models({})
        assert client._models == []
        assert client._default_model is None


# ---------------------------------------------------------------------------
# _try_set_model
# ---------------------------------------------------------------------------


class TestTrySetModel:
    """Model selection tries session/set_model, falls back, or raises."""

    @pytest.mark.asyncio
    async def test_first_method_succeeds(self):
        """session/set_model works — no fallback needed."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        transport = AsyncMock()
        transport.send_request = AsyncMock(return_value={})
        client._transport = transport

        await client._try_set_model("s1", "gpt-4o")
        transport.send_request.assert_called_once_with(
            "session/set_model", {"sessionId": "s1", "modelId": "gpt-4o"}
        )
        assert client._sessions["s1"].model_id == "gpt-4o"

    @pytest.mark.asyncio
    async def test_fallback_to_set_config_option(self):
        """session/set_model fails with 'not found', falls back to set_config_option."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        call_count = 0

        async def mock_send(method, params):
            nonlocal call_count
            call_count += 1
            if method == "session/set_model":
                raise AcpError("Method not found", {"code": -32601})
            return {}

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        await client._try_set_model("s1", "gpt-4o")
        assert call_count == 2
        assert client._sessions["s1"].model_id == "gpt-4o"

    @pytest.mark.asyncio
    async def test_both_methods_fail_raises(self):
        """Both methods return 'not found' — RuntimeError raised."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        async def mock_send(method, params):
            raise AcpError("Method not found", {"code": -32601})

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        with pytest.raises(RuntimeError, match="Model selection not supported"):
            await client._try_set_model("s1", "gpt-4o")


class TestDirectAcpContract:
    """Strict ACP primitives used only by Meadow direct mode."""

    @pytest.mark.asyncio
    async def test_direct_client_logs_never_persist_control_payloads(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ADI-02/09/15: direct client logs keep only bounded metadata."""

        cwd_canary = "/private/T122-WORKSPACE-CREDENTIAL-SECRET"
        session_canary = "T122-BACKEND-SESSION-SECRET"
        agent_canary = "T122-AGENT-INFO-SECRET"
        auth_canary = "T122-AUTH-METHOD-SECRET"
        client = AcpClient("unused", callback_policy=CallbackPolicy.DIRECT_DENY)
        transport = AsyncMock()
        transport.send_request.side_effect = [
            {
                "protocolVersion": 1,
                "agentInfo": {"name": agent_canary, "version": "1"},
                "agentCapabilities": {"secretCapability": auth_canary},
                "authMethods": [{"name": auth_canary}],
            },
            {"sessionId": session_canary},
        ]
        transport.pending_request_count.return_value = 0
        client._transport = transport
        caplog.set_level("DEBUG", logger="acp_proxy.client")

        await client._initialize()
        assert await client.create_session(cwd_canary) == session_canary

        for canary in (
            cwd_canary,
            session_canary,
            agent_canary,
            auth_canary,
        ):
            assert canary not in caplog.text
        assert "session/new" in caplog.text
        assert "protocol=1" in caplog.text

    @pytest.mark.asyncio
    async def test_direct_protocol_mismatch_never_exposes_agent_value(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """ADI-02/15: an agent-controlled protocol value cannot escape startup."""

        protocol_canary = "T122-PROTOCOL-CREDENTIAL-SECRET"
        client = AcpClient("unused", callback_policy=CallbackPolicy.DIRECT_DENY)
        transport = AsyncMock()
        transport.send_request.return_value = {
            "protocolVersion": protocol_canary,
            "agentInfo": {},
            "agentCapabilities": {},
        }
        client._transport = transport
        caplog.set_level("DEBUG", logger="acp_proxy.client")

        with pytest.raises(RuntimeError, match="direct ACP protocol version mismatch") as raised:
            await client._initialize()

        assert protocol_canary not in str(raised.value)
        assert protocol_canary not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_protocol", [True, 1.0])
    async def test_direct_protocol_version_requires_exact_integer(
        self,
        invalid_protocol: object,
    ) -> None:
        """ADI-02: bool/float values cannot alias ACP protocol v1."""

        client = AcpClient("unused", callback_policy=CallbackPolicy.DIRECT_DENY)
        transport = AsyncMock()
        transport.send_request.return_value = {
            "protocolVersion": invalid_protocol,
            "agentInfo": {},
            "agentCapabilities": {},
        }
        client._transport = transport

        with pytest.raises(RuntimeError, match="direct ACP protocol version mismatch"):
            await client._initialize()

    @pytest.mark.asyncio
    async def test_initialize_retains_agent_capabilities_and_advertises_no_callbacks(
        self,
    ) -> None:
        """ADI-02/09: direct initialization is truthful and least-capability."""
        client = AcpClient.__new__(AcpClient)
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        client._protocol_version = None
        client._agent_capabilities = {}
        client._agent_name = None
        client._agent_version = None
        transport = AsyncMock()
        transport.send_request.return_value = {
            "protocolVersion": 1,
            "agentInfo": {"name": "copilot", "version": "1.2.3"},
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {"list": {}},
            },
        }
        client._transport = transport

        await client._initialize()

        sent = transport.send_request.await_args.args
        assert sent[0] == "initialize"
        assert sent[1]["clientCapabilities"] == {}
        assert client.protocol_version == 1
        assert client.agent_capabilities["loadSession"] is True

    @pytest.mark.asyncio
    async def test_exact_model_selection_requires_complete_acknowledgement(self) -> None:
        """ADI-03: method success is not model acknowledgement."""
        client = AcpClient.__new__(AcpClient)
        client._models = [ModelInfo("gpt-5.3-codex", "GPT-5.3 Codex")]
        client._default_model = "gpt-5.5"
        client._sessions = {}
        client._transport = AsyncMock()
        client._transport.send_request.side_effect = [
            {
                "sessionId": "session",
                "models": {
                    "availableModels": [
                        {"modelId": "gpt-5.3-codex", "name": "GPT-5.3 Codex"}
                    ],
                    "currentModelId": "gpt-5.5",
                },
            },
            {
                "configOptions": [
                    {
                        "id": "model",
                        "category": "model",
                        "currentValue": "gpt-5.3-codex",
                    }
                ]
            },
        ]

        descriptor = await client.create_session_exact("/workspace", "gpt-5.3-codex")

        assert descriptor.session_id == "session"
        assert descriptor.model_id == "gpt-5.3-codex"
        assert client._transport.send_request.await_args_list[1].args == (
            "session/set_config_option",
            {
                "sessionId": "session",
                "configId": "model",
                "value": "gpt-5.3-codex",
            },
        )

    @pytest.mark.asyncio
    async def test_exact_model_selection_rejects_wrong_current_value(self) -> None:
        """ADI-03: an acknowledged default cannot substitute for the requested model."""
        client = AcpClient.__new__(AcpClient)
        client._models = [ModelInfo("gpt-5.3-codex", "GPT-5.3 Codex")]
        client._default_model = "gpt-5.5"
        client._sessions = {}
        client._transport = AsyncMock()
        client._transport.send_request.side_effect = [
            {
                "sessionId": "session",
                "models": {
                    "availableModels": [
                        {"modelId": "gpt-5.3-codex", "name": "GPT-5.3 Codex"}
                    ],
                    "currentModelId": "gpt-5.5",
                },
            },
            {
                "configOptions": [
                    {"id": "model", "currentValue": "gpt-5.5"}
                ]
            },
        ]

        with pytest.raises(RuntimeError, match="acknowledged model"):
            await client.create_session_exact("/workspace", "gpt-5.3-codex")

    @pytest.mark.asyncio
    async def test_cancel_is_stable_session_notification(self) -> None:
        """ADI-10: cancellation reaches ACP and is not local-task-only."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"session": SessionState("session")}
        client._transport = AsyncMock()

        await client.cancel_session("session")

        client._transport.send_notification.assert_awaited_once_with(
            "session/cancel", {"sessionId": "session"}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["stop", "abort"])
    async def test_teardown_drains_full_update_queue_and_closes_transport(
        self, method: str
    ) -> None:
        """ADI-13/15: evidence backpressure cannot prevent owned teardown."""

        client = AcpClient.__new__(AcpClient)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait({"sessionUpdate": "agent_message_chunk"})
        client._update_queues = {"session": queue}
        client._direct_prompt_phases = {"session": "active"}
        client._direct_update_budgets = {"session": {}}
        client._expected_model_updates = {}
        client._available_commands_by_session = {}
        client._provisional_session_ids = set()
        client._sessions = {"session": SessionState("session")}
        client._transport = AsyncMock()

        await getattr(client, method)()

        getattr(client._transport, method).assert_awaited_once()
        assert queue.get_nowait() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("result", [{}, {"stopReason": ""}, {"stopReason": "novel"}])
    async def test_direct_prompt_requires_known_explicit_stop_reason(
        self, result: dict[str, Any]
    ) -> None:
        """ADI-08/10: direct terminal state is never synthesized or unknown."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"session": SessionState("session")}
        client._update_queues = {}
        transport = AsyncMock()
        transport.send_request.return_value = result
        client._transport = transport

        with pytest.raises(RuntimeError, match="known non-empty stopReason"):
            async for _ in client.prompt_blocks(
                "session", [{"type": "text", "text": "prompt"}], timeout_s=1
            ):
                pass

    def test_direct_callback_policy_denies_unadvertised_callbacks(self) -> None:
        """ADI-09: direct callbacks fail closed and never select allow_always."""
        client = AcpClient.__new__(AcpClient)
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        permission = client._handle_agent_request(
            {
                "method": "session/request_permission",
                "params": {
                    "options": [
                        {"optionId": "always", "kind": "allow_always"},
                        {"optionId": "once", "kind": "allow_once"},
                    ]
                },
            }
        )
        assert permission == {"outcome": {"outcome": "cancelled"}}
        with pytest.raises(PermissionError):
            client._handle_agent_request(
                {"method": "fs/read_text_file", "params": {"path": "/tmp/x"}}
            )

    def test_direct_callback_evidence_is_ordered_and_sanitized(self) -> None:
        """ADI-08/09: denied callbacks retain outcome without raw sensitive params."""
        client = AcpClient.__new__(AcpClient)
        client._callback_policy = CallbackPolicy.DIRECT_DENY
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        client._update_queues = {"session": queue}
        client._direct_prompt_phases = {"session": "active"}
        client._direct_update_budgets = {
            "session": {
                "bytes": 0,
                "count": 0,
                "byte_limit": 10_000,
                "count_limit": 10,
            }
        }
        client._transport = MagicMock()

        client._observe_agent_request(
            {
                "method": "session/request_permission",
                "params": {
                    "sessionId": "session",
                    "secret": "must-not-survive",
                    "options": [
                        {
                            "optionId": "private-option-id",
                            "kind": "allow_always",
                            "name": "private permission text",
                        },
                        {"optionId": "once", "kind": "allow_once"},
                    ],
                },
            }
        )
        client._observe_agent_request(
            {
                "method": "fs/read_text_file",
                "params": {
                    "sessionId": "session",
                    "path": "/private/sensitive/path",
                },
            }
        )

        permission = queue.get_nowait()
        denied = queue.get_nowait()
        assert permission == {
            "sessionUpdate": "client_permission_request",
            "outcome": "cancelled",
            "offeredKinds": ["allow_always", "allow_once"],
        }
        assert denied == {
            "sessionUpdate": "client_callback_denied",
            "callbackMethod": "fs/read_text_file",
            "outcome": "denied",
        }
        assert "private" not in repr((permission, denied)).lower()

    @pytest.mark.asyncio
    async def test_non_not_found_error_propagates(self):
        """A non-'not found' error is re-raised immediately, no fallback."""
        from acp_proxy.transport import AcpError

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}

        async def mock_send(method, params):
            raise AcpError("Server exploded", {"code": -32000})

        transport = MagicMock()
        transport.send_request = mock_send
        client._transport = transport

        with pytest.raises(AcpError, match="Server exploded"):
            await client._try_set_model("s1", "gpt-4o")


# ---------------------------------------------------------------------------
# Prompt timeout (prompt-level deadline enforcement)
# ---------------------------------------------------------------------------


class TestPromptTimeout:
    """Prompt-level timeout enforces a deadline on session/prompt.

    The prompt() method must raise PromptTimeout if the ACP server does
    not complete within the configured deadline.  This prevents a hung
    language server from blocking the HTTP connection indefinitely.
    """

    @pytest.mark.asyncio
    async def test_timeout_raises_prompt_timeout(self):
        """A prompt that exceeds the deadline raises PromptTimeout."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        # Transport that never responds — simulates a hung server
        async def never_respond(method, params):
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout) as exc_info:
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.2,
            ):
                pass

        assert exc_info.value.session_id == "s1"
        assert exc_info.value.timeout_s == 0.2

    @pytest.mark.asyncio
    async def test_timeout_includes_partial_text(self):
        """Partial text collected before the timeout is preserved in the exception."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def slow_respond(method, params):
            # Wait long enough that chunks are delivered, then hang
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = slow_respond
        client._transport = transport

        async def push_chunks():
            """Push chunks into the queue shortly after it's created."""
            # Wait for prompt() to create the queue
            for _ in range(50):
                if "s1" in client._update_queues:
                    break
                await asyncio.sleep(0.01)
            q = client._update_queues.get("s1")
            if q:
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "partial "},
                    }
                )
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "response"},
                    }
                )

        # Start pushing chunks concurrently
        push_task = asyncio.create_task(push_chunks())

        with pytest.raises(PromptTimeout) as exc_info:
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.5,
            ):
                pass

        await push_task
        assert exc_info.value.partial_text == "partial response"

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(self):
        """A prompt that completes before the deadline works normally."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def fast_respond(method, params):
            # Respond quickly
            await asyncio.sleep(0.05)
            return {"stopReason": "end_turn"}

        transport = MagicMock()
        transport.send_request = fast_respond
        transport.on_notification = MagicMock()
        transport.on_request = MagicMock()
        client._transport = transport

        # Push an update and then let the prompt task complete
        async def push_update():
            await asyncio.sleep(0.01)
            q = client._update_queues.get("s1")
            if q:
                q.put_nowait(
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello"},
                    }
                )

        asyncio.create_task(push_update())

        results = []
        async for update in client.prompt(
            "s1",
            [{"role": "user", "content": "hi"}],
            timeout_s=5.0,
        ):
            results.append(update)

        # Should have the chunk + done sentinel
        assert any(r.get("done") for r in results)

    @pytest.mark.asyncio
    async def test_unknown_session_raises_value_error(self):
        """Prompting an unknown session raises ValueError, not timeout."""
        client = AcpClient.__new__(AcpClient)
        client._sessions = {}
        client._update_queues = {}

        with pytest.raises(ValueError, match="Unknown session"):
            async for _ in client.prompt(
                "nonexistent",
                [{"role": "user", "content": "hello"}],
            ):
                pass

    @pytest.mark.asyncio
    async def test_queue_cleanup_after_timeout(self):
        """The update queue is removed after a timeout to prevent leaks."""
        from acp_proxy.client import PromptTimeout

        client = AcpClient.__new__(AcpClient)
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def never_respond(method, params):
            await asyncio.sleep(999)

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout):
            async for _ in client.prompt(
                "s1",
                [{"role": "user", "content": "hello"}],
                timeout_s=0.1,
            ):
                pass

        # Queue should be cleaned up
        assert "s1" not in client._update_queues
