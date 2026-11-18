"""Real localhost HTTP checks; no Docker or system installation."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tempfile
from threading import Thread
import unittest

import wait_health
from portal_local import init_workspace


class HealthTests(unittest.TestCase):
    def test_ready_invalid_payload_and_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            mode = "ok"
            redirected = False

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path != "/api/health":
                    type(self).redirected = True
                if self.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/other")
                    self.end_headers()
                    return
                body = b'{"status":"ok"}' if self.mode == "ok" else b'<html>Another server</html>'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "source"
            source.mkdir()
            (source / "Dockerfile").write_text("FROM scratch\n")
            (source / "docker-compose.yaml").write_text("services: {}\n")
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                workspace = root / "workspace"
                init_workspace(workspace, None, server.server_port, source)
                for mode, expected in (("ok", 0), ("invalid", 20), ("redirect", 20)):
                    Handler.mode = mode
                    with self.subTest(mode=mode), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(wait_health.main(["--workspace", str(workspace), "--timeout", "1"]), expected)
                self.assertFalse(Handler.redirected)
            finally:
                server.shutdown()
                worker.join(timeout=5)
                server.server_close()

    def test_missing_workspace_fails_before_network(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wait_health.main(["--workspace", temp, "--timeout", "1"]), 1)


if __name__ == "__main__":
    unittest.main()
