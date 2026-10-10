#!/bin/bash
set -euo pipefail

. /etc/beagle-wind-vnc/runtime-env.sh

: "${INSTANCE_ID:?INSTANCE_ID is required}"
: "${WORKSPACE_ROOT:=/workspace}"
: "${BLENDER_MCP_TOKEN_FILE:=/run/user/1000/blender-mcp-token}"

if [ -n "${BLENDER_MCP_SECRET_FILE:-}" ]; then
    mkdir -p "$(dirname "${BLENDER_MCP_TOKEN_FILE}")"
    install -m600 "${BLENDER_MCP_SECRET_FILE}" "${BLENDER_MCP_TOKEN_FILE}"
fi

: "${BDWIND_PASSWORD:?inject an instance-specific GUI credential}"

# Select only the render device injected with the assigned physical GPU. Never
# bind the host's complete /dev/dri directory into an application instance.
assigned_gpu="$(nvidia-smi --query-gpu=uuid --format=csv,noheader)"
if [[ ! "${assigned_gpu}" =~ ^GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "[blender] exactly one assigned GPU is required" >&2
    exit 78
fi
export BLENDER_GPU_UUID="${assigned_gpu}"
render_nodes=()
for candidate in /dev/dri/renderD*; do
    if [ -c "${candidate}" ] && [ ! -L "${candidate}" ]; then
        render_nodes+=("${candidate}")
    fi
done
if [ "${#render_nodes[@]}" -ne 1 ]; then
    echo "[blender] expected one injected DRM render device" >&2
    exit 78
fi
export BDWIND_SMITHAY_RENDER_NODE="${render_nodes[0]}"

# Reuse the KDE6 base entrypoint's device, DBus, runtime-directory and
# machine-id preparation, but pass a no-op command so it does not start the
# generic desktop command.  The Blender supervisor below owns this instance.
/etc/beagle-wind-vnc/entrypoint.sh true

mkdir -p "${XDG_RUNTIME_DIR}" "${WORKSPACE_ROOT}"/{project,inbox,exports,checkpoints,cache}
chmod 700 "${XDG_RUNTIME_DIR}" "${WORKSPACE_ROOT}"/{project,inbox,exports,checkpoints,cache}

# A container restart can leave the adapter socket pathname behind while
# Blender is still starting.  Remove it so /readyz cannot report a stale
# pathname as ready.
rm -f -- "${BLENDER_MCP_ADAPTER_SOCKET}"

if [ ! -s "${BLENDER_MCP_TOKEN_FILE}" ]; then
    if [ "${BLENDER_MCP_DEV_GENERATE_TOKEN:-false}" != "true" ]; then
        echo "[blender-mcp] token file is missing; inject a Secret or set BLENDER_MCP_DEV_GENERATE_TOKEN=true for local smoke tests" >&2
        exit 78
    fi
    umask 077
    mkdir -p "$(dirname "${BLENDER_MCP_TOKEN_FILE}")"
    python3 - <<'PY' >"${BLENDER_MCP_TOKEN_FILE}"
import secrets
print(secrets.token_urlsafe(32))
PY
fi
chmod 600 "${BLENDER_MCP_TOKEN_FILE}"

test -r /etc/beagle-wind-vnc/kde6-blender-mcp.lock
test -x /usr/local/bin/blender
test -f /opt/beagle/blender-mcp/mcp-bridge/bridge.py
test -f /opt/beagle/blender-mcp/blender-mcp-plugin/register.py

exec "$@"
