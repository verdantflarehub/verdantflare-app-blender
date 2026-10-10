"""A worker-owned GUI lease fences direct WebRTC input against RPC writers."""
import subprocess
import threading
import time


class LeaseError(Exception):
    pass


def supervise(action):
    command = ["/usr/bin/supervisorctl", "-c", "/opt/beagle/blender-mcp/supervisord-blender.conf"]
    result = subprocess.run(command + [action, "start-webrtc"], capture_output=True, timeout=12)
    state = subprocess.run(command + ["status", "start-webrtc"], capture_output=True, timeout=3).stdout.decode(errors="replace").split()
    accepted = {"RUNNING"} if action == "start" else {"STOPPED", "EXITED"}
    if len(state) < 2 or state[1] not in accepted:
        raise LeaseError("GUI_PROCESS_UNAVAILABLE")


class GUILease:
    def __init__(self, run=supervise, clock=time.monotonic):
        self.run, self.clock = run, clock
        self.lock = threading.RLock()
        self.session, self.expires = None, 0
        self.deadline = float('inf')

    def expire(self):
        # Caller holds lock; a failed stop keeps writers fenced.
        if self.session and self.clock() >= min(self.expires, self.deadline):
            self.run("stop")
            self.session = None

    def action(self, action, session):
        with self.lock:
            self.expire()
            if action == "open":
                if self.session and self.session != session:
                    raise LeaseError("GUI_LEASE_HELD")
                if not self.session:
                    # Mark held BEFORE starting: uncertain start failure is fenced.
                    self.session, self.expires = session, self.clock()
                    self.run("start")
                self.expires = self.clock() + 20
            elif action == "heartbeat":
                if self.session != session:
                    raise LeaseError("GUI_LEASE_EXPIRED")
                self.expires = self.clock() + 20
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
