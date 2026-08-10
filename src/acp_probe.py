#!/usr/bin/env python3
"""
Probe the copilot-language-server ACP interface.

Keeps the subprocess alive and sends a sequence of JSON-RPC messages
to understand the auth + session flow.

Usage:
    python tmp/acp_probe.py
"""

import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from acp_proxy.discovery import find_binary


def read_ndjson(stream, label="stdout"):
    """Read NDJSON lines from a stream and print them."""
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            print(f"\n<<< [{label}] {json.dumps(msg, indent=2)}")
        except json.JSONDecodeError:
            print(f"\n<<< [{label}] (raw) {line}")


def send(proc, msg):
    """Send a JSON-RPC message to the process stdin."""
    payload = json.dumps(msg)
    print(f"\n>>> {json.dumps(msg, indent=2)}")
    proc.stdin.write(payload + "\n")
    proc.stdin.flush()


def main():
    cls_path = find_binary()
    if not cls_path:
        print("ERROR: No compatible copilot-language-server binary found.")
        print("No named candidate reported the minimum language-server version.")
        sys.exit(1)

    print(f"Starting copilot-language-server in ACP mode...")
    print(f"Binary: {cls_path}")
    print()

    proc = subprocess.Popen(
        [cls_path, "--acp", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,  # line-buffered
    )

    # Background readers for stdout and stderr
    t_out = threading.Thread(
        target=read_ndjson, args=(proc.stdout, "stdout"), daemon=True
    )
    t_err = threading.Thread(
        target=read_ndjson, args=(proc.stderr, "stderr"), daemon=True
    )
    t_out.start()
    t_err.start()

    time.sleep(1)  # let process start

    # Step 1: Initialize
    print("=" * 60)
    print("STEP 1: Initialize")
    print("=" * 60)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientInfo": {"name": "meadow", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        },
    )
    time.sleep(2)

    # Step 2: Try session/new WITHOUT authenticating
    print("\n" + "=" * 60)
    print("STEP 2: session/new (without auth — expect auth_required)")
    print("=" * 60)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/new",
            "params": {
                "cwd": os.getcwd(),
                "mcpServers": [],
            },
        },
    )
    time.sleep(3)

    # Step 3: Authenticate
    print("\n" + "=" * 60)
    print("STEP 3: authenticate with github_oauth")
    print("=" * 60)
    send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "authenticate",
            "params": {
                "methodId": "github_oauth",
            },
        },
    )

    # Wait longer — the server may open a browser or return a device code
    print("\nWaiting for auth response (up to 60s)...")
    print("If a browser opens, complete the auth flow there.")
    time.sleep(60)

    # Cleanup
    proc.terminate()
    proc.wait(timeout=5)
    print("\nDone.")


if __name__ == "__main__":
    main()
