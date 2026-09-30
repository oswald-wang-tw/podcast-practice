"""Local library server with reversible delete and restore endpoints."""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from filelock import Timeout

from .errors import PracticeError
from .library import delete_episode, restore_episode
from .render import library_html, update_library


class LibraryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, root: Path, token: str, **kwargs):
        self.root = root
        self.token = token
        super().__init__(*args, directory=str(root), **kwargs)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404)
            return
        if path in {"/", "/index.html"}:
            payload = library_html(self.root, self.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def reply_json(self, status: int, data: dict):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path not in {"/api/delete", "/api/restore"}:
            self.reply_json(404, {"error": "找不到此操作。"})
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        provided = self.headers.get("X-Practice-Token", "")
        if (
            not hmac.compare_digest(provided.encode(), self.token.encode())
            or self.headers.get("Origin") != expected_origin
            or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
        ):
            self.reply_json(403, {"error": "請從本機練習庫頁面操作。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise PracticeError("請求內容無效。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise PracticeError("請求內容必須是 JSON 物件。")
            if self.path == "/api/delete":
                result = delete_episode(self.root, body.get("episode"))
            else:
                result = restore_episode(self.root, body.get("id"))
            update_library(self.root)
            self.reply_json(200, result)
        except (PracticeError, OSError, ValueError, Timeout) as exc:
            self.reply_json(400, {"error": str(exc)})


def library_server(root: Path, port: int) -> ThreadingHTTPServer:
    from functools import partial

    root = root.expanduser().resolve()
    update_library(root)
    handler = partial(LibraryHandler, root=root, token=secrets.token_urlsafe(32))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
