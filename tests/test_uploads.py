import http.client
import io
import json
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from podcast_practice import cli
from podcast_practice import uploads as uploads_module
from podcast_practice.errors import PracticeError
from podcast_practice.render import update_library
from podcast_practice.runtime import Runtime
from podcast_practice.server import library_server
from podcast_practice.uploads import (
    MAX_AUDIO_BYTES,
    MAX_PUBLIC_AUDIO_BYTES,
    Uploads,
    upload_metadata,
    validate_audio,
)


def metadata(audio=b"MP3 bytes", transcript=b"Hello, world.", **changes):
    return {
        "title": "New episode",
        "audio_name": "recording.mp3",
        "audio_size": len(audio),
        "transcript_name": "recording.txt",
        "transcript_size": len(transcript),
        **changes,
    }


@pytest.fixture
def ready_runtime(monkeypatch):
    monkeypatch.setattr(Runtime, "require_ready", lambda self: None)
    monkeypatch.setattr(uploads_module, "validate_audio", lambda *_: None)


@pytest.fixture
def fake_build(monkeypatch):
    options = {"code": 0, "incomplete": False, "gate": None}
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append((command, kwargs))
            self.returncode = None
            self.stdout = self.lines(command)

        def lines(self, command):
            yield "原稿：1 句。\n"
            if options["gate"]:
                options["gate"].wait(timeout=5)
            yield "MFA 第二輪：按自動搜尋範圍分句對齊…\n"
            if options["code"]:
                yield "錯誤：這不是有效的 MP3。\n"
                return
            root = Path(command[command.index("--library") + 1])
            folder = root / "new-episode"
            folder.mkdir(exist_ok=True)
            for name in [
                "player.html",
                "audio.mp3",
                "transcript.txt",
                "subtitles.srt",
                "subtitles.vtt",
                "alignment.TextGrid",
                "review.json",
                "windows.json",
            ]:
                if name != "player.html" or not options["incomplete"]:
                    (folder / name).write_text("test output")
            (folder / "alignment.json").write_text(
                json.dumps(
                    {
                        "title": "New episode",
                        "created": "2026-10-05",
                        "duration": 1,
                        "sentences": [{"text": "Hello, world."}],
                    }
                )
            )
            result = Path(command[command.index("--result-json") + 1])
            result.write_text(json.dumps({"directory": str(folder)}))
            update_library(root)
            yield "完成：1 句。\n"

        def wait(self, timeout=None):
            self.returncode = options["code"]
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr("podcast_practice.uploads.subprocess.Popen", Process)
    return options, commands


@pytest.fixture
def running_server(tmp_path, ready_runtime):
    server = library_server(tmp_path / "library", 0, runtime_dir=tmp_path / "runtime")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    _, page = request("GET", "/")
    config = json.loads(re.search(rb'<script id="library-config"[^>]*>(.*?)</script>', page)[1])
    headers = {
        "Content-Type": "application/octet-stream",
        "Origin": f"http://127.0.0.1:{server.server_port}",
        "X-Practice-Token": config["token"],
        "X-Practice-Upload": quote(json.dumps(metadata())),
    }
    yield server, request, headers, config
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_upload_build_status_and_reload_use_existing_offline_cli(running_server, fake_build):
    server, request, headers, config = running_server
    audio, transcript = b"MP3 bytes", b"Hello, world."
    status, payload = request("POST", "/api/upload", audio + transcript, headers)
    assert status == 202
    identifier = json.loads(payload)["id"]
    server.uploads._thread.join(timeout=5)
    status, payload = request(
        "GET",
        f"/api/uploads/{identifier}",
        headers={
            "X-Practice-Token": config["token"],
        },
    )
    job = json.loads(payload)
    assert status == 200 and job["state"] == "completed"
    assert job["player"] == "new-episode/player.html"
    assert "MFA 第二輪" in job["log"]
    command, kwargs = fake_build[1][0]
    assert "--offline" in command and "--no-asr" not in command
    assert "--force" not in command and "--allow-low-confidence" not in command
    assert command[command.index("--runtime-dir") + 1] == str(server.uploads.runtime_dir)
    assert command[command.index("--library") + 1] == str(server.uploads.library)
    assert kwargs["cwd"] == Path.cwd()
    assert (server.uploads.root / identifier / "audio.mp3").read_bytes() == audio
    assert (server.uploads.root / identifier / "transcript.txt").read_bytes() == transcript
    _, page = request("GET", "/")
    assert b"new-episode/player.html" in page
    assert server.uploads.config()["job"] == identifier


def test_upload_and_status_require_local_token_and_hide_saved_inputs(running_server, fake_build):
    server, request, headers, _ = running_server
    body = b"MP3 bytesHello, world."
    for changes in [
        {"X-Practice-Token": "wrong"},
        {"Origin": "https://example.com"},
        {"Content-Type": "application/json"},
    ]:
        assert request("POST", "/api/upload", body, {**headers, **changes})[0] == 403
    assert not server.uploads.root.exists()
    status, payload = request("POST", "/api/upload", body, headers)
    assert status == 202
    identifier = json.loads(payload)["id"]
    server.uploads._thread.join(timeout=5)
    assert request("GET", f"/api/uploads/{identifier}")[0] == 403
    assert request("GET", f"/.uploads/{identifier}/audio.mp3")[0] == 404
    assert request("GET", f"/%2euploads/{identifier}/job.json")[0] == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"audio_name": "../recording.mp3"},
        {"transcript_name": "a\\recording.txt"},
        {"audio_name": "recording.exe"},
        {"transcript_name": "script.html"},
        {"audio_size": 0},
        {"audio_size": True},
        {"audio_size": MAX_AUDIO_BYTES + 1},
        {"transcript_size": -1},
        {"transcript_size": 6 * 1024 * 1024},
        {"title": "A\nB"},
        {"title": "x" * 201},
        {"title": None},
    ],
)
def test_reject_invalid_metadata_before_writing(changes):
    with pytest.raises(PracticeError):
        upload_metadata(metadata(**changes), len(b"MP3 bytesHello, world."))


def test_stream_boundaries_unicode_names_and_title_default():
    data = metadata(audio_name="今天的節目.MP3", transcript_name="今天的原稿.SRT", title="")
    assert upload_metadata(data, 22)["title"] == "今天的節目"
    with pytest.raises(PracticeError, match="大小"):
        upload_metadata(data, 21)


@pytest.mark.parametrize("transcript", [b"\xff\xfe", b"[MUSIC PLAYING]"])
def test_invalid_transcript_cleans_partial_upload_and_allows_retry(
    tmp_path, ready_runtime, transcript
):
    root = tmp_path / "library"
    root.mkdir()
    uploads = Uploads(root, tmp_path / "runtime")
    audio = b"MP3 bytes"
    with pytest.raises(PracticeError):
        uploads.receive(
            metadata(audio, transcript), io.BytesIO(audio + transcript), len(audio + transcript)
        )
    assert list(uploads.root.iterdir()) == []
    assert uploads.config()["job"] is None


def test_truncated_upload_does_not_leave_a_busy_job(tmp_path, ready_runtime):
    root = tmp_path / "library"
    root.mkdir()
    uploads = Uploads(root, tmp_path / "runtime")
    with pytest.raises(PracticeError, match="上傳中斷"):
        uploads.receive(metadata(), io.BytesIO(b"MP3"), 22)
    assert uploads._active is None
    assert list(uploads.root.iterdir()) == []


def test_second_upload_is_rejected_while_a_build_runs(running_server, fake_build):
    server, request, headers, _ = running_server
    gate = fake_build[0]["gate"] = threading.Event()
    try:
        assert request("POST", "/api/upload", b"MP3 bytesHello, world.", headers)[0] == 202
        status, payload = request("POST", "/api/upload", b"MP3 bytesHello, world.", headers)
        assert status == 409 and "另一集" in json.loads(payload)["error"]
    finally:
        gate.set()
        server.uploads._thread.join(timeout=5)


@pytest.mark.parametrize("options", [{"code": 2}, {"incomplete": True}])
def test_failed_or_incomplete_build_is_not_reported_as_completed(
    running_server, fake_build, options
):
    server, request, headers, _ = running_server
    fake_build[0].update(options)
    status, payload = request("POST", "/api/upload", b"MP3 bytesHello, world.", headers)
    assert status == 202
    identifier = json.loads(payload)["id"]
    server.uploads._thread.join(timeout=5)
    job = server.uploads.status(identifier)
    assert job["state"] == "failed" and "player" not in job
    assert "message" in job and "log" in job
    assert server.uploads._active is None


def test_service_restart_recovers_failure_and_never_claims_interrupted_success(tmp_path):
    root = tmp_path / "library"
    identifier = "a" * 32
    folder = root / ".uploads" / identifier
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps({"title": "Interrupted", "state": "running"}))
    uploads = Uploads(root, tmp_path / "runtime")
    assert uploads.config()["job"] == identifier
    assert uploads.status(identifier)["state"] == "failed"
    assert "中斷" in uploads.status(identifier)["message"]


def test_result_json_is_written_only_after_cli_build_success(tmp_path, monkeypatch):
    destination = tmp_path / "library" / "one"
    result = tmp_path / "result.json"
    monkeypatch.setattr(cli, "build", lambda *_: destination)
    args = [
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "build",
        "one.mp3",
        "one.txt",
        "--result-json",
        str(result),
    ]
    assert cli.main(args) == 0
    assert json.loads(result.read_text())["directory"] == str(destination)
    result.unlink()

    def fail(*_):
        raise PracticeError("Test build failed")

    monkeypatch.setattr(cli, "build", fail)
    assert cli.main(args) == 2
    assert not result.exists()


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "file:///tmp",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com/?query=1",
        "https://example.com/#fragment",
        "https://exa mple.com",
    ],
)
def test_public_origin_rejects_wildcards_credentials_and_non_origin_urls(tmp_path, origin):
    with pytest.raises(PracticeError, match="public-origin"):
        library_server(tmp_path / "library", 0, public_origin=origin)


def test_https_reverse_proxy_origin_requires_explicit_configuration(
    tmp_path, ready_runtime, fake_build
):
    server = library_server(
        tmp_path / "library",
        0,
        runtime_dir=tmp_path / "runtime",
        public_origin="https://podcast.example.com/",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        connection.request("GET", "/")
        page = connection.getresponse().read()
        config = json.loads(re.search(rb'<script id="library-config"[^>]*>(.*?)</script>', page)[1])
        assert "https://podcast.example.com" in config["upload"]["origins"]
        assert "https://podcast.example.com" not in server.origins
        assert config["upload"]["max_public_audio_bytes"] == MAX_PUBLIC_AUDIO_BYTES
        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "https://podcast.example.com",
            "X-Practice-Token": config["token"],
            "X-Practice-Upload": quote(json.dumps(metadata())),
        }
        connection.request("POST", "/api/upload", b"MP3 bytesHello, world.", headers)
        response = connection.getresponse()
        assert response.status == 202
        response.read()
        server.uploads._thread.join(timeout=5)
        connection.request(
            "POST", "/api/delete", b"{}", {**headers, "Content-Type": "application/json"}
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        oversized = metadata(audio_size=MAX_PUBLIC_AUDIO_BYTES + 1)
        connection.request(
            "POST",
            "/api/upload",
            b"MP3 bytesHello, world.",
            {**headers, "X-Practice-Upload": quote(json.dumps(oversized))},
        )
        response = connection.getresponse()
        assert response.status == 400
        assert "90 MB" in json.loads(response.read())["error"]
        assert len(list(server.uploads.root.iterdir())) == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_public_audio_limit_is_enforced_before_receiving_files():
    data = metadata(audio_size=MAX_PUBLIC_AUDIO_BYTES + 1)
    with pytest.raises(PracticeError, match="90 MB"):
        upload_metadata(data, data["audio_size"] + data["transcript_size"], MAX_PUBLIC_AUDIO_BYTES)
    assert upload_metadata(data, data["audio_size"] + data["transcript_size"])


def test_invalid_mp3_cleans_inputs_without_starting_a_build(tmp_path, ready_runtime, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    uploads = Uploads(root, tmp_path / "runtime")

    def invalid(*_):
        raise PracticeError("音檔不是有效的 MP3")

    monkeypatch.setattr(uploads_module, "validate_audio", invalid)
    with pytest.raises(PracticeError, match="有效的 MP3"):
        uploads.receive(metadata(), io.BytesIO(b"MP3 bytesHello, world."), 22)
    assert uploads._thread is None
    assert uploads._active is None
    assert list(uploads.root.iterdir()) == []


@pytest.mark.parametrize("codec", ["mp3", "pcm_s16le", None])
def test_mp3_probe_rejects_non_mp3_content_and_disallows_network_protocols(
    tmp_path, monkeypatch, codec
):
    runtime = Runtime(tmp_path / "runtime")
    monkeypatch.setattr(runtime, "environment", lambda: {})
    commands = []

    def probe(command, **kwargs):
        commands.append(command)
        streams = [{"codec_name": codec}] if codec else []
        return subprocess.CompletedProcess(command, 0, json.dumps({"streams": streams}))

    monkeypatch.setattr(uploads_module.subprocess, "run", probe)
    if codec == "mp3":
        validate_audio(runtime, tmp_path / "input.mp3")
    else:
        with pytest.raises(PracticeError, match="有效的 MP3"):
            validate_audio(runtime, tmp_path / "input.mp3")
    command = commands[0]
    assert command[command.index("-f") + 1] == "mp3"
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"


def test_stopping_service_terminates_build_process_and_marks_failure(
    tmp_path, ready_runtime, monkeypatch
):
    root = tmp_path / "library"
    root.mkdir()
    uploads = Uploads(root, tmp_path / "runtime")
    real_popen = subprocess.Popen
    processes = []
    ready = threading.Event()
    real_update = uploads._update

    def update(identifier, **changes):
        real_update(identifier, **changes)
        if changes.get("stage") == "第一次語音對齊":
            ready.set()

    def start_process(command, **kwargs):
        child = real_popen(
            [command[0], "-u", "-c", "import time; print('MFA 第一輪'); time.sleep(60)"],
            **kwargs,
        )
        processes.append(child)
        return child

    monkeypatch.setattr(uploads, "_update", update)
    monkeypatch.setattr(uploads_module.subprocess, "Popen", start_process)
    job = uploads.receive(metadata(), io.BytesIO(b"MP3 bytesHello, world."), 22)
    try:
        assert ready.wait(timeout=5)
    finally:
        uploads.close()
    assert processes[0].poll() is not None
    assert uploads.status(job["id"])["state"] == "failed"
    assert uploads._active is None
