"""Unit and integration tests for Blender P0 zero-VRAM residency, hibernate, wake, and H1-H6/M1-M3 guards."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).parents[1]

app_spec = importlib.util.spec_from_file_location("blender_residency_test_app", ROOT / "app/server.py")
app_mod = importlib.util.module_from_spec(app_spec)
sys.modules[app_spec.name] = app_mod
app_spec.loader.exec_module(app_mod)

ctrl_spec = importlib.util.spec_from_file_location("blender_residency_test_ctrl", ROOT / "runtime/instance-control/server.py")
ctrl_mod = importlib.util.module_from_spec(ctrl_spec)
sys.modules[ctrl_spec.name] = ctrl_mod
ctrl_spec.loader.exec_module(ctrl_mod)

startup_spec = importlib.util.spec_from_file_location("blender_startup_workspace", ROOT / "runtime/startup_workspace.py")
startup_mod = importlib.util.module_from_spec(startup_spec)
sys.modules[startup_spec.name] = startup_mod
startup_spec.loader.exec_module(startup_mod)

boot_gate_spec = importlib.util.spec_from_file_location("blender_startup_gate", ROOT / "runtime/blender-startup.py")
boot_gate_mod = importlib.util.module_from_spec(boot_gate_spec)
sys.modules[boot_gate_spec.name] = boot_gate_mod
boot_gate_spec.loader.exec_module(boot_gate_mod)


def _load_plugin_module(bpy_stub):
    spec = importlib.util.spec_from_file_location(
        f"blender_plugin_{uuid.uuid4().hex}",
        ROOT / "runtime/blender-mcp-plugin/__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    with patch.dict(sys.modules, {"bpy": bpy_stub}):
        spec.loader.exec_module(mod)
    return mod


class BlenderPluginAndWakeTests(unittest.TestCase):
    def _make_bpy(self, filepath: str, *, dirty_images=None, busy_job: str | None = None):
        quit_called = []
        saved_calls = []

        def save_as_mainfile(filepath, copy=False, relative_remap=True):
            saved_calls.append((filepath, copy, relative_remap))
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            Path(filepath).write_bytes(b"BLENDER-v450-HIBERNATE-" + Path(filepath).name.encode("utf-8"))
            if not copy:
                bpy_stub.data.filepath = filepath
            return {"FINISHED"}

        def open_mainfile(filepath, load_ui=False, use_scripts=False):
            bpy_stub.opened_stage_parent = Path(filepath).parent
            bpy_stub.data.filepath = filepath
            return {"FINISHED"}

        wm_ns = types.SimpleNamespace(
            is_interface_locked=False,
            windows=[types.SimpleNamespace(modal_operators=[])],
        )
        bpy_stub = types.SimpleNamespace(
            opened_stage_parent=None,
            quit_called=quit_called,
            saved_calls=saved_calls,
            app=types.SimpleNamespace(
                background=False,
                is_job_running=lambda job: job == busy_job,
                timers=types.SimpleNamespace(register=lambda fn, first_interval=0.02: fn()),
            ),
            context=types.SimpleNamespace(
                window_manager=wm_ns,
                scene=types.SimpleNamespace(name="Scene", objects=[]),
            ),
            data=types.SimpleNamespace(
                filepath=filepath,
                is_dirty=True,
                objects={},
                images=list(dirty_images or []),
            ),
            ops=types.SimpleNamespace(
                wm=types.SimpleNamespace(
                    save_as_mainfile=save_as_mainfile,
                    open_mainfile=open_mainfile,
                    quit_blender=lambda: quit_called.append(True),
                )
            ),
        )
        return bpy_stub

    def test_copy_true_hibernate_preserves_main_blend_rotates_5_and_restores_without_path_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project/textures").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "hibernate.sentinel"
            main_blend = ws / "project/main.blend"
            original_main_bytes = b"ORIGINAL-MAIN-BLEND-DO-NOT-TOUCH"
            main_blend.write_bytes(original_main_bytes)
            (ws / "project/textures/albedo.png").write_bytes(b"PNG-DATA")

            env = {
                "WORKSPACE_ROOT": str(ws),
                "BLENDER_HIBERNATE_SENTINEL": str(sentinel),
                "BLENDER_START_EMPTY": "false",
                "BLENDER_START_SHA256": hashlib.sha256(original_main_bytes).hexdigest(),
                "BLENDER_START_SIZE": str(len(original_main_bytes)),
            }
            for k in (
                "BLENDER_START_OPERATION_ID",
                "BLENDER_CREATE_OPERATION_ID",
                "INSTANCE_ID",
                "BLENDER_PROJECT_ID",
                "BLENDER_POD_UID",
                "BLENDER_SOURCE_REVISION_ID",
            ):
                env[k] = str(uuid.uuid4())
            (ws / ".verdantflare/prepare.json").write_text(
                json.dumps({
                    "state": "prepared",
                    "prepare_id": env["BLENDER_CREATE_OPERATION_ID"],
                    "instance_id": env["INSTANCE_ID"],
                    "project_id": env["BLENDER_PROJECT_ID"],
                    "source_revision_id": env["BLENDER_SOURCE_REVISION_ID"],
                    "asset_id": "inbox/0123456789abcdef/restore.blend",
                    "sha256": env["BLENDER_START_SHA256"],
                    "size": len(original_main_bytes),
                }),
                encoding="utf-8",
            )

            bpy1 = self._make_bpy(str(main_blend))
            with patch.dict(os.environ, env, clear=False):
                startup_mod.proof = startup_mod.load(bpy1, env)
                plugin1 = _load_plugin_module(bpy1)
                initial_gen = plugin1._GENERATION

                user_ckpt = ws / ("checkpoints/checkpoint-0-" + ("a" * 32) + ".blend")
                user_ckpt.parent.mkdir(parents=True, exist_ok=True)
                user_ckpt.write_bytes(b"USER-CHECKPOINT")

                for ver in range(1, 8):
                    plugin1._SCENE_VERSION = ver
                    res = plugin1._execute({
                        "protocol_version": 1,
                        "generation": initial_gen,
                        "deadline": int(time.time() * 1000) + 10000,
                        "operation": "instance.hibernate",
                        "args": {"max_checkpoints": 5, "quit": ver == 7},
                    })
                    self.assertTrue(res["ok"], res)
                    time.sleep(0.002)

                self.assertEqual(main_blend.read_bytes(), original_main_bytes)
                self.assertTrue(all(call[1] is True and call[2] is False for call in bpy1.saved_calls))
                auto_ckpts = sorted((ws / "checkpoints").glob("auto_hibernate_gen*_v*.blend"))
                self.assertEqual(len(auto_ckpts), 5)
                self.assertTrue(user_ckpt.is_file())
                self.assertTrue(sentinel.is_file())
                self.assertEqual(len(bpy1.quit_called), 1)

                wake_env = dict(env, BLENDER_WAKE_RESTORE="1")
                bpy2 = self._make_bpy("")
                with patch.dict(os.environ, wake_env, clear=False):
                    startup_mod.proof = startup_mod.load(bpy2, wake_env)
                    plugin2 = _load_plugin_module(bpy2)
                    self.assertEqual(plugin2._GENERATION, initial_gen)
                    self.assertEqual(plugin2._SCENE_VERSION, 7)
                    self.assertEqual(bpy2.opened_stage_parent.resolve(), (ws / "project").resolve())
                    self.assertFalse((ws / "project/.vf_wake_stage.blend").exists())
                    self.assertEqual(main_blend.read_bytes(), original_main_bytes)

                    scene_info = plugin2._execute({
                        "protocol_version": 1,
                        "generation": initial_gen,
                        "deadline": int(time.time() * 1000) + 10000,
                        "operation": "scene.get",
                        "args": {},
                    })
                    self.assertTrue(scene_info["ok"])
                    self.assertEqual(scene_info["generation"], initial_gen)
                    self.assertEqual(scene_info["scene_version"], 7)
                    self.assertEqual(scene_info["scene"]["filepath"], "main.blend")

                    ckpt_path = ws / res["checkpoint_asset_id"]
                    ckpt_path.write_bytes(b"TAMPERED-BYTES")
                    with self.assertRaisesRegex(ValueError, "integrity mismatch"):
                        startup_mod.load(self._make_bpy(""), wake_env)

    def test_dirty_unpacked_external_images_and_active_render_block_hibernate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            unpacked_dirty = types.SimpleNamespace(name="BrushTex.png", is_dirty=True, packed_file=None)
            packed_dirty = types.SimpleNamespace(name="PackedTex.png", is_dirty=True, packed_file=object())
            bpy_stub = self._make_bpy(
                str(ws / "project/main.blend"),
                dirty_images=[unpacked_dirty, packed_dirty],
                busy_job="RENDER",
            )
            with patch.dict(os.environ, {"WORKSPACE_ROOT": str(ws), "BLENDER_HIBERNATE_SENTINEL": str(ws / "s")}, clear=False):
                startup_mod.wake_state = None
                plugin = _load_plugin_module(bpy_stub)
                render_blocked = plugin._execute({
                    "protocol_version": 1,
                    "generation": plugin._GENERATION,
                    "deadline": int(time.time() * 1000) + 10000,
                    "operation": "instance.hibernate",
                    "args": {"preflight_only": True},
                })
                self.assertFalse(render_blocked["ok"])
                self.assertEqual(render_blocked["error"]["code"], "RENDER_IN_PROGRESS")

                bpy_stub.app.is_job_running = lambda _job: False
                blocked = plugin._execute({
                    "protocol_version": 1,
                    "generation": plugin._GENERATION,
                    "deadline": int(time.time() * 1000) + 10000,
                    "operation": "instance.hibernate",
                    "args": {"preflight_only": True},
                })
                self.assertFalse(blocked["ok"])
                self.assertEqual(blocked["error"]["code"], "UNSAVED_EXTERNAL_IMAGES")
                self.assertEqual(blocked["error"]["data"], {"images": ["BrushTex.png"]})

                unpacked_dirty.packed_file = object()
                pf_ok = plugin._execute({
                    "protocol_version": 1,
                    "generation": plugin._GENERATION,
                    "deadline": int(time.time() * 1000) + 10000,
                    "operation": "instance.hibernate",
                    "args": {"preflight_only": True},
                })
                self.assertTrue(pf_ok["ok"], pf_ok)
                self.assertTrue(pf_ok["preflight"])
                self.assertEqual(len(bpy_stub.saved_calls), 0)
                self.assertEqual(len(bpy_stub.quit_called), 0)


class ResidencyLineageAndGrantTests(unittest.TestCase):
    def test_proc_stat_transitive_subtree_epoch_shm_manifest_and_canonical_6tuple_grant(self):
        residency = ctrl_mod.residency
        parsed = residency.parse_proc_stat(
            "4242 (blender (cuda) worker) S 100 4200 4200 0 -1 4194304 1 0 0 0 10 2 0 0 20 0 8 0 987654321 1024 10"
        )
        self.assertEqual(parsed["pid"], 4242)
        self.assertEqual(parsed["comm"], "blender (cuda) worker")
        self.assertEqual(parsed["ppid"], 100)
        self.assertEqual(parsed["pgrp"], 4200)
        self.assertEqual(parsed["starttime_ticks"], 987654321)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            proc = base / "proc"
            shm = base / "shm"
            cgroup_file = base / "cgroup"
            proc.mkdir()
            shm.mkdir()
            expected_cid = "a1b2c3d4" * 8
            cgroup_file.write_text(
                f"0::/kubepods.slice/kubepods-burstable.slice/cri-containerd-{expected_cid}.scope\n",
                encoding="utf-8",
            )
            self.assertEqual(residency.resolve_container_id({}, cgroup_path=cgroup_file), expected_cid)

            def make_stat(pid: int, comm: str, ppid: int, pgrp: int, ticks: int) -> str:
                tail = ["S", str(ppid), str(pgrp)] + (["0"] * 16) + [str(ticks), "0", "0"]
                return f"{pid} ({comm}) " + " ".join(tail)

            for pid, stat_line in (
                (100, make_stat(100, "instance-control", 1, 100, 111)),
                (200, make_stat(200, "blender", 100, 777, 2000)),
                (201, make_stat(201, "orphan-render", 100, 500, 222)),
                (202, make_stat(202, "pgrp-helper", 999, 777, 333)),
                (203, make_stat(203, "unrelated", 1, 888, 444)),
                (204, make_stat(204, "setsid-grandchild", 200, 9999, 555)),
                (205, make_stat(205, "great-grandchild", 204, 9999, 666)),
            ):
                pdir = proc / str(pid)
                pdir.mkdir()
                (pdir / "stat").write_text(stat_line, encoding="utf-8")

            adopted = residency.list_adopted_descendants(100, root_pgrp=777, proc_root=proc, root_pid=200)
            self.assertEqual(adopted, {200: 2000, 201: 222, 202: 333, 204: 555, 205: 666})

            # Epoch-scoped shm cleanup (M2): only epoch 1 directory + registered shm names are deleted!
            e1_dir = residency.epoch_shm_dir(shm, 1)
            e1_dir.mkdir()
            (e1_dir / "tile.raw").write_bytes(b"tile")
            e1_mf = residency.epoch_shm_manifest_path(shm, 1)
            (shm / "psm_epoch1_seg").write_bytes(b"seg1")
            residency.register_epoch_shm_object(e1_mf, "psm_epoch1_seg")

            e2_dir = residency.epoch_shm_dir(shm, 2)
            e2_dir.mkdir()
            (e2_dir / "keep.raw").write_bytes(b"keep2")
            (shm / "psm_epoch2_seg").write_bytes(b"seg2")
            residency.register_epoch_shm_object(residency.epoch_shm_manifest_path(shm, 2), "psm_epoch2_seg")
            (shm / "wayland-shm-keep").write_bytes(b"keep-wayland")

            cleaned = residency.cleanup_scoped_shm(shm_root=shm, gpu_epoch=1)
            self.assertEqual(sorted(cleaned), ["psm_epoch1_seg", "vf-blender-epoch-1"])
            self.assertFalse(e1_dir.exists())
            self.assertFalse((shm / "psm_epoch1_seg").exists())
            self.assertFalse(e1_mf.exists())
            self.assertTrue(e2_dir.is_dir())
            self.assertTrue((shm / "psm_epoch2_seg").is_file())
            self.assertTrue((shm / "wayland-shm-keep").is_file())

            # Canonical 6-tuple validation (H3)
            gpu_uuid = "GPU-12345678-1234-4234-8234-123456789abc"
            valid_grant = {
                "pod_uid": "pod-1",
                "container_id": expected_cid,
                "boot_id": "boot-uuid-1",
                "attempt_id": "attempt-uuid-1",
                "lease_id": "lease-001",
                "fencing_token": 7,
                "gpu_uuid": gpu_uuid,
                "expires_at": "2026-11-15T12:00:00Z",
            }
            now_ts = 1794700000.0
            checked = residency.validate_launch_grant(
                valid_grant,
                expected_pod_uid="pod-1",
                expected_container_id=expected_cid,
                expected_boot_id="boot-uuid-1",
                expected_gpu_uuid=gpu_uuid,
                min_fencing_token=5,
                now_epoch=now_ts,
            )
            self.assertEqual(checked["fencing_token"], 7)

            for missing_key in ("pod_uid", "container_id", "boot_id", "attempt_id", "lease_id", "fencing_token"):
                broken = dict(valid_grant)
                broken.pop(missing_key)
                with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_INVALID"):
                    residency.validate_launch_grant(broken, now_epoch=now_ts)

            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_POD_MISMATCH"):
                residency.validate_launch_grant(valid_grant, expected_pod_uid="pod-other", now_epoch=now_ts)
            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_CONTAINER_MISMATCH"):
                residency.validate_launch_grant(valid_grant, expected_container_id="b" * 64, now_epoch=now_ts)
            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_BOOT_MISMATCH"):
                residency.validate_launch_grant(valid_grant, expected_boot_id="boot-other", now_epoch=now_ts)
            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_DEVICE_MISMATCH"):
                residency.validate_launch_grant(
                    valid_grant,
                    expected_gpu_uuid="GPU-ffffffff-1234-4234-8234-123456789abc",
                    now_epoch=now_ts,
                )
            with self.assertRaisesRegex(residency.ResidencyError, "STALE_FENCING_TOKEN"):
                residency.validate_launch_grant(valid_grant, min_fencing_token=8, now_epoch=now_ts)
            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_REVOKED"):
                residency.validate_launch_grant(valid_grant, revoked_leases={"lease-001"}, now_epoch=now_ts)
            with self.assertRaisesRegex(residency.ResidencyError, "GPU_LEASE_EXPIRED"):
                residency.validate_launch_grant(valid_grant, now_epoch=1794744001.0)

    def test_blender_run_sh_lf_no_wildcard_shm_and_mandatory_bash_syntax_check(self):
        script = (ROOT / "runtime/blender-run.sh").read_bytes()
        self.assertNotIn(b"\r", script, "blender-run.sh must retain pure LF line endings")
        text = script.decode("utf-8")
        self.assertIn("BLENDER_HIBERNATE_SENTINEL", text)
        self.assertIn("BLENDER_WAKE_RESTORE=1", text)
        self.assertIn("BLENDER_GPU_EPOCH", text)
        self.assertIn("BLENDER_SUBREAPER_MANAGED", text)
        self.assertNotIn("/dev/shm/vf-blender-*", text)

        candidates = [
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("/bin/bash"),
            Path("/usr/bin/bash"),
        ]
        which_bash = shutil.which("bash")
        if which_bash and "windowsapps" not in which_bash.lower() and "system32\\bash.exe" not in which_bash.lower():
            candidates.append(Path(which_bash))
        bash_bin = next((str(c) for c in candidates if c.is_file()), None)
        self.assertIsNotNone(bash_bin, "Real bash binary is required for mandatory syntax check")
        proc = subprocess.run(
            [bash_bin, "-n", (ROOT / "runtime/blender-run.sh").as_posix()],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"bash -n failed: {proc.stderr}")

    def test_blender_startup_pre_cuda_handshake_accepts_live_grant_and_exits_78_when_revoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "s"
            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-gate")
            state = ctrl_mod.bridge.BridgeState("inst-gate", "worker-tok", adapter)
            cid = "c" * 64
            boot_id = str(uuid.uuid4())
            pod_uid = str(uuid.uuid4())
            gpu_uuid = "GPU-12345678-1234-4234-8234-123456789abc"

            with patch.dict(
                os.environ,
                {"BLENDER_POD_UID": pod_uid, "BLENDER_GPU_UUID": gpu_uuid},
                clear=False,
            ):
                srv = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": ws / "pid.json",
                        "shm_root": ws / "shm",
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id,
                    },
                )
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.addCleanup(srv.server_close)
                self.addCleanup(srv.shutdown)

                grant = {
                    "pod_uid": pod_uid,
                    "container_id": cid,
                    "boot_id": boot_id,
                    "attempt_id": "att-1",
                    "lease_id": "lease-live-1",
                    "fencing_token": 11,
                    "gpu_uuid": gpu_uuid,
                    "expires_at": time.time() + 600,
                }
                srv.residency.install_launch_grant(grant)
                child_env = srv.residency._build_child_env(srv.residency.gpu_epoch, wake_restore=False)

                # Valid live handshake succeeds
                verified = boot_gate_mod.verify_pre_cuda_gate(child_env)
                self.assertEqual(verified["lease_id"], "lease-live-1")
                self.assertEqual(verified["fencing_token"], 11)

                # Now revoke lease-live-1 in the race window right after Popen:
                # running blender-startup.py as __main__ MUST exit 78 BEFORE importing bpy!
                srv.residency.revoked_leases.add("lease-live-1")
                srv.residency.min_fencing_token = 12
                proc = subprocess.run(
                    [sys.executable, str(ROOT / "runtime/blender-startup.py")],
                    env=child_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 78, proc.stderr)
                self.assertIn("pre-cuda gate rejected startup", proc.stderr)


class FakeHibernatingAdapter:
    def __init__(
        self,
        workspace: Path,
        sentinel: Path,
        generation: str | None = None,
        *,
        instance_id: str = "",
        pod_uid: str = "",
    ):
        self.workspace = workspace
        self.sentinel = sentinel
        self.generation = generation or uuid.uuid4().hex
        self.instance_id = instance_id
        self.pod_uid = pod_uid
        self.scene_version = 0
        self.awake = True
        self.dirty_unpacked_images: list[str] = []
        self.render_in_progress = False
        self.scene_get_calls_while_standby = 0
        self.calls: list[tuple[str, dict]] = []

    def call(self, operation, args, **context):
        self.calls.append((operation, dict(args or {})))
        if not self.awake:
            if operation == "scene.get":
                self.scene_get_calls_while_standby += 1
            raise ctrl_mod.bridge.BridgeError("UPSTREAM_UNAVAILABLE", "hibernated", status=503)
        gen = context.get("generation")
        if gen not in (None, self.generation):
            raise ctrl_mod.bridge.BridgeError("STALE_GENERATION", "changed", status=409)
        if operation == "instance.hibernate":
            if self.render_in_progress:
                return {"ok": False, "error": {"code": "RENDER_IN_PROGRESS", "message": "render active"}}
            if self.dirty_unpacked_images:
                return {
                    "ok": False,
                    "error": {
                        "code": "UNSAVED_EXTERNAL_IMAGES",
                        "message": "dirty images",
                        "data": {"images": list(self.dirty_unpacked_images)},
                    },
                }
            if (args or {}).get("preflight_only") is True:
                return {"ok": True, "preflight": True, "generation": self.generation, "scene_version": self.scene_version}
            ckpt_rel = f"checkpoints/auto_hibernate_gen{self.generation}_v{self.scene_version}.blend"
            ckpt_path = self.workspace / ckpt_rel
            ckpt_path.parent.mkdir(parents=True, exist_ok=True)
            payload = f"BLEND-GEN-{self.generation}-V-{self.scene_version}".encode("utf-8")
            ckpt_path.write_bytes(payload)
            sha = hashlib.sha256(payload).hexdigest()
            manifest_path = self.workspace / ".verdantflare/hibernate.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "instance_id": self.instance_id or os.environ.get("INSTANCE_ID", ""),
                    "pod_uid": self.pod_uid or os.environ.get("BLENDER_POD_UID", ""),
                    "generation": self.generation,
                    "scene_version": self.scene_version,
                    "restore_id": None,
                    "checkpoint_asset_id": ckpt_rel,
                    "sha256": sha,
                    "size": len(payload),
                    "original_filepath": str(self.workspace / "project/main.blend"),
                    "startup_proof": None,
                    "hibernated_at": time.time(),
                }),
                encoding="utf-8",
            )
            self.sentinel.write_text("hibernated", encoding="utf-8")
            self.awake = False
            return {
                "ok": True,
                "generation": self.generation,
                "scene_version": self.scene_version,
                "checkpoint_asset_id": ckpt_rel,
                "sha256": sha,
                "size": len(payload),
            }
        if operation == "object.create":
            self.scene_version += 1
            return {
                "ok": True,
                "generation": self.generation,
                "scene_version": self.scene_version,
                "object": {"object_id": args["object_id"], "type": "MESH"},
            }
        return {"ok": True, "generation": self.generation, "scene_version": self.scene_version, "startup": None}


class WorkerResidencyHttpTests(unittest.TestCase):
    def _http_helper(self, base: str):
        def req(path, body=None, token="worker-secret"):
            r = urllib.request.Request(
                base + path,
                data=None if body is None else json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                resp = urllib.request.urlopen(r, timeout=5)
            except urllib.error.HTTPError as exc:
                resp = exc
            with resp:
                return resp.status, json.loads(resp.read())

        return req

    def test_s2_180s_auto_hibernate_s3_readyz_zero_wake_d4_gui_protection_and_lazy_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "blender-hibernate.sentinel"
            pid_state = ws / "blender-process-state.json"
            shm = ws / "shm"
            shm.mkdir()
            clock = [1000.0]

            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="instance-residency")
            state = ctrl_mod.bridge.BridgeState("instance-residency", "worker-secret", adapter)

            def wake_hook(_manifest, next_epoch):
                adapter.awake = True
                pid_state.write_text(json.dumps({"pid": 999999, "gpu_epoch": next_epoch}), encoding="utf-8")

            env_patch = patch.dict(
                os.environ,
                {
                    "WORKSPACE_ROOT": str(ws),
                    "BLENDER_RTC_FILE": str(ws / "rtc.json"),
                    "BLENDER_GUI_HEARTBEAT_TIMEOUT_SECONDS": "60",
                    "BLENDER_GUI_INPUT_STAMP_FILE": str(ws / "gui-input.stamp"),
                },
                clear=False,
            )
            env_patch.start()
            self.addCleanup(env_patch.stop)

            server = ctrl_mod.Server(
                ("127.0.0.1", 0),
                state,
                residency_kwargs={
                    "workspace_root": ws,
                    "sentinel_path": sentinel,
                    "pid_state_path": pid_state,
                    "shm_root": shm,
                    "clock": lambda: clock[0],
                    "hibernate_idle_seconds": 180,
                    "gui_idle_hibernate_seconds": 3600,
                    "wake_hook": wake_hook,
                },
            )
            server.gui.clock = lambda: clock[0]
            gui_stops = []
            server.gui.run = lambda action: gui_stops.append(action)
            self.assertEqual(server.gui.ttl_seconds, 60)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            req = self._http_helper(f"http://127.0.0.1:{server.server_port}")

            code, st = req("/internal/status")
            self.assertEqual(code, 200)
            self.assertEqual(st["gpu_state"]["residency_state"], "idle_evictable")
            self.assertTrue(st["gpu_state"]["evictable"])
            self.assertEqual(st["gpu_state"]["gpu_epoch"], 1)

            clock[0] += 179
            self.assertIsNone(server.residency.idle_watchdog_step())
            self.assertTrue(adapter.awake)

            epoch1_dir = ctrl_mod.residency.epoch_shm_dir(shm, 1)
            epoch1_dir.mkdir(parents=True, exist_ok=True)
            (epoch1_dir / "temp.bin").write_bytes(b"shm")
            clock[0] += 1
            hib = server.residency.idle_watchdog_step()
            self.assertIsNotNone(hib)
            self.assertEqual(hib["residency_state"], "zero_vram_standby")
            self.assertFalse(adapter.awake)
            self.assertFalse(epoch1_dir.exists())

            r_code, r_body = req("/readyz")
            self.assertEqual(r_code, 200)
            self.assertEqual(r_body["status"], "ready")
            self.assertEqual(r_body["residency_state"], "zero_vram_standby")
            self.assertEqual(r_body["generation"], adapter.generation)
            s_code, s_body = req("/internal/status")
            self.assertEqual(s_code, 200)
            self.assertEqual(s_body["gpu_state"]["residency_state"], "zero_vram_standby")
            self.assertEqual(s_body["gpu_state"]["vram_allocated_mib"], 0)
            self.assertEqual(adapter.scene_get_calls_while_standby, 0)

            e_code, e_body = req("/internal/v1/gpu-residency/evict-vram", {"reason": "PREEMPT"})
            self.assertEqual(e_code, 200)
            self.assertTrue(e_body["already_standby"])

            rpc_code, rpc_body = req(
                "/internal/rpc",
                {
                    "instance_id": "instance-residency",
                    "generation": adapter.generation,
                    "name": "object.create",
                    "arguments": {"object_id": "Cube1", "primitive": "cube", "scene_version": 0},
                    "request_id": str(uuid.uuid4()),
                },
            )
            self.assertEqual(rpc_code, 200, rpc_body)
            self.assertEqual(rpc_body["generation"], adapter.generation)
            self.assertEqual(rpc_body["scene_version"], 1)
            self.assertTrue(adapter.awake)
            self.assertEqual(req("/internal/gpu-state")[1]["gpu_epoch"], 2)

            cas_code, cas_body = req(
                "/internal/v1/gpu-residency/evict-vram",
                {"expected_residency_seq": 999999},
            )
            self.assertEqual(cas_code, 409)
            self.assertEqual(cas_body["code"], "RESIDENCY_SEQ_CONFLICT")

            gui_sid = "g" * 43
            rtc_expires = int(time.time()) + 900
            open_gui_code, _ = req(
                "/internal/gui",
                {
                    "instance_id": "instance-residency",
                    "generation": adapter.generation,
                    "action": "open",
                    "session": gui_sid,
                    "rtc_config": {
                        "iceTransportPolicy": "relay",
                        "expires_at": rtc_expires,
                        "iceServers": [
                            {
                                "urls": ["turn:192.0.2.10:3478?transport=udp"],
                                "username": str(rtc_expires) + ":" + ("a" * 24),
                                "credential": ("b" * 27) + "=",
                            }
                        ],
                    },
                },
            )
            self.assertEqual(open_gui_code, 200)
            server.gui.deadline = float("inf")
            g_state = req("/internal/gpu-state")[1]
            self.assertEqual(g_state["residency_state"], "human_dirty")
            self.assertTrue(g_state["human_protected"])
            self.assertFalse(g_state["evictable"])

            self.assertEqual(req("/internal/v1/gpu-residency/evict-vram", {})[0], 409)
            self.assertEqual(
                req("/internal/v1/gpu-residency/revoke-lease", {"lease_id": "l-1", "fencing_token": 10})[0],
                409,
            )

            for _ in range(75):
                clock[0] += 50
                hb_code, _ = req(
                    "/internal/gui",
                    {
                        "instance_id": "instance-residency",
                        "generation": adapter.generation,
                        "action": "heartbeat",
                        "session": gui_sid,
                    },
                )
                self.assertEqual(hb_code, 200)
            self.assertIsNone(server.residency.idle_watchdog_step())
            self.assertTrue(adapter.awake)

            # H5: Preflight rejection (UNSAVED_EXTERNAL_IMAGES) MUST preserve GUI session without calling gui.run("stop")!
            server.residency.colocated_heavy_queued = True
            adapter.dirty_unpacked_images = ["DirtyCanvas.png"]
            stops_before = len(gui_stops)
            pf_code, pf_body = req(
                "/internal/v1/gpu-residency/evict-vram",
                {"force_gui_idle": True, "colocated_heavy_queued": True},
            )
            self.assertEqual(pf_code, 409)
            self.assertEqual(pf_body["code"], "UNSAVED_EXTERNAL_IMAGES")
            self.assertEqual(server.gui.session, gui_sid)
            self.assertEqual(len(gui_stops), stops_before)
            adapter.dirty_unpacked_images = []

            # H5: Real human input (user_input=True) resets input_idle_seconds to 0 and blocks force_gui_idle hibernate!
            hb_in_code, _ = req(
                "/internal/gui",
                {
                    "instance_id": "instance-residency",
                    "generation": adapter.generation,
                    "action": "heartbeat",
                    "session": gui_sid,
                    "user_input": True,
                },
            )
            self.assertEqual(hb_in_code, 200)
            self.assertEqual(req("/internal/gpu-state")[1]["idle_seconds"], 0)
            self.assertEqual(
                req(
                    "/internal/v1/gpu-residency/evict-vram",
                    {"force_gui_idle": True, "colocated_heavy_queued": True},
                )[0],
                409,
            )

            # Advance another 3605s with zero input -> D4-2 checkpoints & hibernates cleanly!
            clock[0] += 3605
            d4_hib = server.residency.idle_watchdog_step()
            self.assertIsNotNone(d4_hib)
            self.assertEqual(d4_hib["residency_state"], "zero_vram_standby")
            self.assertIsNone(server.gui.session)

            # M1: Same-size byte corruption of checkpoint in S3 fails /readyz AND ensure_awake closed with 503!
            ckpt_file = ws / d4_hib["checkpoint_asset_id"]
            orig_bytes = ckpt_file.read_bytes()
            ckpt_file.write_bytes(bytes([orig_bytes[0] ^ 0xFF]) + orig_bytes[1:])
            self.assertEqual(req("/readyz")[0], 503)
            self.assertEqual(
                req(
                    "/internal/rpc",
                    {
                        "instance_id": "instance-residency",
                        "generation": adapter.generation,
                        "name": "object.create",
                        "arguments": {"object_id": "CubeFail", "primitive": "cube", "scene_version": 1},
                        "request_id": str(uuid.uuid4()),
                    },
                )[0],
                503,
            )

            # M1: Deleting hibernate.json even when last_known_generation is populated STILL returns 503 (zero fallback)!
            ckpt_file.write_bytes(orig_bytes)
            self.assertEqual(req("/readyz")[0], 200)
            (ws / ".verdantflare/hibernate.json").unlink()
            self.assertIsNotNone(server.residency.last_known_generation)
            self.assertEqual(req("/readyz")[0], 503)

    def test_h1_reap_timeout_and_eperm_quarantine_preserves_lineage_shm_and_blocks_new_work(self):
        for simulate_eperm, expected_code in (
            (False, "REAP_TIMEOUT_UNRECLAIMED"),
            (True, "SIGNAL_PERMISSION_DENIED"),
        ):
            with self.subTest(simulate_eperm=simulate_eperm):
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Path(tmp).resolve()
                    (ws / "project").mkdir(parents=True)
                    (ws / ".verdantflare").mkdir(parents=True)
                    proc_root = ws / "proc"
                    shm_root = ws / "shm"
                    proc_root.mkdir()
                    shm_root.mkdir()
                    sentinel = ws / "sentinel"
                    pid_state = ws / "pid.json"

                    # Create fake un-reapable process 4444 in proc_root
                    pdir = proc_root / "4444"
                    pdir.mkdir()
                    (pdir / "stat").write_text(
                        "4444 (blender-stuck) D 1 4444 4444 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 77777 0 0",
                        encoding="utf-8",
                    )
                    pid_state.write_text(
                        json.dumps({"pid": 4444, "starttime_ticks": 77777, "gpu_epoch": 1}),
                        encoding="utf-8",
                    )
                    e1_shm = ctrl_mod.residency.epoch_shm_dir(shm_root, 1)
                    e1_shm.mkdir()
                    (e1_shm / "cuda_ipc.bin").write_bytes(b"active-ipc")

                    def fake_kill(_pid, _sig):
                        if simulate_eperm:
                            raise PermissionError("EPERM")
                        # Do NOT remove proc/4444 -> simulates D-state / un-reapable survivor!

                    adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-h1")
                    state = ctrl_mod.bridge.BridgeState("inst-h1", "worker-secret", adapter)
                    srv = ctrl_mod.Server(
                        ("127.0.0.1", 0),
                        state,
                        residency_kwargs={
                            "workspace_root": ws,
                            "sentinel_path": sentinel,
                            "pid_state_path": pid_state,
                            "proc_root": proc_root,
                            "shm_root": shm_root,
                            "soft_evict_deadline_ms": 200,
                            "kill_fn": fake_kill,
                        },
                    )
                    srv.gui.run = lambda _a: None
                    threading.Thread(target=srv.serve_forever, daemon=True).start()
                    self.addCleanup(srv.server_close)
                    self.addCleanup(srv.shutdown)
                    req = self._http_helper(f"http://127.0.0.1:{srv.server_port}")

                    ev_code, ev_body = req("/internal/v1/gpu-residency/evict-vram", {"reason": "PREEMPT"})
                    self.assertEqual(ev_code, 503)
                    self.assertEqual(ev_body["code"], expected_code)

                    # H1 invariants: quarantined, vram_allocated_mib is None (NOT 0), lineage/pid_state/shm preserved!
                    gs = req("/internal/gpu-state")[1]
                    self.assertEqual(gs["residency_state"], "quarantined")
                    self.assertFalse(gs["evictable"])
                    self.assertIsNone(gs["vram_allocated_mib"])
                    self.assertIsNotNone(srv.residency.lineage)
                    self.assertTrue(pid_state.is_file())
                    self.assertTrue((e1_shm / "cuda_ipc.bin").is_file())
                    self.assertFalse(sentinel.exists())

                    self.assertEqual(req("/readyz")[0], 503)
                    self.assertEqual(req("/internal/status")[0], 503)
                    rpc_code, rpc_body = req(
                        "/internal/rpc",
                        {
                            "instance_id": "inst-h1",
                            "generation": adapter.generation,
                            "name": "object.create",
                            "arguments": {"object_id": "Blocked", "primitive": "cube", "scene_version": 0},
                            "request_id": str(uuid.uuid4()),
                        },
                    )
                    self.assertEqual(rpc_code, 503)
                    self.assertEqual(rpc_body["code"], "UNRECLAIMED_GPU_LINEAGE")

    def test_h2_readiness_timeout_registers_lineage_immediately_and_reaps_real_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "sentinel"
            pid_state = ws / "pid.json"
            shm_root = ws / "shm"
            shm_root.mkdir()

            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-h2")
            # Create a valid hibernate checkpoint first
            adapter.call("instance.hibernate", {"quit": True})
            self.assertTrue(sentinel.is_file())

            spawned_procs: list[subprocess.Popen] = []
            observed_lineage_during_wait: list[int] = []

            def real_spawner(_epoch, _wake_restore, child_env):
                # Spawn a real sleeping child that never opens adapter readiness
                p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=child_env)
                spawned_procs.append(p)
                return p

            orig_call = adapter.call

            def slow_fail_call(op, args, **ctx):
                if srv.residency.lineage is not None:
                    observed_lineage_during_wait.append(srv.residency.lineage.root_pid)
                return orig_call(op, args, **ctx)

            adapter.call = slow_fail_call  # type: ignore[assignment]
            state = ctrl_mod.bridge.BridgeState("inst-h2", "worker-secret", adapter)
            srv = ctrl_mod.Server(
                ("127.0.0.1", 0),
                state,
                residency_kwargs={
                    "workspace_root": ws,
                    "sentinel_path": sentinel,
                    "pid_state_path": pid_state,
                    "shm_root": shm_root,
                    "wake_timeout_seconds": 0.25,
                    "soft_evict_deadline_ms": 1000,
                    "spawner": real_spawner,
                },
            )
            self.addCleanup(srv.server_close)

            with self.assertRaisesRegex(ctrl_mod.residency.ResidencyError, "WAKE_TIMEOUT"):
                srv.residency.ensure_awake()

            self.assertEqual(len(spawned_procs), 1)
            child = spawned_procs[0]
            # Lineage was registered BEFORE readiness completed!
            self.assertIn(child.pid, observed_lineage_during_wait)
            # Child process was terminated and reaped on WAKE_TIMEOUT, and standby sentinel was restored!
            self.assertIsNotNone(child.poll())
            self.assertFalse(srv.residency.quarantined)
            self.assertTrue(sentinel.is_file())

    def test_h6_revoke_lease_fences_stale_epoch_without_killing_newer_and_protects_unsaved_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "sentinel"
            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-h6", pod_uid="pod-h6")
            state = ctrl_mod.bridge.BridgeState("inst-h6", "worker-secret", adapter)
            cid = "d" * 64
            boot_id = str(uuid.uuid4())

            with patch.dict(os.environ, {"BLENDER_POD_UID": "pod-h6"}, clear=False):
                srv = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": ws / "pid.json",
                        "shm_root": ws / "shm",
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id,
                    },
                )
                srv.gui.run = lambda _a: None
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.addCleanup(srv.server_close)
                self.addCleanup(srv.shutdown)
                req = self._http_helper(f"http://127.0.0.1:{srv.server_port}")

                # Install active grant with fencing_token=20
                srv.residency.install_launch_grant({
                    "pod_uid": "pod-h6",
                    "container_id": cid,
                    "boot_id": boot_id,
                    "attempt_id": "att-20",
                    "lease_id": "lease-20",
                    "fencing_token": 20,
                    "expires_at": time.time() + 600,
                })

                # 1) Wrong pod_uid / container_id rejected with 403
                self.assertEqual(
                    req("/internal/v1/gpu-residency/revoke-lease", {"lease_id": "lease-20", "fencing_token": 20, "pod_uid": "wrong"})[0],
                    403,
                )
                self.assertEqual(
                    req("/internal/v1/gpu-residency/revoke-lease", {"lease_id": "lease-20", "fencing_token": 20, "container_id": "e" * 64})[0],
                    403,
                )

                # 2) Stale revoke for old lease-15 (fencing_token=15 < 20) fences lease-15 WITHOUT killing running epoch!
                st_code, st_body = req(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {"lease_id": "lease-15", "fencing_token": 15},
                )
                self.assertEqual(st_code, 200)
                self.assertTrue(st_body["already_superseded"])
                self.assertTrue(adapter.awake)
                self.assertIn("lease-15", srv.residency.revoked_leases)

                # 3) Dirty unpacked images block revoke_lease with 409 UNSAVED_EXTERNAL_IMAGES (no uncheckpointed kill!)
                adapter.dirty_unpacked_images = ["UnsavedSculpt.exr"]
                dirty_code, dirty_body = req(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {"lease_id": "lease-20", "fencing_token": 20},
                )
                self.assertEqual(dirty_code, 409)
                self.assertEqual(dirty_body["code"], "UNSAVED_EXTERNAL_IMAGES")
                self.assertTrue(adapter.awake)
                self.assertFalse(sentinel.exists())

                # 4) Once dirty image is packed, revoke_lease checkpoints & enters zero_vram_standby cleanly!
                adapter.dirty_unpacked_images = []
                ok_code, ok_body = req(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {"lease_id": "lease-20", "fencing_token": 21},
                )
                self.assertEqual(ok_code, 200, ok_body)
                self.assertEqual(ok_body["residency_state"], "zero_vram_standby")
                self.assertTrue(ok_body["revoked"])
                self.assertTrue(ok_body["reclaimed"])
                self.assertFalse(adapter.awake)

    def test_p0_h1_h2_real_subprocess_pre_cuda_handshake_under_wake_and_runtime_wire_contract(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "sentinel"
            pid_state = ws / "pid.json"
            shm_root = ws / "shm"
            shm_root.mkdir()
            cid = "c" * 64
            boot_id = str(uuid.uuid4())
            gpu_uuid = "GPU-11111111-2222-4333-8444-555555555555"

            sched_calls: list[tuple[str, str, dict]] = []
            poll_counts: dict[str, int] = {}

            allowed_acquire_keys = {
                "idempotency_key",
                "request_sha256",
                "holder_app_id",
                "consumer_kind",
                "task_id",
                "instance_id",
                "attempt_id",
                "stage_id",
                "node_uid",
                "pod_uid",
                "container_id",
                "boot_id",
                "candidate_gpu_uuids",
                "card_count",
                "k8s_slots_required",
                "peak_budget_mib",
                "startup_free_min_mib",
                "headroom_override_mib",
                "priority_class",
                "ttl_seconds",
                "source_lease_id",
                "run_nonce",
            }
            attempt_by_lease: dict[str, str] = {}

            class MockRuntimeHandler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    return

                def _reply(self, code: int, obj: dict):
                    raw = json.dumps(obj).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def do_POST(self):
                    if self.headers.get("Authorization") != "Bearer sched-secret":
                        return self._reply(401, {"error_code": "UNAUTHENTICATED"})
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                    sched_calls.append(("POST", self.path, body))
                    if self.path == "/internal/v1/gpu-scheduler/leases/acquire":
                        unknown = set(body) - allowed_acquire_keys
                        ikey = body.get("idempotency_key", "")
                        if unknown or not ikey:
                            return self._reply(400, {"error_code": "INVALID_REQUEST", "unknown": sorted(unknown)})
                        if ikey not in poll_counts:
                            idx = len(poll_counts) + 1
                            lid = f"lease-{idx}"
                            poll_counts[ikey] = (lid, 1)
                            attempt_by_lease[lid] = body["attempt_id"]
                            return self._reply(
                                202,
                                {
                                    "lease_id": lid,
                                    "state": "queued",
                                    "holder_app_id": "app-blender",
                                    "holder_task_id": body.get("task_id", ""),
                                    "attempt_id": body["attempt_id"],
                                    "queue_position": 1,
                                    "blocking_reason": "VRAM_BUSY_ACTIVE_COMPUTE",
                                },
                            )
                        lid, prev_n = poll_counts[ikey]
                        poll_counts[ikey] = (lid, prev_n + 1)
                        idx = int(lid.split("-")[-1])
                        return self._reply(
                            200,
                            {
                                "lease_id": lid,
                                "state": "leased",
                                "holder_app_id": "app-blender",
                                "holder_task_id": body.get("task_id", f"wake-{idx}"),
                                "fencing_token": 100 + idx,
                                "gpu_uuid": gpu_uuid,
                                "gpu_uuids": [gpu_uuid],
                                "pod_uid": "pod-p0h1",
                                "container_id": cid,
                                "boot_id": boot_id,
                                "attempt_id": attempt_by_lease[lid],
                                "peak_budget_mib": 6144,
                                "startup_free_min_mib": 6144,
                                "priority": "shared_slice",
                                "blocking_reason": "",
                                "expires_at": "2029-11-15T00:00:00Z",
                                "version": 2,
                                "ledger_event_seq": 10 + idx,
                            },
                        )
                    if self.path.endswith("/release"):
                        rel_lid = self.path.rstrip("/").split("/")[-2]
                        return self._reply(200, {"lease_id": rel_lid, "state": "released"})
                    return self._reply(404, {"error_code": "NOT_FOUND"})

                def do_GET(self):
                    if self.headers.get("Authorization") != "Bearer sched-secret":
                        return self._reply(401, {"error_code": "UNAUTHENTICATED"})
                    sched_calls.append(("GET", self.path, {}))
                    prefix = "/internal/v1/gpu-scheduler/leases/"
                    if self.path.startswith(prefix):
                        lid = self.path[len(prefix) :]
                        poll_counts[lid] = poll_counts.get(lid, 0) + 1
                        idx = int(lid.split("-")[-1])
                        return self._reply(
                            200,
                            {
                                "lease_id": lid,
                                "state": "leased",
                                "holder_app_id": "app-blender",
                                "holder_task_id": f"wake-{idx}",
                                "fencing_token": 100 + idx,
                                "gpu_uuid": gpu_uuid,
                                "gpu_uuids": [gpu_uuid],
                                "pod_uid": "pod-p0h1",
                                "container_id": cid,
                                "boot_id": boot_id,
                                "attempt_id": attempt_by_lease[lid],
                                "peak_budget_mib": 6144,
                                "startup_free_min_mib": 6144,
                                "priority": "shared_slice",
                                "blocking_reason": "",
                                "expires_at": "2029-11-15T00:00:00Z",
                                "version": 2,
                                "ledger_event_seq": 10 + idx,
                            },
                        )
                    return self._reply(404, {"error_code": "NOT_FOUND"})

            sched_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockRuntimeHandler)
            threading.Thread(target=sched_srv.serve_forever, daemon=True).start()
            self.addCleanup(sched_srv.server_close)
            self.addCleanup(sched_srv.shutdown)

            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-p0h1", pod_uid="pod-p0h1")
            adapter.awake = False
            state = ctrl_mod.bridge.BridgeState("inst-p0h1", "worker-secret", adapter)

            child_procs: list[subprocess.Popen] = []
            handshake_results: list[dict] = []

            child_code = (
                "import importlib.util, json, os, sys, time\n"
                f"spec = importlib.util.spec_from_file_location('bgate', {str(ROOT / 'runtime/blender-startup.py')!r})\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "res = mod.verify_pre_cuda_gate(os.environ)\n"
                "with open(os.environ['HANDSHAKE_OUT'], 'w', encoding='utf-8') as fh:\n"
                "    json.dump(res or {}, fh)\n"
                "time.sleep(30)\n"
            )

            def real_handshake_spawner(epoch, _wake_restore, child_env):
                out_file = ws / f"handshake_ep{epoch}.json"
                env_copy = dict(child_env, HANDSHAKE_OUT=str(out_file))
                proc = subprocess.Popen([sys.executable, "-c", child_code], env=env_copy)
                child_procs.append(proc)

                def mark_adapter_awake_when_gate_passes():
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if out_file.is_file():
                            try:
                                data = json.loads(out_file.read_text(encoding="utf-8"))
                            except Exception:
                                data = None
                            if data:
                                handshake_results.append(data)
                                adapter.awake = True
                                return
                        if proc.poll() is not None:
                            return
                        time.sleep(0.02)

                threading.Thread(target=mark_adapter_awake_when_gate_passes, daemon=True).start()
                return proc

            def cleanup_children():
                for proc in child_procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait(timeout=2)

            self.addCleanup(cleanup_children)

            callback_only_tok = "residency-callback-secret-0123456789abcdef"
            with patch.dict(
                os.environ,
                {
                    "BLENDER_POD_UID": "pod-p0h1",
                    "BLENDER_GPU_UUID": gpu_uuid,
                    "STATION_GPU_SCHEDULER_URL": f"http://127.0.0.1:{sched_srv.server_port}",
                    "STATION_GPU_SCHEDULER_TOKEN": "sched-secret",
                    "VF_RESIDENCY_CALLBACK_TOKEN": callback_only_tok,
                },
                clear=False,
            ):
                srv = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "shm_root": shm_root,
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id,
                        "wake_timeout_seconds": 4.0,
                        "spawner": real_handshake_spawner,
                    },
                )
                srv.gui.run = lambda _a: None
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.addCleanup(srv.server_close)
                self.addCleanup(srv.shutdown)
                req = self._http_helper(f"http://127.0.0.1:{srv.server_port}")

                # Verify strict token scope separation:
                # - VF_RESIDENCY_CALLBACK_TOKEN is rejected (401) on business/status routes and pre-cuda-check!
                # - supervisor handshake token is rejected (401) on evict-vram and business/status routes!
                self.assertEqual(req("/internal/status", token=callback_only_tok)[0], 401)
                self.assertEqual(req("/internal/v1/gpu-residency/pre-cuda-check", {}, token=callback_only_tok)[0], 401)
                self.assertEqual(req("/internal/status", token=srv.residency.handshake_token)[0], 401)
                self.assertEqual(req("/internal/v1/gpu-residency/evict-vram", {}, token=srv.residency.handshake_token)[0], 401)

                # 1) Initial boot spawns real child calling HTTP /internal/v1/gpu-residency/pre-cuda-check -> zero deadlock!
                t0 = time.monotonic()
                srv.residency.start_initial_blender()
                init_state = srv.residency.gpu_state()
                self.assertLess(time.monotonic() - t0, 3.0)
                self.assertEqual(init_state["gpu_epoch"], 1)
                self.assertEqual(len(handshake_results), 1)
                self.assertEqual(handshake_results[0]["fencing_token"], 101)

                # 2) Runtime canonical EvictResidencyCommand wire payload hibernates epoch 1 cleanly using VF_RESIDENCY_CALLBACK_TOKEN
                ev_code, ev_body = req(
                    "/internal/v1/gpu-residency/evict-vram",
                    {
                        "instance_id": "inst-p0h1",
                        "expected_boot_id": boot_id,
                        "expected_gpu_epoch": 1,
                        "expected_residency_seq": init_state["residency_seq"],
                        "reason": "PREEMPT_FOR_QUEUED_TASK",
                        "deadline_ms": 5000,
                    },
                    token=callback_only_tok,
                )
                self.assertEqual(ev_code, 200, ev_body)
                self.assertTrue(ev_body["settled"])
                self.assertEqual(ev_body["boot_id"], boot_id)
                self.assertEqual(ev_body["residency_state"], "zero_vram_standby")
                self.assertGreater(ev_body["supervisor_pid"], 0)
                rel_calls = [c for c in sched_calls if c[1].endswith("/release")]
                self.assertEqual(len(rel_calls), 1)
                self.assertEqual(rel_calls[0][2]["reclaim_mode"], "epoch_unload")
                self.assertGreater(rel_calls[0][2]["supervisor_pid"], 0)
                self.assertTrue(rel_calls[0][2]["reclaim_proof"]["reclaimed"])
                self.assertEqual(rel_calls[0][2]["reclaim_proof"]["sealed_gpu_epoch"], 1)
                adapter.awake = False

                # 3) Waking from zero_vram_standby via activity_scope() acquires second lease (202->200) and handshakes without deadlock!
                srv.residency.active_grant = None
                t1 = time.monotonic()
                with srv.residency.activity_scope():
                    self.assertTrue(adapter.awake)
                self.assertLess(time.monotonic() - t1, 3.0)
                self.assertEqual(srv.residency.gpu_epoch, 2)
                self.assertEqual(len(handshake_results), 2)
                self.assertEqual(handshake_results[1]["fencing_token"], 102)
                self.assertFalse(any(c[0] == "GET" for c in sched_calls))

                # 4) Runtime canonical RevokeLeaseCommand wire payload checkpoints/fences epoch 2 cleanly using VF_RESIDENCY_CALLBACK_TOKEN
                rev_code, rev_body = req(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {
                        "instance_id": "inst-p0h1",
                        "lease_id": "lease-2",
                        "fencing_token": 102,
                        "pod_uid": "pod-p0h1",
                        "container_id": cid,
                        "boot_id": boot_id,
                        "expected_gpu_epoch": 2,
                        "reason": "LEASE_EXPIRED_SWEEP",
                    },
                    token=callback_only_tok,
                )
                self.assertEqual(rev_code, 200, rev_body)
                self.assertTrue(rev_body["revoked"])
                self.assertTrue(rev_body["reclaimed"])
                self.assertTrue(rev_body["settled"])
                self.assertEqual(rev_body["lease_id"], "lease-2")
                self.assertGreater(rev_body["supervisor_starttime_ticks"], 0)
                self.assertEqual(rev_body["reclaim_proof"]["revoked_lease_id"], "lease-2")
                self.assertEqual(rev_body["reclaim_proof"]["consumed_fencing_token"], 102)

    def test_consumed_grant_replay_rejected_and_pending_release_journal_retries_across_restart(self):
        import http.server

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "sentinel-r3"
            pid_state = ws / "pid-r3.json"
            shm_root = ws / "shm-r3"
            proc_root = ws / "proc-r3"
            shm_root.mkdir()
            proc_root.mkdir()
            # Verify PID 1 stat reading works when instance-control runs as container PID 1!
            (proc_root / "1").mkdir()
            (proc_root / "1" / "stat").write_text(
                "1 (instance-control) S 0 1 1 0 -1 4194304 1 0 0 0 10 2 0 0 20 0 1 0 555666 1024 10",
                encoding="utf-8",
            )
            self.assertEqual(ctrl_mod.residency.read_proc_stat(1, proc_root=proc_root)["starttime_ticks"], 555666)

            release_calls = []
            fail_release = [True]

            class SchedHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    release_calls.append((self.path, body))
                    if fail_release[0]:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"TEMP_UNAVAILABLE"}')
                        return
                    rel_lid = self.path.rstrip("/").split("/")[-2]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"lease_id": rel_lid, "state": "released"}).encode("utf-8"))

            sched_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SchedHandler)
            threading.Thread(target=sched_srv.serve_forever, daemon=True).start()
            self.addCleanup(sched_srv.server_close)
            self.addCleanup(sched_srv.shutdown)

            cid = "f" * 64
            boot_id = str(uuid.uuid4())
            gpu_uuid = "GPU-12345678-1234-4234-8234-123456789abc"
            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-r3", pod_uid="pod-r3")
            state = ctrl_mod.bridge.BridgeState("inst-r3", "worker-secret", adapter)

            with patch.dict(
                os.environ,
                {
                    "WORKSPACE_ROOT": str(ws),
                    "INSTANCE_ID": "inst-r3",
                    "BLENDER_POD_UID": "pod-r3",
                    "BLENDER_GPU_UUID": gpu_uuid,
                    "STATION_GPU_SCHEDULER_URL": f"http://127.0.0.1:{sched_srv.server_port}",
                    "STATION_GPU_SCHEDULER_TOKEN": "sched-secret",
                },
                clear=False,
            ):
                srv1 = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "proc_root": proc_root,
                        "shm_root": shm_root,
                        "supervisor_pid": 1,
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id,
                    },
                )
                srv1.gui.run = lambda _a: None
                threading.Thread(target=srv1.serve_forever, daemon=True).start()
                self.addCleanup(srv1.server_close)
                self.addCleanup(srv1.shutdown)

                grant1 = {
                    "pod_uid": "pod-r3",
                    "container_id": cid,
                    "boot_id": boot_id,
                    "attempt_id": "att-b-1",
                    "lease_id": "lease-b-1",
                    "fencing_token": 25,
                    "gpu_uuid": gpu_uuid,
                    "expires_at": time.time() + 600,
                }
                srv1.residency.install_launch_grant(grant1)

                # Hibernate while scheduler /release fails 503 -> grant1 is sealed and pending_releases persisted!
                hib_out = srv1.residency.hibernate(reason="IDLE_WARM_TTL")
                self.assertTrue(hib_out["settled"])
                self.assertEqual(hib_out["supervisor_pid"], 1)
                self.assertEqual(hib_out["supervisor_starttime_ticks"], 555666)
                self.assertEqual(hib_out["reclaim_proof"]["revoked_lease_id"], "lease-b-1")
                self.assertEqual(hib_out["reclaim_proof"]["consumed_fencing_token"], 25)
                self.assertEqual(len(release_calls), 1)
                self.assertIn("lease-b-1", srv1.residency.pending_releases)

                # Replay of grant1 (or any token <= 25) MUST fail 409!
                with self.assertRaisesRegex(ctrl_mod.residency.ResidencyError, "GPU_LEASE_REVOKED"):
                    srv1.residency.install_launch_grant(grant1)
                with self.assertRaisesRegex(ctrl_mod.residency.ResidencyError, "STALE_FENCING_TOKEN"):
                    srv1.residency.install_launch_grant(
                        dict(grant1, lease_id="lease-b-same-25", attempt_id="att-b-same-25")
                    )

                # Recover scheduler and instantiate fresh controller on the same workspace_root:
                # pending_releases flushes on init, and consumed watermark (25) still blocks replay!
                fail_release[0] = False
                boot_id_2 = str(uuid.uuid4())
                srv2 = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "proc_root": proc_root,
                        "shm_root": shm_root,
                        "supervisor_pid": 1,
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id_2,
                    },
                )
                self.addCleanup(srv2.server_close)
                self.assertGreaterEqual(len(release_calls), 2)
                self.assertEqual(srv2.residency.pending_releases, {})
                with self.assertRaisesRegex(ctrl_mod.residency.ResidencyError, "GPU_LEASE_REVOKED"):
                    srv2.residency.install_launch_grant(dict(grant1, boot_id=boot_id_2))
                with self.assertRaisesRegex(ctrl_mod.residency.ResidencyError, "STALE_FENCING_TOKEN"):
                    srv2.residency.install_launch_grant(
                        dict(grant1, boot_id=boot_id_2, lease_id="lease-b-25b", attempt_id="att-b-25b")
                    )
                accepted = srv2.residency.install_launch_grant(
                    dict(grant1, boot_id=boot_id_2, lease_id="lease-b-26", attempt_id="att-b-26", fencing_token=26)
                )
                self.assertEqual(accepted["fencing_token"], 26)

    def test_fence_state_persistence_failure_corrupt_boot_and_strict_release_ack(self):
        import errno
        import http.server

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "sentinel-r4"
            pid_state = ws / "pid-r4.json"
            shm_root = ws / "shm-r4"
            proc_root = ws / "proc-r4"
            shm_root.mkdir()
            proc_root.mkdir()

            release_mode = ["404"]

            class StrictSchedHandler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def _json(self, code: int, body_obj):
                    raw = json.dumps(body_obj).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    if length:
                        self.rfile.read(length)
                    lid = self.path.rstrip("/").split("/")[-2]
                    mode = release_mode[0]
                    if mode == "404":
                        return self._json(404, {"error": "NOT_FOUND"})
                    if mode == "empty_dict":
                        return self._json(200, {})
                    if mode == "non_dict":
                        return self._json(200, ["released"])
                    if mode == "missing_lease_id":
                        return self._json(200, {"state": "released"})
                    if mode == "wrong_lease_id":
                        return self._json(200, {"lease_id": "other-lease", "state": "released"})
                    if mode == "expired":
                        return self._json(200, {"lease_id": lid, "state": "expired"})
                    return self._json(200, {"lease_id": lid, "state": "released"})

            sched_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StrictSchedHandler)
            threading.Thread(target=sched_srv.serve_forever, daemon=True).start()
            self.addCleanup(sched_srv.server_close)
            self.addCleanup(sched_srv.shutdown)

            cid = "a" * 64
            boot_id = str(uuid.uuid4())
            gpu_uuid = "GPU-12345678-1234-4234-8234-123456789abc"
            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-r4", pod_uid="pod-r4")
            state = ctrl_mod.bridge.BridgeState("inst-r4", "worker-secret", adapter)

            with patch.dict(
                os.environ,
                {
                    "WORKSPACE_ROOT": str(ws),
                    "INSTANCE_ID": "inst-r4",
                    "BLENDER_POD_UID": "pod-r4",
                    "BLENDER_GPU_UUID": gpu_uuid,
                    "STATION_GPU_SCHEDULER_URL": f"http://127.0.0.1:{sched_srv.server_port}",
                    "STATION_GPU_SCHEDULER_TOKEN": "sched-secret",
                },
                clear=False,
            ):
                srv = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "proc_root": proc_root,
                        "shm_root": shm_root,
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": boot_id,
                    },
                )
                srv.gui.run = lambda _a: None
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.addCleanup(srv.server_close)
                self.addCleanup(srv.shutdown)
                req = self._http_helper(f"http://127.0.0.1:{srv.server_port}")

                grant1 = {
                    "pod_uid": "pod-r4",
                    "container_id": cid,
                    "boot_id": boot_id,
                    "attempt_id": "att-r4-1",
                    "lease_id": "lease-r4-1",
                    "fencing_token": 51,
                    "gpu_uuid": gpu_uuid,
                    "expires_at": time.time() + 600,
                }

                # 1) ENOSPC during install_launch_grant MUST fail 503 FENCE_STATE_PERSISTENCE_FAILED and NOT install active_grant!
                with patch.object(ctrl_mod.residency.os, "replace", side_effect=OSError(errno.ENOSPC, "No space")):
                    code, body = req("/internal/launch-grant", grant1)
                self.assertEqual(code, 503, body)
                self.assertEqual(body["code"], "FENCE_STATE_PERSISTENCE_FAILED")
                self.assertIsNone(srv.residency.active_grant)

                # Once disk space recovers, install_launch_grant persists durably and clears unhealthy flag automatically!
                code, body = req("/internal/launch-grant", grant1)
                self.assertEqual(code, 200, body)
                self.assertIsNone(srv.residency.fence_state_unhealthy)
                self.assertIsNotNone(srv.residency.active_grant)

                # 2) EACCES during hibernate (evict-vram) -> checkpoints & reaps, returns 503 FENCE_STATE_PERSISTENCE_FAILED, settled=False, NO reclaim_proof!
                with patch.object(ctrl_mod.residency.os, "replace", side_effect=OSError(errno.EACCES, "Denied")):
                    ev_code, ev_body = req("/internal/v1/gpu-residency/evict-vram", {"instance_id": "inst-r4"})
                self.assertEqual(ev_code, 503, ev_body)
                self.assertFalse(ev_body["settled"])
                self.assertEqual(ev_body["code"], "FENCE_STATE_PERSISTENCE_FAILED")
                self.assertNotIn("reclaim_proof", ev_body)

                # 3) EACCES during revoke-lease -> ALSO returns 503 FENCE_STATE_PERSISTENCE_FAILED, settled=False, NO reclaim_proof!
                with patch.object(ctrl_mod.residency.os, "replace", side_effect=OSError(errno.EACCES, "Denied")):
                    rev_code, rev_body = req(
                        "/internal/v1/gpu-residency/revoke-lease",
                        {"instance_id": "inst-r4", "lease_id": "lease-r4-1", "fencing_token": 51},
                    )
                self.assertEqual(rev_code, 503, rev_body)
                self.assertFalse(rev_body["settled"])
                self.assertFalse(rev_body["reclaimed"])
                self.assertEqual(rev_body["code"], "FENCE_STATE_PERSISTENCE_FAILED")
                self.assertNotIn("reclaim_proof", rev_body)

                # Once disk recovers, revoke-lease durably persists fence state and returns settled=True + reclaim_proof!
                rev_ok_code, rev_ok_body = req(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {"instance_id": "inst-r4", "lease_id": "lease-r4-1", "fencing_token": 51},
                )
                self.assertEqual(rev_ok_code, 200, rev_ok_body)
                self.assertTrue(rev_ok_body["settled"])
                self.assertEqual(rev_ok_body["reclaim_proof"]["consumed_fencing_token"], 51)

                # 4) Strict /release acknowledgement: 404, {}, [], missing lease_id, and wrong lease_id MUST retain pending_releases!
                with srv.residency.lock:
                    srv.residency._seal_grant_and_journal_release_locked(grant1, 1, enqueue_release=True)
                self.assertIn("lease-r4-1", srv.residency.pending_releases)
                for bad_mode in ("404", "empty_dict", "non_dict", "missing_lease_id", "wrong_lease_id"):
                    release_mode[0] = bad_mode
                    self.assertEqual(srv.residency.flush_pending_releases(), 0, bad_mode)
                    self.assertIn("lease-r4-1", srv.residency.pending_releases, bad_mode)

                release_mode[0] = "expired"
                self.assertEqual(srv.residency.flush_pending_releases(), 1)
                self.assertNotIn("lease-r4-1", srv.residency.pending_releases)

                # 5) Corrupt existing gpu_fence_state.json on boot MUST fail-closed (503 FENCE_STATE_CORRUPTED)!
                srv.residency.fence_state_path.write_text("{corrupt-json", encoding="utf-8")
                srv_corrupt = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "proc_root": proc_root,
                        "shm_root": shm_root,
                        "grant_policy": "required",
                        "container_id": cid,
                        "boot_id": str(uuid.uuid4()),
                    },
                )
                threading.Thread(target=srv_corrupt.serve_forever, daemon=True).start()
                self.addCleanup(srv_corrupt.server_close)
                self.addCleanup(srv_corrupt.shutdown)
                self.assertTrue(srv_corrupt.residency.fence_state_corrupted)
                self.assertTrue(srv_corrupt.residency.quarantined)
                req_c = self._http_helper(f"http://127.0.0.1:{srv_corrupt.server_port}")
                c_code, c_body = req_c(
                    "/internal/launch-grant",
                    dict(grant1, boot_id=srv_corrupt.residency.boot_id, lease_id="lease-c-60", attempt_id="att-c-60", fencing_token=60),
                )
                self.assertEqual(c_code, 503, c_body)
                self.assertEqual(c_body["code"], "FENCE_STATE_CORRUPTED")
                c_ev_code, c_ev_body = req_c("/internal/v1/gpu-residency/evict-vram", {"instance_id": "inst-r4"})
                self.assertEqual(c_ev_code, 503, c_ev_body)
                self.assertEqual(c_ev_body["code"], "FENCE_STATE_CORRUPTED")
                c_rev_code, c_rev_body = req_c(
                    "/internal/v1/gpu-residency/revoke-lease",
                    {"instance_id": "inst-r4", "lease_id": "lease-c-60", "fencing_token": 60},
                )
                self.assertEqual(c_rev_code, 503, c_rev_body)
                self.assertEqual(c_rev_body["code"], "FENCE_STATE_CORRUPTED")


class ApplicationReleaseGpuIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "project").mkdir(parents=True)
        (self.root / ".verdantflare").mkdir(parents=True)

        self.subject = str(uuid.uuid4())
        self.viewer = str(uuid.uuid4())
        self.org = str(uuid.uuid4())
        self.project = str(uuid.uuid4())
        self.instance_id = str(uuid.uuid4())
        self.worker_token = "worker-app-residency-token"
        os.environ["BLENDER_WORKER_RESIDENCY"] = self.worker_token

        self.sentinel = self.root / "app-hibernate.sentinel"
        self.adapter = FakeHibernatingAdapter(self.root, self.sentinel, instance_id=self.instance_id)
        w_state = ctrl_mod.bridge.BridgeState(self.instance_id, self.worker_token, self.adapter)
        self.worker_srv = ctrl_mod.Server(
            ("127.0.0.1", 0),
            w_state,
            residency_kwargs={
                "workspace_root": self.root,
                "sentinel_path": self.sentinel,
                "pid_state_path": self.root / "pid.json",
                "shm_root": self.root / "shm",
                "wake_hook": lambda _m, _e: setattr(self.adapter, "awake", True),
            },
        )
        self.worker_srv.gui.run = lambda _a: None
        threading.Thread(target=self.worker_srv.serve_forever, daemon=True).start()
        self.addCleanup(self.worker_srv.server_close)
        self.addCleanup(self.worker_srv.shutdown)

        cfg_path = self.root / "instances.json"
        cfg_path.write_text(
            json.dumps({
                "instances": {
                    "blenderRes": {
                        "id": self.instance_id,
                        "organization_id": self.org,
                        "project_id": self.project,
                        "endpoint": f"http://127.0.0.1:{self.worker_srv.server_port}",
                        "grants": {self.subject: "edit", self.viewer: "read"},
                        "managers": [self.subject],
                        "token_env": "BLENDER_WORKER_RESIDENCY",
                    }
                }
            }),
            encoding="utf-8",
        )

        proj_id = self.project
        rev_id = app_mod.content.uuid7()

        class StubContent:
            def open(self, subject, org, project_id, revision=None, require_write=False):
                return {"project_id": proj_id, "revision_id": rev_id, "manifest": {"name": "ResidencyProj", "files": []}}

        self.app = app_mod.Application(cfg_path, self.root / "app.db", "studio-secret", StubContent())
        self.addCleanup(self.app.store.db.close)

    def test_session_open_gpu_state_and_instance_release_gpu_preserves_1800s_edit_session(self):
        self.assertEqual(app_mod.LEASE_TTL_SECONDS, 1800)
        self.assertEqual(app_mod.GUI_LEASE_TTL_SECONDS, 60)

        opened = self.app.call("blenderRes", self.subject, self.org, "session.open", {"project_id": self.project, "mode": "edit"})
        sid = opened["editing_session_id"]
        self.assertEqual(opened["lease_ttl_seconds"], 1800)
        self.assertEqual(opened["gpu_state"]["residency_state"], "idle_evictable")
        self.assertTrue(opened["gpu_state"]["evictable"])

        with self.assertRaisesRegex(app_mod.Error, "READ_ONLY"):
            self.app.call("blenderRes", self.viewer, self.org, "instance.release_gpu", {"idempotency_key": "viewer-release-01"})

        rel1 = self.app.call(
            "blenderRes",
            self.subject,
            self.org,
            "instance.release_gpu",
            {"editing_session_id": sid, "idempotency_key": "editor-release-01"},
        )
        self.assertTrue(rel1["released"])
        self.assertFalse(rel1["already_standby"])
        self.assertEqual(rel1["residency_state"], "zero_vram_standby")
        self.assertFalse(self.adapter.awake)

        rel1_replay = self.app.call(
            "blenderRes",
            self.subject,
            self.org,
            "instance.release_gpu",
            {"editing_session_id": sid, "idempotency_key": "editor-release-01"},
        )
        self.assertEqual(rel1_replay, rel1)

        view = self.app.instance_view("blenderRes", self.subject, self.org)
        self.assertEqual(view["status"], "running")
        self.assertEqual(view["gpu_state"]["residency_state"], "zero_vram_standby")
        self.assertEqual(view["gpu_state"]["vram_allocated_mib"], 0)
        self.assertFalse(self.adapter.awake)

        created = self.app.call(
            "blenderRes",
            self.subject,
            self.org,
            "object.create",
            {
                "editing_session_id": sid,
                "idempotency_key": "create-after-wake-01",
                "scene_version": 0,
                "object_id": "WakeCube",
                "primitive": "cube",
            },
        )
        self.assertTrue(created["ok"])
        self.assertEqual(created["generation"], opened["generation"])
        self.assertEqual(created["scene_version"], 1)
        self.assertTrue(self.adapter.awake)

        evict_out = self.app.evict_instance_vram(self.subject, self.org, {"alias": "blenderRes", "reason": "SCHEDULER_PREEMPT"})
        self.assertTrue(evict_out["released"])
        self.assertEqual(evict_out["residency_state"], "zero_vram_standby")


class AdminForceHardDrainTests(unittest.TestCase):
    def _http_helper(self, base: str):
        def req(path, body=None, token="worker-secret"):
            r = urllib.request.Request(
                base + path,
                data=None if body is None else json.dumps(body).encode("utf-8"),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                resp = urllib.request.urlopen(r, timeout=5)
            except urllib.error.HTTPError as exc:
                resp = exc
            with resp:
                return resp.status, json.loads(resp.read())

        return req

    def test_force_hard_evict_drains_s1_gui_s0_busy_quarantined_and_wakes_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp).resolve()
            (ws / "project").mkdir(parents=True)
            (ws / ".verdantflare").mkdir(parents=True)
            sentinel = ws / "blender-hibernate.sentinel"
            pid_state = ws / "blender-process-state.json"
            shm = ws / "shm"
            proc_root = ws / "proc"
            shm.mkdir()
            proc_root.mkdir()
            clock = [2000.0]

            adapter = FakeHibernatingAdapter(ws, sentinel, instance_id="inst-force-hard", pod_uid="pod-fh-1")
            state = ctrl_mod.bridge.BridgeState("inst-force-hard", "worker-secret", adapter)
            wake_calls: list[tuple[dict, int]] = []

            def wake_hook(manifest, next_epoch):
                wake_calls.append((dict(manifest or {}), next_epoch))
                adapter.awake = True
                pid_state.write_text(json.dumps({"pid": 888888, "gpu_epoch": next_epoch}), encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "WORKSPACE_ROOT": str(ws),
                    "BLENDER_POD_UID": "pod-fh-1",
                    "BLENDER_RTC_FILE": str(ws / "rtc.json"),
                },
                clear=False,
            ):
                server = ctrl_mod.Server(
                    ("127.0.0.1", 0),
                    state,
                    residency_kwargs={
                        "workspace_root": ws,
                        "sentinel_path": sentinel,
                        "pid_state_path": pid_state,
                        "shm_root": shm,
                        "proc_root": proc_root,
                        "clock": lambda: clock[0],
                        "wake_hook": wake_hook,
                    },
                )
                gui_stops: list[str] = []
                server.gui.clock = lambda: clock[0]
                server.gui.run = lambda action: gui_stops.append(action)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                req = self._http_helper(f"http://127.0.0.1:{server.server_port}")

                # 1) S1 GUI held: force_hard=False -> 409 GUI_EDIT_LEASE_HELD; force_hard=True -> 200 HARD_EVICTED
                gui_sid = "g" * 43
                rtc_expires = int(time.time()) + 900
                open_code, _ = req(
                    "/internal/gui",
                    {
                        "instance_id": "inst-force-hard",
                        "generation": adapter.generation,
                        "action": "open",
                        "session": gui_sid,
                        "rtc_config": {
                            "iceTransportPolicy": "relay",
                            "expires_at": rtc_expires,
                            "iceServers": [
                                {
                                    "urls": ["turn:192.0.2.10:3478?transport=udp"],
                                    "username": str(rtc_expires) + ":" + ("a" * 24),
                                    "credential": ("b" * 27) + "=",
                                }
                            ],
                        },
                    },
                )
                self.assertEqual(open_code, 200)
                server.gui.deadline = float("inf")
                s1 = req("/internal/gpu-state")[1]
                self.assertEqual(s1["residency_state"], "human_dirty")
                c_soft, b_soft = req("/internal/v1/gpu-residency/evict-vram", {"reason": "USER_RESIDENCY_EVICT"})
                self.assertEqual(c_soft, 409)
                self.assertEqual(b_soft["code"], "GUI_EDIT_LEASE_HELD")

                c_hard, b_hard = req(
                    "/internal/v1/gpu-residency/evict-vram",
                    {
                        "expected_boot_id": server.residency.boot_id,
                        "expected_gpu_epoch": s1["gpu_epoch"],
                        "expected_residency_seq": s1["residency_seq"],
                        "reason": "ADMIN_FORCE_DRAIN",
                        "force_hard": True,
                    },
                )
                self.assertEqual(c_hard, 200, b_hard)
                self.assertTrue(b_hard["settled"])
                self.assertEqual(b_hard["residency_state"], "zero_vram_standby")
                self.assertEqual(b_hard["reason_code"], "HARD_EVICTED")
                self.assertIsNone(server.gui.session)
                self.assertIn("stop", gui_stops)
                self.assertEqual(b_hard["reclaim_proof"]["sealed_gpu_epoch"], 1)
                self.assertEqual(req("/readyz")[0], 200)

                # 2) Subsequent RPC wakes cleanly from hard_drained sentinel into gpu_epoch 2!
                c_rpc, b_rpc = req(
                    "/internal/rpc",
                    {
                        "instance_id": "inst-force-hard",
                        "generation": adapter.generation,
                        "name": "object.create",
                        "arguments": {"object_id": "HardWakeCube", "primitive": "cube", "scene_version": 0},
                        "request_id": str(uuid.uuid4()),
                    },
                )
                self.assertEqual(c_rpc, 200, b_rpc)
                self.assertEqual(wake_calls[-1], ({}, 2))
                self.assertEqual(req("/internal/gpu-state")[1]["gpu_epoch"], 2)

                # 3) Real /internal/batch pre-Popen race + live spawned subprocess reaping under force_hard=True
                batch_run_mod = ctrl_mod.batch_run

                inbox_dir = ws / "inbox" / "batchrace0001"
                inbox_dir.mkdir(parents=True, exist_ok=True)
                script_file = inbox_dir / "sleep_job.py"
                script_bytes = b"import time\ntime.sleep(30)\n"
                script_file.write_bytes(script_bytes)
                script_sha = hashlib.sha256(script_bytes).hexdigest()
                batch_payload = {
                    "instance_id": "inst-force-hard",
                    "generation": adapter.generation,
                    "request_id": str(uuid.uuid4()),
                    "script_asset_id": "inbox/batchrace0001/sleep_job.py",
                    "script_sha256": script_sha,
                    "script_size": len(script_bytes),
                    "output_subdir": "race_out",
                    "timeout_seconds": 30,
                }

                # 3a) Pre-Popen race: batch thread enters activity_scope & batch_run, pauses right before Popen;
                #     concurrent force_hard drains & seals epoch 2, and even wakes into epoch 3;
                #     resumed batch thread MUST NOT invoke subprocess.Popen (0 spawns)!
                entered_pre_launch = threading.Event()
                release_pre_launch = threading.Event()
                popen_calls: list[list[str]] = []
                real_popen = subprocess.Popen

                def _spy_popen(cmd, *p_args, **p_kwargs):
                    popen_calls.append([str(x) for x in cmd])
                    return real_popen([sys.executable, str(script_file)], *p_args, **p_kwargs)

                def _pre_hook() -> None:
                    entered_pre_launch.set()
                    release_pre_launch.wait(timeout=5.0)

                batch_res_holder: list[tuple[int, dict]] = []
                orig_hook = batch_run_mod._PRE_LAUNCH_TEST_HOOK
                batch_run_mod._PRE_LAUNCH_TEST_HOOK = _pre_hook
                try:
                    with patch.object(batch_run_mod.subprocess, "Popen", side_effect=_spy_popen):
                        t_batch = threading.Thread(
                            target=lambda: batch_res_holder.append(req("/internal/batch", batch_payload)),
                            daemon=True,
                        )
                        t_batch.start()
                        self.assertTrue(entered_pre_launch.wait(timeout=3.0))

                        # Soft evict must still reject with 409 WORKER_BUSY while batch is in-flight
                        self.assertEqual(req("/internal/v1/gpu-residency/evict-vram", {"force_hard": False})[0], 409)

                        s0 = req("/internal/gpu-state")[1]
                        c_s0, b_s0 = req(
                            "/internal/v1/gpu-residency/evict-vram",
                            {
                                "expected_boot_id": server.residency.boot_id,
                                "expected_gpu_epoch": s0["gpu_epoch"],
                                "expected_residency_seq": s0["residency_seq"],
                                "reason": "ADMIN_FORCE_DRAIN",
                                "force_hard": True,
                            },
                        )
                        self.assertEqual(c_s0, 200, b_s0)
                        self.assertEqual(b_s0["residency_state"], "zero_vram_standby")
                        self.assertEqual(b_s0["reclaim_proof"]["sealed_gpu_epoch"], 2)

                        # Wake into epoch 3 BEFORE releasing the paused epoch-2 batch thread!
                        c_wake3, b_wake3 = req(
                            "/internal/rpc",
                            {
                                "instance_id": "inst-force-hard",
                                "generation": adapter.generation,
                                "name": "object.create",
                                "arguments": {"object_id": "Epoch3Cube", "primitive": "cube", "scene_version": 0},
                                "request_id": str(uuid.uuid4()),
                            },
                        )
                        self.assertEqual(c_wake3, 200, b_wake3)
                        self.assertEqual(req("/internal/gpu-state")[1]["gpu_epoch"], 3)

                        release_pre_launch.set()
                        t_batch.join(timeout=5.0)
                        self.assertFalse(t_batch.is_alive())
                        self.assertEqual(len(popen_calls), 0, "Stale pre-Popen batch MUST NEVER spawn after force-drain!")
                        self.assertEqual(batch_res_holder[0][0], 409, batch_res_holder[0])
                        self.assertEqual(batch_res_holder[0][1]["code"], "BATCH_ABORTED_BY_FORCE_DRAIN")
                finally:
                    batch_run_mod._PRE_LAUNCH_TEST_HOOK = orig_hook

                # 3b) Live spawned subprocess race: batch spawns a real OS child process; force_hard kills & reaps it BEFORE sealing!
                spawned_procs: list[subprocess.Popen] = []
                proc_spawned_evt = threading.Event()

                def _live_popen(cmd, *p_args, **p_kwargs):
                    p = real_popen([sys.executable, str(script_file)], *p_args, **p_kwargs)
                    spawned_procs.append(p)
                    proc_spawned_evt.set()
                    return p

                live_batch_holder: list[tuple[int, dict]] = []
                with patch.object(batch_run_mod.subprocess, "Popen", side_effect=_live_popen):
                    t_live = threading.Thread(
                        target=lambda: live_batch_holder.append(req("/internal/batch", batch_payload)),
                        daemon=True,
                    )
                    t_live.start()
                    self.assertTrue(proc_spawned_evt.wait(timeout=3.0))
                    self.assertIsNone(spawned_procs[0].poll())

                    s0_live = req("/internal/gpu-state")[1]
                    c_live, b_live = req(
                        "/internal/v1/gpu-residency/evict-vram",
                        {
                            "expected_boot_id": server.residency.boot_id,
                            "expected_gpu_epoch": s0_live["gpu_epoch"],
                            "expected_residency_seq": s0_live["residency_seq"],
                            "reason": "ADMIN_FORCE_DRAIN",
                            "force_hard": True,
                        },
                    )
                    self.assertEqual(c_live, 200, b_live)
                    self.assertIsNotNone(spawned_procs[0].poll(), "Batch OS child process MUST be reaped before seal proof returns!")
                    self.assertEqual(b_live["residency_state"], "zero_vram_standby")
                    self.assertEqual(b_live["reclaim_proof"]["sealed_gpu_epoch"], 3)
                    t_live.join(timeout=5.0)
                    self.assertFalse(t_live.is_alive())
                    self.assertEqual(live_batch_holder[0][0], 409, live_batch_holder[0])

                server.residency.quarantined = True
                server.residency.quarantine_reason = "REAP_TIMEOUT_UNRECLAIMED"
                sq = req("/internal/gpu-state")[1]
                self.assertEqual(sq["residency_state"], "quarantined")
                c_q, b_q = req(
                    "/internal/v1/gpu-residency/evict-vram",
                    {
                        "expected_boot_id": server.residency.boot_id,
                        "expected_gpu_epoch": sq["gpu_epoch"],
                        "expected_residency_seq": sq["residency_seq"],
                        "reason": "ADMIN_FORCE_DRAIN",
                        "force_hard": True,
                    },
                )
                self.assertEqual(c_q, 200, b_q)
                self.assertEqual(b_q["residency_state"], "zero_vram_standby")
                self.assertFalse(server.residency.quarantined)


if __name__ == "__main__":
    unittest.main()
