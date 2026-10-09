# Blender MCP P0 runtime

This directory contains the first executable slice of the Blender MCP design:

```text
MCP Streamable HTTP → mcp-bridge/bridge.py
                       → Unix JSON-lines socket
                       → blender-mcp-plugin (Blender main thread)
```

The bridge exposes the L0/L1 scene tools plus bounded GLB/glTF import/export.
Import and export use a single worker queue and are polled with `job.get` or
stopped while queued with `job.cancel`. The addon queues every Blender
operation and executes it from a Blender timer; the socket worker never
accesses `bpy`. `file-agent/workspace.py` centralizes workspace path and
SHA-256 checks.

Run the bridge locally with:

```bash
INSTANCE_ID=dev-instance \
BLENDER_MCP_TOKEN=dev-token \
BLENDER_MCP_ADAPTER_SOCKET=/tmp/blender-mcp-adapter.sock \
python3 mcp-bridge/bridge.py --host 127.0.0.1 --port 48084
```

The bridge intentionally reports `503` on `/readyz` until Blender has loaded
the addon and created the adapter socket. It does not create a fallback mock
adapter, download plugins at runtime, or accept arbitrary Blender paths.
