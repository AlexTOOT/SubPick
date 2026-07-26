from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from subtitle_sidecar.providers.assrt_adapter import (
    AssrtApiError,
    AssrtProvider,
    _download_direct_files,
    _parse_7zip_members,
    _supported_direct_files,
    _validated_7zip_members,
)
from subtitle_sidecar.providers.base import SubtitleSearchRequest
from subtitle_sidecar.providers.negative_cache import ProviderNegativeCache


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/v1/sub/search"):
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "id": 100,
                                "native_name": "测试电影",
                                "videoname": "Test.Movie.2026.1080p.WEB-DL",
                                "subtype": "Subrip(srt)",
                                "vote_score": 25,
                                "lang": {"desc": "简体中文 双语", "langlist": {"langchs": True}},
                            },
                            {
                                "id": 101,
                                "native_name": "Korean only",
                                "lang": {"desc": "韩 双语", "langlist": {"langkor": True, "langdou": True}},
                            },
                        ]
                    },
                }
            )
        if url.endswith("/v1/sub/detail"):
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "filelist": [
                                    {"f": "readme.txt", "url": "https://download.invalid/readme"},
                                    {"f": "Test.zh-CN.srt", "url": "https://download.invalid/test.srt"},
                                ]
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v1/user/quota"):
            return FakeResponse({"status": 0, "user": {"quota": 5}})
        if url == "https://download.invalid/test.srt":
            return FakeResponse(content=b"1\n00:00:01,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n")
        raise AssertionError(url)


class BrokenDirectClient:
    def __init__(self) -> None:
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise AssrtApiError("assrt_download_failed")


class WrongYearDetailClient(FakeClient):
    def get(self, url, **kwargs):
        if url.endswith("/v1/sub/search"):
            self.calls.append((url, kwargs))
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "id": 200,
                                "native_name": "目标电影",
                                "videoname": "Target.Movie.WEB-DL",
                                "subtype": "Subrip(srt)",
                                "lang": {"desc": "Chinese", "langlist": {"langchs": True}},
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v1/sub/detail"):
            self.calls.append((url, kwargs))
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "filelist": [
                                    {
                                        "f": "Target.Movie.2010.zh-CN.srt",
                                        "url": "https://download.invalid/wrong-year.srt",
                                    }
                                ]
                            }
                        ]
                    },
                }
            )
        raise AssertionError(f"unexpected binary download: {url}")


class EpisodeSearchClient(FakeClient):
    def __init__(self, *, season_has_result: bool) -> None:
        super().__init__()
        self.season_has_result = season_has_result

    def get(self, url, **kwargs):
        if url.endswith("/v1/sub/search"):
            self.calls.append((url, kwargs))
            query = kwargs["params"]["q"]
            has_result = self.season_has_result or "S01E01" in query
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "id": 710623,
                                "native_name": "Series Name Season 1",
                                "videoname": "Series.Name.S01.1080p.WEB-DL",
                                "subtype": "Subrip(srt)",
                                "vote_score": 25,
                                "lang": {
                                    "desc": "Chinese",
                                    "langlist": {"langchs": True},
                                },
                            }
                        ]
                        if has_result
                        else []
                    },
                }
            )
        return super().get(url, **kwargs)


class EmptySearchClient(FakeClient):
    def get(self, url, **kwargs):
        if url.endswith("/v1/sub/search"):
            self.calls.append((url, kwargs))
            return FakeResponse({"status": 0, "sub": {"subs": []}})
        return super().get(url, **kwargs)


class ModernAssrtClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        archive = BytesIO()
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("Only.Murders.in.the.Building.S01E01.ass", "[Script Info]\n")
            bundle.writestr("Only.Murders.in.the.Building.S01E02.ass", "[Script Info]\n")
        self.archive = archive.getvalue()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/v1/sub/search"):
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "fileid": "710623",
                                "m_title": "Only.Murders.in.the.Building.S01",
                                "m_subtype": "SSA",
                                "m_lang": "English Chinese Bilingual",
                                "m_extras": {"langchs": "1", "langdou": "1"},
                            }
                        ]
                    },
                }
            )
        if url.endswith("/v1/sub/detail"):
            return FakeResponse(
                {
                    "status": 0,
                    "sub": {
                        "subs": [
                            {
                                "id": 710623,
                                "filename": "Only.Murders.in.the.Building.S01.zip",
                                "url": "https://download.invalid/season.zip",
                            }
                        ]
                    },
                }
            )
        if url == "https://download.invalid/season.zip":
            return FakeResponse(content=self.archive)
        raise AssertionError(url)


def test_assrt_search_download_and_rate_limit(tmp_path: Path) -> None:
    client = FakeClient()
    now = [0.0]
    sleeps = []

    def sleeper(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    provider = AssrtProvider(
        token="test-token",
        client=client,
        clock=lambda: now[0],
        sleeper=sleeper,
    )
    request = SubtitleSearchRequest(
        video_path=Path("/media/Test.Movie.2026.mkv"),
        title="测试电影",
        year=2026,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    candidates = provider.search(request)
    downloaded = provider.download(candidates[0], tmp_path)
    quota = provider.quota()

    assert len(candidates) == 1
    assert candidates[0].provider == "assrt"
    assert candidates[0].is_bilingual is True
    assert candidates[0].raw_metadata["assrt_subtitle_id"] == 100
    assert downloaded.path.name == "assrt-100-1-Test.zh-CN.srt"
    assert downloaded.path.read_bytes().startswith(b"1\n00:00:01")
    assert [member.filename for member in downloaded.files] == ["Test.zh-CN.srt"]
    assert candidates[0].source_url == "https://assrt.net/xml/sub/100/100.xml"
    assert quota == 5
    api_calls = [call for call in client.calls if "api.assrt.net" in call[0]]
    assert len(api_calls) == 3
    assert api_calls[0][1]["params"]["q"] == "测试电影 2026"
    assert all(call[1]["headers"]["Authorization"] == "Bearer test-token" for call in api_calls)
    assert sleeps == [12.0, 12.0]


def test_assrt_skips_without_token() -> None:
    reports = []
    provider = AssrtProvider(token="", client=FakeClient())
    provider.set_reporter(reports.append)

    candidates = provider.search(
        SubtitleSearchRequest(
            video_path=Path("/media/Test.mkv"),
            title="测试",
            year=None,
            media_type="movie",
            season=None,
            episode=None,
            preferred="bilingual",
            fallback_languages=["zh-cn"],
        )
    )

    assert candidates == []
    assert reports[-1].status == "skipped"
    assert reports[-1].error == "missing_token"


def test_assrt_rejects_conflicting_detail_filename_before_binary_download(tmp_path: Path) -> None:
    client = WrongYearDetailClient()
    provider = AssrtProvider(token="test-token", client=client, sleeper=lambda _: None)
    request = SubtitleSearchRequest(
        video_path=Path("/media/Target.Movie.2025.mkv"),
        title="目标电影",
        original_title="Target Movie",
        year=2025,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    candidate = provider.search(request)[0]
    try:
        provider.download(candidate, tmp_path)
    except AssrtApiError as error:
        assert str(error) == "assrt_detail_year_mismatch:2010:2025"
    else:
        raise AssertionError("conflicting detail filename was accepted")
    assert not any("wrong-year.srt" in call[0] for call in client.calls)


def test_assrt_rejects_unrelated_chinese_title_collision() -> None:
    provider = AssrtProvider(token="test-token", client=FakeClient())
    request = SubtitleSearchRequest(
        video_path=Path("/media/My.Sister.2021.mkv"),
        title="我的姐姐",
        year=2021,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    assert provider.search(request) == []


def test_assrt_episode_search_prefers_season_pack() -> None:
    client = EpisodeSearchClient(season_has_result=True)
    provider = AssrtProvider(token="test-token", client=client, sleeper=lambda _: None)
    request = SubtitleSearchRequest(
        video_path=Path("/media/Series.Name.S01E01.mkv"),
        title="Series Name",
        original_title="Series Name - S01E01",
        year=2026,
        media_type="episode",
        season=1,
        episode=1,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    candidates = provider.search(request)

    assert len(candidates) == 1
    assert [call[1]["params"]["q"] for call in client.calls] == ["Series Name S01"]


def test_assrt_direct_files_prefer_explicit_chinese_tracks() -> None:
    selected = _supported_direct_files(
        [
            {"f": "Show.S01E01.[eng].srt", "url": "https://download.invalid/eng"},
            {"f": "Show.S01E01.[chi].srt", "url": "https://download.invalid/chi"},
            {"f": "Show.S01E01.[jpn].ass", "url": "https://download.invalid/jpn"},
            {"f": "Show.S01E01.[zho].ass", "url": "https://download.invalid/zho"},
        ]
    )

    assert selected == [
        ("https://download.invalid/chi", "Show.S01E01.[chi].srt"),
        ("https://download.invalid/zho", "Show.S01E01.[zho].ass"),
    ]


def test_assrt_direct_files_stop_after_three_failures(tmp_path: Path) -> None:
    client = BrokenDirectClient()
    selected = [
        (f"https://download.invalid/{index}", f"Show.S01E{index:02d}.[chi].ass")
        for index in range(1, 11)
    ]

    members = _download_direct_files(
        client=client,
        selected=selected,
        subtitle_id=710623,
        target_dir=tmp_path,
        timeout_seconds=1,
    )

    assert members == []
    assert len(client.calls) == 3


def test_parse_7zip_members_rejects_unsafe_paths() -> None:
    output = """Path = archive.rar
Path = Show.S01E01.[chi].srt
Path = ../escape.srt
Path = extras/Show.S01E02.[eng].ass
"""

    assert _parse_7zip_members(output) == [
        "Show.S01E01.[chi].srt",
        "extras/Show.S01E02.[eng].ass",
    ]


def test_validated_7zip_members_rejects_unsafe_paths() -> None:
    output = """Path = archive.rar
Path = Show.S01E01.[chi].srt
Size = 123
Path = ../escape.srt
Size = 10
"""

    try:
        _validated_7zip_members(output)
    except AssrtApiError as error:
        assert str(error) == "assrt_unsafe_archive_member"
    else:
        raise AssertionError("unsafe archive member was accepted")


def test_assrt_episode_search_falls_back_to_exact_episode() -> None:
    client = EpisodeSearchClient(season_has_result=False)
    provider = AssrtProvider(token="test-token", client=client, sleeper=lambda _: None)
    request = SubtitleSearchRequest(
        video_path=Path("/media/Series.Name.S01E01.mkv"),
        title="Series Name",
        original_title="Series Name - S01E01",
        year=2026,
        media_type="episode",
        season=1,
        episode=1,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    candidates = provider.search(request)

    assert len(candidates) == 1
    assert [call[1]["params"]["q"] for call in client.calls] == ["Series Name S01", "Series Name S01E01"]


def test_assrt_negative_cache_reuses_season_miss_but_not_another_episode_miss() -> None:
    client = EmptySearchClient()
    now = [0.0]
    cache = ProviderNegativeCache(ttl_seconds=12 * 60 * 60, clock=lambda: now[0])
    provider = AssrtProvider(
        token="test-token",
        client=client,
        sleeper=lambda _: None,
        negative_cache=cache,
    )

    def request(episode: int) -> SubtitleSearchRequest:
        return SubtitleSearchRequest(
            video_path=Path(f"/media/Series.Name.S01E{episode:02d}.mkv"),
            title="Series Name",
            original_title="Series Name",
            series_id="series-1",
            year=2026,
            media_type="episode",
            season=1,
            episode=episode,
            preferred="bilingual",
            fallback_languages=["zh-cn"],
        )

    assert provider.search(request(1)) == []
    assert [call[1]["params"]["q"] for call in client.calls] == [
        "Series Name S01",
        "Series Name S01E01",
    ]

    client.calls.clear()
    assert provider.search(request(2)) == []
    assert [call[1]["params"]["q"] for call in client.calls] == ["Series Name S01E02"]

    client.calls.clear()
    assert provider.search(request(2)) == []
    assert client.calls == []

    now[0] += 12 * 60 * 60 + 1
    assert provider.search(request(2)) == []
    assert [call[1]["params"]["q"] for call in client.calls] == [
        "Series Name S01",
        "Series Name S01E02",
    ]


def test_assrt_supports_current_search_fields_and_zip_season_pack(tmp_path: Path) -> None:
    client = ModernAssrtClient()
    provider = AssrtProvider(token="test-token", client=client, sleeper=lambda _: None)
    request = SubtitleSearchRequest(
        video_path=Path("/media/Series.S01E01.mkv"),
        title="Series",
        original_title="Only Murders in the Building",
        year=2026,
        media_type="episode",
        season=1,
        episode=1,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    candidates = provider.search(request)
    downloaded = provider.download(candidates[0], tmp_path)

    assert candidates[0].raw_metadata["assrt_subtitle_id"] == 710623
    assert candidates[0].is_bilingual is True
    assert candidates[0].source_url == "https://assrt.net/xml/sub/710/710623.xml"
    assert [member.filename for member in downloaded.files] == [
        "Only.Murders.in.the.Building.S01E01.ass",
        "Only.Murders.in.the.Building.S01E02.ass",
    ]
