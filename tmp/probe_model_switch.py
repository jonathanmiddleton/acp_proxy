#!/usr/bin/env python3
"""
Systematically probe model switching methods on the copilot-language-server.

Tests every plausible approach to changing the model in an ACP session.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meadow_bridge.transport import AcpTransport, AcpError


async def probe(
    transport: AcpTransport,
    method: str,
    params: dict[str, object],
    label: str | None = None,
) -> dict[str, object] | None:
    """Try a method and report the result."""
    label = label or method
    print(f"\n--- {label} ---")
    print(f"  params: {json.dumps(params)[:200]}")
    try:
        r = await transport.send_request(method, params)
        print("  SUCCESS")
        # Print relevant fields
        if isinstance(r, dict):
            if "models" in r:
                print(f"  currentModelId: {r['models'].get('currentModelId')}")
            if "configOptions" in r:
                for opt in r["configOptions"]:
                    if opt.get("id") == "model" or opt.get("category") == "model":
                        print(f"  model config currentValue: {opt.get('currentValue')}")
            print(f"  response keys: {list(r.keys())}")
            # Print full response if small enough
            dumped = json.dumps(r, indent=2)
            if len(dumped) < 1000:
                print(f"  full response: {dumped}")
            else:
                print(f"  response preview: {dumped[:500]}...")
        return r
    except AcpError as e:
        print(f"  FAILED: {e}")
        if e.error_obj:
            print(f"  error detail: {json.dumps(e.error_obj)}")
        return None


async def main() -> None:
    """Report live model-selection responses from the installed ACP server."""
    binary = sys.argv[1] if len(sys.argv) > 1 else None
    if not binary:
        import subprocess

        out = subprocess.check_output(["ps", "-eo", "command"], text=True)
        for line in out.splitlines():
            if "copilot-language-server" in line and "grep" not in line:
                binary = line.split(" --")[0].strip()
                break
    if not binary:
        print("Pass binary path as argument")
        sys.exit(1)

    print(f"Binary: {binary}\n")

    transport = AcpTransport()
    await transport.start(binary)

    # Initialize
    init_result = await transport.send_request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": {"name": "meadow-bridge-model-probe", "version": "0.1.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        },
    )
    version = init_result.get("agentInfo", {}).get("version")
    print(f"Agent: {init_result.get('agentInfo', {}).get('name')} v{version}")
    print(f"Capabilities: {json.dumps(init_result.get('agentCapabilities', {}))}")

    # Create initial session
    session_result = await transport.send_request(
        "session/new",
        {
            "cwd": os.getcwd(),
            "mcpServers": [],
        },
    )
    sid = session_result["sessionId"]
    cur_model = session_result.get("models", {}).get("currentModelId")
    models = session_result.get("models", {}).get("availableModels", [])
    model_ids = [m["modelId"] for m in models]
    modes = session_result.get("modes", {}).get("availableModes", [])
    mode_ids = [m["id"] for m in modes]
    config_options = session_result.get("configOptions", [])

    print(f"\nSession: {sid}")
    print(f"Current model: {cur_model}")
    print(f"Available models: {model_ids}")
    print(f"Available modes: {mode_ids}")
    print(f"Config options: {json.dumps(config_options, indent=2)}")

    # Pick target model
    other = None
    for m in model_ids:
        if m != cur_model:
            other = m
            break
    if not other:
        print("\nOnly one model, can't test switching")
        await transport.stop()
        return

    print(f"\n{'=' * 60}")
    print(f"Target: switch from '{cur_model}' to '{other}'")
    print(f"{'=' * 60}")

    # ========================================
    # BLOCK 1: session/set_config_option variants
    # ========================================
    print(f"\n\n{'=' * 60}")
    print("BLOCK 1: session/set_config_option variants")
    print(f"{'=' * 60}")

    await probe(
        transport,
        "session/set_config_option",
        {
            "sessionId": sid,
            "configId": "model",
            "value": other,
        },
        "set_config_option (standard)",
    )

    await probe(
        transport,
        "session/setConfigOption",
        {
            "sessionId": sid,
            "configId": "model",
            "value": other,
        },
        "setConfigOption (camelCase)",
    )

    await probe(
        transport,
        "session/set_config",
        {
            "sessionId": sid,
            "configId": "model",
            "value": other,
        },
        "set_config",
    )

    await probe(
        transport,
        "session/config",
        {
            "sessionId": sid,
            "model": other,
        },
        "session/config",
    )

    # ========================================
    # BLOCK 2: session/set_mode with different param shapes
    # ========================================
    print(f"\n\n{'=' * 60}")
    print("BLOCK 2: session/set_mode variants")
    print(f"{'=' * 60}")

    # Try with actual mode IDs from availableModes
    for mode in modes:
        await probe(
            transport,
            "session/set_mode",
            {
                "sessionId": sid,
                "modeId": mode["id"],
            },
            f"set_mode (modeId={mode['id'].split('#')[-1]})",
        )

    # Try set_mode with model as modeId
    await probe(
        transport,
        "session/set_mode",
        {
            "sessionId": sid,
            "modeId": other,
        },
        f"set_mode (modeId=model:{other})",
    )

    # Try set_mode with model field
    await probe(
        transport,
        "session/set_mode",
        {
            "sessionId": sid,
            "modeId": mode_ids[0] if mode_ids else "agent",
            "model": other,
        },
        "set_mode with extra model field",
    )

    # ========================================
    # BLOCK 3: session/update variants (client->agent)
    # ========================================
    print(f"\n\n{'=' * 60}")
    print("BLOCK 3: session/update and configure variants")
    print(f"{'=' * 60}")

    await probe(
        transport,
        "session/configure",
        {
            "sessionId": sid,
            "model": other,
        },
        "session/configure",
    )

    await probe(
        transport,
        "session/update",
        {
            "sessionId": sid,
            "update": {
                "sessionUpdate": "config_option_update",
                "configId": "model",
                "value": other,
            },
        },
        "session/update (config_option_update)",
    )

    await probe(
        transport,
        "session/setModel",
        {
            "sessionId": sid,
            "modelId": other,
        },
        "session/setModel",
    )

    await probe(
        transport,
        "session/set_model",
        {
            "sessionId": sid,
            "modelId": other,
        },
        "session/set_model",
    )

    # ========================================
    # BLOCK 4: new session with model hints
    # ========================================
    print(f"\n\n{'=' * 60}")
    print("BLOCK 4: session/new with model parameters")
    print(f"{'=' * 60}")

    await probe(
        transport,
        "session/new",
        {
            "cwd": os.getcwd(),
            "mcpServers": [],
            "model": other,
        },
        "session/new with model param",
    )

    await probe(
        transport,
        "session/new",
        {
            "cwd": os.getcwd(),
            "mcpServers": [],
            "modelId": other,
        },
        "session/new with modelId param",
    )

    await probe(
        transport,
        "session/new",
        {
            "cwd": os.getcwd(),
            "mcpServers": [],
            "configOptions": [{"configId": "model", "value": other}],
        },
        "session/new with configOptions",
    )

    await probe(
        transport,
        "session/new",
        {
            "cwd": os.getcwd(),
            "mcpServers": [],
            "config": {"model": other},
        },
        "session/new with config.model",
    )

    # ========================================
    # BLOCK 5: Prompt-based model selection
    # ========================================
    print(f"\n\n{'=' * 60}")
    print("BLOCK 5: prompt with model override")
    print(f"{'=' * 60}")

    # Some systems allow per-prompt model override
    await probe(
        transport,
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Reply with exactly: MODEL_TEST"}],
            "model": other,
        },
        "session/prompt with model param",
    )

    await probe(
        transport,
        "session/prompt",
        {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": "Reply with exactly: MODEL_TEST"}],
            "modelId": other,
        },
        "session/prompt with modelId param",
    )

    # Wait a moment for any streaming to complete
    await asyncio.sleep(3)

    await transport.stop()
    print(f"\n{'=' * 60}")
    print("DONE")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
