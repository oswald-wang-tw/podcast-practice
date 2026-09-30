from pathlib import Path

import pytest

from podcast_practice.errors import PracticeError
from podcast_practice.transcript import parse_transcript, split_sentences, spoken_tokens


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("2.5%", ["two", "point", "five", "percent"]),
        ("29th,", ["twenty", "ninth"]),
        ("$150bn", ["one", "hundred", "and", "fifty", "billion"]),
        ("UK’s", ["u", "k's"]),
        ("24/7", ["twenty", "four", "seven"]),
        ("1830s", ["eighteen", "thirties"]),
        ("60", ["sixty"]),
        ("NATO", ["nato"]),
        ("U.S.", ["u", "s"]),
    ],
)
def test_spoken_normalization(token, expected):
    assert spoken_tokens(token) == expected


def test_unrelated_podcasts_preserve_original_text_and_speakers(tmp_path: Path):
    path = tmp_path / "daily.txt"
    path.write_text(
        "Host\nWelcome to our show.\n\n[MUSIC PLAYING]\n\n"
        "Alex Rivera\nIn 2028, 60 people will join us.\n\n"
        "Guest: That sounds useful.\n",
        encoding="utf-8",
    )
    data = parse_transcript(path)
    assert [s.speaker for s in data.sentences] == ["Host", "Alex Rivera", "Guest"]
    assert data.sentences[1].text == "In 2028, 60 people will join us."
    assert "music" not in data.normalized
    assert all("Alex" not in s.text for s in data.sentences)


def test_title_abbreviation_and_quoted_question():
    assert split_sentences("Dr. Brown is here. Welcome, everyone.") == [
        "Dr. Brown is here.",
        "Welcome, everyone.",
    ]
    assert split_sentences("She asks: why work here? in a short advert.") == [
        "She asks: why work here? in a short advert."
    ]


def test_subtitles_keep_supplied_ranges(tmp_path):
    path = tmp_path / "sample.srt"
    path.write_text(
        "1\n00:00:02,000 --> 00:00:04,000\nHello, everyone.\n\n"
        "2\n00:00:07,500 --> 00:00:10,000\nHow are you today?\n"
    )
    data = parse_transcript(path)
    assert len(data.sentences) == 2
    assert data.sentences[1].supplied_start == 7.5
    assert data.sentences[1].supplied_end == 10.0


def test_overlapping_subtitles_are_rejected(tmp_path):
    path = tmp_path / "bad.vtt"
    path.write_text(
        "WEBVTT\n\n00:02.000 --> 00:04.000\nHello.\n\n00:03.000 --> 00:05.000\nWorld.\n"
    )
    with pytest.raises(PracticeError, match="重疊"):
        parse_transcript(path)


def test_no_english_input_fails_without_running_anything(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("[MUSIC PLAYING]\nhttps://example.com\n# Title\n")
    with pytest.raises(PracticeError, match="沒有可對齊"):
        parse_transcript(path)


def test_ft_notice_is_data_to_strip_not_an_instruction(tmp_path):
    path = tmp_path / "pasted.txt"
    path.write_text(
        "Please use the sharing tools found at the top.\n"
        "https://example.com/transcript\n\nHost\nGood morning.\n"
    )
    assert parse_transcript(path).normalized == ["good", "morning"]
