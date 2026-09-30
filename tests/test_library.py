import http.client
import json
import re
import threading

import pytest

from podcast_practice.cli import main
from podcast_practice.errors import PracticeError
from podcast_practice.library import delete_episode, list_trash, purge_episode, restore_episode
from podcast_practice.render import library_html
from podcast_practice.server import library_server


def make_episode(root, name="test-episode", title="Test episode"):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "alignment.json").write_text(
        json.dumps({"title": title, "created": "2026-01-01", "duration": 23, "sentences": []})
    )
    (folder / "player.html").write_text("<html>offline player</html>")
    (folder / "audio.mp3").write_bytes(b"episode audio")
    return folder


def test_delete_and_restore_preserve_entire_episode_and_unrelated_files(tmp_path):
    root = tmp_path / "library"
    folder = make_episode(root)
    original = tmp_path / "original.mp3"
    original.write_bytes(b"original")
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    record = delete_episode(root, folder.name)
    assert not folder.exists()
    assert list_trash(root)[0]["id"] == record["id"]
    assert original.read_bytes() == b"original"
    restore_episode(root, record["id"])
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert list_trash(root) == []


@pytest.mark.parametrize("name", ["../outside", "/tmp", "..", ".trash", "a/b", "a\\b", None])
def test_delete_rejects_paths_outside_single_episode(tmp_path, name):
    root = tmp_path / "library"
    make_episode(root)
    with pytest.raises(PracticeError):
        delete_episode(root, name)
    assert (root / "test-episode" / "audio.mp3").is_file()


def test_delete_rejects_symlinks_and_non_episode_directories(tmp_path):
    root = tmp_path / "library"
    external = make_episode(tmp_path / "outside")
    root.mkdir()
    (root / "linked").symlink_to(external, target_is_directory=True)
    (root / "other").mkdir()
    with pytest.raises(PracticeError, match="符號連結"):
        delete_episode(root, "linked")
    with pytest.raises(PracticeError, match="alignment.json"):
        delete_episode(root, "other")
    assert (external / "audio.mp3").read_bytes() == b"episode audio"


def test_restore_does_not_overwrite_new_episode_and_rejects_tampered_path(tmp_path):
    root = tmp_path / "library"
    folder = make_episode(root)
    record = delete_episode(root, folder.name)
    make_episode(root, title="Replacement")
    with pytest.raises(PracticeError, match="同名"):
        restore_episode(root, record["id"])
    manifest = json.loads((folder / "alignment.json").read_text())
    assert manifest["title"] == "Replacement"
    metadata = root / ".trash" / record["id"] / "metadata.json"
    record["directory"] = "../outside"
    metadata.write_text(json.dumps(record))
    with pytest.raises(PracticeError):
        restore_episode(root, record["id"])
    with pytest.raises(PracticeError):
        restore_episode(root, "../outside")


def test_library_escapes_episode_and_recycled_titles(tmp_path):
    title = '<img src="x" onerror="alert(1)">'
    folder = make_episode(tmp_path, title=title)
    assert title not in library_html(tmp_path)
    delete_episode(tmp_path, folder.name)
    content = library_html(tmp_path)
    assert title not in content
    assert 'data-action="restore"' in content
    assert '"token": null' in content


def test_cli_delete_list_and_restore_work_without_mfa(tmp_path, capsys):
    root = tmp_path / "library"
    folder = make_episode(root)
    assert main(["delete", folder.name, "--library", str(root)]) == 0
    record = list_trash(root)[0]
    assert main(["trash", "--library", str(root)]) == 0
    assert record["id"] in capsys.readouterr().out
    assert main(["restore", record["id"], "--library", str(root)]) == 0
    assert folder.is_dir()


def test_purge_requires_confirmation_and_removes_only_the_selected_trash_entry(tmp_path):
    root = tmp_path / "library"
    first = delete_episode(root, make_episode(root, "first").name)
    second = delete_episode(root, make_episode(root, "second").name)
    original = tmp_path / "original.mp3"
    original.write_bytes(b"keep original")
    cache = tmp_path / ".runtime" / "jobs" / "example"
    cache.mkdir(parents=True)
    (cache / "episode.wav").write_bytes(b"keep cache")
    with pytest.raises(PracticeError, match="確認"):
        purge_episode(root, first["id"])
    assert (root / ".trash" / first["id"] / "episode" / "audio.mp3").is_file()
    purge_episode(root, first["id"], confirmed=True)
    assert not (root / ".trash" / first["id"]).exists()
    assert [row["id"] for row in list_trash(root)] == [second["id"]]
    assert original.read_bytes() == b"keep original"
    assert (cache / "episode.wav").read_bytes() == b"keep cache"
    with pytest.raises(PracticeError, match="找不到"):
        restore_episode(root, first["id"])


@pytest.mark.parametrize("identifier", ["../outside", "/tmp", ".trash", "a" * 31, None])
def test_purge_rejects_invalid_ids(tmp_path, identifier):
    root = tmp_path / "library"
    make_episode(root)
    with pytest.raises(PracticeError, match="ID"):
        purge_episode(root, identifier, confirmed=True)
    assert (root / "test-episode" / "audio.mp3").is_file()


def test_purge_does_not_follow_symlinks_into_original_files(tmp_path):
    root = tmp_path / "library"
    record = delete_episode(root, make_episode(root).name)
    external = tmp_path / "outside"
    external.mkdir()
    protected = external / "original.txt"
    protected.write_text("keep this")
    entry = root / ".trash" / record["id"]
    (entry / "episode" / "linked-folder").symlink_to(external, target_is_directory=True)
    purge_episode(root, record["id"], confirmed=True)
    assert protected.read_text() == "keep this"
    linked = root / ".trash" / ("a" * 32)
    linked.symlink_to(external, target_is_directory=True)
    with pytest.raises(PracticeError, match="符號連結"):
        purge_episode(root, linked.name, confirmed=True)
    assert protected.read_text() == "keep this"


def test_cli_purge_refuses_without_yes_then_removes_the_entry(tmp_path, capsys):
    root = tmp_path / "library"
    record = delete_episode(root, make_episode(root).name)
    args = ["purge", record["id"], "--library", str(root)]
    assert main(args) == 2
    assert "--yes" in capsys.readouterr().err
    assert len(list_trash(root)) == 1
    assert main(args + ["--yes"]) == 0
    assert list_trash(root) == []


def test_server_requires_token_and_origin_then_deletes_and_restores(tmp_path):
    root = tmp_path / "library"
    folder = make_episode(root)
    server = library_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)

    def post(path, body, headers):
        connection.request("POST", path, json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        page = response.read().decode()
        token = json.loads(
            re.search(r'<script id="library-config"[^>]*>(.*?)</script>', page).group(1)
        )["token"]
        origin = f"http://127.0.0.1:{server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}
        assert post("/api/delete", {"episode": folder.name}, headers)[0] == 403
        headers["X-Practice-Token"] = token
        headers["Origin"] = "https://example.com"
        assert post("/api/delete", {"episode": folder.name}, headers)[0] == 403
        assert post("/api/purge", {"id": "a" * 32, "confirm": True}, headers)[0] == 403
        assert folder.is_dir()
        headers["Origin"] = origin
        assert post("/api/delete", {"episode": "../outside"}, headers)[0] == 400
        status, record = post("/api/delete", {"episode": folder.name}, headers)
        assert status == 200
        assert not folder.exists()
        connection.request("GET", f"/.trash/{record['id']}/metadata.json")
        response = connection.getresponse()
        assert response.status == 404
        response.read()
        assert post("/api/restore", {"id": record["id"]}, headers)[0] == 200
        assert folder.is_dir()
        status, record = post("/api/delete", {"episode": folder.name}, headers)
        assert status == 200
        assert post("/api/purge", {"id": record["id"]}, headers)[0] == 400
        assert post("/api/purge", {"id": record["id"], "confirm": "true"}, headers)[0] == 400
        assert len(list_trash(root)) == 1
        assert post("/api/purge", {"id": record["id"], "confirm": True}, headers)[0] == 200
        assert list_trash(root) == []
        assert not (root / ".trash" / record["id"]).exists()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
