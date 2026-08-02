from dataclasses import dataclass
from pathlib import Path


SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa"}
MAX_READ_BYTES = 64 * 1024
CHINESE_MARKERS = (
    "zh",
    "chi",
    "zho",
    "chs",
    "cht",
    "chinese",
    "中文",
    "简体",
    "繁体",
    "字幕",
    "中英",
    "简英",
    "繁英",
    "双语",
)
BILINGUAL_MARKERS = ("bilingual", "双语", "中英", "简英", "繁英")


@dataclass(frozen=True)
class ExternalSubtitleMatch:
    path: Path
    has_chinese: bool
    is_bilingual: bool


@dataclass(frozen=True)
class ExternalSubtitleResult:
    has_chinese: bool
    has_bilingual: bool
    matches: list[ExternalSubtitleMatch]


def detect_external_subtitles(video_path: Path) -> ExternalSubtitleResult:
    matches: list[ExternalSubtitleMatch] = []

    for candidate in video_path.parent.iterdir():
        if not candidate.is_file() or candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if not _shares_video_stem(video_path, candidate):
            continue

        text = _read_subtitle_text(candidate)
        name_has_chinese = _contains_chinese(candidate.name)
        text_has_chinese = _contains_chinese(text)
        has_chinese = name_has_chinese or text_has_chinese
        is_bilingual = _contains_bilingual_marker(candidate.name) or _contains_bilingual_marker(text)
        if not is_bilingual and text_has_chinese:
            is_bilingual = _contains_english(text)

        matches.append(
            ExternalSubtitleMatch(
                path=candidate,
                has_chinese=has_chinese,
                is_bilingual=has_chinese and is_bilingual,
            )
        )

    return ExternalSubtitleResult(
        has_chinese=any(match.has_chinese for match in matches),
        has_bilingual=any(match.is_bilingual for match in matches),
        matches=matches,
    )


def _shares_video_stem(video_path: Path, subtitle_path: Path) -> bool:
    stem = video_path.stem
    return subtitle_path.name == f"{stem}{subtitle_path.suffix}" or subtitle_path.name.startswith(
        f"{stem}."
    )


def _read_subtitle_text(path: Path) -> str:
    raw = path.read_bytes()[:MAX_READ_BYTES]
    encodings: list[str] = []
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encodings.append("utf-32")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(("utf-8-sig", "utf-8", "gb18030"))
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _contains_chinese(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in CHINESE_MARKERS):
        return True
    return any("\u4e00" <= character <= "\u9fff" for character in value)


def _contains_bilingual_marker(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in BILINGUAL_MARKERS)


def _contains_english(value: str) -> bool:
    return any("a" <= character.lower() <= "z" for character in value)
