import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from praatio import textgrid

from podcast_practice.errors import PracticeError
from podcast_practice.pipeline import BuildOptions, build
from podcast_practice.runtime import Runtime


@pytest.mark.parametrize("cross_gap", [False, True])
def test_omitted_ad_is_excluded_before_mfa_and_checked_before_publication(
    tmp_path, monkeypatch, cross_gap
):
    audio, source = tmp_path / "source.mp3", tmp_path / "source.txt"
    audio.write_bytes(b"test audio")
    source.write_text("Welcome to our show. The next topic starts here.")
    runtime = Runtime(tmp_path / "runtime")
    monkeypatch.setattr(runtime, "require_ready", lambda: None)
    monkeypatch.setattr(runtime, "lexicon", lambda *_: tmp_path / "test.dict")

    def convert(args, **kwargs):
        with wave.open(str(args[-1]), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 16000 * 70)

    monkeypatch.setattr(runtime, "run", convert)
    tokens = "welcome to our show the next topic starts here".split()
    entries = [[1 + i * 0.5, 1.5 + i * 0.5, token] for i, token in enumerate(tokens[:4])]
    entries += [[63 + i * 0.5, 63.5 + i * 0.5, token] for i, token in enumerate(tokens[4:])]
    events = []

    def recognize(*args, **kwargs):
        events.append("recognize")
        return [{"text": token, "start": start, "end": end} for start, end, token in entries]

    def align(runtime, audio, text, dictionary, result, job, **kwargs):
        assert events[0] == "recognize"
        assert Path(text).suffix == ".TextGrid"
        grid = textgrid.openTextgrid(str(text), includeEmptyIntervals=False)
        intervals = grid.getTier("speech").entries
        # Neither MFA pass is allowed to force transcript words onto the midroll.
        assert len(intervals) == 2
        assert intervals[0].end <= 3.5
        assert intervals[1].start >= 62.5
        events.append("align")
        rows = [list(entry) for entry in entries]
        if cross_gap and len(events) == 3:
            rows[3][1] = 20
        return {"end": 70, "tiers": {"words": {"entries": rows}}}

    monkeypatch.setattr("podcast_practice.pipeline.transcribe", recognize)
    monkeypatch.setattr("podcast_practice.pipeline.mfa_align", align)
    monkeypatch.setattr(
        "podcast_practice.pipeline.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps({"format": {"duration": "70"}})
        ),
    )
    library = tmp_path / "library"
    options = BuildOptions(audio, source, library=library, offline=True)
    if cross_gap:
        with pytest.raises(PracticeError, match="字幕跨過缺稿區間"):
            build(runtime, options)
        assert not list(library.glob("*/player.html"))
        assert not (library / "index.html").exists()
    else:
        output = build(runtime, options)
        report = json.loads((output / "review.json").read_text())
        assert report["untranscribed_audio"] == [{"begin": 3.5, "end": 62.5}]
        assert (output / "player.html").is_file()
        assert (library / "index.html").is_file()
        subtitles = (output / "subtitles.srt").read_text()
        assert "00:00:03,000" in subtitles
        assert "00:01:03,000" in subtitles
