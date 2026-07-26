from pathlib import Path

import subliminal.core as subliminal_core

import subtitle_sidecar.providers.subliminal_adapter as subliminal_adapter
from subtitle_sidecar.providers.base import (
    DownloadedSubtitle,
    ProviderSearchReport,
    SubtitleCandidate,
    SubtitleSearchRequest,
)
from subtitle_sidecar.providers.registry import ProviderRegistry
from subtitle_sidecar.providers.scheduler import ProviderSearchScheduler
from subtitle_sidecar.providers.subliminal_adapter import ReportingProviderPool, SubliminalProvider


class FakeProvider:
    def __init__(self, name: str = "fake", *, should_fail: bool = False) -> None:
        self.name = name
        self.should_fail = should_fail
        self.calls = 0

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError(f"{self.name} unavailable")
        return [
            SubtitleCandidate(
                provider=self.name,
                language="zh-cn",
                is_bilingual=True,
                format="srt",
                title=request.title,
                source_url=f"https://example.invalid/{self.name}.srt",
                release_info="WEB-DL",
                confidence=0.8,
                raw_metadata={},
            )
        ]

    def download(self, candidate: SubtitleCandidate, target_dir: Path) -> DownloadedSubtitle:
        raise NotImplementedError


class ReportingFailingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__("reporting")
        self._reporter = None

    def set_reporter(self, reporter) -> None:
        self._reporter = reporter

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        assert self._reporter is not None
        self._reporter(
            ProviderSearchReport(
                provider=self.name,
                status="failed",
                error="request_rejected",
            )
        )
        raise RuntimeError("request rejected")


class FakeSubtitle:
    def __init__(
        self,
        *,
        language: str = "zh-CN",
        page_link: str | None = "https://example.invalid/subtitle",
        release_info: str | None = "WEB-DL",
        title: str = "Movie bilingual",
        download_content: bytes = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n",
        metadata: dict | None = None,
    ) -> None:
        self.language = language
        self.page_link = page_link
        self.release_info = release_info
        self.title = title
        self.content: bytes | None = None
        self.download_content = download_content
        self.metadata = metadata or {}


class FakeVideo:
    def __init__(self, path: Path) -> None:
        self.path = path


class FakeEpisodeVideo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.title = "Original Episode Title"
        self.series = "Original Series"
        self.season = 9
        self.episodes = [99]
        self.year = 2000

    @property
    def episode(self) -> int:
        return self.episodes[0]

    @episode.setter
    def episode(self, value: int) -> None:
        raise AssertionError("episode is read-only")


class FakeSubliminalClient:
    def __init__(self) -> None:
        self.scan_calls: list[Path] = []
        self.list_calls: list[tuple[tuple[Path, ...], tuple[str, ...], tuple[str, ...], dict]] = []
        self.list_call_types: list[tuple[type, type]] = []
        self.download_calls: list[
            tuple[tuple[FakeSubtitle, ...], tuple[str, ...], dict]
        ] = []
        self._results: list[FakeSubtitle] = []

    def scan_video(self, video_path: str) -> FakeVideo:
        path = Path(video_path)
        self.scan_calls.append(path)
        return FakeVideo(path)

    def list_subtitles(
        self,
        videos: set[FakeVideo],
        languages: set[str],
        **kwargs,
    ) -> dict[FakeVideo, list[FakeSubtitle]]:
        self.list_call_types.append((type(videos), type(languages)))
        ordered_videos = sorted(videos, key=lambda video: str(video.path))
        self.list_calls.append(
            (
                tuple(video.path for video in ordered_videos),
                tuple(sorted(languages)),
                tuple(kwargs.get("providers") or []),
                kwargs.get("provider_configs") or {},
            )
        )
        return {ordered_videos[0]: list(self._results)}

    def download_subtitles(self, subtitles: list[FakeSubtitle], **kwargs) -> None:
        self.download_calls.append(
            (
                tuple(subtitles),
                tuple(kwargs.get("providers") or []),
                kwargs.get("provider_configs") or {},
            )
        )
        for subtitle in subtitles:
            subtitle.content = subtitle.download_content


class FakeProviderExtension:
    def __init__(self, plugin) -> None:
        self.plugin = plugin


def fake_provider_plugin(*, languages: set[str], valid_video: bool = True):
    class FakeProviderPlugin:
        @classmethod
        def check(cls, video) -> bool:
            return valid_video

        @classmethod
        def check_languages(cls, requested_languages: set[str]) -> set[str]:
            return languages & requested_languages

    return FakeProviderPlugin


def build_request() -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        video_path=Path("/media/Movie.mkv"),
        title="Movie",
        year=2024,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn", "zh-hant"],
    )


def test_registry_collects_candidates() -> None:
    registry = ProviderRegistry([FakeProvider()])

    candidates = registry.search(build_request())

    assert len(candidates) == 1
    assert candidates[0].provider == "fake"
    assert registry.errors == []


def test_registry_keeps_surviving_provider_results_when_one_fails() -> None:
    registry = ProviderRegistry([FakeProvider("broken", should_fail=True), FakeProvider("healthy")])

    candidates = registry.search(build_request())

    assert [candidate.provider for candidate in candidates] == ["healthy"]
    assert registry.errors == ["broken: broken unavailable"]


def test_registry_does_not_duplicate_a_provider_emitted_failure() -> None:
    registry = ProviderRegistry([ReportingFailingProvider()])

    registry.search(build_request())

    failures = [report for report in registry.search_reports if report.status == "failed"]
    assert len(failures) == 1
    assert failures[0].error == "request_rejected"


class EmptyProvider(FakeProvider):
    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        self.calls += 1
        return []


def test_registry_emits_fallback_completed_report_for_non_empty_batch() -> None:
    registry = ProviderRegistry([FakeProvider("plain")])

    candidates = registry.search(build_request())

    assert [candidate.provider for candidate in candidates] == ["plain"]
    completed = [report for report in registry.search_reports if report.status == "completed"]
    assert len(completed) == 1
    assert completed[0].provider == "plain"
    assert completed[0].candidate_count == 1


def test_registry_emits_fallback_completed_report_for_empty_batch() -> None:
    registry = ProviderRegistry([EmptyProvider("empty")])

    candidates = registry.search(build_request())

    assert candidates == []
    completed = [report for report in registry.search_reports if report.status == "completed"]
    assert len(completed) == 1
    assert completed[0].provider == "empty"
    assert completed[0].candidate_count == 0


def test_search_batches_is_lazy_and_defers_lower_provider_until_resumed() -> None:
    first = FakeProvider("first")
    second = FakeProvider("second")
    registry = ProviderRegistry([first, second])

    batches = registry.search_batches(build_request())
    first_batch = next(batches)

    assert [candidate.provider for candidate in first_batch] == ["first"]
    assert first.calls == 1
    assert second.calls == 0


def test_search_batches_resumes_with_next_provider_after_yield() -> None:
    first = FakeProvider("first")
    second = FakeProvider("second")
    registry = ProviderRegistry([first, second])

    batches = registry.search_batches(build_request())

    assert [candidate.provider for candidate in next(batches)] == ["first"]
    assert [candidate.provider for candidate in next(batches)] == ["second"]
    assert list(batches) == []
    assert first.calls == 1
    assert second.calls == 1


def test_search_aggregates_all_batches_for_backward_compatibility() -> None:
    registry = ProviderRegistry([FakeProvider("first"), FakeProvider("second")])

    candidates = registry.search(build_request())

    assert [candidate.provider for candidate in candidates] == ["first", "second"]


def test_search_batches_continue_after_failure_and_empty_results() -> None:
    registry = ProviderRegistry(
        [
            FakeProvider("broken", should_fail=True),
            EmptyProvider("empty"),
            FakeProvider("healthy"),
        ]
    )

    batches = list(registry.search_batches(build_request()))

    assert len(batches) == 1
    assert [candidate.provider for candidate in batches[0]] == ["healthy"]
    assert registry.errors == ["broken: broken unavailable"]


def test_search_batches_use_scheduler_to_bypass_high_priority_cooldown() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"high": 30.0, "low": 0.0},
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    high = FakeProvider("high")
    low = FakeProvider("low")
    registry = ProviderRegistry([high, low], scheduler=scheduler)

    batches = registry.search_batches(build_request())

    assert [candidate.provider for candidate in next(batches)] == ["high"]
    assert [candidate.provider for candidate in next(batches)] == ["low"]
    assert high.calls == 1
    assert low.calls == 1


def test_registry_marks_provider_completed_from_search_finish_time() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class SlowProvider(FakeProvider):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__("slow")
            self.clock = clock

        def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
            self.calls += 1
            self.clock.sleep(4.0)
            return super().search(request)

    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"slow": 10.0},
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    provider = SlowProvider(clock)
    registry = ProviderRegistry([provider], scheduler=scheduler)

    candidates = registry.search(build_request())

    assert [candidate.provider for candidate in candidates] == ["slow"]
    snapshot = scheduler.snapshot()
    assert snapshot["slow"]["ready_at"] == 14.0
    assert snapshot["slow"]["remaining_seconds"] == 10.0


def test_registry_marks_provider_completed_after_exception() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class SlowFailingProvider(FakeProvider):
        def __init__(self, clock: FakeClock) -> None:
            super().__init__("slow-fail", should_fail=True)
            self.clock = clock

        def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
            self.calls += 1
            self.clock.sleep(3.0)
            raise RuntimeError("slow-fail unavailable")

    clock = FakeClock()
    scheduler = ProviderSearchScheduler(
        {"slow-fail": 10.0},
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    provider = SlowFailingProvider(clock)
    registry = ProviderRegistry([provider], scheduler=scheduler)

    assert registry.search(build_request()) == []
    snapshot = scheduler.snapshot()
    assert snapshot["slow-fail"]["ready_at"] == 13.0
    assert snapshot["slow-fail"]["remaining_seconds"] == 10.0


def test_subliminal_provider_uses_injected_client_and_maps_candidates() -> None:
    client = FakeSubliminalClient()
    client._results = [
        FakeSubtitle(
            language="zh-CN",
            page_link="https://example.invalid/zh",
            release_info="BluRay",
            title="Movie bilingual",
            metadata={"kind": "bilingual"},
        )
    ]
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitles"],
    )

    candidates = provider.search(build_request())

    assert len(candidates) == 1
    assert client.scan_calls == [Path("/media/Movie.mkv")]
    assert client.list_calls == [
        (
            (Path("/media/Movie.mkv"),),
                ("lang:zh", "lang:zh-CN", "lang:zh-Hant", "lang:zh-TW"),
            ("opensubtitles",),
            {"opensubtitles": {}},
        )
    ]
    assert client.list_call_types == [(set, set)]
    assert candidates[0].provider == "subliminal:opensubtitles"
    assert candidates[0].language == "zh-cn"
    assert candidates[0].is_bilingual is True
    assert candidates[0].format == "srt"
    assert candidates[0].release_info == "BluRay"


def test_subliminal_provider_does_not_stringify_missing_page_link() -> None:
    client = FakeSubliminalClient()
    client._results = [
        FakeSubtitle(
            page_link=None,
            release_info=None,
            title="No public detail link",
        )
    ]
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitlescom"],
    )

    candidates = provider.search(build_request())

    assert len(candidates) == 1
    assert candidates[0].source_url == ""
    assert candidates[0].release_info == "No public detail link"


def test_subliminal_reports_the_metadata_supplied_to_each_internal_provider() -> None:
    client = FakeSubliminalClient()
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitles", "opensubtitlescom"],
    )
    reports: list[ProviderSearchReport] = []
    provider.set_reporter(reports.append)
    request = SubtitleSearchRequest(
        video_path=Path("/media/新驯龙高手.How.To.Train.Your.Dragon.2025.mkv"),
        title="新·驯龙高手",
        original_title="How to Train Your Dragon",
        imdb_id="tt26743210",
        year=2025,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    provider.search(request)

    completed = {report.provider: report for report in reports if report.status == "completed"}
    assert completed["subliminal:opensubtitles"].search_context == {
        "title": "新·驯龙高手",
        "title_source": "title",
        "year": 2025,
        "imdb_id": "tt26743210",
        "file_name": "新驯龙高手.How.To.Train.Your.Dragon.2025.mkv",
        "media_type": "movie",
    }
    assert completed["subliminal:opensubtitlescom"].search_context == {
        "title": "How to Train Your Dragon",
        "title_source": "original_title",
        "year": 2025,
        "imdb_id": "tt26743210",
        "file_name": "新驯龙高手.How.To.Train.Your.Dragon.2025.mkv",
        "media_type": "movie",
    }


def test_subliminal_provider_defaults_bilingual_to_false_without_clear_metadata() -> None:
    client = FakeSubliminalClient()
    client._results = [
        FakeSubtitle(
            language="zh-TW",
            page_link="https://example.invalid/zh-tw",
            release_info="WEB-DL",
            title="Movie subtitle",
        )
    ]
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitles"],
    )

    candidates = provider.search(build_request())

    assert len(candidates) == 1
    assert candidates[0].language == "zh-hant"
    assert candidates[0].is_bilingual is False


def test_subliminal_provider_expands_chinese_language_aliases() -> None:
    provider = SubliminalProvider(
        client=FakeSubliminalClient(),
        language_factory=lambda language: language,
    )

    assert provider._to_client_languages("zh-cn") == {"zh", "zh-CN"}
    assert provider._to_client_languages("chs") == {"zh", "zh-CN"}
    assert provider._to_client_languages("zh-hans") == {"zh", "zh-CN"}
    assert provider._to_client_languages("zh-hant") == {"zh-Hant", "zh-TW"}
    assert provider._to_client_languages("cht") == {"zh-Hant", "zh-TW"}
    assert provider._to_client_languages("zh-tw") == {"zh-Hant", "zh-TW"}
    assert provider._normalize_language("zh-US") == "zh-cn"


def test_each_opensubtitles_provider_receives_its_supported_chinese_language() -> None:
    client = FakeSubliminalClient()
    manager = {
        "opensubtitles": FakeProviderExtension(
            fake_provider_plugin(languages={"lang:zh"})
        ),
        "opensubtitlescom": FakeProviderExtension(
            fake_provider_plugin(languages={"lang:zh-CN"})
        ),
    }
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitles", "opensubtitlescom"],
        languages=["zh-cn"],
        provider_manager_instance=manager,
    )

    provider.search(build_request())

    assert [call[1] for call in client.list_calls] == [
        ("lang:zh",),
        ("lang:zh-CN",),
    ]
    assert [call[2] for call in client.list_calls] == [
        ("opensubtitles",),
        ("opensubtitlescom",),
    ]


def test_subliminal_provider_applies_episode_metadata_without_writing_read_only_episode() -> None:
    class EpisodeClient(FakeSubliminalClient):
        def __init__(self) -> None:
            super().__init__()
            self.video = FakeEpisodeVideo(Path("/media/Show.S01E04.mkv"))

        def scan_video(self, video_path: str) -> FakeEpisodeVideo:
            self.scan_calls.append(Path(video_path))
            return self.video

    client = EpisodeClient()
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitles"],
    )
    request = SubtitleSearchRequest(
        video_path=Path("/media/Show.S01E04.mkv"),
        title="Show",
        year=2024,
        media_type="episode",
        season=1,
        episode=4,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    provider.search(request)

    assert client.video.series == "Show"
    assert client.video.season == 1
    assert client.video.episodes == [4]
    assert client.video.episode == 4
    assert client.video.year == 2024
    assert client.video.title == "Original Episode Title"


def test_opensubtitlescom_uses_longer_original_title_for_short_query() -> None:
    class EpisodeClient(FakeSubliminalClient):
        def __init__(self) -> None:
            super().__init__()
            self.video = FakeEpisodeVideo(Path("/media/Show.S01E04.mkv"))

        def scan_video(self, video_path: str) -> FakeEpisodeVideo:
            self.scan_calls.append(Path(video_path))
            return self.video

    client = EpisodeClient()
    manager = {
        "opensubtitlescom": FakeProviderExtension(
            fake_provider_plugin(languages={"zh-cn"})
        )
    }
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitlescom"],
        provider_manager_instance=manager,
    )
    request = SubtitleSearchRequest(
        video_path=Path("/media/Show.S01E04.mkv"),
        title="主角",
        original_title="The Lead",
        year=2024,
        media_type="episode",
        season=1,
        episode=4,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    provider.search(request)

    assert client.video.series == "The Lead"


def test_opensubtitlescom_prefers_original_title_and_applies_imdb_id() -> None:
    class MovieClient(FakeSubliminalClient):
        def __init__(self) -> None:
            super().__init__()
            self.video = FakeVideo(Path("/media/Localized.Movie.2025.mkv"))

        def scan_video(self, video_path: str) -> FakeVideo:
            self.scan_calls.append(Path(video_path))
            return self.video

    client = MovieClient()
    manager = {
        "opensubtitlescom": FakeProviderExtension(
            fake_provider_plugin(languages={"zh-cn"})
        )
    }
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitlescom"],
        provider_manager_instance=manager,
    )
    request = SubtitleSearchRequest(
        video_path=client.video.path,
        title="本地化电影名",
        original_title="Original Movie Title",
        imdb_id="tt1234567",
        year=2025,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )

    provider.search(request)

    assert client.video.title == "Original Movie Title"
    assert client.video.imdb_id == "tt1234567"


def test_opensubtitlescom_restores_simplified_chinese_support() -> None:
    class Language:
        def __init__(self, code: str) -> None:
            self.opensubtitlescom = code

    simplified = Language("zh-cn")
    traditional = Language("zh-tw")

    class ProviderPlugin:
        @staticmethod
        def check(video) -> bool:
            return True

        @staticmethod
        def check_languages(languages) -> set:
            return {traditional}

    provider = SubliminalProvider(
        client=FakeSubliminalClient(),
        provider_manager_instance={
            "opensubtitlescom": FakeProviderExtension(ProviderPlugin)
        },
    )

    supported, reason = provider._check_provider_support(
        "opensubtitlescom",
        FakeVideo(Path("/media/Movie.mkv")),
        {simplified, traditional},
    )

    assert supported == {simplified, traditional}
    assert reason is None


def test_opensubtitlescom_skips_unrecoverably_short_query() -> None:
    client = FakeSubliminalClient()
    manager = {
        "opensubtitlescom": FakeProviderExtension(
            fake_provider_plugin(languages={"zh-cn"})
        )
    }
    reports = []
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitlescom"],
        provider_manager_instance=manager,
    )
    request = SubtitleSearchRequest(
        video_path=Path("/media/Show.S01E04.mkv"),
        title="主角",
        original_title="主角",
        year=2024,
        media_type="episode",
        season=1,
        episode=4,
        preferred="bilingual",
        fallback_languages=["zh-cn"],
    )
    provider.set_reporter(reports.append)

    provider.search(request)

    assert client.list_calls == []
    assert reports[-1].status == "skipped"
    assert reports[-1].reason == "query_too_short"


def test_subliminal_provider_download_uses_injected_client(tmp_path: Path) -> None:
    client = FakeSubliminalClient()
    subtitle = FakeSubtitle(download_content=b"downloaded subtitle")
    provider = SubliminalProvider(
        client=client,
        authentication={
            "opensubtitlescom": {
                "username": "user",
                "password": "password",
                "apikey": "api-key",
            }
        },
    )
    candidate = SubtitleCandidate(
        provider="subliminal:opensubtitles",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Movie subtitle",
        source_url="https://example.invalid/subtitle",
        release_info="WEB-DL",
        confidence=1.0,
        raw_metadata={
            "subtitle": subtitle,
            "internal_provider": "opensubtitlescom",
        },
    )
    target_dir = tmp_path / "subtitles"

    downloaded = provider.download(candidate, target_dir)

    assert downloaded.path == target_dir / "downloaded.srt"
    assert downloaded.candidate == candidate
    assert downloaded.path.read_bytes() == b"downloaded subtitle"
    assert client.download_calls == [
        (
            (subtitle,),
            ("opensubtitlescom",),
            {
                "opensubtitlescom": {
                    "username": "user",
                    "password": "password",
                    "apikey": "api-key",
                }
            },
        )
    ]


def test_opensubtitlescom_native_download_retries_after_stale_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from subliminal.exceptions import AuthenticationError

    subtitle = FakeSubtitle()

    class FakeOpenSubtitlesComProvider:
        attempts = 0
        resets = 0
        token = "stale-token"

        @classmethod
        def reset_token(cls) -> None:
            cls.resets += 1

        def download_subtitle(self, source) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise AuthenticationError("stale token")
            source.content = b"downloaded after fresh login"

    source_provider = FakeOpenSubtitlesComProvider()

    class FakeStrictPool:
        observed_kwargs = None

        def __init__(self, **kwargs) -> None:
            self.observed_kwargs = kwargs
            FakeStrictPool.observed_kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def __getitem__(self, name: str):
            assert name == "opensubtitlescom"
            return source_provider

    monkeypatch.setattr(
        "subtitle_sidecar.providers.subliminal_adapter.ReportingProviderPool",
        FakeStrictPool,
    )
    monkeypatch.setattr(
        "subtitle_sidecar.providers.subliminal_adapter._ensure_subliminal_cache",
        lambda: None,
    )
    provider = SubliminalProvider(
        authentication={
            "opensubtitlescom": {
                "username": "user",
                "password": "password",
                "apikey": "api-key",
            }
        },
    )
    candidate = SubtitleCandidate(
        provider="subliminal:opensubtitlescom",
        language="zh-cn",
        is_bilingual=False,
        format="srt",
        title="Episode subtitle",
        source_url="",
        release_info="WEB-DL",
        confidence=1.0,
        raw_metadata={
            "subtitle": subtitle,
            "internal_provider": "opensubtitlescom",
        },
    )

    downloaded = provider.download(candidate, tmp_path)

    assert downloaded.path.read_bytes() == b"downloaded after fresh login"
    assert source_provider.attempts == 2
    assert source_provider.resets == 1
    assert FakeStrictPool.observed_kwargs == {
        "providers": ["opensubtitlescom"],
        "provider_configs": {
            "opensubtitlescom": {
                "username": "user",
                "password": "password",
                "apikey": "api-key",
            }
        },
    }


def test_subliminal_provider_searches_each_configured_provider_and_reports_failures() -> None:
    class SelectiveClient(FakeSubliminalClient):
        def list_subtitles(self, videos, languages, **kwargs):
            if kwargs["providers"] == ["broken"]:
                raise RuntimeError("password secret-value rejected")
            return super().list_subtitles(videos, languages, **kwargs)

    client = SelectiveClient()
    client._results = [FakeSubtitle()]
    reports = []
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["broken", "opensubtitlescom"],
        authentication={"broken": {"password": "secret-value"}},
    )
    provider.set_reporter(reports.append)

    candidates = provider.search(build_request())

    assert [report.status for report in reports] == ["started", "failed", "started", "completed"]
    assert reports[1].provider == "subliminal:broken"
    assert "secret-value" not in reports[1].error
    assert reports[-1].candidate_count == 1
    assert candidates[0].provider == "subliminal:opensubtitlescom"


def test_strict_pool_initialization_error_is_reported_as_failed(monkeypatch) -> None:
    class AuthenticationFailureProvider:
        @classmethod
        def check(cls, video) -> bool:
            return True

        @classmethod
        def check_languages(cls, languages):
            return languages

        def __init__(self, **kwargs) -> None:
            raise RuntimeError("authentication initialization failed")

    class AuthenticationFailureClient(FakeSubliminalClient):
        def list_subtitles(self, videos, languages, **kwargs):
            assert kwargs["pool_class"] is ReportingProviderPool
            video = next(iter(videos))
            with kwargs["pool_class"](
                providers=kwargs["providers"],
                provider_configs=kwargs["provider_configs"],
            ) as pool:
                return {video: pool.list_subtitles(video, languages)}

    client = AuthenticationFailureClient()
    manager = {
        "opensubtitles": FakeProviderExtension(AuthenticationFailureProvider)
    }
    monkeypatch.setattr(subliminal_adapter, "provider_manager", manager)
    monkeypatch.setattr(subliminal_core, "provider_manager", manager)
    reports = []
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: language,
        providers=["opensubtitles"],
        provider_manager_instance=manager,
    )
    provider.set_reporter(reports.append)

    candidates = provider.search(build_request())

    assert candidates == []
    assert reports[-1].status == "failed"
    assert reports[-1].error == "authentication initialization failed"


def test_unsupported_provider_is_skipped_without_network_request() -> None:
    client = FakeSubliminalClient()
    manager = {
        "opensubtitles": FakeProviderExtension(
            fake_provider_plugin(languages={"lang:eng"})
        )
    }
    reports = []
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitles"],
        languages=["zh-cn"],
        provider_manager_instance=manager,
    )
    provider.set_reporter(reports.append)

    candidates = provider.search(build_request())

    assert candidates == []
    assert client.list_calls == []
    assert reports[-1].status == "skipped"
    assert reports[-1].reason == "unsupported_language"
    assert reports[-1].error == "unsupported_language"


def test_invalid_video_provider_is_skipped_without_network_request() -> None:
    client = FakeSubliminalClient()
    manager = {
        "opensubtitles": FakeProviderExtension(
            fake_provider_plugin(languages={"lang:zh"}, valid_video=False)
        )
    }
    reports = []
    provider = SubliminalProvider(
        client=client,
        language_factory=lambda language: f"lang:{language}",
        providers=["opensubtitles"],
        languages=["zh-cn"],
        provider_manager_instance=manager,
    )
    provider.set_reporter(reports.append)

    candidates = provider.search(build_request())

    assert candidates == []
    assert client.list_calls == []
    assert reports[-1].status == "skipped"
    assert reports[-1].reason == "invalid_video"
