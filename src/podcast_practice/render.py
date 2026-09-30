"""Portable HTML, word timestamps and subtitles for any episode."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import shutil
import textwrap
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

from praatio import textgrid


def timestamp(seconds: float, *, comma: bool = False) -> str:
    value = round(seconds * 1000)
    hours, value = divmod(value, 3600000)
    minutes, value = divmod(value, 60000)
    seconds, value = divmod(value, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}{',' if comma else '.'}{value:03}"


def subtitle_cues(sentences: list[dict]) -> list[list[dict]]:
    cues = []
    for sentence in sentences:
        chunk = []
        for word in sentence["words"]:
            timed = [w for w in chunk if w["start"] is not None]
            if (
                timed
                and word["start"] is not None
                and (
                    len(" ".join(w["text"] for w in chunk)) + len(word["text"]) + 1 > 84
                    or word["end"] - timed[0]["start"] > 6.8
                    or word["start"] - timed[-1]["end"] > 0.9
                )
            ):
                cues.append(chunk)
                chunk = []
            chunk.append(word)
        if any(w["start"] is not None for w in chunk):
            cues.append(chunk)
    return cues


def export_episode(data: dict, audio: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    playback = destination / "audio.mp3"
    if audio.resolve() != playback.resolve():
        shutil.copyfile(audio, playback)
    data["audio_sha256"] = hashlib.sha256(playback.read_bytes()).hexdigest()
    sentences = data["sentences"]
    step = max(1, math.ceil(len(sentences) / 7))
    data["chapters"] = [
        {"title": f"{i // step + 1}. " + " ".join(s["text"].split()[:5]), "sentence": i}
        for i, s in enumerate(sentences)
        if i % step == 0
    ]
    (destination / "alignment.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    (destination / "review.json").write_text(
        json.dumps(data["review"], ensure_ascii=False, indent=2)
    )
    (destination / "transcript.txt").write_text("\n\n".join(s["text"] for s in sentences) + "\n")
    srt, vtt = [], ["WEBVTT", ""]
    for i, chunk in enumerate(subtitle_cues(sentences)):
        timed = [w for w in chunk if w["start"] is not None]
        start, end = timed[0]["start"], timed[-1]["end"]
        text = "\n".join(
            textwrap.wrap(
                " ".join(w["text"] for w in chunk),
                width=43,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
        srt.extend(
            [
                str(i + 1),
                f"{timestamp(start, comma=True)} --> {timestamp(end, comma=True)}",
                text,
                "",
            ]
        )
        vtt.extend([f"{timestamp(start)} --> {timestamp(end)}", text, ""])
    (destination / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
    (destination / "subtitles.vtt").write_text("\n".join(vtt), encoding="utf-8")
    grid = textgrid.Textgrid()
    duration = max(data["duration"], sentences[-1]["end"])
    word_entries = [
        (w["start"], w["end"], w["text"])
        for s in sentences
        for w in s["words"]
        if w["start"] is not None and w["end"] > w["start"]
    ]
    grid.addTier(textgrid.IntervalTier("Original words", word_entries, 0, duration))
    grid.addTier(
        textgrid.IntervalTier(
            "Sentences", [(s["start"], s["end"], s["text"]) for s in sentences], 0, duration
        )
    )
    grid.save(
        str(destination / "alignment.TextGrid"), format="long_textgrid", includeBlankSpaces=True
    )
    render_player(data, playback, destination / "player.html")


def render_player(data: dict, audio: Path, output: Path) -> None:
    template = files("podcast_practice").joinpath("assets/player.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    encoded = base64.b64encode(audio.read_bytes()).decode("ascii")
    result = (
        template.replace("__TITLE__", html.escape(data["title"]))
        .replace("__EPISODE_META__", html.escape(data.get("created", "")))
        .replace("__ALIGNMENT_JSON__", payload)
        .replace("__AUDIO_DATA_URI__", "data:audio/mpeg;base64," + encoded)
    )
    temporary = output.with_suffix(".html.tmp")
    temporary.write_text(result, encoding="utf-8")
    temporary.replace(output)


def update_library(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    episodes = []
    for path in root.glob("*/alignment.json"):
        if not (path.parent / "player.html").is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        episodes.append((data.get("created", ""), path.parent.name, data))
    episodes.sort(reverse=True, key=lambda row: (row[0], row[2].get("title", "")))
    items = []
    for created, directory, data in episodes:
        count = len(data["sentences"])
        items.append(
            f'<a class="episode" href="{quote(directory)}/player.html">'
            f"<strong>{html.escape(data['title'])}</strong>"
            f"<span>{html.escape(created)} · {count} 句 · "
            f"{int(data['duration'] / 60)} 分鐘</span></a>"
        )
    listing = "\n".join(items) or "<p>還沒有練習內容。先用 build 加入一集。</p>"
    (root / "index.html").write_text(
        f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>我的英文聽力練習</title>
<style>body{{background:#fbf7f0;color:#242a2c;font-family:system-ui;max-width:850px;
margin:50px auto;padding:0 24px}}h1{{font-family:Georgia,serif;font-weight:500}}
.episode{{display:block;text-decoration:none;color:inherit;padding:20px;margin:14px 0;
border:1px solid #ddd6cb;border-radius:12px;background:#fffdf9}}.episode:hover{{background:#e8f0eb}}
strong{{font-size:20px;font-weight:600}}span{{display:block;margin-top:9px;color:#657071;font-size:13px}}
</style><h1>我的英文聽力練習</h1><p>每天一集。點選內容開始離線練習。</p>{listing}</html>""",
        encoding="utf-8",
    )
