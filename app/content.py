"""Bounded private HTTP client for Studio's Project/Artifact content bridge."""
from __future__ import annotations

import json
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_FILE = 512 * 1024 * 1024
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


def uuid7():
    return str(uuid.UUID(int=(int(time.time() * 1000) << 80) | (7 << 76) | (secrets.randbits(12) << 64) | (2 << 62) | secrets.randbits(62)))


class ContentError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, origin, token):
        u = urllib.parse.urlsplit(origin)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {"", "/"} or not token:
            raise ValueError("invalid private content configuration")
        self.origin, self.token = origin.rstrip("/"), token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, subject, org, method, path, value=None, stream=None, size=None):
        if not all(isinstance(v, str) and ID.fullmatch(v) for v in (subject, org)):
            raise ContentError("INVALID_IDENTITY", 403)
        headers = {"Authorization": "Bearer " + self.token, "X-User-Id": subject,
                   "X-Organization-Id": org, "X-Request-Id": uuid7(), "Content-Type": "application/json"}
        data = json.dumps(value, allow_nan=False).encode() if value is not None else b""
        if stream is not None:
            if type(size) is not int or not 0 < size <= MAX_FILE:
                raise ContentError("CONTENT_TOO_LARGE", 413)
            data = stream
            headers.update({"Content-Length": str(size), "Content-Type": "application/octet-stream"})
        try:
            request = urllib.request.Request(self.origin + path, method=method, data=data, headers=headers)
            with self.opener.open(request, timeout=240) as response:
                raw = response.read((8 << 20) + 1)
                if len(raw) > 8 << 20:
                    raise ValueError()
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read(4096)).get("code")
            except (ValueError, AttributeError):
                code = None
            allowed = {"PERMISSION_DENIED", "NOT_FOUND", "INVALID_ARGUMENT", "REVISION_CONFLICT", "IDEMPOTENCY_CONFLICT", "CONTENT_NOT_READY", "COMMIT_IN_PROGRESS"}
            raise ContentError(code if code in allowed else "CONTENT_SERVICE_UNAVAILABLE", exc.code if exc.code in {400, 403, 404, 409} else 503) from None
        except (OSError, ValueError):
            raise ContentError("CONTENT_SERVICE_UNAVAILABLE") from None

    def open(self, subject, org, project):
        result = self.request(subject, org, "POST", "/project/open", {"project_id": project})
        if result.get("project_id") != project or not ID.fullmatch(result.get("revision_id", "")):
            raise ContentError("PROJECT_RESPONSE_INVALID", 502)
        return result

    def upload(self, subject, org, project, write_id, path, sha256, size):
        source = {"kind": "user_edit", "project_id": project}
        upload = self.request(subject, org, "POST", "/artifact/uploads", {
            "write_id": write_id, "source": source, "sha256": sha256, "size": size, "mime": "application/x-blender"})
        if not all(isinstance(upload.get(k), str) and ID.fullmatch(upload[k]) for k in ("upload_id", "artifact_id", "version_id")) or upload.get("state") not in {"prepared", "committed"}:
            raise ContentError("ARTIFACT_RESPONSE_INVALID", 502)
        expected = "/v2/artifacts/uploads/" + upload["upload_id"] + "/content"
        if upload.get("content_path") != expected:
            raise ContentError("ARTIFACT_RESPONSE_INVALID", 502)
        if upload["state"] == "prepared":
            with Path(path).open("rb") as stream:
                self.request(subject, org, "PUT", "/artifact/uploads/" + upload["upload_id"] + "/content", stream=stream, size=size)
        version = self.request(subject, org, "POST", "/artifact/uploads/" + upload["upload_id"] + "/commit")
        if not all(isinstance(version.get(k), str) and ID.fullmatch(version[k]) for k in ("store_id", "artifact_id", "version_id")) or version.get("schema_version") != 2 or version.get("organization_id") != org or version.get("artifact_id") != upload["artifact_id"] or version.get("version_id") != upload["version_id"] or version.get("sha256") != sha256 or version.get("size") != size or version.get("mime") != "application/x-blender" or version.get("source") != source:
            raise ContentError("ARTIFACT_RESPONSE_INVALID", 502)
        return {key: version[key] for key in ("store_id", "artifact_id", "version_id")}
