#!/bin/bash
set -euo pipefail

. /etc/beagle-wind-vnc/runtime-env.sh

wait_for_display() {
    until [ -S "${XDG_RUNTIME_DIR}/bus" ] && \
          [ -r "${BDWIND_SMITHAY_READY_FILE}" ] && \
          [ -S "${BDWIND_SMITHAY_CONTROL_SOCKET}" ] && \
          [ -S "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}" ] && \
          kscreen-doctor -o >/dev/null 2>&1; do
        sleep 0.2
    done
}

result=1
for attempt in $(seq 1 30); do
    rm -f -- "${BLENDER_MCP_ADAPTER_SOCKET}"
    wait_for_display

    set +e
    blender --disable-autoexec --factory-startup --python /opt/beagle/blender-mcp/blender-startup.py &
    blender_pid=$!
    adapter_ready=false
    for _ in $(seq 1 300); do
        if [ -S "${BLENDER_MCP_ADAPTER_SOCKET}" ]; then
            adapter_ready=true
            break
        fi
        if ! kill -0 "${blender_pid}" 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
    if [ "${adapter_ready}" = true ]; then
        wait "${blender_pid}"
        result=$?
        set -e
        break
    fi
    kill -TERM "${blender_pid}" 2>/dev/null || true
    wait "${blender_pid}" 2>/dev/null || true
    result=1
    set -e
    sleep 2
done

# A user closing Blender closes the whole single-application instance.  The
# supervisor is still responsible for ordered process cleanup.
supervisorctl -s unix:///tmp/supervisor.sock shutdown >/dev/null 2>&1 || true
exit "${result}"
