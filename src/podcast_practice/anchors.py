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


def match_anchors(
    transcript: Transcript, recognized: list[dict], *, min_block: int = 3
) -> tuple[dict[int, dict], float]:
    tokens, rows = [], []
    for row in recognized:
        for token in spoken_tokens(row["text"]):
            tokens.append(token)
            rows.append(row)
    matcher = difflib.SequenceMatcher(a=transcript.normalized, b=tokens, autojunk=False)
    anchors = {}
    for block in matcher.get_matching_blocks():
        if block.size < min(min_block, len(transcript.normalized)):
            continue
        for offset in range(block.size):
            row = rows[block.b + offset]
            if row.get("probability", 1) >= 0.2 and 0 < row["end"] - row["start"] <= 1.5:
                anchors[block.a + offset] = row
    return anchors, len(anchors) / len(transcript.normalized)


def gap_segments(
    transcript: Transcript, recognized: list[dict], duration: float
) -> tuple[list[dict], list[dict]]:
    """Seed MFA with bounded text blocks when reliable anchors expose omitted audio.

    Only cut at sentence boundaries with three consecutive matched tokens on
    each side. ASR locates the blocks; it never supplies the displayed text or
    final word timestamps. Empty TextGrid intervals retain the original audio.
    """
    # Short transition phrases can be separated from the larger matching blocks
    # by an advert (e.g. "Great point." / advert / "Okay, David, ..."). Include
    # them here, but still require three consecutive matched tokens on each side
    # of every cut. The ordinary window corrections retain their stricter blocks.
    anchors, _ = match_anchors(transcript, recognized, min_block=1)
    size = len(transcript.normalized)
    cuts = []
    gaps = []
    begin = 0.0
    end = duration
    if all(k in anchors for k in range(min(3, size))):
        candidate = max(0.0, anchors[0]["start"] - 0.5)
        if candidate >= 8:
            begin = candidate
            gaps.append({"begin": 0.0, "end": begin})
    for sentence in transcript.sentences[:-1]:
        token = sentence.words[-1].norm_end
        exact = all(k in anchors for k in range(token - 3, token + 3))
        if exact:
            left, right = anchors[token - 1]["end"], anchors[token]["start"]
        else:
            # Proper names at the resumption are often misrecognized. Allow up
            # to five unmatched edge tokens only for a long gap, with matched
            # three-word context beyond them and conservative speech padding.
            left_tokens = [
                k
                for k in range(max(0, token - 6), token)
                if all(j in anchors for j in range(k - 2, k + 1))
            ]
            right_tokens = [
                k
                for k in range(token, min(size, token + 6))
                if all(j in anchors for j in range(k, k + 3))
            ]
            if not left_tokens or not right_tokens:
                continue
            lkey, rkey = left_tokens[-1], right_tokens[0]
            left = anchors[lkey]["end"] + (token - lkey - 1) * 0.6
            right = anchors[rkey]["start"] - (rkey - token) * 0.6
            if right - left < 8:
                continue
        if right < left:
            continue
        if right - left >= 8:
            left, right = left + 0.5, right - 0.5
            gaps.append({"begin": round(left, 4), "end": round(right, 4)})
        else:
            left = right = (left + right) / 2
        cuts.append((token, left, right))
    if all(k in anchors for k in range(max(0, size - 3), size)):
        candidate = min(duration, anchors[size - 1]["end"] + 0.5)
        if duration - candidate >= 8:
            end = candidate
            gaps.append({"begin": end, "end": duration})
    if not gaps:
        return [], []
    segments = []
    previous_token = 0
    for token, left, right in [*cuts, (size, end, end)]:
        if not (previous_token < token <= size and 0 <= begin < left <= duration):
            raise PracticeError("缺稿區間的粗定位範圍衝突；停止對齊，請檢查原稿。")
        segments.append(
            {
                "begin": round(begin, 4),
                "end": round(left, 4),
                "token_start": previous_token,
                "token_end": token,
            }
        )
        previous_token, begin = token, right
    return segments, gaps


def search_windows(
    transcript: Transcript,
    initial: list[dict],
    duration: float,
    recognized: list[dict] | None = None,
    overrides: list[dict] | None = None,
    *,
    gaps: list[dict] | None = None,
) -> tuple[list[dict], float | None]:
    anchors, coverage = (
        match_anchors(transcript, recognized) if recognized is not None else ({}, None)
    )
    windows = []
    baseline_windows = []
    for i, (sentence, baseline) in enumerate(zip(transcript.sentences, initial, strict=True)):
        begin = (
            (initial[i - 1]["end"] + baseline["start"]) / 2
            if i
            else max(0, baseline["start"] - 0.4)
        )
        end = (baseline["end"] + initial[i + 1]["start"]) / 2 if i + 1 < len(initial) else duration
        # A midpoint across omitted speech assigns half an advert to each
        # neighboring sentence even when MFA and ASR already agree. Keep long
        # gaps blank instead of stretching correctly aligned sentences into them.
        if i and baseline["start"] - initial[i - 1]["end"] > 3:
            begin = max(begin, baseline["start"] - 0.5)
        if i + 1 < len(initial) and initial[i + 1]["start"] - baseline["end"] > 3:
            end = min(end, baseline["end"] + 0.5)
        if i + 1 == len(initial) and duration - baseline["end"] > 3:
            end = min(end, baseline["end"] + 0.5)
        baseline_windows.append(
            {"id": i, "begin": round(begin, 4), "end": round(end, 4), "adjusted": False}
        )
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
    # A coarse anchor can land on a neighboring repetition. Reject conflicting
    # automatic adjustments instead of averaging away an entire short sentence.
    # Retry from the proposals so a reverted boundary cannot affect earlier ones.
    proposed = windows
    while True:
        windows = [dict(window) for window in proposed]
        conflicts = []
        group_start = 0
        for i, right in enumerate(windows):
            if not (0 <= right["begin"] < right["end"] <= duration + 0.01):
                conflicts = [i]
                break
            if not i:
                continue
            left = windows[i - 1]
            if left["end"] <= right["begin"]:
                group_start = i
                continue
            if transcript.sentences[i].supplied_start is not None:
                raise PracticeError("提供的字幕搜尋範圍有重疊。")
            boundary = round((left["end"] + right["begin"]) / 2, 4)
            if not (left["begin"] < boundary < right["end"]):
                conflicts = list(range(group_start, i + 1))
                break
            left["end"] = right["begin"] = boundary
        fallback = [
            i
            for i in conflicts
            if proposed[i]["adjusted"] and transcript.sentences[i].supplied_start is None
        ]
        if not fallback:
            break
        for i in fallback:
            proposed[i] = {**baseline_windows[i], "anchor_fallback": True}
    # MFA can shift a boundary slightly relative to ASR. Its normal half-second
    # padding must not bring a known omitted interval back into the final pass.
    for baseline, window in zip(initial, windows, strict=True):
        for gap in gaps or []:
            if baseline["end"] <= gap["begin"]:
                window["end"] = min(window["end"], gap["begin"])
            elif baseline["start"] >= gap["end"]:
                window["begin"] = max(window["begin"], gap["end"])
            else:
                raise PracticeError("第一輪 MFA 的句子跨過缺稿區間；停止對齊，請檢查原稿。")
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
                windows[index].pop("anchor_fallback", None)
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
        if any(window["begin"] < g["end"] and window["end"] > g["begin"] for g in gaps or []):
            raise PracticeError("提供的字幕搜尋範圍跨過缺稿區間；請修正 --windows 後重試。")
    return windows, coverage
