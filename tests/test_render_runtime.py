import base64
import json

import pytest

from podcast_practice.errors import PracticeError
from podcast_practice.render import export_episode, timestamp, update_library
from podcast_practice.runtime import Runtime, conda_platform


def test_linux_and_mac_platforms_are_independent_of_developer_machine():
    assert conda_platform("Linux", "x86_64") == "linux-64"
    assert conda_platform("Darwin", "arm64") == "osx-arm64"
    assert conda_platform("Darwin", "x86_64") == "osx-64"
    with pytest.raises(PracticeError, match="Linux x86_64"):
        conda_platform("Linux", "aarch64")


def test_export_escapes_title_and_script_content_and_preserves_audio(tmp_path):
    audio = tmp_path / "input.mp3"
    audio.write_bytes(b"a small fake audio payload for renderer testing")
    title = '</script><script>alert("oops")</script>'
    sentence = {
        "id": 0,
        "speaker": "Guest",
        "paragraph": 0,
        "text": "Hello, world.",
        "start": 0.5,
        "end": 1.7,
        "words": [
            {"text": "Hello,", "start": 0.5, "end": 1},
            {"text": "world.", "start": 1.1, "end": 1.7},
        ],
    }
    data = {
        "title": title,
        "created": "2026-01-01",
        "duration": 2.0,
        "sentences": [sentence],
        "review": {"warnings": []},
    }
    folder = tmp_path / "library" / "one"
    export_episode(data, audio, folder)
    content = (folder / "player.html").read_text()
    assert "&lt;/script&gt;" in content
    assert "<\\/script>" in content
    assert base64.b64encode(audio.read_bytes()).decode() in content
    assert "__TITLE__" not in content
    assert "FT News Briefing" not in content
    assert json.loads((folder / "alignment.json").read_text())["title"] == title
    assert "00:00:00,500 --> 00:00:01,700" in (folder / "subtitles.srt").read_text()
    update_library(folder.parent)
    index = (folder.parent / "index.html").read_text()
    assert "one/player.html" in index
    assert title not in index


def test_runtime_does_not_write_to_default_mfa_documents(tmp_path):
    runtime = Runtime(tmp_path / "runtime")
    env = runtime.environment()
    assert env["MFA_ROOT_DIR"] == str(tmp_path / "runtime" / "mfa-state")
    assert env["MAMBA_ROOT_PREFIX"] == str(tmp_path / "runtime" / "conda")


def test_timestamp_rounds_across_seconds_and_minutes():
    assert timestamp(59.9998, comma=True) == "00:01:00,000"
    assert timestamp(3600.123) == "01:00:00.123"
