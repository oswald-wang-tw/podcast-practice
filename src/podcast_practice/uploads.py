"""Stream local uploads and run the existing offline CLI in a background process."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from .errors import PracticeError
from .runtime import Runtime
from .transcript import parse_transcript

MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_PUBLIC_AUDIO_BYTES = 90 * 1024 * 1024
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
TRANSCRIPT_EXTENSIONS = {".txt", ".md", ".srt", ".vtt"}
STAGES = {
    "原稿：": "讀取原稿",
    "轉換 MFA 使用的音訊": "準備音訊",
    "MFA 第一輪": "第一次語音對齊",
    "找語音、轉場及尾段位置": "核對音訊與原稿",
    "粗定位至": "核對音訊與原稿",
    "粗定位吻合率": "核對音訊與原稿",
    "MFA 第二輪": "逐句語音對齊",
    "完成：": "輸出播放器",
}


class UploadBusy(PracticeError):
    """Another request is uploading or building an episode."""


def upload_metadata(data: dict, length: int, audio_limit: int = MAX_AUDIO_BYTES) -> dict:
    if not isinstance(data, dict):
        raise PracticeError("上傳資料格式無效。")
    for key, extensions, limit in (
        ("audio", {".mp3"}, audio_limit),
        ("transcript", TRANSCRIPT_EXTENSIONS, MAX_TRANSCRIPT_BYTES),
    ):
        name, size = data.get(f"{key}_name"), data.get(f"{key}_size")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or "/" in name
            or "\\" in name
            or any(ord(char) < 32 for char in name)
            or Path(name).suffix.lower() not in extensions
        ):
            raise PracticeError("請提供 MP3 音檔與 .txt、.md、.srt 或 .vtt 英文原稿。")
        if type(size) is not int or not 0 < size <= limit:
            raise PracticeError(
                f"檔案不能是空的；MP3 上限為 {audio_limit // 1024 // 1024} MB，原稿上限為 5 MB。"
            )
    if data["audio_size"] + data["transcript_size"] != length:
        raise PracticeError("上傳檔案大小與請求內容不一致。")
    title = data.get("title")
    if not isinstance(title, str) or len(title) > 200 or any(ord(char) < 32 for char in title):
        raise PracticeError("集數名稱需為 200 字以內的單行文字。")
    return {**data, "title": title.strip() or Path(data["audio_name"]).stem[:200]}


def validate_audio(runtime: Runtime, path: Path) -> None:
    """Require real MP3 data before invoking tools that autodetect input formats."""
    try:
        result = subprocess.run(
            [
                str(runtime.bin / "ffprobe"),
                "-v",
                "error",
                "-f",
                "mp3",
                "-protocol_whitelist",
                "file,pipe",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "json",
                str(path),
            ],
            env=runtime.environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if result.returncode or not streams or streams[0].get("codec_name") != "mp3":
            raise ValueError("Invalid MP3")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise PracticeError("音檔不是有效的 MP3，請確認檔案內容後重試。") from exc


class Uploads:
    def __init__(self, library: Path, runtime_dir: Path):
        self.library = library
        self.runtime_dir = runtime_dir.expanduser().resolve()
        self.cwd = Path.cwd()
        self.root = library / ".uploads"
        self._lock = threading.RLock()
        self._active: str | None = None
        self._last: str | None = None
        self._jobs: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._closing = False
        # Recover the most recent result, and never claim an interrupted build succeeded.
        if not self.root.is_symlink():
            records = sorted(self.root.glob("*/job.json"), key=lambda p: p.stat().st_mtime)
            for path in records:
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    identifier = path.parent.name
                    if not re.fullmatch(r"[0-9a-f]{32}", identifier) or not isinstance(job, dict):
                        continue
                    job["id"] = identifier
                    self._jobs[identifier] = job
                    self._last = identifier
                    if job.get("state") in {"uploading", "running"}:
                        self._update(
                            identifier,
                            state="failed",
                            message="上次建置被服務中斷，請重新選取檔案後重試。",
                        )
                except (OSError, ValueError):
                    continue

    def config(self) -> dict:
        with self._lock:
            return {
                "max_audio_bytes": MAX_AUDIO_BYTES,
                "max_public_audio_bytes": MAX_PUBLIC_AUDIO_BYTES,
                "max_transcript_bytes": MAX_TRANSCRIPT_BYTES,
                "job": self._active or self._last,
            }

    def _update(self, identifier: str, **changes) -> None:
        with self._lock:
            job = self._jobs[identifier]
            job.update(changes)
            temporary = self.root / identifier / "job.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            temporary.replace(temporary.with_name("job.json"))

    def status(self, identifier: str) -> dict:
        with self._lock:
            if identifier not in self._jobs:
                raise PracticeError("找不到這次上傳，請重新整理練習庫。")
            job = dict(self._jobs[identifier])
        log = self.root / identifier / "build.log"
        if log.is_file():
            with log.open("rb") as source:
                source.seek(max(0, log.stat().st_size - 12000))
                job["log"] = source.read().decode("utf-8", errors="replace")
        return job

    def receive(
        self, data: dict, stream: BinaryIO, length: int, *, audio_limit: int = MAX_AUDIO_BYTES
    ) -> dict:
        metadata = upload_metadata(data, length, audio_limit)
        runtime = Runtime(self.runtime_dir)
        runtime.require_ready()
        with self._lock:
            if self._active or self._closing:
                raise UploadBusy("目前正在上傳或建置另一集，請等完成後再新增。")
            if self.root.is_symlink():
                raise PracticeError("上傳目錄不能是符號連結。")
            self.root.mkdir(mode=0o700, exist_ok=True)
            identifier = secrets.token_hex(16)
            folder = self.root / identifier
            folder.mkdir(mode=0o700)
            self._active = self._last = identifier
            self._jobs[identifier] = {
                "id": identifier,
                "title": metadata["title"],
                "state": "uploading",
                "stage": "接收檔案",
                "created": datetime.now(timezone.utc).isoformat(),
            }
            self._update(identifier)
        try:
            audio = folder / "audio.mp3"
            transcript = folder / ("transcript" + Path(metadata["transcript_name"]).suffix.lower())
            for path, size in (
                (audio, metadata["audio_size"]),
                (transcript, metadata["transcript_size"]),
            ):
                with path.open("xb") as target:
                    remaining = size
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise PracticeError("上傳中斷，請重新選取兩個檔案後重試。")
                        target.write(chunk)
                        remaining -= len(chunk)
            try:
                transcript.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError as exc:
                raise PracticeError("英文原稿需使用 UTF-8 編碼。") from exc
            parse_transcript(transcript)
            validate_audio(runtime, audio)
            self._update(identifier, state="running", stage="開始建置")
            self._thread = threading.Thread(
                target=self._build,
                args=(identifier, audio, transcript),
                daemon=True,
            )
            self._thread.start()
            return {"id": identifier}
        except Exception:
            with self._lock:
                self._active = None
                self._jobs.pop(identifier, None)
                self._last = None
            shutil.rmtree(folder)
            raise

    def _build(self, identifier: str, audio: Path, transcript: Path) -> None:
        folder = self.root / identifier
        result = folder / "result.json"
        command = [
            sys.executable,
            "-u",
            "-m",
            "podcast_practice.cli",
            "--runtime-dir",
            str(self.runtime_dir),
            "build",
            str(audio),
            str(transcript),
            "--title",
            self._jobs[identifier]["title"],
            "--library",
            str(self.library),
            "--offline",
            "--result-json",
            str(result),
        ]
        process = None
        try:
            with (folder / "build.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=self.cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
                with self._lock:
                    self._process = process
                    if self._closing:
                        os.killpg(process.pid, signal.SIGTERM)
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    for marker, stage in STAGES.items():
                        if marker in line:
                            self._update(identifier, stage=stage)
                            break
                code = process.wait()
            if code:
                raise PracticeError(
                    "建置隨服務停止而中斷，請重試。"
                    if self._closing
                    else "建置失敗，請查看建置紀錄，確認模型已安裝且音檔與原稿為同一集。"
                )
            output = Path(json.loads(result.read_text(encoding="utf-8"))["directory"])
            if output.is_symlink() or output.resolve().parent != self.library:
                raise PracticeError("建置輸出不在目前練習庫。")
            required = [
                "player.html",
                "audio.mp3",
                "alignment.json",
                "transcript.txt",
                "subtitles.srt",
                "subtitles.vtt",
                "alignment.TextGrid",
                "review.json",
                "windows.json",
            ]
            if any(
                not (output / name).is_file() or not (output / name).stat().st_size
                for name in required
            ):
                raise PracticeError("建置輸出不完整，請查看建置紀錄後重試。")
            alignment = json.loads((output / "alignment.json").read_text(encoding="utf-8"))
            if not alignment.get("sentences"):
                raise PracticeError("建置結果沒有可練習的句子。")
            self._update(
                identifier,
                state="completed",
                stage="建置完成",
                player=f"{quote(output.name)}/player.html",
                message=f"已新增「{self._jobs[identifier]['title']}」。",
            )
        except Exception as exc:
            self._update(identifier, state="failed", message=str(exc))
        finally:
            self._stop(process)
            with self._lock:
                self._active = None
                self._process = None

    @staticmethod
    def _stop(process: subprocess.Popen | None) -> None:
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

    def close(self) -> None:
        with self._lock:
            self._closing = True
            process = self._process
        self._stop(process)
        if self._thread:
            self._thread.join(timeout=5)
