from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from subtitle_sidecar.config import AppSettings
from subtitle_sidecar.media.identity import MediaIdentity
from subtitle_sidecar.media.nfo import (
    MAX_NFO_BYTES,
    NfoIdentityError,
    NfoIdentityPending,
    resolve_nfo_identity,
)
from subtitle_sidecar.pipeline.orchestrator import SubtitleOrchestrator


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _movie_xml(
    *,
    title: str = "群体",
    original_title: str = "Colony",
    year: int = 2026,
    tmdb_id: str = "1375646",
    imdb_id: str = "tt34385135",
    premiered: str | None = None,
) -> str:
    premiered_xml = f"<premiered>{premiered}</premiered>" if premiered else ""
    return (
        "<movie>"
        f"<title>{title}</title>"
        f"<originaltitle>{original_title}</originaltitle>"
        f"<year>{year}</year>"
        f"{premiered_xml}"
        f"<tmdbid>{tmdb_id}</tmdbid>"
        f"<imdbid>{imdb_id}</imdbid>"
        f'<uniqueid type="tmdb">{tmdb_id}</uniqueid>'
        f'<uniqueid type="imdb">{imdb_id}</uniqueid>'
        "</movie>"
    )


def _tvshow_xml() -> str:
    return (
        "<tvshow>"
        "<title>辐射</title>"
        "<originaltitle>Fallout</originaltitle>"
        "<year>2024</year>"
        "<tmdbid>106379</tmdbid>"
        "<tvdbid>416744</tvdbid>"
        "<imdbid>tt12637874</imdbid>"
        "</tvshow>"
    )


def _episode_xml(*, season: int = 1, episode: int = 3) -> str:
    return (
        "<episodedetails>"
        "<title>第三集</title>"
        "<year>2024</year>"
        f"<season>{season}</season>"
        f"<episode>{episode}</episode>"
        "<tmdbid>episode-id-must-not-be-used</tmdbid>"
        "</episodedetails>"
    )


def test_movie_identity_prefers_movie_nfo_over_conflicting_same_stem_nfo(
    tmp_path: Path,
) -> None:
    video = _write(tmp_path / "GT赛车：极速狂飙 (2023).mkv", "video")
    _write(
        video.with_suffix(".nfo"),
        _movie_xml(title="movie", year=2024, tmdb_id="1194295"),
    )
    movie_nfo = _write(
        tmp_path / "movie.nfo",
        _movie_xml(
            title="GT赛车：极速狂飙",
            original_title="Gran Turismo",
            year=2023,
            tmdb_id="980489",
            imdb_id="tt4495098",
        ),
    )

    identity = resolve_nfo_identity(video)

    assert identity.title == "GT赛车：极速狂飙"
    assert identity.year == 2023
    assert identity.tmdb_id == "980489"
    assert identity.nfo_paths == (movie_nfo,)


def test_movie_identity_uses_same_stem_nfo_when_movie_nfo_is_absent(tmp_path: Path) -> None:
    video = _write(tmp_path / "无信息文件名.mkv", "video")
    sidecar = _write(video.with_suffix(".NFO"), _movie_xml())

    identity = resolve_nfo_identity(video)

    assert identity == MediaIdentity(
        media_type="movie",
        title="群体",
        original_title="Colony",
        year=2026,
        imdb_id="tt34385135",
        tmdb_id="1375646",
        nfo_paths=(sidecar,),
    )


def test_movie_identity_keeps_only_nfo_year_evidence(tmp_path: Path) -> None:
    video = _write(tmp_path / "Crash.2013.mkv", "video")
    _write(
        tmp_path / "movie.nfo",
        _movie_xml(
            title="撞车",
            original_title="Crash",
            year=2004,
            premiered="2005-05-06",
        ),
    )

    identity = resolve_nfo_identity(video)

    assert identity.year == 2004
    assert identity.alternate_years == (2005,)
    assert 2013 not in identity.alternate_years


def test_episode_identity_merges_tvshow_and_episode_nfo(tmp_path: Path) -> None:
    series = tmp_path / "辐射 Fallout (2024)"
    season = series / "Season 1"
    video = _write(season / "辐射 - S01E03 - 1080p.mkv", "video")
    tvshow_nfo = _write(series / "tvshow.nfo", _tvshow_xml())
    episode_nfo = _write(video.with_suffix(".nfo"), _episode_xml())

    identity = resolve_nfo_identity(video)

    assert identity.media_type == "episode"
    assert identity.title == "辐射"
    assert identity.original_title == "Fallout"
    assert identity.year == 2024
    assert (identity.season, identity.episode) == (1, 3)
    assert identity.tmdb_id == "106379"
    assert identity.tvdb_id == "416744"
    assert identity.imdb_id == "tt12637874"
    assert identity.series_id == "tmdb:106379"
    assert identity.nfo_paths == (tvshow_nfo, episode_nfo)


def test_episode_identity_ignores_moviepilot_movie_shaped_episode_nfo(
    tmp_path: Path,
) -> None:
    series = tmp_path / "辐射 Fallout (2024)"
    season = series / "Season 1"
    video = _write(season / "辐射 - S01E08 - 1080p.mkv", "video")
    tvshow_nfo = _write(series / "tvshow.nfo", _tvshow_xml())
    _write(
        video.with_suffix(".nfo"),
        _movie_xml(title="TV", year=2019, tmdb_id="720590"),
    )

    identity = resolve_nfo_identity(video)

    assert identity.title == "辐射"
    assert identity.year == 2024
    assert (identity.season, identity.episode) == (1, 8)
    assert identity.tmdb_id == "106379"
    assert identity.nfo_paths == (tvshow_nfo,)


def test_episode_identity_supports_specials_and_missing_episode_nfo(tmp_path: Path) -> None:
    series = tmp_path / "辐射 Fallout (2024)"
    video = _write(series / "Specials" / "辐射 - S00E02.mkv", "video")
    _write(series / "tvshow.nfo", _tvshow_xml())

    identity = resolve_nfo_identity(video)

    assert (identity.season, identity.episode) == (0, 2)


def test_episode_nfo_conflicting_with_filename_fails_safely(tmp_path: Path) -> None:
    series = tmp_path / "辐射 Fallout (2024)"
    video = _write(series / "Season 1" / "辐射 - S01E03.mkv", "video")
    _write(series / "tvshow.nfo", _tvshow_xml())
    _write(video.with_suffix(".nfo"), _episode_xml(season=1, episode=4))

    with pytest.raises(NfoIdentityError, match="冲突") as raised:
        resolve_nfo_identity(video)

    assert raised.value.code == "nfo_episode_path_conflict"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("<movie><title>broken", "nfo_malformed"),
        (
            '<!DOCTYPE movie [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<movie><title>&xxe;</title></movie>",
            "nfo_unsafe_xml",
        ),
    ],
)
def test_unsafe_or_malformed_nfo_fails_safely(
    tmp_path: Path,
    content: str,
    code: str,
) -> None:
    video = _write(tmp_path / "Movie.mkv", "video")
    _write(tmp_path / "movie.nfo", content)

    with pytest.raises(NfoIdentityError) as raised:
        resolve_nfo_identity(video)

    assert raised.value.code == code


def test_oversized_nfo_is_rejected(tmp_path: Path) -> None:
    video = _write(tmp_path / "Movie.mkv", "video")
    nfo = tmp_path / "movie.nfo"
    nfo.write_bytes(b" " * (MAX_NFO_BYTES + 1))

    with pytest.raises(NfoIdentityError) as raised:
        resolve_nfo_identity(video)

    assert raised.value.code == "nfo_too_large"


def test_missing_nfo_does_not_fall_back_to_path(tmp_path: Path) -> None:
    video = _write(tmp_path / "Colony.2026.mkv", "video")

    with pytest.raises(NfoIdentityError) as raised:
        resolve_nfo_identity(video)

    assert raised.value.code == "nfo_not_found"


def test_new_moviepilot_task_waits_for_authoritative_movie_nfo(tmp_path: Path) -> None:
    video = _write(tmp_path / "Colony.2026.mkv", "video")
    _write(video.with_suffix(".nfo"), _movie_xml())
    task = _task(video, source="moviepilot-csf", created_at=datetime.now(UTC))
    orchestrator = _orchestrator(tmp_path)

    with pytest.raises(NfoIdentityPending) as raised:
        orchestrator._build_search_request(task, video)

    assert raised.value.code == "nfo_pending"
    assert raised.value.details["fallback_nfo_paths"] == [str(video.with_suffix(".nfo"))]


def test_moviepilot_and_jellyfin_manual_tasks_build_the_same_nfo_request(
    tmp_path: Path,
) -> None:
    video = _write(tmp_path / "Colony.2013.mkv", "video")
    _write(tmp_path / "movie.nfo", _movie_xml())
    moviepilot = _task(video, source="moviepilot-csf", created_at=datetime.now(UTC))
    jellyfin = _task(video, source="jellyfin-manual", created_at=datetime.now(UTC))
    jellyfin.media_server_id = "jellyfin-wrong-2013-item"
    jellyfin.title = "The Colony"
    jellyfin.year = 2013
    orchestrator = _orchestrator(tmp_path)

    moviepilot_request = orchestrator._build_search_request(moviepilot, video)
    jellyfin_request = orchestrator._build_search_request(jellyfin, video)

    assert moviepilot_request == jellyfin_request
    assert moviepilot_request.title == "群体"
    assert moviepilot_request.year == 2026
    assert moviepilot_request.alternate_years == ()
    assert moviepilot_request.tmdb_id == "1375646"


def _task(video: Path, *, source: str, created_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        video_path_original=str(video),
        video_path_resolved=str(video),
        media_server_id=None,
        title=None,
        year=None,
        season=None,
        episode=None,
        job=SimpleNamespace(
            source=source,
            created_at=created_at,
            raw_payload_json={},
        ),
    )


def _orchestrator(tmp_path: Path) -> SubtitleOrchestrator:
    events: list[dict] = []
    repository = SimpleNamespace(
        has_task_event=lambda _task_id, _stage: False,
        record_task_event=lambda **kwargs: events.append(kwargs),
    )
    return SubtitleOrchestrator(
        settings=AppSettings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache"),
        repository=repository,
        resolver=None,
        provider_registry=None,
    )
