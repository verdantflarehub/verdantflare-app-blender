"""A worker-owned GUI lease fences direct WebRTC input against RPC writers."""
import os
from pathlib import Path
import subprocess
import threading
import time


class LeaseError(Exception):
    pass


def supervise(action):
    command = ["/usr/bin/supervisorctl", "-c", "/opt/beagle/blender-mcp/supervisord-blender.conf"]
    subprocess.run(command + [action, "start-webrtc"], capture_output=True, timeout=12)
    state = subprocess.run(command + ["status", "start-webrtc"], capture_output=True, timeout=3).stdout.decode(errors="replace").split()
    accepted = {"RUNNING"} if action == "start" else {"STOPPED", "EXITED"}
    if len(state) < 2 or state[1] not in accepted:
        raise LeaseError("GUI_PROCESS_UNAVAILABLE")


class GUILease:
    def __init__(self, run=supervise, clock=time.monotonic, ttl_seconds=20, input_stamp_file=None):
        self.run, self.clock = run, clock
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.input_stamp_file = Path(
            input_stamp_file or os.environ.get("BLENDER_GUI_INPUT_STAMP_FILE", "/run/user/1000/blender-gui-last-input")
        )
        self.lock = threading.RLock()
        self.session, self.expires = None, 0
        self.opened_at, self.last_input_at = 0, 0
        self.deadline = float('inf')

    def expire(self):
        # Caller holds lock; a failed stop keeps writers fenced.
        if self.session and self.clock() >= min(self.expires, self.deadline):
            self.run("stop")
            self.session = None

    def _read_stamp_monotonic(self):
        if not self.input_stamp_file.is_file() or self.input_stamp_file.is_symlink():
            return None
        try:
            raw = self.input_stamp_file.read_text(encoding="utf-8", errors="replace").strip()
            val = float(raw)
            now = self.clock()
            if 0 < val <= now + 1.0:
                return min(val, now)
        except (OSError, ValueError):
            return None
        return None

    def record_input(self):
        with self.lock:
            self.last_input_at = self.clock()

    def input_idle_seconds(self):
        # Observation must never expire or close the GUI lease.
        with self.lock:
            if not self.session:
                return 0
            effective_last = self.last_input_at
            stamped = self._read_stamp_monotonic()
            if stamped is not None and stamped > effective_last:
                effective_last = stamped
            return max(0, int(self.clock() - effective_last))

    def action(self, action, session):
        with self.lock:
            self.expire()
            if action == "open":
                if self.session and self.session != session:
                    raise LeaseError("GUI_LEASE_HELD")
                now = self.clock()
                if not self.session:
                    # Mark held BEFORE starting: uncertain start failure is fenced.
                    self.session, self.expires = session, now
                    self.opened_at = self.last_input_at = now
                    self.run("start")
                self.expires = now + self.ttl_seconds
            elif action == "heartbeat":
                if self.session != session:
                    raise LeaseError("GUI_LEASE_EXPIRED")
                self.expires = self.clock() + self.ttl_seconds
            elif action == "close":
                if self.session and self.session != session:
                    raise LeaseError("GUI_LEASE_MISMATCH")
                self.run("stop")
                self.session = None
            else:
                raise LeaseError("INVALID_GUI_ACTION")

    def guard_write(self):
        self.expire()
        if self.session:
            raise LeaseError("GUI_EDIT_LEASE_HELD")

    def watchdog(self):
        while True:
            time.sleep(1)
            try:
                with self.lock:
                    self.expire()
            except (LeaseError, OSError, subprocess.TimeoutExpired):
                pass  # Keep fencing; retry stop on the next tick.
