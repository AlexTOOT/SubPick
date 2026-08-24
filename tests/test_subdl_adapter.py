from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from subtitle_sidecar.providers.base import SubtitleSearchRequest
from subtitle_sidecar.providers.subdl_adapter import (
    SubdlProvider,
    _matches_episode,
    _search_queries,
    _select_sd_id,
)


class Response:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class Client:
    def __init__(self, archive: bytes):
        self.calls = []
        self.archive = archive

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        params = kwargs.get("params") or {}
        if url.endswith("/movies/search"):
            return Response({"results": [{"sd_id": "sd1665663", "imdb_id": "tt7599146"}]})
        if url.endswith("/subtitles/search"):
            page = params["page"]
            if page == 1:
                return Response({"totalPages": 2, "subtitles": [{"language": "EN", "url": "/subtitle/en.zip"}]})
            return Response({"totalPages": 2, "subtitles": [{"lang": "chinese-bg-code", "name": "Sound.of.Freedom.zh.zip", "subtitlePage": "/subtitle/sd1665663/sound-of-freedom", "url": "/subtitle/3171836-3187161.zip"}]})
        if url == "https://dl.subdl.com/subtitle/3171836-3187161.zip":
            return Response(content=self.archive)
        if url.endswith("/me"):
            return Response({"plan": {"name": "Free"}})
        raise AssertionError(url)


def request() -> SubtitleSearchRequest:
    return SubtitleSearchRequest(video_path=Path("/media/Sound.of.Freedom.2023.mkv"), title="自由之声", original_title="Sound of Freedom", imdb_id="tt7599146", year=2023, media_type="movie", season=None, episode=None, preferred="bilingual", fallback_languages=["zh-cn"])


def test_subdl_v2_pages_through_undocumented_chinese_alias_and_unpacks_archive(tmp_path):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("Sound.of.Freedom.zh.srt", "1\n00:00:01,000 --> 00:00:02,000\nTest\n")
    client = Client(data.getvalue())
    provider = SubdlProvider(api_key="secret", client=client)
    reports = []
    provider.set_reporter(reports.append)

    candidates = provider.search(request())
    downloaded = provider.download(candidates[0], tmp_path)

    assert len(candidates) == 1
    assert candidates[0].raw_metadata["subdl_download_path"] == "/subtitle/3171836-3187161.zip"
    assert candidates[0].raw_metadata["subdl_work_titles"] == []
    assert "secret" not in str(candidates[0])
    assert downloaded.path.suffix == ".srt"
    assert downloaded.path.read_text() == "1\n00:00:01,000 --> 00:00:02,000\nTest\n"
    assert [member.filename for member in downloaded.files] == ["Sound.of.Freedom.zh.srt"]
    search_calls = [call for call in client.calls if call[0].endswith("/subtitles/search")]
    assert [call[1]["params"]["page"] for call in search_calls] == [1, 2]
    assert all("languages" not in call[1]["params"] for call in search_calls)
    progress = next(report for report in reports if report.status == "progress")
    assert progress.reason == "IMDb tt7599146"
    assert progress.search_context == {
        "strategy": "imdb_id",
        "query": "tt7599146",
        "sd_id": "sd1665663",
        "pages": 2,
    }
    completed = next(report for report in reports if report.status == "completed")
    assert completed.reason == "IMDb tt7599146：1 条/2 页"


def test_subdl_usage_uses_v2_bearer_auth():
    client = Client(b"")
    usage = SubdlProvider(api_key="secret", client=client).usage()
    assert usage["plan"]["name"] == "Free"
    assert client.calls[-1][1]["headers"]["Authorization"] == "Bearer secret"


def test_subdl_queries_include_tmdb_identity() -> None:
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "tmdb_id": "67890",
        }
    )

    assert _search_queries(item)[:2] == [
        ("imdb_id", "tt7599146"),
        ("tmdb_id", "67890"),
    ]


def test_subdl_selects_same_title_work_with_matching_year() -> None:
    results = [
        {
            "sd_id": "old-colony",
            "name": "Colony",
            "year": 2013,
        },
        {
            "sd_id": "new-colony",
            "name": "Colony",
            "year": 2026,
        },
    ]
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "title": "群体",
            "original_title": "Colony",
            "imdb_id": None,
            "year": 2026,
        }
    )

    assert _select_sd_id(results, item) == "new-colony"


def test_subdl_prefers_exact_tmdb_identity_over_result_order() -> None:
    results = [
        {"sd_id": "wrong", "tmdb_id": 111, "name": "Colony", "year": 2026},
        {"sd_id": "right", "tmdb_id": 222, "name": "Colony", "year": 2026},
    ]
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "imdb_id": None,
            "tmdb_id": "222",
        }
    )

    assert _select_sd_id(results, item) == "right"


def test_subdl_rejects_search_results_with_only_distant_years() -> None:
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "title": "群体",
            "original_title": "Colony",
            "imdb_id": None,
            "year": 2026,
        }
    )

    assert _select_sd_id(
        [{"sd_id": "old-colony", "name": "Colony", "year": 2013}],
        item,
    ) is None


def test_subdl_rejects_unrelated_work_when_search_result_omits_year() -> None:
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "title": "群体",
            "original_title": "Colony",
            "imdb_id": None,
            "year": 2026,
        }
    )

    assert _select_sd_id(
        [{"sd_id": "wrong", "name": "Completely Different Movie"}],
        item,
    ) is None


def test_subdl_episode_metadata_rejects_wrong_season() -> None:
    item = request()
    item = SubtitleSearchRequest(
        **{
            **item.__dict__,
            "media_type": "episode",
            "season": 2,
            "episode": 3,
        }
    )

    assert _matches_episode({"season": "2", "episode": "E03"}, item) is True
    assert _matches_episode({"season": 1, "episode": 3}, item) is False
    assert _matches_episode({"season": 2, "episode": 4}, item) is False
