"""Parse user-supplied text and retain a mapping to its original display words."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from num2words import num2words

from .errors import PracticeError

ACRONYM_WORDS = {"NATO", "NASA", "OPEC", "UNESCO", "NASDAQ", "COVID", "AIDS"}
EVENT = re.compile(
    r"^\s*(?:\[|\()(?:music|ad|advert|applause|laughter|silence|inaudible|pause|sound)"
    r"[^\]\)]*(?:\]|\))\s*$",
    re.I,
)
NAME = re.compile(r"^(?:[A-Z][\w'’.-]*\s+){1,4}[A-Z][\w'’.-]*$")
ROLE = re.compile(r"^(?:host|guest|narrator|interviewer|speaker\s*\d*)(?:\s+\d+)?$", re.I)
TIMESTAMP = re.compile(r"(?:\[|\()?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:\]|\))?")
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "st.", "vs.", "e.g.", "i.e."}


@dataclass
class DisplayWord:
    text: str
    norm_start: int
    norm_end: int


@dataclass
class Sentence:
    speaker: str
    paragraph: int
    text: str
    words: list[DisplayWord]
    supplied_start: float | None = None
    supplied_end: float | None = None


@dataclass
class Transcript:
    sentences: list[Sentence]
    normalized: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def number_words(value: str, *, ordinal: bool = False) -> str:
    value = value.replace(",", "")
    if ordinal:
        return num2words(int(value), to="ordinal", lang="en")
    if "." in value:
        whole, decimal = value.split(".", 1)
        return (
            num2words(int(whole), lang="en")
            + " point "
            + " ".join(num2words(int(n), lang="en") for n in decimal)
        )
    integer = int(value)
    if len(value) == 4 and 1000 <= integer <= 2099:
        return num2words(integer, to="year", lang="en")
    return num2words(integer, lang="en")


def spoken_tokens(token: str) -> list[str]:
    """English normalization; the displayed word is never replaced."""
    token = unicodedata.normalize("NFKD", token.replace("’", "'").replace("‘", "'"))
    token = "".join(c for c in token if not unicodedata.combining(c))
    token = token.strip('.,!?;:"“”()[]{}')
    # Dotted initialisms, e.g. U.S., and possessive initialisms.
    dotted = re.fullmatch(r"(?:[A-Z]\.)+[A-Z]?", token)
    if dotted:
        token = token.replace(".", "")
    acronym = re.fullmatch(r"([A-Z]{2,6})('s)?", token)
    if acronym and acronym[1] not in ACRONYM_WORDS:
        token = " ".join(acronym[1]) + (acronym[2] or "")
    token = re.sub(
        r"(?<!\w)(\d+)(?:st|nd|rd|th)(?!\w)",
        lambda m: number_words(m[1], ordinal=True),
        token,
        flags=re.I,
    )
    token = re.sub(
        r"(?<!\w)(\d{4})s(?!\w)",
        lambda m: decade_words(int(m[1])),
        token,
    )
    # Currency symbols are omitted: podcasts often say '$150bn' as '150 billion'.
    # The user can spell amounts out in the input if the spoken form differs.
    token = re.sub(r"[$£€]", "", token)
    token = re.sub(
        r"(\d[\d,]*(?:\.\d+)?)(bn|billion|mn|million|trn|tn|m|b|k)\b",
        lambda m: (
            number_words(m[1])
            + " "
            + {
                "bn": "billion",
                "billion": "billion",
                "b": "billion",
                "mn": "million",
                "million": "million",
                "m": "million",
                "trn": "trillion",
                "tn": "trillion",
                "k": "thousand",
            }[m[2].lower()]
        ),
        token,
        flags=re.I,
    )
    token = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda m: number_words(m[0]), token)
    token = token.replace("%", " percent ").replace("&", " and ")
    token = re.sub(r"[-—–/]", " ", token).lower()
    return re.findall(r"[a-z]+(?:'[a-z]+)?", token)


def decade_words(year: int) -> str:
    century, decade = divmod(year, 100)
    if decade == 0:
        return num2words(century, lang="en") + " hundreds"
    endings = {
        10: "tens",
        20: "twenties",
        30: "thirties",
        40: "forties",
        50: "fifties",
        60: "sixties",
        70: "seventies",
        80: "eighties",
        90: "nineties",
    }
    if decade in endings:
        return num2words(century, lang="en") + " " + endings[decade]
    return number_words(str(year)) + " s"


def split_sentences(text: str) -> list[str]:
    """Split normal prose without breaking titles such as 'Dr. Smith'."""
    result, current = [], []
    tokens = text.split()
    for i, token in enumerate(tokens):
        current.append(token)
        stop = re.search(r"[.!?][\"”')]*$", token) and token.lower() not in ABBREVIATIONS
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        # Quoted questions followed by lower-case narration belong to the same sentence.
        if stop and (not following or re.match(r'[A-Z“"(]', following)):
            result.append(" ".join(current))
            current = []
    if current:
        result.append(" ".join(current))
    return result


def speaker_name(line: str, explicit: set[str]) -> str | None:
    clean = re.sub(r"\s+voice clip$", "", line.strip().rstrip(":"), flags=re.I)
    if clean in explicit or ROLE.fullmatch(clean) or NAME.fullmatch(clean):
        return clean
    return None


def parse_plain(text: str, speakers: set[str]) -> list[tuple[str, str, float | None, float | None]]:
    paragraphs = []
    current: list[str] = []
    speaker = "Narrator"

    def flush() -> None:
        if current:
            paragraphs.append((speaker, " ".join(current), None, None))
            current.clear()

    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if (
            not line
            or EVENT.fullmatch(line)
            or re.match(r"https?://", line)
            or line.startswith("Please use the sharing tools")
            or line.startswith("#")
        ):
            flush()
            continue
        line = TIMESTAMP.sub("", line, count=1).strip() if TIMESTAMP.match(line) else line
        if not line:
            continue
        prefix, sep, rest = line.partition(":")
        name = speaker_name(prefix, speakers) if sep else speaker_name(line, speakers)
        if name:
            flush()
            speaker = name
            if sep and rest.strip():
                current.append(rest.strip())
        else:
            current.append(line)
    flush()
    return paragraphs


def seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


def parse_subtitles(text: str) -> list[tuple[str, str, float | None, float | None]]:
    rows = []
    pattern = re.compile(
        r"(?m)^(\d{1,2}:\d{2}(?::\d{2})?[.,]\d+)\s*-->\s*"
        r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d+)[^\n]*\n([^\n]*(?:\n(?!\s*\n)[^\n]+)*)"
    )
    previous = 0.0
    for match in pattern.finditer(text.replace("\r\n", "\n")):
        start, end = seconds(match[1]), seconds(match[2])
        if not (previous <= start < end):
            raise PracticeError("字幕時間重疊或順序錯誤；請修正 SRT/VTT 後重試。")
        previous = end
        content = html.unescape(re.sub(r"<[^>]*>", "", match[3])).strip()
        if content and not EVENT.fullmatch(content):
            rows.append(("Narrator", " ".join(content.split()), start, end))
    if not rows:
        raise PracticeError("讀不到 SRT/VTT 字幕；請檢查字幕的時間格式。")
    return rows


def parse_transcript(path: Path, speakers: set[str] | None = None) -> Transcript:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PracticeError("Transcript 必須使用 UTF-8 編碼，請另存成 UTF-8 純文字。") from exc
    if path.suffix.lower() in {".srt", ".vtt"}:
        paragraphs = parse_subtitles(text)
    else:
        paragraphs = parse_plain(text, speakers or set())
    normalized: list[str] = []
    sentences: list[Sentence] = []
    for paragraph_id, (speaker, paragraph, start, end) in enumerate(paragraphs):
        pieces = [paragraph] if start is not None else split_sentences(paragraph)
        for piece in pieces:
            words = []
            for token in piece.split():
                spoken = spoken_tokens(token)
                words.append(DisplayWord(token, len(normalized), len(normalized) + len(spoken)))
                normalized.extend(spoken)
            if words and any(w.norm_end > w.norm_start for w in words):
                sentences.append(Sentence(speaker, paragraph_id, piece, words, start, end))
    if not normalized or not sentences:
        raise PracticeError("Transcript 沒有可對齊的英文內容。請提供英文原稿。")
    return Transcript(sentences, normalized)
