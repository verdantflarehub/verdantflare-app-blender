import hashlib
import http.server
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("content_client_test", Path(__file__).parents[1] / "app/content.py")
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.requests.append((self.path, dict(self.headers), data))
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.end_headers()
            return
        if self.path == "/project/open":
            result = {"project_id": json.loads(data)["project_id"], "revision_id": self.server.revision}
        elif self.path == "/artifact/uploads":
            self.server.prepare = json.loads(data)
            result = dict(self.server.ids, state="prepared", content_path="/v2/artifacts/uploads/" + self.server.ids["upload_id"] + "/content")
            if self.server.bad_path:
                result["content_path"] = "http://untrusted.example/content"
        elif self.path.endswith("/content"):
            self.server.binary = data
            result = {"state": "prepared"}
        else:
            p = self.server.prepare
            result = dict(p, schema_version=2, store_id=content.uuid7(), artifact_id=self.server.ids["artifact_id"], version_id=self.server.ids["version_id"], organization_id=self.headers["X-Organization-Id"])
        raw = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_PUT = do_POST


class ClientTests(unittest.TestCase):
    def test_binary_identity_no_redirect_and_returned_path_validation(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.requests, server.redirect, server.bad_path = [], False, False
        server.ids = {k: content.uuid7() for k in ("upload_id", "version_id", "artifact_id")}
        server.revision = content.uuid7()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = content.Client(f"http://127.0.0.1:{server.server_port}", "fixture-internal")
            user, org, project = [content.uuid7() for _ in range(3)]
            self.assertEqual(client.open(user, org, project)["revision_id"], server.revision)
            payload = bytes(range(256)) * 12000
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "scene.blend"
                path.write_bytes(payload)
                digest = hashlib.sha256(payload).hexdigest()
                client.upload(user, org, project, content.uuid7(), path, digest, len(payload))
                self.assertEqual(server.binary, payload)
                self.assertEqual(server.requests[2][1]["Content-Type"], "application/octet-stream")
                server.bad_path = True
                before = len(server.requests)
                with self.assertRaisesRegex(content.ContentError, "ARTIFACT_RESPONSE_INVALID"):
                    client.upload(user, org, project, content.uuid7(), path, digest, len(payload))
                self.assertEqual(len(server.requests), before + 1)
            for _, headers, _ in server.requests:
                self.assertEqual(headers["Authorization"], "Bearer fixture-internal")
                self.assertEqual(headers["X-User-Id"], user)
                self.assertEqual(headers["X-Organization-Id"], org)
                self.assertRegex(headers["X-Request-Id"], content.ID)
            server.redirect = True
            with self.assertRaisesRegex(content.ContentError, "CONTENT_SERVICE_UNAVAILABLE"):
                client.open(user, org, project)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
