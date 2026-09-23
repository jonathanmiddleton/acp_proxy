"""Unit tests for the ACP client layer.

Covers denied callback authority, model binding, session-state correlation,
and prompt deadlines. Tests use in-process transport boundaries and never
launch a Copilot subprocess.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from meadow_bridge.client import (
    AcpClient,
    DirectModelBindingStrategy,
    ModelAcknowledgementError,
    ModelInfo,
    SessionState,
)
from meadow_bridge.transport import AcpTransport
from tests.test_transport import FakeProcess

class TestHandleNotification:
    """Notifications are routed to the correct session's update queue."""

    def _make_client(self) -> AcpClient:
        client = AcpClient("unused")
        client._sessions = {}
        client._update_queues = {}
        client._direct_prompt_phases = {}
        client._direct_update_budgets = {}
        client._model_bindings = {}
        client._provisional_session_ids = set()
        client._session_new_response_ids = set()
        return client


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
            {
                "method": "session/update",
                "params": {
                    "sessionId": "active",
                    "update": {"sessionUpdate": ["not", "a", "kind"]},
                },
            },
            {"method": "unknown/notification", "params": {}},
        ],
    )
    def test_direct_unknown_late_or_malformed_update_fails_continuity(
        self, message: dict[str, object]
    ) -> None:
        """ADI-08/10: direct mode never silently drops ambiguous evidence."""

        client = self._make_client()
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
        client._direct_prompt_phases = {"active": "active"}
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
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
        client._direct_prompt_phases = {"active": "active"}
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
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

    def test_direct_binding_model_update_matches_expected_without_mutating_state(
        self,
    ) -> None:
        """A binding notification corroborates but does not settle the model RPC."""

        client = self._make_client()
        client._sessions = {
            "session": SessionState(session_id="session", model_id="auto")
        }
        client._begin_model_binding("session", "target", "session/set_config_option")
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
                                "currentValue": "target",
                            },
                            {
                                "id": "mode",
                                "category": "mode",
                                "currentValue": "agent",
                            },
                        ],
                    },
                },
            }
        )

        assert client._sessions["session"].model_id == "auto"
        client._transport.fail_closed.assert_not_called()

    def test_direct_binding_third_model_update_fails_continuity(self) -> None:
        """A pending binding permits only its prior model and requested target."""

        client = self._make_client()
        client._sessions = {
            "session": SessionState(session_id="session", model_id="auto")
        }
        client._begin_model_binding("session", "target", "session/set_config_option")
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
                                "currentValue": "unrequested-model",
                            }
                        ],
                    },
                },
            }
        )

        client._transport.fail_closed.assert_called_once_with(
            "direct ACP selected model drifted"
        )

    @pytest.mark.parametrize(
        "binding_pending",
        [False, True],
    )
    def test_direct_config_snapshot_cannot_omit_selected_model(
        self,
        binding_pending: bool,
    ) -> None:
        """A complete config snapshot must preserve ready and binding models."""

        client = self._make_client()
        client._sessions = {
            "session": SessionState(session_id="session", model_id="ready-model")
        }
        if binding_pending:
            client._begin_model_binding(
                "session", "target", "session/set_config_option"
            )
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
                                "id": "mode",
                                "category": "mode",
                                "currentValue": "agent",
                            }
                        ],
                    },
                },
            }
        )

        client._transport.fail_closed.assert_called_once_with(
            "direct ACP selected model state missing"
        )

    def test_direct_ready_model_update_matches_without_retaining_config(self) -> None:
        """A matching post-binding snapshot is validated and discarded."""

        client = self._make_client()
        client._sessions = {
            "session": SessionState(session_id="session", model_id="target")
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
                                "currentValue": "target",
                            },
                            {
                                "id": "reasoning_effort",
                                "category": "thought_level",
                                "currentValue": "sensitive-payload",
                            },
                        ],
                    },
                },
            }
        )

        assert client._sessions["session"].model_id == "target"
        assert "sensitive-payload" not in repr(
            {
                key: value
                for key, value in client.__dict__.items()
                if key != "_transport"
            }
        )
        client._transport.fail_closed.assert_not_called()

    def test_direct_active_prompt_model_drift_fails_before_evidence_retention(
        self,
    ) -> None:
        """ADI-03/08: active prompt evidence cannot normalize a model change."""

        client = self._make_client()
        client._sessions = {
            "session": SessionState(
                session_id="session", model_id="gpt-5.3-codex"
            )
        }
        client._direct_prompt_phases = {"session": "active"}
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
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

    def test_direct_known_command_update_is_validated_and_discarded_outside_prompt(
        self,
    ) -> None:
        """Known state is accepted without retaining an unused command snapshot."""

        client = self._make_client()
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

        client._transport.fail_closed.assert_not_called()

    @pytest.mark.parametrize(
        ("update_kind", "expected_kind"),
        [
            ("current_mode_update", "current_mode_update"),
            ("sensitive-unrecognized-kind", "unrecognized"),
            (None, "invalid"),
        ],
    )
    def test_direct_session_update_logging_is_structural_and_payload_safe(
        self,
        caplog: pytest.LogCaptureFixture,
        update_kind: str | None,
        expected_kind: str,
    ) -> None:
        """Diagnostics identify update shape without retaining agent-controlled data."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1
        sensitive_session_id = "sensitive-session-id"
        sensitive_payload = "sensitive-update-payload"
        update = {
            "sessionUpdate": update_kind,
            "currentModeId": sensitive_payload,
        }
        caplog.set_level("DEBUG", logger="meadow_bridge.client")

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": sensitive_session_id,
                    "update": update,
                },
            }
        )

        assert f"kind={expected_kind}" in caplog.text
        assert "session_id_present=True" in caplog.text
        assert "session_known=False" in caplog.text
        assert "provisional_session=False" in caplog.text
        assert "response_observed=False" in caplog.text
        assert "prompt_phase=none" in caplog.text
        assert "pending_session_new=1" in caplog.text
        assert "update_queue_present=False" in caplog.text
        assert sensitive_session_id not in caplog.text
        assert sensitive_payload not in caplog.text
        assert "sensitive-unrecognized-kind" not in caplog.text

    def test_direct_config_update_logging_classifies_without_logging_values(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Config diagnostics expose semantic shape without option data."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1
        sensitive_value = "sensitive-current-value"
        caplog.set_level("DEBUG", logger="meadow_bridge.client")

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "sensitive-session-id",
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {
                                "id": "model",
                                "currentValue": sensitive_value,
                            },
                            {"category": "mode", "currentValue": sensitive_value},
                            {
                                "category": "thought_level",
                                "currentValue": sensitive_value,
                            },
                            {"id": sensitive_value, "currentValue": sensitive_value},
                            42,
                        ],
                    },
                },
            }
        )

        assert "kind=config_option_update" in caplog.text
        assert "options_list=True" in caplog.text
        assert "option_count=5" in caplog.text
        assert "invalid_options=1" in caplog.text
        assert "model_options=1" in caplog.text
        assert "mode_options=1" in caplog.text
        assert "thought_level_options=1" in caplog.text
        assert "other_options=1" in caplog.text
        assert sensitive_value not in caplog.text
        assert "sensitive-session-id" not in caplog.text

    @pytest.mark.parametrize(
        "update",
        [
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "sensitive-command", "description": "sensitive-payload"}
                ],
            },
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {
                        "id": "mode",
                        "category": "mode",
                        "currentValue": "sensitive-payload",
                    }
                ],
            },
            {
                "sessionUpdate": "current_mode_update",
                "currentModeId": "sensitive-payload",
            },
            {"sessionUpdate": "usage_update", "opaque": "sensitive-payload"},
            {
                "sessionUpdate": "session_info_update",
                "opaque": "sensitive-payload",
            },
        ],
    )
    def test_direct_provisional_session_state_is_accepted_and_discarded(
        self,
        update: dict[str, Any],
    ) -> None:
        """Stable state may precede session/new without becoming client state."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "provisional-session",
                    "update": update,
                },
            }
        )

        assert client._provisional_session_ids == {"provisional-session"}
        assert client._sessions == {}
        assert client._update_queues == {}
        assert "sensitive-payload" not in repr(
            {
                key: value
                for key, value in client.__dict__.items()
                if key != "_transport"
            }
        )
        client._transport.fail_closed.assert_not_called()

    @pytest.mark.parametrize(
        "update",
        [
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "effect"},
            },
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "effect"},
            },
            {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "effect"},
            },
            {"sessionUpdate": "tool_call", "toolCallId": "effect"},
            {"sessionUpdate": "tool_call_update", "toolCallId": "effect"},
            {"sessionUpdate": "plan", "entries": []},
        ],
    )
    def test_direct_provisional_prompt_or_effect_update_fails_closed(
        self,
        update: dict[str, Any],
    ) -> None:
        """A pending create does not authorize prompt-scoped evidence."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "provisional-session",
                    "update": update,
                },
            }
        )

        assert client._provisional_session_ids == set()
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )

    @pytest.mark.parametrize(
        ("update", "expected_error"),
        [
            (
                {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [{"name": "valid"}, 42],
                },
                "direct ACP session update protocol failure",
            ),
            (
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [{"currentValue": "missing-id"}],
                },
                "direct ACP config update malformed",
            ),
            (
                {"sessionUpdate": "current_mode_update", "currentModeId": ""},
                "direct ACP session update protocol failure",
            ),
            (
                {"sessionUpdate": "usage_update", "opaque": {"not-json"}},
                "direct ACP session update protocol failure",
            ),
            (
                {"sessionUpdate": "session_info_update", "opaque": object()},
                "direct ACP session update protocol failure",
            ),
        ],
    )
    def test_direct_provisional_malformed_session_state_fails_closed(
        self,
        update: dict[str, Any],
        expected_error: str,
    ) -> None:
        """Creation correlation never turns malformed state into tolerated noise."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "provisional-session",
                    "update": update,
                },
            }
        )

        client._transport.fail_closed.assert_called_once_with(expected_error)

    @pytest.mark.parametrize(
        "update",
        [
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [
                    {"name": "command", "description": "x" * 256_000}
                ],
            },
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {
                        "id": "mode",
                        "currentValue": "mode",
                        "description": "x" * 256_000,
                    }
                ],
            },
            {
                "sessionUpdate": "current_mode_update",
                "currentModeId": "mode",
                "opaque": "x" * 256_000,
            },
            {
                "sessionUpdate": "usage_update",
                "opaque": "x" * 256_000,
            },
            {
                "sessionUpdate": "session_info_update",
                "opaque": "x" * 256_000,
            },
        ],
    )
    def test_direct_provisional_oversized_session_state_fails_closed(
        self,
        update: dict[str, Any],
    ) -> None:
        """Every accepted creation-state kind shares one per-update byte bound."""

        client = self._make_client()
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 1

        client._handle_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "provisional-session",
                    "update": update,
                },
            }
        )

        client._transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )

    def test_direct_provisional_session_ids_are_bounded_to_pending_creates(
        self,
    ) -> None:
        """ADI-08/15: unknown pre-response IDs cannot flood retained state."""

        client = self._make_client()
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
        client._transport.fail_closed.assert_called_once_with(
            "direct ACP session update protocol failure"
        )

    def test_direct_provisional_session_response_mismatch_fails_and_clears(
        self,
    ) -> None:
        """ADI-03/08: a returned session ID must match its provisional stream."""

        client = self._make_client()
        client._provisional_session_ids = {"provisional"}
        client._transport = MagicMock()
        client._transport.pending_request_count.return_value = 0

        with pytest.raises(ConnectionError, match="provisional session identity"):
            client._bind_provisional_session("different")

        assert client._provisional_session_ids == set()
        client._transport.fail_closed.assert_called_once()

    @pytest.mark.parametrize("first_waiter_resumes", [False, True])
    def test_direct_concurrent_session_new_responses_preserve_correlation(
        self,
        first_waiter_resumes: bool,
    ) -> None:
        """Response and waiter scheduling cannot orphan another valid create."""

        client = self._make_client()
        client._provisional_session_ids = {"session-a"}
        client._transport = MagicMock()

        client._transport.pending_request_count.return_value = 2
        client._observe_session_new_response(
            {"jsonrpc": "2.0", "id": 1, "result": {"sessionId": "session-b"}}
        )
        assert client._provisional_session_ids == {"session-a"}
        assert client._session_new_response_ids == {"session-b"}

        if first_waiter_resumes:
            client._transport.pending_request_count.return_value = 1
            client._bind_provisional_session("session-b")
            assert client._provisional_session_ids == {"session-a"}

        client._transport.pending_request_count.return_value = 1
        client._observe_session_new_response(
            {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "session-a"}}
        )
        client._transport.pending_request_count.return_value = 0
        client._bind_provisional_session("session-a")
        if not first_waiter_resumes:
            client._bind_provisional_session("session-b")

        assert client._provisional_session_ids == set()
        assert client._session_new_response_ids == set()
        client._transport.fail_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_session_new_response_bridges_registration_gap(self) -> None:
        """Buffered state after the response is correlated before task resumption."""

        client = AcpClient("unused")
        transport = AcpTransport()
        process = FakeProcess()
        transport._process = process
        transport.on_notification(client._handle_notification)
        transport.on_response_observed(client._observe_response)
        client._transport = transport

        task = asyncio.create_task(client.create_session("/workspace"))
        await asyncio.sleep(0)
        request = json.loads(process.stdin.written[0])
        session_id = "response-session"

        # Dispatch synchronously to keep the create coroutine suspended between
        # transport response observation and SessionState registration.
        transport._dispatch(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "sessionId": session_id,
                    "models": {
                        "availableModels": [
                            {"modelId": "auto", "name": "Auto"},
                            {"modelId": "target", "name": "Target"},
                        ],
                        "currentModelId": "auto",
                    },
                },
            }
        )
        assert session_id not in client._sessions
        assert not task.done()

        transport._dispatch(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [
                            {
                                "id": "model",
                                "category": "model",
                                "currentValue": "notification-is-not-authoritative",
                            }
                        ],
                    },
                },
            }
        )
        assert transport.is_open is True

        assert await task == session_id
        assert client._sessions[session_id].model_id == "auto"
        assert client._provisional_session_ids == set()
        assert client._session_new_response_ids == set()
        transport._process = None

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
        client._sessions = {"session": SessionState(session_id="session")}
        client._direct_prompt_phases = {"session": "active"}
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=count_limit + 1)
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
        client._sessions = {"active": SessionState(session_id="active")}
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
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
# create_session
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_models
# ---------------------------------------------------------------------------


class TestExtractModels:
    """Model catalog parsing from ACP session/new response."""

    def _make_client(self) -> AcpClient:
        client = AcpClient("unused")
        client._models = []
        client._default_model = None
        return client

    def test_typical_response(self) -> None:
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

    def test_empty_models_list(self) -> None:
        client = self._make_client()
        client._extract_models({"availableModels": [], "currentModelId": None})
        assert client._models == []
        assert client._default_model is None

    def test_missing_name_uses_model_id(self) -> None:
        client = self._make_client()
        client._extract_models(
            {
                "availableModels": [{"modelId": "auto"}],
                "currentModelId": "auto",
            }
        )
        assert client._models[0].name == "auto"

    def test_missing_available_models_key(self) -> None:
        client = self._make_client()
        client._extract_models({})
        assert client._models == []
        assert client._default_model is None


class TestDirectModelBindingNegotiation:
    """Direct startup selects one model-binding strategy for the generation."""

    @staticmethod
    def _client() -> AcpClient:
        client = AcpClient("unused")
        client._sessions = {
            "catalog": SessionState(
                "catalog",
                model_id="auto",
                available_model_ids=frozenset({"auto", "target"}),
            )
        }
        client._transport = AsyncMock()
        return client


    @pytest.mark.asyncio
    async def test_standard_strategy_wins_without_probing_copilot(self) -> None:
        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._transport.send_request.return_value = {
            "configOptions": [{"id": "model", "currentValue": "auto"}]
        }

        strategy = await client.negotiate_direct_model_binding("catalog", "auto")

        assert strategy is DirectModelBindingStrategy.STANDARD_CONFIG
        assert client.direct_model_binding_strategy is strategy
        client._transport.send_request.assert_awaited_once_with(
            "session/set_config_option",
            {"sessionId": "catalog", "configId": "model", "value": "auto"},
        )
        assert client._model_bindings == {}

    @pytest.mark.asyncio
    async def test_method_not_found_selects_copilot_strategy(self) -> None:
        from meadow_bridge.transport import AcpError

        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._transport.send_request.side_effect = [
            AcpError("standard unavailable", {"code": -32601}),
            {},
        ]

        strategy = await client.negotiate_direct_model_binding("catalog", "auto")

        assert strategy is DirectModelBindingStrategy.COPILOT_SET_MODEL
        assert client._transport.send_request.await_args_list == [
            call(
                "session/set_config_option",
                {"sessionId": "catalog", "configId": "model", "value": "auto"},
            ),
            call(
                "session/set_model",
                {"sessionId": "catalog", "modelId": "auto"},
            ),
        ]
        assert client._model_bindings == {}

    @pytest.mark.asyncio
    async def test_neither_strategy_fails_without_freezing_state(self) -> None:
        from meadow_bridge.transport import AcpError

        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        child_canary = "CHILD-ERROR-MUST-NOT-CROSS"
        client._transport.send_request.side_effect = [
            AcpError(child_canary, {"code": -32601}),
            AcpError(child_canary, {"code": -32601}),
        ]

        with pytest.raises(ModelAcknowledgementError) as exc_info:
            await client.negotiate_direct_model_binding("catalog", "auto")

        assert child_canary not in str(exc_info.value)
        assert client.direct_model_binding_strategy is None
        assert client._sessions["catalog"].model_id == "auto"
        assert client._model_bindings == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_code", [-32000, -32601.0, "-32601", True])
    async def test_non_method_error_never_downgrades_to_copilot(
        self, error_code: object
    ) -> None:
        from meadow_bridge.transport import AcpError

        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._transport.send_request.side_effect = AcpError(
            "rejected with child detail",
            {"code": error_code},
        )

        with pytest.raises(ModelAcknowledgementError, match="rejected standard"):
            await client.negotiate_direct_model_binding("catalog", "auto")

        client._transport.send_request.assert_awaited_once()
        assert client.direct_model_binding_strategy is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result",
        [
            {},
            {"configOptions": []},
            {"configOptions": [{"id": "model", "currentValue": "target"}]},
        ],
    )
    async def test_broken_standard_success_never_downgrades(
        self, result: dict[str, Any]
    ) -> None:
        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._transport.send_request.return_value = result

        with pytest.raises(ModelAcknowledgementError):
            await client.negotiate_direct_model_binding("catalog", "auto")

        client._transport.send_request.assert_awaited_once()
        assert client.direct_model_binding_strategy is None
        assert client._model_bindings == {}

    @pytest.mark.asyncio
    async def test_strategy_is_immutable_until_client_state_is_cleared(self) -> None:
        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._transport.send_request.return_value = {
            "configOptions": [{"id": "model", "currentValue": "auto"}]
        }
        await client.negotiate_direct_model_binding("catalog", "auto")

        with pytest.raises(RuntimeError, match="already negotiated"):
            await client.negotiate_direct_model_binding("catalog", "auto")
        assert client._transport.send_request.await_count == 1

        client._clear_client_state()
        assert client.direct_model_binding_strategy is None

    @pytest.mark.asyncio
    async def test_teardown_cannot_restore_an_inflight_strategy(self) -> None:
        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        setter_started = asyncio.Event()
        release_setter = asyncio.Event()

        async def send_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            setter_started.set()
            await release_setter.wait()
            return {"configOptions": [{"id": "model", "currentValue": "auto"}]}

        client._transport.send_request.side_effect = send_request
        negotiation = asyncio.create_task(
            client.negotiate_direct_model_binding("catalog", "auto")
        )
        await setter_started.wait()

        with pytest.raises(RuntimeError, match="in progress"):
            await client.negotiate_direct_model_binding("catalog", "auto")

        await client.stop()
        release_setter.set()
        with pytest.raises(ModelAcknowledgementError, match="interrupted"):
            await negotiation

        assert client.direct_model_binding_strategy is None
        assert client._direct_model_binding_negotiation is None


    @pytest.mark.asyncio
    async def test_public_direct_set_model_uses_frozen_strategy(self) -> None:
        client = self._client()
        assert isinstance(client._transport, AsyncMock)
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.COPILOT_SET_MODEL
        )
        client._transport.send_request.return_value = {}

        await client.set_model("catalog", "target")

        client._transport.send_request.assert_awaited_once_with(
            "session/set_model",
            {"sessionId": "catalog", "modelId": "target"},
        )
        assert client._sessions["catalog"].model_id == "target"


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
        client = AcpClient("unused")
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
        caplog.set_level("DEBUG", logger="meadow_bridge.client")

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
        client = AcpClient("unused")
        transport = AsyncMock()
        transport.send_request.return_value = {
            "protocolVersion": protocol_canary,
            "agentInfo": {},
            "agentCapabilities": {},
        }
        client._transport = transport
        caplog.set_level("DEBUG", logger="meadow_bridge.client")

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

        client = AcpClient("unused")
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
        client = AcpClient("unused")
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
    @pytest.mark.parametrize(
        ("strategy", "current_model", "setter_method"),
        [
            (DirectModelBindingStrategy.STANDARD_CONFIG, "auto", "session/set_config_option"),
            (DirectModelBindingStrategy.STANDARD_CONFIG, "target", "session/set_config_option"),
            (DirectModelBindingStrategy.COPILOT_SET_MODEL, "auto", "session/set_model"),
            (DirectModelBindingStrategy.COPILOT_SET_MODEL, "target", "session/set_model"),
        ],
    )
    async def test_frozen_strategy_binds_before_descriptor_returns(
        self,
        strategy: DirectModelBindingStrategy,
        current_model: str,
        setter_method: str,
    ) -> None:
        """Every logical session awaits its frozen setter, including the default."""

        client = AcpClient("unused")
        client._models = [ModelInfo("target", "Target")]
        client._direct_model_binding_strategy = strategy
        setter_started = asyncio.Event()
        release_setter = asyncio.Event()

        async def send_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "session/new":
                return {
                    "sessionId": "session",
                    "models": {
                        "availableModels": [
                            {"modelId": "auto", "name": "Auto"},
                            {"modelId": "target", "name": "Target"},
                        ],
                        "currentModelId": current_model,
                    },
                }
            setter_started.set()
            await release_setter.wait()
            if method == "session/set_config_option":
                return {
                    "configOptions": [{"id": "model", "currentValue": "target"}]
                }
            return {}

        client._transport = AsyncMock()
        client._transport.send_request = AsyncMock(side_effect=send_request)

        task = asyncio.create_task(client.create_session_exact("/workspace", "target"))
        await setter_started.wait()
        assert not task.done()
        assert client._sessions["session"].model_id == current_model

        release_setter.set()
        descriptor = await task

        assert descriptor.session_id == "session"
        assert descriptor.model_id == "target"
        assert client._sessions["session"].model_id == "target"
        setter_params = (
            {"sessionId": "session", "configId": "model", "value": "target"}
            if strategy is DirectModelBindingStrategy.STANDARD_CONFIG
            else {"sessionId": "session", "modelId": "target"}
        )
        assert client._transport.send_request.await_args_list == [
            call("session/new", {"cwd": "/workspace", "mcpServers": []}),
            call(setter_method, setter_params),
        ]
        assert client._model_bindings == {}

    @pytest.mark.asyncio
    async def test_exact_session_requires_negotiated_strategy_before_new(self) -> None:
        client = AcpClient("unused")
        client._models = [ModelInfo("target", "Target")]
        client._transport = AsyncMock()

        with pytest.raises(ModelAcknowledgementError, match="not negotiated"):
            await client.create_session_exact("/workspace", "target")

        client._transport.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "session_models",
        [
            None,
            {
                "availableModels": [{"modelId": "auto", "name": "Auto"}],
                "currentModelId": "",
            },
            {
                "availableModels": [],
                "currentModelId": "auto",
            },
        ],
    )
    async def test_exact_session_requires_its_own_consistent_model_catalog(
        self,
        session_models: dict[str, Any] | None,
    ) -> None:
        """A logical session never inherits the catalog probe's default."""
        client = AcpClient("unused")
        client._models = [ModelInfo("auto", "Auto")]
        client._default_model = "auto"
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.COPILOT_SET_MODEL
        )
        client._transport = AsyncMock()
        response: dict[str, Any] = {"sessionId": "session"}
        if session_models is not None:
            response["models"] = session_models
        client._transport.send_request.return_value = response

        with pytest.raises(ModelAcknowledgementError, match="per-session model catalog"):
            await client.create_session_exact("/workspace", "auto")

        client._transport.send_request.assert_awaited_once_with(
            "session/new",
            {"cwd": "/workspace", "mcpServers": []},
        )
        assert client._sessions["session"].model_id in {None, "auto"}

    @pytest.mark.asyncio
    async def test_exact_session_revalidates_requested_model_catalog(self) -> None:
        """A model removed from the new session's catalog is not selected."""
        client = AcpClient("unused")
        client._models = [ModelInfo("target", "Target")]
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.COPILOT_SET_MODEL
        )
        client._transport = AsyncMock()
        client._transport.send_request.return_value = {
            "sessionId": "session",
            "models": {
                "availableModels": [{"modelId": "auto", "name": "Auto"}],
                "currentModelId": "auto",
            },
        }

        with pytest.raises(ModelAcknowledgementError, match="did not advertise"):
            await client.create_session_exact("/workspace", "target")

        client._transport.send_request.assert_awaited_once()
        assert client._sessions["session"].model_id == "auto"

    @pytest.mark.asyncio
    async def test_rejected_session_cannot_replace_startup_model_catalog(self) -> None:
        """Per-session divergence leaves generation capabilities immutable."""
        client = AcpClient("unused")
        client._transport = AsyncMock()
        client._transport.send_request.side_effect = [
            {
                "sessionId": "catalog",
                "models": {
                    "availableModels": [
                        {"modelId": "auto", "name": "Auto"},
                        {"modelId": "target", "name": "Target"},
                    ],
                    "currentModelId": "auto",
                },
            },
            {
                "sessionId": "logical",
                "models": {
                    "availableModels": [{"modelId": "auto", "name": "Auto"}],
                    "currentModelId": "auto",
                },
            },
        ]
        await client.create_session("/workspace")
        startup_models = [model.model_id for model in client.models]
        startup_default = client.default_model
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.COPILOT_SET_MODEL
        )

        with pytest.raises(ModelAcknowledgementError, match="did not advertise"):
            await client.create_session_exact("/workspace", "target")

        assert [model.model_id for model in client.models] == startup_models
        assert client.default_model == startup_default

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("strategy", "setter_method"),
        [
            (
                DirectModelBindingStrategy.STANDARD_CONFIG,
                "session/set_config_option",
            ),
            (
                DirectModelBindingStrategy.COPILOT_SET_MODEL,
                "session/set_model",
            ),
        ],
    )
    async def test_frozen_strategy_never_renegotiates_on_method_not_found(
        self,
        strategy: DirectModelBindingStrategy,
        setter_method: str,
    ) -> None:
        """A post-readiness method loss fails instead of switching strategies."""
        from meadow_bridge.transport import AcpError

        client = AcpClient("unused")
        client._models = [ModelInfo("gpt-5.3-codex", "GPT-5.3 Codex")]
        client._direct_model_binding_strategy = strategy
        client._transport = AsyncMock()
        client._transport.send_request.side_effect = [
            {
                "sessionId": "session",
                "models": {
                    "availableModels": [
                        {"modelId": "auto", "name": "Auto"},
                        {"modelId": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
                    ],
                    "currentModelId": "auto",
                },
            },
            AcpError(
                f'"Method not found": {setter_method}',
                {"code": -32601, "data": "sensitive child output"},
            ),
        ]

        with pytest.raises(ModelAcknowledgementError) as exc_info:
            await client.create_session_exact("/workspace", "gpt-5.3-codex")

        message = str(exc_info.value)
        assert "rejected the requested session model" in message
        assert "Method not found" not in message
        assert "sensitive child output" not in message
        assert client._sessions["session"].model_id == "auto"
        expected_params = (
            {
                "sessionId": "session",
                "configId": "model",
                "value": "gpt-5.3-codex",
            }
            if strategy is DirectModelBindingStrategy.STANDARD_CONFIG
            else {"sessionId": "session", "modelId": "gpt-5.3-codex"}
        )
        assert client._transport.send_request.await_args_list == [
            call("session/new", {"cwd": "/workspace", "mcpServers": []}),
            call(setter_method, expected_params),
        ]
        assert client.direct_model_binding_strategy is strategy

    @pytest.mark.parametrize(
        "result",
        [
            {},
            {"configOptions": []},
            {"configOptions": [{"id": "model"}]},
        ],
    )
    def test_config_fallback_rejects_incomplete_config_options(
        self, result: dict[str, Any]
    ) -> None:
        with pytest.raises(ModelAcknowledgementError, match="configOptions|model"):
            AcpClient._model_from_config_options(result)

    @pytest.mark.asyncio
    async def test_standard_strategy_rejects_wrong_current_model(self) -> None:
        client = AcpClient("unused")
        requested = "MODEL_TEXT_CANARY_REQUESTED"
        observed = "MODEL_TEXT_CANARY_OBSERVED"
        client._sessions = {"session": SessionState("session", model_id="auto")}
        client._model_bindings = {}
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.STANDARD_CONFIG
        )
        client._transport = AsyncMock()
        client._transport.send_request.return_value = {
            "configOptions": [{"id": "model", "currentValue": observed}]
        }

        with pytest.raises(ModelAcknowledgementError) as exc_info:
            await client._bind_direct_model("session", requested)
        assert requested not in str(exc_info.value)
        assert observed not in str(exc_info.value)
        assert client._sessions["session"].model_id is None
        client._transport.send_request.assert_awaited_once_with(
            "session/set_config_option",
            {"sessionId": "session", "configId": "model", "value": requested},
        )

    @pytest.mark.asyncio
    async def test_rejected_nondefault_binding_retains_observed_current_model(self) -> None:
        """A rejected setter cannot make the requested model appear bound."""
        from meadow_bridge.transport import AcpError

        client = AcpClient("unused")
        client._models = [ModelInfo("gpt-5.3-codex", "GPT-5.3 Codex")]
        client._direct_model_binding_strategy = (
            DirectModelBindingStrategy.COPILOT_SET_MODEL
        )
        client._transport = AsyncMock()
        client._transport.send_request.side_effect = [
            {
                "sessionId": "session",
                "models": {
                    "availableModels": [
                        {"modelId": "auto", "name": "Auto"},
                        {"modelId": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
                    ],
                    "currentModelId": "auto",
                },
            },
            AcpError("selection rejected", {"code": -32000}),
        ]

        with pytest.raises(ModelAcknowledgementError, match="rejected"):
            await client.create_session_exact("/workspace", "gpt-5.3-codex")
        assert client._sessions["session"].model_id == "auto"
        assert len(client._transport.send_request.await_args_list) == 2

    @pytest.mark.asyncio
    async def test_cancel_is_stable_session_notification(self) -> None:
        """ADI-10: cancellation reaches ACP and is not local-task-only."""
        client = AcpClient("unused")
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

        client = AcpClient("unused")
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue(maxsize=1)
        queue.put_nowait({"sessionUpdate": "agent_message_chunk"})
        client._update_queues = {"session": queue}
        client._direct_prompt_phases = {"session": "active"}
        client._direct_update_budgets = {"session": {}}
        client._model_bindings = {}
        client._provisional_session_ids = {"provisional-session"}
        client._session_new_response_ids = {"response-session"}
        client._sessions = {"session": SessionState("session")}
        client._transport = AsyncMock()

        await getattr(client, method)()

        getattr(client._transport, method).assert_awaited_once()
        assert queue.get_nowait() is None
        assert client._provisional_session_ids == set()
        assert client._session_new_response_ids == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("result", [{}, {"stopReason": ""}, {"stopReason": "novel"}])
    async def test_direct_prompt_requires_known_explicit_stop_reason(
        self, result: dict[str, Any]
    ) -> None:
        """ADI-08/10: direct terminal state is never synthesized or unknown."""
        client = AcpClient("unused")
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
        client = AcpClient("unused")
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
        client = AcpClient("unused")
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


# ---------------------------------------------------------------------------
# Prompt timeout (prompt-level deadline enforcement)
# ---------------------------------------------------------------------------


class TestPromptTimeout:
    """Prompt-level timeout enforces a deadline on session/prompt.

    The prompt_blocks() method must raise PromptTimeout if the ACP server does
    not complete within the configured deadline.  This prevents a hung
    language server from blocking the HTTP connection indefinitely.
    """

    @pytest.mark.asyncio
    async def test_timeout_raises_prompt_timeout(self) -> None:
        """A prompt that exceeds the deadline raises PromptTimeout."""
        from meadow_bridge.client import PromptTimeout

        client = AcpClient("unused")
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        # Transport that never responds — simulates a hung server
        async def never_respond(method: str, params: dict[str, object]) -> dict[str, object]:
            await asyncio.sleep(999)
            raise AssertionError("unresponsive transport unexpectedly completed")

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout) as exc_info:
            async for _ in client.prompt_blocks(
                "s1",
                [{"type": "text", "text": "hello"}],
                timeout_s=0.2,
            ):
                pass

        assert exc_info.value.session_id == "s1"
        assert exc_info.value.timeout_s == 0.2

    @pytest.mark.asyncio
    async def test_timeout_includes_partial_text(self) -> None:
        """Partial text collected before the timeout is preserved in the exception."""
        from meadow_bridge.client import PromptTimeout

        client = AcpClient("unused")
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def slow_respond(method: str, params: dict[str, object]) -> dict[str, object]:
            # Wait long enough that chunks are delivered, then hang
            await asyncio.sleep(999)
            raise AssertionError("unresponsive transport unexpectedly completed")

        transport = MagicMock()
        transport.send_request = slow_respond
        client._transport = transport

        async def push_chunks() -> None:
            """Push chunks into the queue shortly after it's created."""
            # Wait for prompt_blocks() to create the queue
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
            async for _ in client.prompt_blocks(
                "s1",
                [{"type": "text", "text": "hello"}],
                timeout_s=0.5,
            ):
                pass

        await push_task
        assert exc_info.value.partial_text == "partial response"

    @pytest.mark.asyncio
    async def test_normal_completion_within_timeout(self) -> None:
        """A prompt that completes before the deadline works normally."""
        client = AcpClient("unused")
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def fast_respond(method: str, params: dict[str, object]) -> dict[str, object]:
            # Respond quickly
            await asyncio.sleep(0.05)
            return {"stopReason": "end_turn"}

        transport = MagicMock()
        transport.send_request = fast_respond
        transport.on_notification = MagicMock()
        transport.on_request = MagicMock()
        client._transport = transport

        # Push an update and then let the prompt task complete
        async def push_update() -> None:
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
        async for update in client.prompt_blocks(
            "s1",
            [{"type": "text", "text": "hi"}],
            timeout_s=5.0,
        ):
            results.append(update)

        # Should have the chunk + done sentinel
        assert any(r.get("done") for r in results)

    @pytest.mark.asyncio
    async def test_unknown_session_raises_value_error(self) -> None:
        """Prompting an unknown session raises ValueError, not timeout."""
        client = AcpClient("unused")
        client._sessions = {}
        client._update_queues = {}

        with pytest.raises(ValueError, match="Unknown session"):
            async for _ in client.prompt_blocks(
                "nonexistent",
                [{"type": "text", "text": "hello"}],
            ):
                pass

    @pytest.mark.asyncio
    async def test_queue_cleanup_after_timeout(self) -> None:
        """The update queue is removed after a timeout to prevent leaks."""
        from meadow_bridge.client import PromptTimeout

        client = AcpClient("unused")
        client._sessions = {"s1": SessionState(session_id="s1")}
        client._update_queues = {}

        async def never_respond(method: str, params: dict[str, object]) -> dict[str, object]:
            await asyncio.sleep(999)
            raise AssertionError("unresponsive transport unexpectedly completed")

        transport = MagicMock()
        transport.send_request = never_respond
        client._transport = transport

        with pytest.raises(PromptTimeout):
            async for _ in client.prompt_blocks(
                "s1",
                [{"type": "text", "text": "hello"}],
                timeout_s=0.1,
            ):
                pass

        # Queue should be cleaned up
        assert "s1" not in client._update_queues
