"""Учебный stateless HTTP-стенд без внешних зависимостей."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            status, body = 200, {"status": "ok"}
        elif path == "/version":
            status, body = 200, {"version": "1.0.0"}
        else:
            status, body = 404, {"error": "not found"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    server = ThreadingHTTPServer(
        (os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8080"))),
        Handler,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
