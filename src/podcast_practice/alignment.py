"""Restore original text, and reject incomplete or non-monotonic alignments."""

from __future__ import annotations

import re

from .errors import PracticeError
from .transcript import Transcript


def letters(text: str) -> str:
    return "".join(re.findall("[a-z]+", text.lower()))


def restore(transcript: Transcript, raw: dict) -> list[dict]:
    try:
        entries = [
            row
            for row in raw["tiers"]["words"]["entries"]
            if row[2] not in ("", "<eps>", "sil", "sp")
        ]
        duration = float(raw["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PracticeError("MFA 輸出沒有可讀取的逐字時間。請檢查 mfa.log。") from exc
    expected = "".join(letters(word) for word in transcript.normalized)
    actual = "".join(letters(row[2]) for row in entries)
    if expected != actual:
        raise PracticeError("MFA 輸出的字詞與原稿不一致；停止匯出，請檢查未知字詞及 mfa.log。")
    char_times: list[tuple[float, float]] = []
    previous = 0.0
    for start, end, word in entries:
        if not (previous - 0.002 <= start < end <= duration + 0.01):
            raise PracticeError(f"MFA 字詞時間無效：{word} {start}–{end}")
        previous = end
        char_times.extend([(start, end)] * len(letters(word)))
    offsets = [0]
    for word in transcript.normalized:
        offsets.append(offsets[-1] + len(letters(word)))
    sentences = []
    for i, sentence in enumerate(transcript.sentences):
        words = []
        for word in sentence.words:
            begin, end = offsets[word.norm_start], offsets[word.norm_end]
            start_time = char_times[begin][0] if end > begin else None
            end_time = char_times[end - 1][1] if end > begin else None
            words.append(
                {
                    "text": word.text,
                    "start": round(start_time, 4) if start_time is not None else None,
                    "end": round(end_time, 4) if end_time is not None else None,
                }
            )
        timed = [word for word in words if word["start"] is not None]
        sentences.append(
            {
                "id": i,
                "speaker": sentence.speaker,
                "paragraph": sentence.paragraph,
                "text": sentence.text,
                "start": timed[0]["start"],
                "end": timed[-1]["end"],
                "words": words,
            }
        )
    return sentences


def quality_report(
    raw: dict, sentences: list[dict], windows: list[dict], anchor_coverage: float | None
) -> dict:
    warnings = []
    for start, end, token in raw["tiers"]["words"]["entries"]:
        if token not in ("", "<eps>", "sil", "sp") and end - start > 1.5:
            sentence = next((s["id"] for s in sentences if s["start"] <= start <= s["end"]), None)
            warnings.append(
                {
                    "type": "long_word",
                    "sentence": sentence,
                    "word": token,
                    "start": round(start, 3),
                    "duration": round(end - start, 3),
                    "message": "單字時間偏長，請核對是否跨過音樂或缺少原稿。",
                }
            )
    for sentence, window in zip(sentences, windows, strict=True):
        if window.get("anchor_fallback"):
            warnings.append(
                {
                    "type": "anchor_conflict",
                    "sentence": sentence["id"],
                    "message": "粗定位與相鄰句子範圍衝突，已使用第一輪 MFA 範圍，請抽聽核對。",
                }
            )
        if window.get("adjusted") and (
            sentence["start"] - window["begin"] < 0.025 or window["end"] - sentence["end"] < 0.025
        ):
            warnings.append(
                {
                    "type": "window_edge",
                    "sentence": sentence["id"],
                    "message": "語音接近搜尋範圍邊緣，請核對句首或句尾。",
                }
            )
    if anchor_coverage is not None and anchor_coverage < 0.6:
        warnings.append(
            {
                "type": "low_anchor_coverage",
                "sentence": None,
                "message": "本地辨識與原稿吻合率偏低，請核對原稿是否為同一集。",
            }
        )
    if anchor_coverage is None:
        warnings.append(
            {
                "type": "no_asr_check",
                "sentence": None,
                "message": "本次沒有執行本地辨識抽查；請特別核對音樂轉場及尾段。",
            }
        )
    return {
        "anchor_coverage": anchor_coverage,
        "checked_sentences": len(sentences),
        "warnings": warnings,
    }
