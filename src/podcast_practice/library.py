"""Move episode outputs into a local trash folder and restore them safely."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .errors import PracticeError


def episode_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name:
        raise PracticeError("請指定練習庫內的單集資料夾名稱。")
    path = root / name
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise PracticeError("不能操作練習庫外的資料夾或符號連結。")
    return path


def trash_root(root: Path) -> Path:
    path = root / ".trash"
    if path.is_symlink():
        raise PracticeError("回收區不能是符號連結。")
    return path


def read_episode(path: Path) -> dict:
    manifest = path / "alignment.json"
    if not path.is_dir() or not manifest.is_file() or manifest.is_symlink():
        raise PracticeError("找不到這集的 alignment.json。")
    return json.loads(manifest.read_text(encoding="utf-8"))


def list_trash(root: Path) -> list[dict]:
    rows = []
    for path in trash_root(root).glob("*/metadata.json"):
        if path.is_symlink() or path.parent.is_symlink() or not (path.parent / "episode").is_dir():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["id"] = path.parent.name
            rows.append(data)
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row.get("deleted", ""), reverse=True)


def delete_episode(root: Path, name: str) -> dict:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ".library.lock"), timeout=10):
        source = episode_path(root, name)
        data = read_episode(source)
        trash = trash_root(root)
        trash.mkdir(exist_ok=True)
        identifier = uuid.uuid4().hex
        target = trash / identifier
        target.mkdir()
        record = {
            "id": identifier,
            "directory": name,
            "title": data.get("title", name),
            "deleted": datetime.now(timezone.utc).isoformat(),
        }
        (target / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
        source.rename(target / "episode")
        return record


def restore_episode(root: Path, identifier: str) -> dict:
    root = root.expanduser().resolve()
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise PracticeError("無效的回收區 ID。")
    with FileLock(str(root / ".library.lock"), timeout=10):
        source = trash_root(root) / identifier
        if source.is_symlink() or (source / "episode").is_symlink():
            raise PracticeError("不能復原符號連結。")
        try:
            record = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PracticeError("回收區找不到這集，可能已經復原。") from exc
        destination = episode_path(root, record.get("directory"))
        if destination.exists():
            raise PracticeError("練習庫已有同名資料夾，請先移走它再復原。")
        read_episode(source / "episode")
        (source / "episode").rename(destination)
        (source / "metadata.json").unlink()
        source.rmdir()
        return record
