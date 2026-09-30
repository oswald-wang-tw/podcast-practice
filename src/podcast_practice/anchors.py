"""Find coarse speech boundaries; final timestamps always come from MFA."""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import PracticeError
from .transcript import Transcript, spoken_tokens

if TYPE_CHECKING:
    from .runtime import Runtime


def load_asr(runtime: Runtime, model: str, *, offline: bool):
    for key, relative in [("HF_HOME", "huggingface"), ("XDG_CACHE_HOME", "cache")]:
        os.environ[key] = str(runtime.root / relative)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
    import onnxruntime

    onnxruntime.disable_telemetry_events()
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(
            model,
            device="cpu",
            compute_type="int8",
            cpu_threads=3,
            download_root=str(runtime.asr_cache),
            local_files_only=offline,
        )
    except Exception as exc:
        raise PracticeError(
            f"無法載入本地辨識模型 {model}。請先執行 setup --asr-model {model}"
            "，或關閉 --offline 以下載模型。"
        ) from exc


def transcribe(
    runtime: Runtime, audio: Path, job: Path, model: str, *, offline: bool
) -> list[dict]:
    cache = job / f"anchors-{model.replace('/', '_')}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    recognizer = load_asr(runtime, model, offline=offline)
    print(f"用本地 {model} 找語音、轉場及尾段位置…", flush=True)
    segments, _ = recognizer.transcribe(
        str(audio),
        language="en",
        beam_size=3,
        word_timestamps=True,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    result = []
    next_progress = 0.0
    for segment in segments:
        for word in segment.words or []:
            result.append(
                {
                    "text": word.word.strip(),
                    "start": word.start,
                    "end": word.end,
                    "probability": word.probability,
                }
            )
        if segment.end >= next_progress:
            print(f"  粗定位至 {segment.end / 60:.1f} 分鐘", flush=True)
            next_progress = segment.end + 60
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def match_anchors(transcript: Transcript, recognized: list[dict]) -> tuple[dict[int, dict], float]:
    tokens, rows = [], []
    for row in recognized:
        for token in spoken_tokens(row["text"]):
            tokens.append(token)
            rows.append(row)
    matcher = difflib.SequenceMatcher(a=transcript.normalized, b=tokens, autojunk=False)
    anchors = {}
    for block in matcher.get_matching_blocks():
        if block.size < min(3, len(transcript.normalized)):
            continue
        for offset in range(block.size):
            row = rows[block.b + offset]
            if row.get("probability", 1) >= 0.2 and 0 < row["end"] - row["start"] <= 1.5:
                anchors[block.a + offset] = row
    return anchors, len(anchors) / len(transcript.normalized)


def search_windows(
    transcript: Transcript,
    initial: list[dict],
    duration: float,
    recognized: list[dict] | None = None,
    overrides: list[dict] | None = None,
) -> tuple[list[dict], float | None]:
    anchors, coverage = (
        match_anchors(transcript, recognized) if recognized is not None else ({}, None)
    )
    windows = []
    for i, (sentence, baseline) in enumerate(zip(transcript.sentences, initial, strict=True)):
        begin = (
            (initial[i - 1]["end"] + baseline["start"]) / 2
            if i
            else max(0, baseline["start"] - 0.4)
        )
        end = (baseline["end"] + initial[i + 1]["start"]) / 2 if i + 1 < len(initial) else duration
        adjusted = False
        start_token, end_token = sentence.words[0].norm_start, sentence.words[-1].norm_end
        first = [
            (k, anchors[k])
            for k in range(start_token, min(end_token, start_token + 6))
            if k in anchors
        ]
        last = [
            (k, anchors[k])
            for k in range(max(start_token, end_token - 6), end_token)
            if k in anchors
        ]
        if len(first) >= 2:
            k, row = first[0]
            speech_start = row["start"] - (k - start_token) * 0.32
            candidate = max(0, speech_start - 0.5)
            if abs(speech_start - baseline["start"]) > 0.9:
                begin, adjusted = candidate, True
        if len(last) >= 2:
            k, row = last[-1]
            speech_end = row["end"] + (end_token - k - 1) * 0.32
            candidate = min(duration, speech_end + 0.5)
            if abs(speech_end - baseline["end"]) > 0.9:
                end, adjusted = candidate, True
        if sentence.supplied_start is not None:
            begin, end, adjusted = sentence.supplied_start, sentence.supplied_end, True
        windows.append(
            {"id": i, "begin": round(begin, 4), "end": round(end, 4), "adjusted": adjusted}
        )
    # Reconcile neighboring windows without introducing overlap; leave actual gaps blank.
    for i in range(1, len(windows)):
        left, right = windows[i - 1], windows[i]
        if left["end"] > right["begin"]:
            if transcript.sentences[i].supplied_start is not None:
                raise PracticeError("提供的字幕搜尋範圍有重疊。")
            boundary = (left["end"] + right["begin"]) / 2
            left["end"] = right["begin"] = round(boundary, 4)
    # Explicit ranges are authoritative: reject overlap rather than silently changing them.
    if overrides:
        for row in overrides:
            try:
                index = row["id"]
                if not isinstance(index, int) or not 0 <= index < len(windows):
                    raise ValueError("invalid sentence id")
                windows[index].update(
                    {"begin": float(row["begin"]), "end": float(row["end"]), "adjusted": True}
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PracticeError(
                    "--windows 檔案需為 [{id, begin, end}, ...]，時間單位是秒。"
                ) from exc
    previous = 0.0
    for window in windows:
        if not (previous <= window["begin"] < window["end"] <= duration + 0.01):
            raise PracticeError(
                f"第 {window['id'] + 1} 句的搜尋範圍不合理。"
                "請檢查原稿或使用 --windows 提供正確範圍。"
            )
        previous = window["end"]
    return windows, coverage
