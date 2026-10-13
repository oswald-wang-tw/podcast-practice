"""Exercise native setup and executable discovery on every supported platform."""

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

from podcast_practice import runtime as runtime_module
from podcast_practice.errors import PracticeError
from podcast_practice.runtime import (
    MFA_VERSION,
    MICROMAMBA_VERSION,
    MODELS,
    Runtime,
    conda_platform,
)


@pytest.mark.parametrize(
    ("system", "machine", "subdir"),
    [
        ("Windows", "AMD64", "win-64"),
        ("Windows", "x86_64", "win-64"),
        ("Linux", "x86_64", "linux-64"),
        ("Darwin", "arm64", "osx-arm64"),
        ("Darwin", "x86_64", "osx-64"),
    ],
)
def test_native_platforms(system, machine, subdir):
    assert conda_platform(system, machine) == subdir


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Windows", "ARM64"), ("Windows", "x86"), ("Linux", "aarch64"), ("FreeBSD", "amd64")],
)
def test_unsupported_platforms_fail_before_installation(system, machine):
    with pytest.raises(PracticeError, match="Windows x86_64"):
        conda_platform(system, machine)


@pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin"])
def test_executables_and_environment_match_conda_layout(tmp_path, monkeypatch, system):
    monkeypatch.setattr(runtime_module.platform, "system", lambda: system)
    runtime = Runtime(tmp_path / "runtime with spaces")
    env = runtime.environment()
    paths = env["PATH"].split(os.pathsep)
    if system == "Windows":
        assert runtime.mfa == runtime.prefix / "Scripts" / "mfa.exe"
        assert runtime.ffmpeg == runtime.prefix / "Library" / "bin" / "ffmpeg.exe"
        assert runtime.ffprobe == runtime.bin / "ffprobe.exe"
        assert paths[:6] == [
            str(runtime.prefix),
            str(runtime.prefix / "Library" / "mingw-w64" / "bin"),
            str(runtime.prefix / "Library" / "usr" / "bin"),
            str(runtime.bin),
            str(runtime.prefix / "Scripts"),
            str(runtime.prefix / "bin"),
        ]
    else:
        assert runtime.mfa == runtime.prefix / "bin" / "mfa"
        assert runtime.ffmpeg == runtime.bin / "ffmpeg"
        assert runtime.ffprobe == runtime.bin / "ffprobe"
        assert paths[0] == str(runtime.bin)
        assert env["LC_ALL"] == ("C.UTF-8" if system == "Linux" else "en_US.UTF-8")
    assert env["CONDA_PREFIX"] == str(runtime.prefix)
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["MFA_ROOT_DIR"] == str(runtime.state)


def seed_runtime(runtime):
    for path in (runtime.mfa, runtime.ffmpeg, runtime.ffprobe):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake executable")
    for kind in MODELS:
        path = runtime.model_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake model")


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_ready_requires_mfa_ffmpeg_ffprobe_and_models(tmp_path, monkeypatch, system):
    monkeypatch.setattr(runtime_module.platform, "system", lambda: system)
    runtime = Runtime(tmp_path / "runtime")
    with pytest.raises(PracticeError, match="尚未安裝"):
        runtime.require_ready()
    seed_runtime(runtime)
    runtime.require_ready()
    runtime.ffprobe.unlink()
    with pytest.raises(PracticeError, match="尚未安裝"):
        runtime.require_ready()
    runtime.ffprobe.write_bytes(b"fake probe")
    runtime.model_path("g2p").unlink()
    with pytest.raises(PracticeError, match="缺少模型"):
        runtime.require_ready()


@pytest.mark.parametrize(
    ("system", "machine", "subdir", "member"),
    [
        ("Windows", "AMD64", "win-64", "Library/bin/micromamba.exe"),
        ("Linux", "x86_64", "linux-64", "bin/micromamba"),
        ("Darwin", "arm64", "osx-arm64", "bin/micromamba"),
    ],
)
def test_setup_downloads_only_platform_binary_and_persists_utf8_config(
    tmp_path, monkeypatch, system, machine, subdir, member
):
    monkeypatch.setattr(runtime_module.platform, "system", lambda: system)
    monkeypatch.setattr(runtime_module.platform, "machine", lambda: machine)
    runtime = Runtime(tmp_path / "聽力 runtime")
    archive = io.BytesIO()
    payload = b"official micromamba binary"
    with tarfile.open(fileobj=archive, mode="w:bz2") as bundle:
        for name in (member, "../unexpected.txt"):
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    urls, commands = [], []

    def download(request, **kwargs):
        urls.append(request.full_url)
        return io.BytesIO(archive.getvalue())

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "create":
            seed_runtime(runtime)

    monkeypatch.setattr(runtime_module.urllib.request, "urlopen", download)
    monkeypatch.setattr(runtime, "run", run)
    runtime.install(skip_asr=True)
    binary = runtime.root / "bin" / Path(member).name
    assert binary.read_bytes() == payload
    assert not (tmp_path / "unexpected.txt").exists()
    assert urls == [f"https://micro.mamba.pm/api/micromamba/{subdir}/{MICROMAMBA_VERSION}"]
    assert commands[0][0] == binary
    assert f"montreal-forced-aligner={MFA_VERSION}" in commands[0]
    assert commands[1] == [runtime.mfa, "version"]
    restored = Runtime(runtime.root)
    assert restored.prefix == runtime.prefix
    assert restored.state == runtime.state
    assert json.loads(runtime.config_path.read_text(encoding="utf-8"))["asr_model"] == "base.en"


def test_windows_setup_reuses_existing_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.platform, "system", lambda: "Windows")
    runtime = Runtime(tmp_path / "runtime")
    runtime.prefix = tmp_path / "既有 MFA 環境"
    seed_runtime(runtime)
    prefix = runtime.prefix
    commands = []
    monkeypatch.setattr(runtime, "run", lambda command, **kwargs: commands.append(command))
    runtime.install(mfa_prefix=prefix, skip_asr=True)
    assert commands == [[prefix / "Scripts" / "mfa.exe", "version"]]
    assert not (runtime.root / "micromamba.tar.bz2").exists()
    assert Runtime(runtime.root).prefix == prefix


def test_native_logs_preserve_utf8_with_spaces_in_paths(tmp_path, capsys):
    runtime = Runtime(tmp_path / "runtime with spaces")
    log = runtime.root / "native.log"
    runtime.run([sys.executable, "-c", "print('完成：聽力練習 🎧')"], log=log)
    assert log.read_text(encoding="utf-8") == "完成：聽力練習 🎧\n"
    assert "完成：聽力練習 🎧" in capsys.readouterr().out


def test_failed_native_command_reports_log(tmp_path):
    runtime = Runtime(tmp_path / "runtime")
    log = runtime.root / "native.log"
    with pytest.raises(PracticeError, match="exit 3"):
        runtime.run([sys.executable, "-c", "raise SystemExit(3)"], log=log)


def test_export_and_trash_use_utf8_even_with_a_legacy_default_encoding(tmp_path, monkeypatch):
    from podcast_practice.library import delete_episode, list_trash, restore_episode
    from podcast_practice.render import export_episode

    write_text = Path.write_text

    def legacy_write(path, data, encoding=None, **kwargs):
        return write_text(path, data, encoding=encoding or "cp1252", **kwargs)

    monkeypatch.setattr(Path, "write_text", legacy_write)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"test audio")
    sentence = {
        "id": 0,
        "text": "It’s café time.",
        "start": 0,
        "end": 1,
        "words": [{"text": "It’s café time.", "start": 0, "end": 1}],
    }
    data = {
        "title": "聽力練習 🎧",
        "duration": 2,
        "sentences": [sentence],
        "review": {"warnings": [{"message": "需要核對 🎧"}]},
    }
    folder = tmp_path / "library" / "episode"
    export_episode(data, audio, folder)
    assert (folder / "transcript.txt").read_text(encoding="utf-8").strip() == sentence["text"]
    assert "需要核對 🎧" in (folder / "review.json").read_text(encoding="utf-8")
    record = delete_episode(folder.parent, folder.name)
    assert list_trash(folder.parent)[0]["title"] == data["title"]
    restore_episode(folder.parent, record["id"])
    manifest = json.loads((folder / "alignment.json").read_text(encoding="utf-8"))
    assert manifest["title"] == data["title"]
