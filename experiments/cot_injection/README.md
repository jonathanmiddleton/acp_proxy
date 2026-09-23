# Historical CoT injection experiments

This directory preserves experiments against ACP Proxy's removed
OpenAI-compatible `/v1` adapter. Its scripts, configurations and recorded
results are historical evidence, not supported Meadow Bridge usage.

The [original experiment description and run instructions](historical/README.md)
are preserved byte-for-byte from the pre-removal repository. Their startup
commands, adapter behavior and result claims describe that historical context.

Meadow Bridge serves the authenticated `/meadow/v1` direct ACP contract. It
has no OpenAI-compatible endpoint or `--system-prompt` startup option. See
[ADR-018](../../adrs/018-remove-openai-compatible-adapter.md) for the removal
decision and retained direct ACP behavior.
