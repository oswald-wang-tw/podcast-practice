import pytest

from podcast_practice.alignment import quality_report, restore
from podcast_practice.anchors import search_windows
from podcast_practice.errors import PracticeError
from podcast_practice.transcript import parse_transcript


def test_music_and_untranscribed_outro_are_bounded_automatically(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("Welcome to the show.\n\nThe new story begins here.\n")
    transcript = parse_transcript(source)
    # Baseline forced alignment has stretched across music (2–10s) and outro (12–30s).
    initial = [{"start": 0, "end": 2}, {"start": 2, "end": 30}]
    recognized = []
    for i, token in enumerate(transcript.normalized[:4]):
        recognized.append({"text": token, "start": i * 0.4, "end": (i + 1) * 0.4})
    for i, token in enumerate(transcript.normalized[4:]):
        recognized.append({"text": token, "start": 10 + i * 0.4, "end": 10.4 + i * 0.4})
    windows, coverage = search_windows(transcript, initial, 40, recognized)
    assert coverage == 1
    assert windows[1]["begin"] >= 9.5
    assert windows[1]["end"] < 13
    assert windows[0]["end"] <= windows[1]["begin"]


def test_two_word_clip_can_match_and_exclude_untranscribed_outro(tmp_path):
    source = tmp_path / "short.txt"
    source.write_text("Hello world.")
    transcript = parse_transcript(source)
    recognized = [
        {"text": "hello", "start": 1, "end": 1.4},
        {"text": "world", "start": 1.5, "end": 2},
    ]
    windows, coverage = search_windows(transcript, [{"start": 1, "end": 10}], 10, recognized)
    assert coverage == 1
    assert windows[0]["end"] == 2.5


def test_explicit_window_must_not_silently_change_or_overlap(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("One short sentence. Another short sentence.")
    transcript = parse_transcript(source)
    initial = [{"start": 1, "end": 3}, {"start": 4, "end": 6}]
    with pytest.raises(PracticeError, match="範圍不合理"):
        search_windows(transcript, initial, 10, overrides=[{"id": 0, "begin": 1, "end": 5}])
    windows, _ = search_windows(
        transcript, initial, 10, overrides=[{"id": 1, "begin": 4.2, "end": 6.1}]
    )
    assert windows[1]["begin"] == 4.2
    assert windows[1]["end"] == 6.1


def test_repeated_phrase_anchor_cannot_invert_previous_short_sentence(tmp_path, monkeypatch):
    source = tmp_path / "episode.txt"
    source.write_text("It's a guy. It's a guy, yeah. It's a guy.")
    transcript = parse_transcript(source)
    initial = [
        {"start": 9.4301, "end": 9.75},
        {"start": 9.75, "end": 10.28},
        {"start": 10.5601, "end": 11.47},
    ]
    # The third repetition matched ASR speech overlapping the preceding two.
    first = transcript.sentences[2].words[0].norm_start
    anchors = {
        first: {"start": 9.42, "end": 10.72},
        first + 1: {"start": 10.72, "end": 10.8},
        first + 2: {"start": 10.8, "end": 10.96},
    }
    monkeypatch.setattr("podcast_practice.anchors.match_anchors", lambda *_: (anchors, 0.93))
    windows, coverage = search_windows(transcript, initial, 12, recognized=[])
    assert coverage == 0.93
    assert windows[1]["begin"] == 9.75
    assert windows[1]["end"] == windows[2]["begin"] == 10.42
    assert windows[2]["anchor_fallback"] is True
    report = quality_report(
        {"tiers": {"words": {"entries": []}}},
        [{"id": i, **row} for i, row in enumerate(initial)],
        windows,
        coverage,
    )
    assert [(w["type"], w["sentence"]) for w in report["warnings"]] == [("anchor_conflict", 2)]
    explicit, _ = search_windows(
        transcript,
        initial,
        12,
        recognized=[],
        overrides=[{"id": 2, "begin": 10.5, "end": 11.9}],
    )
    assert explicit[2]["begin"] == 10.5
    assert explicit[2]["end"] == 11.9
    assert "anchor_fallback" not in explicit[2]


def test_coarse_end_cannot_collapse_following_sentence(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("First short sentence. Another small sentence. Final small sentence.")
    transcript = parse_transcript(source)
    initial = [{"start": 0, "end": 1}, {"start": 2, "end": 3}, {"start": 4, "end": 5}]
    recognized = [
        {"text": "first", "start": 0, "end": 0.3},
        {"text": "short", "start": 0.3, "end": 0.6},
        {"text": "sentence", "start": 4, "end": 5},
    ]
    windows, _ = search_windows(transcript, initial, 6, recognized)
    assert windows[0]["anchor_fallback"] is True
    assert windows[0]["end"] == windows[1]["begin"] == 1.5
    assert windows[1]["end"] == windows[2]["begin"] == 3.5


def test_invalid_mfa_baseline_still_stops_without_coarse_adjustments(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("Hello world.")
    with pytest.raises(PracticeError, match="範圍不合理"):
        search_windows(parse_transcript(source), [{"start": 5, "end": 6}], 2)


def test_contractions_and_numeric_expansion_restore_original_words(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("It's 60.")
    transcript = parse_transcript(source)
    raw = {
        "end": 3,
        "tiers": {
            "words": {
                "entries": [
                    [0, 0.3, "it"],
                    [0.3, 0.4, "'s"],
                    [0.4, 1.2, "sixty"],
                    [1.2, 3, "<eps>"],
                ]
            }
        },
    }
    sentences = restore(transcript, raw)
    assert sentences[0]["text"] == "It's 60."
    assert sentences[0]["words"] == [
        {"text": "It's", "start": 0, "end": 0.4},
        {"text": "60.", "start": 0.4, "end": 1.2},
    ]
    assert sentences[0]["end"] == 1.2


def test_missing_or_invalid_word_times_prevent_export(tmp_path):
    source = tmp_path / "episode.txt"
    source.write_text("Hello world.")
    transcript = parse_transcript(source)
    raw = {"end": 3, "tiers": {"words": {"entries": [[0, 1, "hello"]]}}}
    with pytest.raises(PracticeError, match="字詞與原稿不一致"):
        restore(transcript, raw)
    raw["tiers"]["words"]["entries"] += [[0.8, 2, "world"]]
    with pytest.raises(PracticeError, match="字詞時間無效"):
        restore(transcript, raw)
