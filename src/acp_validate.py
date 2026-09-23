
"""
Validate ACP integration with copilot-language-server.

Runs through the full lifecycle: init, session creation, prompt, response.
Prints structured results for each step.

Usage:
    python3 acp_validate.py [/path/to/copilot-language-server]

If no path is given, attempts to find the binary via 'ps'.
"""

import json
import logging
import subprocess
import sys
import threading
import time
import os
from collections.abc import Iterable, Mapping

from pydantic import JsonValue


MessageLog = list[tuple[str, object]]
logger = logging.getLogger(__name__)

# Collect all streamed updates here
UPDATES: MessageLog = []


def find_binary() -> str | None:
    """Try to locate a running copilot-language-server process."""
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            if "copilot-language-server" in line and "grep" not in line:
                # Extract the binary path (everything before ' --')
                parts = line.split(" --")
                return parts[0].strip()
    except Exception:
        pass
    return None


def read_ndjson(
    stream: Iterable[str], collected: MessageLog, label: str = "out"
) -> None:
    """Read NDJSON lines from a stream into collected list."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            decoded: object = json.loads(line)
            collected.append((label, decoded))
        except json.JSONDecodeError:
            collected.append((label, {"_raw": line}))


def send(proc: subprocess.Popen[str], msg: Mapping[str, JsonValue]) -> None:
    """Send a JSON-RPC message."""
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def drain(collected: MessageLog, timeout: float = 3.0) -> MessageLog:
    """Wait for messages to arrive, return them, and clear the buffer."""
    time.sleep(timeout)
    result = list(collected)
    collected.clear()
    return result


def find_response(messages: MessageLog, req_id: int) -> dict[str, object] | None:
    """Find the JSON-RPC response matching a request id."""
    for _, msg in messages:
        if isinstance(msg, dict):
            response = _as_object(msg)
            if response.get("id") == req_id:
                return response
    return None


def find_notifications(
    messages: MessageLog, method: str | None = None
) -> list[dict[str, object]]:
    """Find all JSON-RPC notifications (no 'id' field)."""
    results: list[dict[str, object]] = []
    for _, msg in messages:
        if isinstance(msg, dict) and "id" not in msg:
            notification = _as_object(msg)
            if method is None or notification.get("method") == method:
                results.append(notification)
    return results


def _as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        logger.debug("Expected a JSON object, received %r", value)
        raise ValueError(f"Expected a JSON object, received {type(value).__name__}")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            logger.debug("Expected a JSON object with string keys, received %r", value)
            raise ValueError("Expected a JSON object with string keys")
        result[key] = item
    return result


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        logger.debug("Expected a JSON array, received %r", value)
        raise ValueError(f"Expected a JSON array, received {type(value).__name__}")
    return value


def _as_string(value: object) -> str:
    if not isinstance(value, str):
        logger.debug("Expected a JSON string, received %r", value)
        raise ValueError(f"Expected a JSON string, received {type(value).__name__}")
    return value


def main() -> None:
    binary = sys.argv[1] if len(sys.argv) > 1 else find_binary()
    if not binary:
        print("ERROR: Could not find copilot-language-server.")
        print("Pass the path as an argument: python3 acp_validate.py /path/to/binary")
        sys.exit(1)

    print(f"Binary: {binary}")
    print(f"CWD:    {os.getcwd()}")
    print()

    collected: MessageLog = []

    proc = subprocess.Popen(
        [binary, "--acp", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    threading.Thread(
        target=read_ndjson, args=(proc.stdout, collected, "out"), daemon=True
    ).start()
    threading.Thread(
        target=read_ndjson, args=(proc.stderr, collected, "err"), daemon=True
    ).start()

    time.sleep(1)
    results = {}

    # --- Step 1: Initialize ---
    print("=" * 50)
    print("STEP 1: initialize")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "meadow-validate", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        },
    )
    msgs = drain(collected, 2)
    resp = find_response(msgs, 1)
    if resp and "result" in resp:
        r = _as_object(resp["result"])
        info = _as_object(r.get("agentInfo", {}))
        caps = _as_object(r.get("agentCapabilities", {}))
        prompt_caps = _as_object(caps.get("promptCapabilities", {}))
        methods = _as_list(r.get("authMethods", []))
        print(f"  Agent:       {info.get('name')} v{info.get('version')}")
        print(f"  Protocol:    {r.get('protocolVersion')}")
        print(
            f"  Capabilities: loadSession={caps.get('loadSession')}, "
            f"image={prompt_caps.get('image')}, "
            f"embeddedContext={prompt_caps.get('embeddedContext')}"
        )
        print(f"  Signin methods: {[_as_object(m).get('id') for m in methods]}")
        results["init"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["init"] = "FAIL"
    else:
        print(f"  No response. Raw: {msgs}")
        results["init"] = "FAIL"
    print()

    if results.get("init") != "OK":
        print("Cannot continue without init. Exiting.")
        proc.terminate()
        sys.exit(1)

    # --- Step 2: session/new ---
    print("=" * 50)
    print("STEP 2: session/new")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {"cwd": os.getcwd(), "mcpServers": []},
        },
    )
    msgs = drain(collected, 3)
    resp = find_response(msgs, 2)
    session_id = None
    if resp and "result" in resp:
        r = _as_object(resp["result"])
        session_id = r.get("sessionId")
        model_state = _as_object(r.get("models", {}))
        models = _as_list(model_state.get("availableModels", []))
        current = model_state.get("currentModelId")
        modes = _as_list(_as_object(r.get("modes", {})).get("availableModes", []))
        print(f"  Session ID:  {session_id}")
        print(
            f"  Models ({len(models)}): {[_as_object(m)['modelId'] for m in models[:5]]}{'...' if len(models) > 5 else ''}"
        )
        print(f"  Default model: {current}")
        print(f"  Modes:       {[_as_string(_as_object(m)['id']).split('#')[-1] for m in modes]}")
        results["session"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["session"] = "FAIL"
    else:
        print(f"  No response. Raw: {msgs}")
        results["session"] = "FAIL"
    print()

    if not session_id:
        print("No session. Exiting.")
        proc.terminate()
        sys.exit(1)
    session_id = _as_string(session_id)

    # --- Step 3: session/prompt ---
    print("=" * 50)
    print("STEP 3: session/prompt")
    print("=" * 50)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": "Reply with exactly: MEADOW_ACP_OK"}
                ],
            },
        },
    )
    # Streaming — give it more time
    msgs = drain(collected, 15)
    resp = find_response(msgs, 3)
    notifs = find_notifications(msgs, "session/update")

    agent_text = ""
    thought_text = ""
    update_types = set()
    for n in notifs:
        u = _as_object(_as_object(n.get("params", {})).get("update", {}))
        kind = _as_string(u.get("sessionUpdate", ""))
        update_types.add(kind)
        if kind == "agent_message_chunk":
            content = _as_object(u.get("content", {}))
            if content.get("type") == "text":
                agent_text += _as_string(content.get("text", ""))
        elif kind == "agent_thought_chunk":
            content = _as_object(u.get("content", {}))
            if content.get("type") == "text":
                thought_text += _as_string(content.get("text", ""))

    print(f"  Updates received: {len(notifs)}")
    print(f"  Update types:     {sorted(update_types)}")
    if thought_text:
        preview = thought_text[:200].replace("\n", " ")
        print(
            f"  Thought preview:  {preview}{'...' if len(thought_text) > 200 else ''}"
        )
    print(f"  Agent response:   {agent_text[:500]}")
    if resp and "result" in resp:
        print(f"  Stop reason:      {_as_object(resp['result']).get('stopReason')}")
        results["prompt"] = "OK"
    elif resp and "error" in resp:
        print(f"  ERROR: {resp['error']}")
        results["prompt"] = "FAIL"
    else:
        print("  No final response yet (may need more time)")
        results["prompt"] = "PARTIAL"
    print()

    # --- Summary ---
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)
    for step, status in results.items():
        print(f"  {step:10s} {status}")
    print()

    all_ok = all(v == "OK" for v in results.values())
    if all_ok:
        print("All steps passed. ACP integration is viable.")
    else:
        print("Some steps failed. See details above.")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


if __name__ == "__main__":
    main()
