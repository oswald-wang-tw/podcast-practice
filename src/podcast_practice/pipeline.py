"""Reusable daily pipeline. No podcast-specific names, timestamps or paths."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from praatio import textgrid

from . import __version__
from .alignment import quality_report, restore
from .anchors import search_windows, transcribe
from .errors import PracticeError
from .render import export_episode, update_library
from .runtime import MODELS, Runtime
from .transcript import Transcript, parse_transcript


@dataclass
class BuildOptions:
    audio: Path
    transcript: Path
    title: str | None = None
    output: Path | None = None
    library: Path = Path("library")
    speakers: tuple[str, ...] = ()
    asr_model: str | None = None
    no_asr: bool = False
    offline: bool = False
    force: bool = False
    windows: Path | None = None
    allow_low_confidence: bool = False


def fingerprint(paths: list[Path], settings: dict) -> str:
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True).encode())
    for path in paths:
        digest.update(b"\0file\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()[:16]


def slugify(title: str) -> str:
    return re.sub(r"[^\w-]+", "-", title, flags=re.UNICODE).strip("-_")[:70] or "episode"


def write_segments(
    path: Path, transcript: Transcript, windows: list[dict], duration: float
) -> None:
    entries = []
    for sentence, window in zip(transcript.sentences, windows, strict=True):
        first, last = sentence.words[0].norm_start, sentence.words[-1].norm_end
        entries.append(
            (window["begin"], window["end"], " ".join(transcript.normalized[first:last]))
        )
    grid = textgrid.Textgrid()
    grid.addTier(textgrid.IntervalTier("speech", entries, 0, duration))
    grid.save(str(path), format="long_textgrid", includeBlankSpaces=True)


def mfa_align(
    runtime: Runtime,
    audio: Path,
    text: Path,
    dictionary: Path,
    result: Path,
    job: Path,
    *,
    clean: bool,
) -> dict:
    if not result.exists():
        runtime.run(
            [
                runtime.mfa,
                "align_one",
                audio,
                text,
                dictionary,
                MODELS["acoustic"],
                result,
                "--no_tokenization",
                "--output_format",
                "json",
                "--beam",
                "60",
                "--retry_beam",
                "200",
                "--no_use_mp",
                "--clean" if clean else "--no_clean",
            ],
            log=job / "mfa.log",
        )
    return json.loads(result.read_text(encoding="utf-8"))


def build(runtime: Runtime, options: BuildOptions) -> Path:
    runtime.require_ready()
    audio, transcript_path = (
        options.audio.expanduser().resolve(),
        options.transcript.expanduser().resolve(),
    )
    for path in [audio, transcript_path] + ([options.windows] if options.windows else []):
        if not path.is_file():
            raise PracticeError(f"找不到檔案：{path}")
    transcript = parse_transcript(transcript_path, set(options.speakers))
    title = options.title or audio.stem
    model = options.asr_model or runtime.config.get("asr_model", "base.en")
    settings = {
        "version": __version__,
        "title": title,
        "speakers": options.speakers,
        "asr_model": model,
        "no_asr": options.no_asr,
        "allow_low_confidence": options.allow_low_confidence,
    }
    key = fingerprint(
        [audio, transcript_path] + ([options.windows] if options.windows else []), settings
    )
    destination = (options.output or options.library / f"{slugify(title)}-{key[:8]}").resolve()
    manifest_path = destination / "alignment.json"
    if manifest_path.exists() and not options.force:
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("fingerprint") == key and (destination / "player.html").exists():
            print(f"這集已完成，直接使用：{destination / 'player.html'}", flush=True)
            return destination
        raise PracticeError("輸出目錄已有另一個結果。請使用不同 --output，或加 --force 重建。")
    if destination.exists() and any(destination.iterdir()) and not options.force:
        raise PracticeError("輸出目錄不是空的。請選擇新目錄，或加 --force。")
    job = runtime.root / "jobs" / key
    job.mkdir(parents=True, exist_ok=True)
    (job / "transcript-map.json").write_text(
        json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2)
    )
    normalized = job / "episode.lab"
    normalized.write_text(" ".join(transcript.normalized) + "\n", encoding="utf-8")
    print(f"原稿：{len(transcript.sentences)} 句。工作目錄：{job}", flush=True)
    wav = job / "episode.wav"
    if not wav.exists():
        print("轉換 MFA 使用的音訊…", flush=True)
        runtime.run(
            [
                runtime.bin / "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                audio,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                wav,
            ],
            log=job / "ffmpeg.log",
        )
    with wave.open(str(wav), "rb") as source:
        duration = source.getnframes() / source.getframerate()
    print(f"音檔長度：{duration / 60:.1f} 分鐘。", flush=True)
    dictionary = runtime.lexicon(transcript.normalized, job)
    supplied_times = all(sentence.supplied_start is not None for sentence in transcript.sentences)
    if supplied_times:
        initial = [{"start": s.supplied_start, "end": s.supplied_end} for s in transcript.sentences]
    else:
        print("MFA 第一輪：原稿與整集對齊…", flush=True)
        raw_initial = mfa_align(
            runtime, wav, normalized, dictionary, job / "initial-mfa.json", job, clean=True
        )
        initial = restore(transcript, raw_initial)
    recognized = None
    if not options.no_asr and not supplied_times:
        recognized = transcribe(runtime, wav, job, model, offline=options.offline)
    overrides = json.loads(options.windows.read_text(encoding="utf-8")) if options.windows else None
    if overrides is not None and not isinstance(overrides, list):
        raise PracticeError("--windows 的 JSON 最外層必須是陣列。")
    windows, coverage = search_windows(transcript, initial, duration, recognized, overrides)
    (job / "windows.json").write_text(json.dumps(windows, ensure_ascii=False, indent=2))
    if coverage is not None:
        print(f"原稿與本地辨識的粗定位吻合率：{coverage:.0%}", flush=True)
        if coverage < 0.35 and not options.allow_low_confidence:
            raise PracticeError(
                "原稿和音檔的吻合率低於 35%，停止產生可能錯位的播放器。"
                "請確認是同一集；若原稿經過大幅編輯，可用 --allow-low-confidence 繼續並人工核對。"
            )
    segments_path = job / "episode.TextGrid"
    write_segments(segments_path, transcript, windows, duration)
    print("MFA 第二輪：按自動搜尋範圍分句對齊…", flush=True)
    raw_final = mfa_align(
        runtime, wav, segments_path, dictionary, job / "refined-mfa.json", job, clean=supplied_times
    )
    sentences = restore(transcript, raw_final)
    report = quality_report(raw_final, sentences, windows, coverage)
    if supplied_times:
        report["warnings"] = [w for w in report["warnings"] if w["type"] != "no_asr_check"]
    mp3 = job / "audio.mp3"
    if not mp3.exists():
        if audio.suffix.lower() == ".mp3":
            shutil.copyfile(audio, mp3)
        else:
            runtime.run(
                [
                    runtime.bin / "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    audio,
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    mp3,
                ],
                log=job / "ffmpeg.log",
            )
    # The encoded original/converted MP3 can differ by a few milliseconds from the analysis WAV.
    probe = subprocess.run(
        [
            str(runtime.bin / "ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(mp3),
        ],
        env=runtime.environment(),
        capture_output=True,
        text=True,
        check=True,
    )
    playback_duration = float(json.loads(probe.stdout)["format"]["duration"])
    metadata = {
        "title": title,
        "created": date.today().isoformat(),
        "fingerprint": key,
        "duration": playback_duration,
        "source": transcript_path.name,
        "source_audio": audio.name,
        "sentences": sentences,
        "method": {
            "aligner": "Montreal Forced Aligner",
            "version": runtime.config.get("mfa_version", "3.4.2"),
            "acoustic_model": MODELS["acoustic"],
            "dictionary": MODELS["dictionary"],
            "g2p_model": MODELS["g2p"],
            "coarse_model": model if recognized is not None else None,
            "timestamps": "All final word timestamps are produced by MFA.",
        },
        "review": report,
    }
    destination.mkdir(parents=True, exist_ok=True)
    export_episode(metadata, mp3, destination)
    (destination / "windows.json").write_text(json.dumps(windows, ensure_ascii=False, indent=2))
    if options.output is None:
        update_library(options.library.resolve())
    print(f"完成：{len(sentences)} 句；{len(report['warnings'])} 項建議核對。", flush=True)
    print(f"播放器：{destination / 'player.html'}", flush=True)
    return destination
