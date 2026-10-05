import http.client
import json
import threading

import pytest

from podcast_practice.render import export_episode
from podcast_practice.server import byte_range, library_server


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("items=1-3", None),
        ("bytes=0-1,4-5", None),
        ("bytes=0-0", (0, 0)),
        ("bytes=12-", (12, 99)),
        ("bytes=12-200", (12, 99)),
        ("bytes=-5", (95, 99)),
        ("bytes=-200", (0, 99)),
    ],
)
def test_byte_ranges(value, expected):
    assert byte_range(value, 100) == expected


@pytest.mark.parametrize("value", ["bytes=100-", "bytes=9-2", "bytes=-0", "bytes=-", "bytes=no"])
def test_invalid_or_unsatisfiable_ranges(value):
    with pytest.raises(ValueError):
        byte_range(value, 100)
    with pytest.raises(ValueError):
        byte_range("bytes=0-", 0)


@pytest.fixture
def episode_server(tmp_path):
    root = tmp_path / "library"
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(bytes(range(256)) * 4)
    folder = root / "episode"
    data = {
        "title": "A long episode",
        "duration": 10000,
        "sentences": [
            {
                "text": "Hello.",
                "speaker": "Host",
                "start": 8000,
                "end": 8001,
                "words": [{"text": "Hello.", "start": 8000, "end": 8001}],
            }
        ],
        "review": {"warnings": []},
    }
    export_episode(data, audio, folder)
    server = library_server(root, 0, runtime_dir=tmp_path / "runtime")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    yield folder, audio.read_bytes(), request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_player_uses_lightweight_audio_source_and_preserves_portable_file(episode_server):
    folder, _, request = episode_server
    original = (folder / "player.html").read_bytes()
    status, headers, payload = request("/episode/player.html")
    assert status == 200
    assert b'audio.src="audio.mp3"' in payload
    assert b"data:audio/mpeg;base64," not in payload
    assert b"data:audio/mpeg;base64," in original
    assert (folder / "player.html").read_bytes() == original
    assert headers["Cache-Control"] == "no-store"
    assert int(headers["Content-Length"]) == len(payload)
    assert b'"start":8000' in payload
    assert json.loads((folder / "alignment.json").read_text())["sentences"][0]["start"] == 8000
    status, head, body = request("/episode/player.html", method="HEAD")
    assert status == 200 and not body
    assert head["Content-Length"] == headers["Content-Length"]


@pytest.mark.parametrize(
    ("requested", "start", "end"),
    [("bytes=300-399", 300, 399), ("bytes=900-", 900, 1023), ("bytes=-25", 999, 1023)],
)
def test_audio_seeks_return_only_requested_bytes(episode_server, requested, start, end):
    _, audio, request = episode_server
    status, headers, payload = request("/episode/audio.mp3", {"Range": requested})
    assert status == 206
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Range"] == f"bytes {start}-{end}/{len(audio)}"
    assert payload == audio[start : end + 1]
    assert int(headers["Content-Length"]) == len(payload)
    status, head, body = request("/episode/audio.mp3", {"Range": requested}, method="HEAD")
    assert status == 206 and not body
    assert head["Content-Range"] == headers["Content-Range"]
    assert head["Content-Length"] == headers["Content-Length"]


def test_audio_ranges_handle_invalid_ranges_and_changed_resources(episode_server):
    _, audio, request = episode_server
    status, headers, payload = request("/episode/audio.mp3")
    assert status == 200 and payload == audio
    assert headers["Accept-Ranges"] == "bytes"
    for if_range in [headers["ETag"], headers["Last-Modified"]]:
        status, _, payload = request(
            "/episode/audio.mp3", {"Range": "bytes=4-7", "If-Range": if_range}
        )
        assert status == 206 and payload == audio[4:8]
    status, _, payload = request(
        "/episode/audio.mp3", {"Range": "bytes=4-7", "If-Range": '"old-version"'}
    )
    assert status == 200 and payload == audio
    status, headers, payload = request("/episode/audio.mp3", {"Range": "bytes=1024-"})
    assert status == 416 and not payload
    assert headers["Content-Range"] == "bytes */1024"
    for method in ["GET", "HEAD"]:
        assert request("/%2e/episode/audio.mp3", method=method)[0] == 404
