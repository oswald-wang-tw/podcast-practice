"""Local library server with uploads, background builds, and trash management."""

from __future__ import annotations

import hmac
import json
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from filelock import Timeout

from .errors import PracticeError
from .library import delete_episode, purge_episode, restore_episode
from .render import library_html, update_library
from .uploads import MAX_AUDIO_BYTES, MAX_PUBLIC_AUDIO_BYTES, UploadBusy, Uploads


class LibraryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, root: Path, token: str, uploads: Uploads, **kwargs):
        self.root = root
        self.token = token
        self.uploads = uploads
        super().__init__(*args, directory=str(root), **kwargs)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404)
            return
        if path in {"/", "/index.html"}:
            upload = {**self.uploads.config(), "origins": sorted(self.server.upload_origins)}
            payload = library_html(self.root, self.token, upload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        if path.startswith("/api/uploads/"):
            if not self.valid_token():
                self.reply_json(403, {"error": "請重新整理練習庫頁面後操作。"})
                return
            try:
                self.reply_json(200, self.uploads.status(path.removeprefix("/api/uploads/")))
            except PracticeError as exc:
                self.reply_json(404, {"error": str(exc)})
            return
        super().do_GET()

    def valid_token(self) -> bool:
        provided = self.headers.get("X-Practice-Token", "")
        return hmac.compare_digest(provided.encode(), self.token.encode())

    def reply_json(self, status: int, data: dict):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path not in {"/api/delete", "/api/restore", "/api/purge", "/api/upload"}:
            self.reply_json(404, {"error": "找不到此操作。"})
            return
        content_type = (
            "application/octet-stream" if self.path == "/api/upload" else "application/json"
        )
        origins = self.server.upload_origins if self.path == "/api/upload" else self.server.origins
        if (
            not self.valid_token()
            or self.headers.get("Origin") not in origins
            or self.headers.get("Content-Type", "").split(";")[0] != content_type
        ):
            self.reply_json(403, {"error": "請從已啟用此功能的練習庫頁面操作。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path == "/api/upload":
                self.connection.settimeout(120)
                metadata = self.headers.get("X-Practice-Upload", "")
                if len(metadata) > 12000:
                    raise PracticeError("上傳資料格式無效。")
                audio_limit = (
                    MAX_AUDIO_BYTES
                    if self.headers.get("Origin") in self.server.origins
                    else MAX_PUBLIC_AUDIO_BYTES
                )
                result = self.uploads.receive(
                    json.loads(unquote(metadata)), self.rfile, length, audio_limit=audio_limit
                )
                self.reply_json(202, result)
                return
            if not 0 < length <= 4096:
                raise PracticeError("請求內容無效。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise PracticeError("請求內容必須是 JSON 物件。")
            if self.path == "/api/delete":
                result = delete_episode(self.root, body.get("episode"))
            elif self.path == "/api/restore":
                result = restore_episode(self.root, body.get("id"))
            else:
                result = purge_episode(
                    self.root, body.get("id"), confirmed=body.get("confirm") is True
                )
            update_library(self.root)
            self.reply_json(200, result)
        except UploadBusy as exc:
            self.reply_json(409, {"error": str(exc)})
        except (PracticeError, OSError, ValueError, Timeout) as exc:
            self.reply_json(400, {"error": str(exc)})


class PracticeServer(ThreadingHTTPServer):
    def __init__(self, *args, uploads: Uploads, **kwargs):
        self.uploads = uploads
        super().__init__(*args, **kwargs)

    def server_close(self):
        self.uploads.close()
        super().server_close()


def library_server(
    root: Path, port: int, *, runtime_dir: Path = Path(".runtime"), public_origin: str | None = None
) -> ThreadingHTTPServer:
    from functools import partial

    if public_origin:
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in public_origin)
        ):
            raise PracticeError("--public-origin 需為完整的 HTTP(S) 網址，不包含路徑或登入資訊。")
        public_origin = f"{parsed.scheme}://{parsed.netloc}"
    root = root.expanduser().resolve()
    update_library(root)
    uploads = Uploads(root, runtime_dir)
    handler = partial(LibraryHandler, root=root, token=secrets.token_urlsafe(32), uploads=uploads)
    server = PracticeServer(("127.0.0.1", port), handler, uploads=uploads)
    server.origins = {
        f"http://127.0.0.1:{server.server_port}",
        f"http://localhost:{server.server_port}",
    }
    server.upload_origins = server.origins.copy()
    if public_origin:
        server.upload_origins.add(public_origin)
    return server
