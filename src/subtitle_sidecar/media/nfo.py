from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable
from xml.etree import ElementTree

from subtitle_sidecar.media.identity import MediaIdentity


MAX_NFO_BYTES = 4 * 1024 * 1024
_EPISODE_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])S(?P<season>\d{1,3})E(?P<episode>\d{1,4})(?!\d)")
_YEAR_PATTERN = re.compile(r"(?<!\d)(?P<year>(?:18|19|20)\d{2})(?!\d)")
_IMDB_PATTERN = re.compile(r"tt\d{7,10}", re.IGNORECASE)
_NUMERIC_ID_PATTERN = re.compile(r"\d+")


class NfoIdentityError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class NfoIdentityPending(NfoIdentityError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float = 2.0,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__("nfo_pending", message, details=details)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class _NfoDocument:
    path: Path
    root: ElementTree.Element
    root_name: str


def resolve_nfo_identity(video_path: Path) -> MediaIdentity:
    """Resolve a movie or episode identity from local MoviePilot/Kodi NFO files.

    Series-level identity is always read from ``tvshow.nfo``. A malformed or wrongly typed
    episode sidecar is ignored only when an unambiguous SxxExx coordinate is present in the
    media filename; it is never allowed to contribute titles, years, or provider IDs.
    """

    path = Path(video_path)
    if not path.exists():
        raise NfoIdentityError(
            "nfo_video_not_found",
            f"媒体文件不存在：{path}",
            details={"video_path": str(path)},
        )

    tvshow_document = _find_tvshow_document(path)
    episode_document, episode_error = _find_episode_document(path)
    path_coordinate = _episode_coordinate(path.name)
    if tvshow_document is not None or episode_document is not None:
        if tvshow_document is None:
            raise NfoIdentityError(
                "nfo_series_not_found",
                f"未找到剧集 tvshow.nfo：{path}",
                details={"video_path": str(path)},
            )
        return _episode_identity(
            path,
            tvshow_document,
            episode_document,
            path_coordinate,
            episode_error=episode_error,
        )

    movie_documents = _find_movie_documents(path)
    if not movie_documents:
        details: dict[str, object] = {"video_path": str(path)}
        if episode_error is not None:
            details["sidecar_error"] = episode_error.code
        raise NfoIdentityError(
            "nfo_not_found",
            f"未找到可用 NFO：{path}",
            details=details,
        )
    return _movie_identity(path, movie_documents)


def _movie_identity(video_path: Path, documents: list[_NfoDocument]) -> MediaIdentity:
    identities = [_identity_fields(document) for document in documents]
    signatures = {
        (
            fields["title"],
            fields["original_title"],
            fields["year"],
            fields["imdb_id"],
            fields["tmdb_id"],
            fields["tvdb_id"],
        )
        for fields in identities
    }
    if len(signatures) > 1:
        raise NfoIdentityError(
            "nfo_ambiguous",
            f"同一影片的 NFO 身份冲突：{video_path}",
            details={"nfo_paths": [str(document.path) for document in documents]},
        )
    fields = identities[0]
    _require_title_year(fields, documents[0].path)
    return MediaIdentity(
        media_type="movie",
        title=str(fields["title"]),
        original_title=_as_optional_string(fields["original_title"]),
        year=int(fields["year"]),
        imdb_id=_as_optional_string(fields["imdb_id"]),
        tmdb_id=_as_optional_string(fields["tmdb_id"]),
        tvdb_id=_as_optional_string(fields["tvdb_id"]),
        alternate_years=tuple(fields["alternate_years"]),
        nfo_paths=tuple(document.path for document in documents),
    )


def _episode_identity(
    video_path: Path,
    series_document: _NfoDocument,
    episode_document: _NfoDocument | None,
    path_coordinate: tuple[int, int] | None,
    *,
    episode_error: NfoIdentityError | None,
) -> MediaIdentity:
    series = _identity_fields(series_document)
    _require_title_year(series, series_document.path)

    nfo_coordinate: tuple[int, int] | None = None
    episode_title: str | None = None
    if episode_document is not None:
        season = _parse_nonnegative_int(_text(episode_document.root, "season"))
        episode = _parse_nonnegative_int(_text(episode_document.root, "episode"))
        if season is not None and episode is not None:
            nfo_coordinate = (season, episode)
        episode_title = _text(episode_document.root, "title")

    if (
        nfo_coordinate is not None
        and path_coordinate is not None
        and nfo_coordinate != path_coordinate
    ):
        raise NfoIdentityError(
            "nfo_episode_path_conflict",
            f"分集 NFO 与文件名季集号冲突：{video_path}",
            details={
                "nfo_path": str(episode_document.path) if episode_document else None,
                "nfo_season": nfo_coordinate[0],
                "nfo_episode": nfo_coordinate[1],
                "path_season": path_coordinate[0],
                "path_episode": path_coordinate[1],
            },
        )
    coordinate = nfo_coordinate or path_coordinate
    if coordinate is None:
        details: dict[str, object] = {"video_path": str(video_path)}
        if episode_error is not None:
            details["sidecar_error"] = episode_error.code
        raise NfoIdentityError(
            "nfo_episode_identity_incomplete",
            f"无法确认分集季号和集号：{video_path}",
            details=details,
        )

    season_document = _find_season_document(video_path, coordinate[0])
    episode_fields = _identity_fields(episode_document) if episode_document is not None else None
    season_fields = _identity_fields(season_document) if season_document is not None else None
    alternate_years = set(_years_from_fields(series))
    if season_fields is not None:
        alternate_years.update(_years_from_fields(season_fields))
    if episode_fields is not None:
        alternate_years.update(_years_from_fields(episode_fields))
    alternate_years.discard(int(series["year"]))

    nfo_paths = [series_document.path]
    if season_document is not None:
        nfo_paths.append(season_document.path)
    if episode_document is not None:
        nfo_paths.append(episode_document.path)
    return MediaIdentity(
        media_type="episode",
        title=str(series["title"]),
        original_title=_as_optional_string(series["original_title"]),
        year=int(series["year"]),
        season=coordinate[0],
        episode=coordinate[1],
        imdb_id=_as_optional_string(series["imdb_id"]),
        tmdb_id=_as_optional_string(series["tmdb_id"]),
        tvdb_id=_as_optional_string(series["tvdb_id"]),
        episode_title=episode_title,
        alternate_years=tuple(sorted(alternate_years)),
        nfo_paths=tuple(nfo_paths),
    )


def _find_tvshow_document(video_path: Path) -> _NfoDocument | None:
    current = video_path if video_path.is_dir() else video_path.parent
    for _ in range(5):
        candidate = _find_named_file(current, "tvshow.nfo")
        if candidate is not None:
            document = _parse_document(candidate)
            if document.root_name != "tvshow":
                raise NfoIdentityError(
                    "nfo_wrong_type",
                    f"tvshow.nfo 根节点不是 tvshow：{candidate}",
                    details={"nfo_path": str(candidate), "root": document.root_name},
                )
            return document
        if current.parent == current:
            break
        current = current.parent
    return None


def _find_episode_document(
    video_path: Path,
) -> tuple[_NfoDocument | None, NfoIdentityError | None]:
    if video_path.is_dir():
        return None, None
    candidate = _find_stem_nfo(video_path)
    if candidate is None:
        return None, None
    try:
        document = _parse_document(candidate)
    except NfoIdentityError as error:
        return None, error
    if document.root_name == "episodedetails":
        return document, None
    if document.root_name == "movie":
        return None, NfoIdentityError(
            "nfo_wrong_type",
            f"分集 NFO 被写成 movie：{candidate}",
            details={"nfo_path": str(candidate), "root": document.root_name},
        )
    return None, NfoIdentityError(
        "nfo_wrong_type",
        f"分集 NFO 根节点无效：{candidate}",
        details={"nfo_path": str(candidate), "root": document.root_name},
    )


def _find_season_document(video_path: Path, expected_season: int) -> _NfoDocument | None:
    """Return a trusted, same-directory season.nfo when it matches the episode season.

    Season metadata supplements the authoritative tvshow identity.  Because it is
    optional, malformed or wrongly typed season files are ignored instead of making
    an otherwise valid episode identity unavailable.
    """

    directory = video_path if video_path.is_dir() else video_path.parent
    candidate = _find_named_file(directory, "season.nfo")
    if candidate is None:
        return None
    try:
        document = _parse_document(candidate)
    except NfoIdentityError:
        return None
    if document.root_name != "season":
        return None
    season_number = _parse_nonnegative_int(_text(document.root, "seasonnumber", "season"))
    if season_number is not None and season_number != expected_season:
        return None
    return document


def _find_movie_documents(video_path: Path) -> list[_NfoDocument]:
    directory = video_path if video_path.is_dir() else video_path.parent
    movie_nfo = _find_named_file(directory, "movie.nfo")
    # MoviePilot may write the same-stem sidecar first and replace/correct it with
    # movie.nfo later. The generic movie.nfo is therefore authoritative when present.
    if movie_nfo is not None:
        document = _parse_document(movie_nfo)
        return [document] if document.root_name == "movie" else []

    candidates: list[Path] = []
    if not video_path.is_dir():
        stem_nfo = _find_stem_nfo(video_path)
        if stem_nfo is not None:
            candidates.append(stem_nfo)

    documents: list[_NfoDocument] = []
    for candidate in candidates:
        document = _parse_document(candidate)
        if document.root_name != "movie":
            continue
        documents.append(document)
    return documents


def _find_stem_nfo(video_path: Path) -> Path | None:
    target_name = f"{video_path.stem}.nfo".casefold()
    return next(
        (
            child
            for child in _directory_files(video_path.parent)
            if child.name.casefold() == target_name
        ),
        None,
    )


def _find_named_file(directory: Path, name: str) -> Path | None:
    target_name = name.casefold()
    return next(
        (child for child in _directory_files(directory) if child.name.casefold() == target_name),
        None,
    )


def _directory_files(directory: Path) -> Iterable[Path]:
    try:
        return tuple(child for child in directory.iterdir() if child.is_file())
    except OSError:
        return ()


def _parse_document(path: Path) -> _NfoDocument:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise NfoIdentityError(
            "nfo_unreadable",
            f"无法读取 NFO：{path}",
            details={"nfo_path": str(path), "error": type(error).__name__},
        ) from error
    if size > MAX_NFO_BYTES:
        raise NfoIdentityError(
            "nfo_too_large",
            f"NFO 超过 {MAX_NFO_BYTES} 字节限制：{path}",
            details={"nfo_path": str(path), "size": size},
        )
    try:
        content = path.read_bytes()
    except OSError as error:
        raise NfoIdentityError(
            "nfo_unreadable",
            f"无法读取 NFO：{path}",
            details={"nfo_path": str(path), "error": type(error).__name__},
        ) from error
    lowered = content[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise NfoIdentityError(
            "nfo_unsafe_xml",
            f"NFO 包含不允许的 XML 声明：{path}",
            details={"nfo_path": str(path)},
        )
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise NfoIdentityError(
            "nfo_malformed",
            f"NFO XML 格式错误：{path}",
            details={"nfo_path": str(path), "error": str(error)},
        ) from error
    return _NfoDocument(path=path, root=root, root_name=_tag_name(root.tag))


def _identity_fields(document: _NfoDocument) -> dict[str, object]:
    root = document.root
    title = _text(root, "title")
    original_title = _text(root, "originaltitle", "original_title")
    year = _parse_year(_text(root, "year"))
    date_years = {
        parsed
        for value in (
            _text(root, "premiered"),
            _text(root, "releasedate"),
            _text(root, "firstaired"),
            _text(root, "aired"),
        )
        if (parsed := _parse_year(value)) is not None
    }
    if year is None and date_years:
        year = min(date_years)
    unique_ids = _unique_ids(root)
    imdb_id = _valid_provider_id("imdb", unique_ids.get("imdb") or _text(root, "imdbid", "imdb_id"))
    tmdb_id = _valid_provider_id("tmdb", unique_ids.get("tmdb") or _text(root, "tmdbid", "tmdb_id"))
    tvdb_id = _valid_provider_id("tvdb", unique_ids.get("tvdb") or _text(root, "tvdbid", "tvdb_id"))
    return {
        "title": title,
        "original_title": original_title,
        "year": year,
        "alternate_years": tuple(sorted(value for value in date_years if value != year)),
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
    }


def _years_from_fields(fields: dict[str, object]) -> set[int]:
    years = {value for value in fields.get("alternate_years", ()) if isinstance(value, int)}
    year = fields.get("year")
    if isinstance(year, int):
        years.add(year)
    return years


def _require_title_year(fields: dict[str, object], path: Path) -> None:
    if not fields.get("title") or not fields.get("year"):
        raise NfoIdentityError(
            "nfo_identity_incomplete",
            f"NFO 缺少标题或年份：{path}",
            details={
                "nfo_path": str(path),
                "has_title": bool(fields.get("title")),
                "has_year": bool(fields.get("year")),
            },
        )


def _unique_ids(root: ElementTree.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for element in root.iter():
        if _tag_name(element.tag) != "uniqueid":
            continue
        provider = str(element.attrib.get("type") or "").strip().casefold()
        value = str(element.text or "").strip()
        if provider in {"imdb", "tmdb", "tvdb"} and value:
            values.setdefault(provider, value)
    return values


def _text(root: ElementTree.Element, *names: str) -> str | None:
    expected = {name.casefold() for name in names}
    for element in root.iter():
        if _tag_name(element.tag) not in expected:
            continue
        value = str(element.text or "").strip()
        if value:
            return value
    return None


def _tag_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _episode_coordinate(value: str) -> tuple[int, int] | None:
    match = _EPISODE_PATTERN.search(value)
    if match is None:
        return None
    return int(match.group("season")), int(match.group("episode"))


def _parse_nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _parse_year(value: str | None) -> int | None:
    if value is None:
        return None
    match = _YEAR_PATTERN.search(value)
    return int(match.group("year")) if match else None


def _valid_provider_id(provider: str, value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if provider == "imdb":
        match = _IMDB_PATTERN.fullmatch(normalized)
        return match.group(0).casefold() if match else None
    return normalized if _NUMERIC_ID_PATTERN.fullmatch(normalized) else None


def _as_optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
