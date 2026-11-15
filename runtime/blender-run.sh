#!/bin/bash
set -euo pipefail

. /etc/beagle-wind-vnc/runtime-env.sh

if [ "${BLENDER_SUBREAPER_MANAGED:-0}" = "1" ]; then
    exit 0
fi

wait_for_display() {
    until [ -S "${XDG_RUNTIME_DIR}/bus" ] && \
          [ -r "${BDWIND_SMITHAY_READY_FILE}" ] && \
          [ -S "${BDWIND_SMITHAY_CONTROL_SOCKET}" ] && \
          [ -S "${XDG_RUNTIME_DIR}/${WAYLAND_DISPLAY}" ] && \
          kscreen-doctor -o >/dev/null 2>&1; do
        sleep 0.2
    done
}

cleanup_epoch_shm() {
    local epoch="$1"
    local epoch_dir="/dev/shm/vf-blender-epoch-${epoch}"
    local manifest="/dev/shm/.vf-blender-manifest-epoch-${epoch}.json"
    rm -rf -- "${epoch_dir}" 2>/dev/null || true
    if [ -f "${manifest}" ]; then
        python3 - "${manifest}" <<'PY' || true
import json, pathlib, sys
mf = pathlib.Path(sys.argv[1])
try:
    items = json.loads(mf.read_text(encoding="utf-8"))
except Exception:
    items = []
if isinstance(items, list):
    for name in items:
        if isinstance(name, str) and name and "/" not in name and name not in (".", ".."):
            target = pathlib.Path("/dev/shm") / name
            try:
                if target.is_file() or target.is_symlink():
                    target.unlink(missing_ok=True)
            except OSError:
                pass
mf.unlink(missing_ok=True)
PY
    fi
}

SENTINEL="${BLENDER_HIBERNATE_SENTINEL:-/tmp/blender-hibernate.sentinel}"
PID_STATE="${BLENDER_PROCESS_STATE_FILE:-/tmp/blender-process-state.json}"
gpu_epoch="${BLENDER_GPU_EPOCH:-0}"

while true; do
    gpu_epoch=$((gpu_epoch + 1))
    export BLENDER_GPU_EPOCH="${gpu_epoch}"
    export BLENDER_SHM_EPOCH_DIR="/dev/shm/vf-blender-epoch-${gpu_epoch}"
    export BLENDER_SHM_MANIFEST_FILE="/dev/shm/.vf-blender-manifest-epoch-${gpu_epoch}.json"
    mkdir -p -- "${BLENDER_SHM_EPOCH_DIR}"
    export TMPDIR="${BLENDER_SHM_EPOCH_DIR}"

    result=1
    for _ in $(seq 1 30); do
        rm -f -- "${BLENDER_MCP_ADAPTER_SOCKET}"
        wait_for_display

        set +e
        blender --disable-autoexec --factory-startup --python /opt/beagle/blender-mcp/blender-startup.py &
        blender_pid=$!
        printf '{"pid":%d,"gpu_epoch":%d}\n' "${blender_pid}" "${gpu_epoch}" > "${PID_STATE}.tmp" && mv -f "${PID_STATE}.tmp" "${PID_STATE}"
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

    rm -f -- "${BLENDER_MCP_ADAPTER_SOCKET}" "${PID_STATE}"
    cleanup_epoch_shm "${gpu_epoch}"

    if [ -f "${SENTINEL}" ]; then
        while [ -f "${SENTINEL}" ]; do
            sleep 0.2
        done
        export BLENDER_WAKE_RESTORE=1
        continue
    fi

    supervisorctl -s unix:///tmp/supervisor.sock shutdown >/dev/null 2>&1 || true
    exit "${result}"
done
