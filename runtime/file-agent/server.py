#!/usr/bin/env python3
"""Workspace-scoped upload/download agent for the Blender MCP instance."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.server
import json
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

from workspace import WorkspaceError, asset_path, safe_filename, sha256_file, workspace_root


ALLOWED_EXTENSIONS = {".blend", ".glb", ".gltf", ".png", ".jpg", ".jpeg", ".exr", ".tif", ".tiff"}


class FileState:
    def __init__(self, root: Path, token: str, max_bytes: int):
        if not token:
            raise ValueError("token is required")
        self.root = root
        self.token = token
        self.max_bytes = max_bytes
        for name in ("project", "inbox", "exports", "checkpoints", "cache"):
            (root / name).mkdir(parents=True, exist_ok=True)

    def authorized(self, header: str | None) -> bool:
        return bool(header and header.startswith("Bearer ") and hmac.compare_digest(header[7:].strip(), self.token))


class FileHandler(http.server.BaseHTTPRequestHandler):
    server: "FileServer"

    def _json(self, value: object, status: int = 200) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        if self.server.state.authorized(self.headers.get("Authorization")):
            return True
        self._json({"error": "AUTH_REQUIRED"}, 401)
        return False

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/healthz":
            self._json({"status": "ok"})
            return
        if self.path.rstrip("/") == "/readyz":
            self._json({"status": "ready"})
            return
        if not self.path.startswith("/assets/"):
            self._json({"error": "not_found"}, 404)
            return
        if not self._authorized():
            return
        asset_id = urllib.parse.unquote(self.path[len("/assets/"):])
        try:
            path = asset_path(self.server.state.root, asset_id)
        except (WorkspaceError, FileNotFoundError):
            self._json({"error": "ASSET_NOT_FOUND"}, 404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("X-Asset-SHA256", sha256_file(path))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                self.wfile.write(chunk)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/upload" or not self._authorized():
            if self.path.rstrip("/") != "/upload":
                self._json({"error": "not_found"}, 404)
            return
        length = self.headers.get("Content-Length")
        try:
            size = int(length or "-1")
        except ValueError:
            size = -1
        if size < 1 or size > self.server.state.max_bytes:
            self._json({"error": "INVALID_ARGUMENT", "message": "invalid content length"}, 413)
            return
        filename = self.headers.get("X-Asset-Name", "")
        expected = self.headers.get("X-Asset-SHA256", "").lower()
        try:
            filename = safe_filename(filename)
            if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
                raise WorkspaceError("file extension is not allowed")
            if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
                raise WorkspaceError("X-Asset-SHA256 is required")
        except WorkspaceError as exc:
            self._json({"error": "ASSET_REJECTED", "message": str(exc)}, 400)
            return
        upload_id = secrets.token_urlsafe(12)
        destination = self.server.state.root / "inbox" / upload_id / filename
        destination.parent.mkdir(parents=True, exist_ok=False)
        temporary = destination.with_name(f".{filename}.upload")
        digest = hashlib.sha256()
        remaining = size
        try:
            with temporary.open("xb") as stream:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("upload ended before Content-Length")
                    stream.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            actual = digest.hexdigest()
            if not hmac.compare_digest(actual, expected):
                temporary.unlink(missing_ok=True)
                destination.parent.rmdir()
                self._json({"error": "ASSET_REJECTED", "message": "SHA-256 mismatch"}, 400)
                return
            os.replace(temporary, destination)
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            self._json({"error": "ASSET_REJECTED", "message": str(exc)}, 400)
            return
        self._json({"asset_id": str(destination.relative_to(self.server.state.root)), "sha256": expected}, 201)

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"[blender-file-agent] {self.command} {self.path} {fmt % args}\n")


class FileServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: FileState):
        self.state = state
        super().__init__(address, FileHandler)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("BLENDER_FILE_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_FILE_AGENT_PORT", "48085")))
    args = parser.parse_args()
    token_file = os.environ.get("BLENDER_MCP_TOKEN_FILE", "/run/user/1000/blender-mcp-token")
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        root = workspace_root(os.environ.get("WORKSPACE_ROOT", "/workspace"))
    except OSError as exc:
        print(f"[blender-file-agent] configuration error: {exc}", file=sys.stderr)
        return 78
    try:
        state = FileState(root, token, int(os.environ.get("BLENDER_FILE_MAX_BODY_BYTES", "1073741824")))
    except ValueError as exc:
        print(f"[blender-file-agent] configuration error: {exc}", file=sys.stderr)
        return 78
    server = FileServer((args.host, args.port), state)
    print(f"[blender-file-agent] listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
