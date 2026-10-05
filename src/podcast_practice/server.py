"""Local library server with uploads, background builds, and trash management."""

from __future__ import annotations

import hmac
import json
import re
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from filelock import Timeout

from .errors import PracticeError
from .library import delete_episode, purge_episode, restore_episode
from .render import library_html, player_html, update_library
from .uploads import MAX_AUDIO_BYTES, MAX_PUBLIC_AUDIO_BYTES, UploadBusy, Uploads


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return one inclusive byte range; ignore unsupported units and multipart ranges."""
    if value is None or not value.startswith("bytes=") or "," in value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Invalid byte range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise ValueError("Empty suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


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
        if self.serve_episode(path):
            return
        super().do_GET()

    def do_HEAD(self):
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404)
            return
        if not self.serve_episode(path, head_only=True):
            super().do_HEAD()

    def serve_episode(self, path: str, *, head_only: bool = False) -> bool:
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[1] not in {"player.html", "audio.mp3"}:
            return False
        folder = (self.root / parts[0]).resolve()
        if folder.parent != self.root or not (folder / parts[1]).is_file():
            return False
        if parts[1] == "player.html":
            alignment = folder / "alignment.json"
            if not alignment.is_file() or not (folder / "audio.mp3").is_file():
                return False
            data = json.loads(alignment.read_text(encoding="utf-8"))
            payload = player_html(data, "audio.mp3").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head_only:
                self.wfile.write(payload)
            return True
        audio = folder / "audio.mp3"
        with audio.open("rb") as source:
            stat = audio.stat()
            size = stat.st_size
            modified = self.date_time_string(stat.st_mtime)
            etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
            requested = self.headers.get("Range")
            if self.headers.get("If-Range") not in {None, modified, etag}:
                requested = None
            try:
                bounds = byte_range(requested, size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            start, end = bounds if bounds else (0, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206 if bounds else 200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", modified)
            self.send_header("ETag", etag)
            if bounds:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not head_only:
                source.seek(start)
                try:
                    while length:
                        chunk = source.read(min(length, 64 * 1024))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        length -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Browsers cancel a previous range when the user seeks again.
        return True

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
