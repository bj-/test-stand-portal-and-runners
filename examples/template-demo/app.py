"""Нейтральный API с SQLite для проверки полного развёртывания."""
import json
from contextlib import closing
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


DATABASE = Path(os.environ.get("DATABASE", "/data/demo.sqlite3"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = 404, {"error": "not found"}
        try:
            if self.path == "/health":
                with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as db:
                    db.execute("SELECT id FROM items LIMIT 1")
                status, body = 200, {"status": "ok"}
            elif self.path == "/items":
                with closing(sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)) as db:
                    rows = db.execute("SELECT id, name FROM items ORDER BY id").fetchall()
                status, body = 200, {"items": [{"id": row[0], "name": row[1]} for row in rows]}
            elif self.path == "/version":
                status, body = 200, json.loads(Path("version.json").read_text(encoding="utf-8"))
        except (sqlite3.Error, OSError, ValueError):
            status, body = 503, {"error": "not ready"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
