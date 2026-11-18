"""Локальная проверка приложения и runner без Docker и сторонних пакетов."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import Handler


class QuietHandler(Handler):
    def log_message(self, *_args):
        pass


class WrongVersionHandler(QuietHandler):
    def do_GET(self):
        if self.path == "/version":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"version":"unexpected"}')
        else:
            super().do_GET()


@contextmanager
def running_app(handler=QuietHandler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_cli(url, directory):
    return subprocess.run(
        [sys.executable, str(Path(__file__).with_name("runner.py")),
         "--base-url", url, "--output-dir", directory, "--wait-seconds", "0"],
        capture_output=True, text=True, timeout=10,
    )


class ExampleTests(unittest.TestCase):
    def test_endpoints_and_not_found(self):
        with running_app() as url:
            for path, expected in [("/health", {"status": "ok"}), ("/version", {"version": "1.0.0"})]:
                with self.subTest(path=path), urllib.request.urlopen(url + path, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "application/json")
                    self.assertEqual(json.load(response), expected)
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url + "/missing", timeout=2)
            self.assertEqual(error.exception.code, 404)
            self.assertEqual(json.load(error.exception), {"error": "not found"})
            error.exception.close()

    def test_runner_success_and_reports(self):
        with running_app() as url, tempfile.TemporaryDirectory(prefix="http-smoke-") as directory:
            process = run_cli(url, directory)
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads((Path(directory) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual((report["tests"], report["failures"]), (2, 0))
            suite = ET.parse(Path(directory) / "junit.xml").getroot()
            self.assertEqual(suite.attrib["tests"], "2")
            self.assertEqual(suite.attrib["failures"], "0")
            self.assertEqual(len(suite.findall("testcase")), 2)

    def test_runner_rejects_wrong_version_and_reports_failure(self):
        with running_app(WrongVersionHandler) as url, tempfile.TemporaryDirectory(prefix="http-smoke-") as directory:
            process = run_cli(url, directory)
            self.assertEqual(process.returncode, 1, process.stderr)
            report = json.loads((Path(directory) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(report["failures"], 1)
            self.assertFalse(report["checks"][1]["passed"])
            failures = ET.parse(Path(directory) / "junit.xml").findall("testcase/failure")
            self.assertEqual(len(failures), 1)

    def test_runner_unavailable_api_still_writes_reports(self):
        # Bound socket without listen: reserved ephemeral port, no accepting server.
        with ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler, bind_and_activate=False) as server:
            server.server_bind()
            url = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory(prefix="http-smoke-") as directory:
                process = run_cli(url, directory)
                self.assertEqual(process.returncode, 1, process.stderr)
                report = json.loads((Path(directory) / "results.json").read_text(encoding="utf-8"))
                self.assertEqual(report["failures"], 2)
                self.assertEqual(ET.parse(Path(directory) / "junit.xml").getroot().attrib["failures"], "2")


if __name__ == "__main__":
    unittest.main()
