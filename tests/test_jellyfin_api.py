from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app
from subtitle_sidecar.db.repository import JellyfinMediaItemData, Repository
from subtitle_sidecar.db.session import session_scope


class FakeJellyfinClient:
    def __init__(self) -> None:
        self.libraries = [
            {"id": "movie-lib", "name": "电影", "collection_type": "movies"},
            {"id": "tv-lib", "name": "TV", "collection_type": "tvshows"},
        ]
        self.items_by_library: dict[str, list[dict]] = {}
        self.items_by_id: dict[str, dict] = {}
        self.primary_images: dict[str, tuple[bytes, str] | int] = {}
        self.image_requests: list[str] = []

    def list_libraries(self) -> list[dict]:
        return list(self.libraries)

    def list_library_items(self, library_id: str) -> list[dict]:
        return list(self.items_by_library.get(library_id, []))

    def get_item(self, item_id: str) -> dict:
        return dict(self.items_by_id[item_id])

    def get_primary_image(self, item_id: str) -> tuple[bytes, str]:
        self.image_requests.append(item_id)
        result = self.primary_images.get(item_id)
        if result == 404:
            request = httpx.Request("GET", "http://jellyfin.test")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("image not found", request=request, response=response)
        if result is None:
            raise AssertionError(f"unexpected image request for {item_id}")
        return result


def _save_jellyfin_settings(client: TestClient, *, api_key: str = "secret") -> None:
    response = client.put(
        "/api/v1/jellyfin/settings",
        json={
            "server_url": "http://jellyfin.test",
            "api_key": api_key,
            "user_id": "user-1",
        },
    )
    assert response.status_code == 200


def _cache_jellyfin_item(app, **overrides) -> None:
    data = {
        "jellyfin_item_id": "item-1",
        "library_id": "movie-lib",
        "library_name": "Movie Library",
        "item_type": "Movie",
        "name": "Movie",
        "path": "/media/Movie.mkv",
        "series_name": None,
        "year": 2024,
        "season": None,
        "episode": None,
        "provider_ids": {},
        "production_locations": [],
        "primary_image_tag": "tag-1",
        "subtitle_status": "unknown",
        "has_external_chinese_subtitle": False,
        "has_embedded_chinese_subtitle": False,
        "has_bilingual_subtitle": False,
    }
    data.update(overrides)
    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        repo.upsert_jellyfin_media_item(JellyfinMediaItemData(**data))


def test_jellyfin_settings_can_be_saved_and_libraries_are_separated(tmp_path: Path):
    fake_client = FakeJellyfinClient()
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        save_response = client.put(
            "/api/v1/jellyfin/settings",
            json={
                "server_url": "http://jellyfin.test",
                "api_key": "secret",
                "user_id": "user-1",
            },
        )
        settings_response = client.get("/api/v1/jellyfin/settings")
        retained_response = client.put(
            "/api/v1/jellyfin/settings",
            json={
                "server_url": "http://jellyfin.test",
                "user_id": "user-1",
            },
        )
        libraries_response = client.get("/api/v1/jellyfin/libraries")

    assert save_response.status_code == 200
    assert settings_response.status_code == 200
    assert retained_response.json()["api_key_configured"] is True
    assert settings_response.json() == {
        "server_url": "http://jellyfin.test",
        "user_id": "user-1",
        "configured": True,
        "api_key_configured": True,
    }
    assert libraries_response.status_code == 200
    assert libraries_response.json()["libraries"] == fake_client.libraries


def test_jellyfin_connection_check_records_verified_health(tmp_path: Path):
    fake_client = FakeJellyfinClient()
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        _save_jellyfin_settings(client)
        before = client.get("/api/v1/diagnostics").json()
        checked = client.post("/api/v1/jellyfin/check")
        after = client.get("/api/v1/diagnostics").json()

    assert before["jellyfin"]["connected"] is False
    assert checked.json() == {"connected": True, "library_count": 2}
    assert after["jellyfin"]["connected"] is True
    assert after["jellyfin"]["last_checked_at"]


def test_recent_jellyfin_media_is_sorted_by_jellyfin_date_across_libraries(
    tmp_path: Path,
):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-old",
            library_id="movie-lib",
            library_name="电影",
            item_type="Movie",
            name="Older Movie",
            jellyfin_date_created=datetime(2026, 7, 1, tzinfo=UTC),
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="series-mid",
            library_id="tv-lib",
            library_name="TV",
            item_type="Series",
            name="Recent Series",
            path="",
            jellyfin_date_created=datetime(2026, 7, 10, tzinfo=UTC),
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-mid",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_id="series-mid",
            series_name="Recent Series",
            season=1,
            episode=1,
            subtitle_status="missing",
            jellyfin_date_created=datetime(2026, 7, 10, tzinfo=UTC),
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-new",
            library_id="movie-lib",
            library_name="电影",
            item_type="Movie",
            name="Newest Movie",
            jellyfin_date_created=datetime(2026, 7, 20, tzinfo=UTC),
        )
        response = client.get("/api/v1/jellyfin/recent?limit=3")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == ["movie-new", "series-mid", "movie-old"]
    assert items[1]["library_name"] == "TV"
    assert items[1]["item_type"] == "Series"


def test_moviepilot_callback_enriches_task_from_single_jellyfin_item(tmp_path: Path):
    fake_client = FakeJellyfinClient()
    fake_client.items_by_id["movie-2025"] = {
        "id": "movie-2025",
        "name": "新·驯龙高手",
        "original_title": "How to Train Your Dragon",
        "series_name": None,
        "type": "Movie",
        "path": "/media/Movie/新·驯龙高手 (2025).mkv",
        "year": 2025,
        "season": None,
        "episode": None,
        "provider_ids": {"Imdb": "tt26743210", "Tmdb": "1087192"},
    }
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        _save_jellyfin_settings(client)
        response = client.post(
            "/api/v1/add-job",
            json={
                "physical_video_file_full_path": "/media/Movie/新·驯龙高手 (2025).mkv",
                "media_server_inside_video_id": "movie-2025",
            },
        )

    assert response.status_code == 200
    with session_scope(app.state.engine) as session:
        task = Repository(session).list_video_tasks(limit=1)[0]
        assert task.title == "新·驯龙高手"
        assert task.year == 2025
        assert task.job.raw_payload_json["jellyfin_metadata"]["provider_ids"] == {
            "Imdb": "tt26743210",
            "Tmdb": "1087192",
        }
        assert any(event.stage == "metadata" for event in task.events)


def test_jellyfin_scan_caches_subtitle_status_without_creating_tasks(tmp_path: Path):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    (tmp_path / "Movie.zh.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        encoding="utf-8",
    )
    fake_client = FakeJellyfinClient()
    fake_client.items_by_library["movie-lib"] = [
        {
            "id": "movie-1",
            "name": "Movie",
            "type": "Movie",
            "path": str(media),
            "year": 2024,
            "provider_ids": {"Tmdb": "123"},
            "production_locations": ["China"],
            "primary_image_tag": "tag-1",
            "media_streams": [],
        }
    ]
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        client.put(
            "/api/v1/jellyfin/settings",
            json={
                "server_url": "http://jellyfin.test",
                "api_key": "secret",
                "user_id": "user-1",
            },
        )
        scan_response = client.post("/api/v1/jellyfin/libraries/movie-lib/scan")
        items_response = client.get("/api/v1/jellyfin/libraries/movie-lib/items")

    assert scan_response.status_code == 200
    assert scan_response.json()["scanned_count"] == 1
    assert scan_response.json()["created"] == 1
    assert scan_response.json()["updated"] == 0
    assert items_response.status_code == 200
    item = items_response.json()["items"][0]
    assert item["jellyfin_item_id"] == "movie-1"
    assert item["library_name"] == "电影"
    assert item["subtitle_status"] == "has_chinese"
    assert item["has_external_chinese_subtitle"] is True
    assert item["has_embedded_chinese_subtitle"] is False
    assert item["production_locations"] == ["China"]

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        assert repo.list_jobs() == []


def test_jellyfin_scan_is_incremental_and_removes_missing_items(tmp_path: Path):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    fake_client = FakeJellyfinClient()
    fake_client.items_by_library["movie-lib"] = [
        {
            "id": "movie-1",
            "name": "Movie",
            "type": "Movie",
            "path": str(media),
            "year": 2024,
            "primary_image_tag": "tag-1",
            "media_streams": [],
        },
        {
            "id": "movie-removed",
            "name": "Removed",
            "type": "Movie",
            "path": str(media),
            "year": 2024,
            "primary_image_tag": "tag-removed",
            "media_streams": [],
        },
    ]
    fake_client.primary_images["movie-removed"] = (b"removed-image", "image/jpeg")
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        _save_jellyfin_settings(client)
        first = client.post("/api/v1/jellyfin/libraries/movie-lib/scan")
        cached_image = client.get("/api/v1/jellyfin/items/movie-removed/primary-image")
        fake_client.items_by_library["movie-lib"] = [fake_client.items_by_library["movie-lib"][0]]
        second = client.post("/api/v1/jellyfin/libraries/movie-lib/scan")
        items = client.get("/api/v1/jellyfin/libraries/movie-lib/items")

    assert first.json()["created"] == 2
    assert cached_image.status_code == 200
    assert second.json()["unchanged"] == 1
    assert second.json()["removed"] == 1
    assert [item["jellyfin_item_id"] for item in items.json()["items"]] == ["movie-1"]
    assert not list((tmp_path / "data" / "cache" / "jellyfin-images").glob("movie-removed+*"))


def test_jellyfin_batch_add_creates_jobs_from_cached_media_items(tmp_path: Path):
    media = tmp_path / "Show.S01E02.mkv"
    media.write_bytes(b"video")
    fake_client = FakeJellyfinClient()
    fake_client.items_by_library["tv-lib"] = [
        {
            "id": "episode-1",
            "name": "第 2 集",
            "series_name": "剧集",
            "type": "Episode",
            "path": str(media),
            "year": 2025,
            "season": 1,
            "episode": 2,
            "provider_ids": {"Tmdb": "456"},
            "primary_image_tag": "tag-2",
            "media_streams": [],
        }
    ]
    enqueued_task_ids: list[int] = []
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client
    app.state.enqueue_task = enqueued_task_ids.append

    with TestClient(app) as client:
        client.put(
            "/api/v1/jellyfin/settings",
            json={
                "server_url": "http://jellyfin.test",
                "api_key": "secret",
                "user_id": "user-1",
            },
        )
        client.post("/api/v1/jellyfin/libraries/tv-lib/scan")
        add_response = client.post(
            "/api/v1/jellyfin/tasks",
            json={"item_ids": ["episode-1"]},
        )

    assert add_response.status_code == 200
    result = add_response.json()["results"][0]
    assert result["ok"] is True
    assert result["task_id"] in enqueued_task_ids

    with session_scope(app.state.engine) as session:
        repo = Repository(session)
        task = repo.get_video_task(result["task_id"])

    assert task is not None
    assert task.video_path_original == str(media)
    assert task.media_server_id == "episode-1"
    assert task.title == "剧集"
    assert task.season == 1
    assert task.episode == 2


def test_jellyfin_movie_library_tree_returns_media_cards_from_cache(tmp_path: Path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-1",
            library_id="movie-lib",
            library_name="Movies",
            item_type="Movie",
            name="Movie A",
            year=2024,
            path="/media/Movie A.mkv",
            production_locations=["China"],
            primary_image_tag="poster-a",
            subtitle_status="has_chinese",
            has_external_chinese_subtitle=True,
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-2",
            library_id="movie-lib",
            library_name="Movies",
            item_type="Movie",
            name="Movie B",
            year=2023,
            path="/media/Movie B.mkv",
            primary_image_tag=None,
            subtitle_status="missing",
        )
        response = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")

    assert response.status_code == 200
    body = response.json()
    assert body["library_id"] == "movie-lib"
    assert body["library_name"] == "Movies"
    assert body["collection_type"] == "movies"
    assert body["series"] == []
    assert body["movies"] == [
        {
            "id": "movie-1",
            "name": "Movie A",
            "year": 2024,
            "status": "has_chinese",
            "has_external_chinese_subtitle": True,
            "has_embedded_chinese_subtitle": False,
            "production_locations": ["China"],
            "path": "/media/Movie A.mkv",
            "primary_image_tag": "poster-a",
                "image_url": "/api/v1/jellyfin/items/movie-1/primary-image",
                "ignored": False,
                "date_created": None,
        },
        {
            "id": "movie-2",
            "name": "Movie B",
            "year": 2023,
            "status": "missing",
            "has_external_chinese_subtitle": False,
            "has_embedded_chinese_subtitle": False,
            "production_locations": [],
            "path": "/media/Movie B.mkv",
            "primary_image_tag": None,
                "image_url": None,
                "ignored": False,
                "date_created": None,
        },
    ]


def test_jellyfin_tv_library_tree_groups_series_seasons_and_rolls_up_status(tmp_path: Path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-1",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_name="Series A",
            season=1,
            episode=1,
            path="/media/Series A/S01E01.mkv",
            primary_image_tag="poster-1",
            subtitle_status="has_chinese",
            has_external_chinese_subtitle=True,
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-2",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 2",
            series_name="Series A",
            season=1,
            episode=2,
            path="/media/Series A/S01E02.mkv",
            subtitle_status="missing",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-3",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_name="Series B",
            season=1,
            episode=1,
            path="/media/Series B/S01E01.mkv",
            subtitle_status="missing",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-4",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 2",
            series_name="Series B",
            season=1,
            episode=2,
            path="/media/Series B/S01E02.mkv",
            subtitle_status="missing",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-5",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_name="Series C",
            season=1,
            episode=1,
            path="/media/Series C/S01E01.mkv",
            subtitle_status="has_chinese",
            has_embedded_chinese_subtitle=True,
        )
        response = client.get("/api/v1/jellyfin/libraries/tv-lib/tree")

    assert response.status_code == 200
    body = response.json()
    assert body["collection_type"] == "tvshows"
    assert body["movies"] == []
    series = body["series"]
    assert [item["name"] for item in series] == ["Series A", "Series B", "Series C"]
    assert [item["status"] for item in series] == ["partial", "missing", "has_chinese"]
    assert series[0]["seasons"][0]["status"] == "partial"
    assert series[1]["seasons"][0]["status"] == "missing"
    assert series[2]["seasons"][0]["status"] == "has_chinese"
    assert series[0]["has_external_chinese_subtitle"] is True
    assert series[0]["ignored"] is False
    assert series[0]["has_embedded_chinese_subtitle"] is False
    assert series[2]["has_external_chinese_subtitle"] is False
    assert series[2]["has_embedded_chinese_subtitle"] is True
    assert series[0]["seasons"][0]["has_external_chinese_subtitle"] is True
    assert series[2]["seasons"][0]["has_embedded_chinese_subtitle"] is True
    assert series[0]["seasons"][0]["episodes"] == [
        {
            "id": "episode-1",
            "name": "Episode 1",
            "year": 2024,
            "status": "has_chinese",
            "has_external_chinese_subtitle": True,
            "has_embedded_chinese_subtitle": False,
            "production_locations": [],
            "path": "/media/Series A/S01E01.mkv",
            "primary_image_tag": "poster-1",
                "image_url": "/api/v1/jellyfin/items/episode-1/primary-image",
                "ignored": False,
                "date_created": None,
            "season": 1,
            "episode": 1,
        },
        {
            "id": "episode-2",
            "name": "Episode 2",
            "year": 2024,
            "status": "missing",
            "has_external_chinese_subtitle": False,
            "has_embedded_chinese_subtitle": False,
            "production_locations": [],
            "path": "/media/Series A/S01E02.mkv",
            "primary_image_tag": "tag-1",
                "image_url": "/api/v1/jellyfin/items/episode-2/primary-image",
                "ignored": False,
                "date_created": None,
            "season": 1,
            "episode": 2,
        },
    ]


def test_jellyfin_tree_uses_series_poster_and_metadata(tmp_path: Path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="series-1",
            library_id="tv-lib",
            library_name="TV",
            item_type="Series",
            name="Series A",
            year=2020,
            path="",
            production_locations=["Hong Kong"],
            primary_image_tag="series-poster",
            subtitle_status="unknown",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-1",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_id="series-1",
            series_name="Series A",
            season=1,
            episode=1,
            path="/media/Series A/S01E01.mkv",
            primary_image_tag="episode-poster",
            subtitle_status="missing",
        )
        response = client.get("/api/v1/jellyfin/libraries/tv-lib/tree")

    series = response.json()["series"]
    assert len(series) == 1
    assert series[0]["id"] == "series-1"
    assert series[0]["name"] == "Series A"
    assert series[0]["year"] == 2020
    assert series[0]["status"] == "missing"
    assert series[0]["production_locations"] == ["Hong Kong"]
    assert series[0]["primary_image_tag"] == "series-poster"
    assert series[0]["image_url"] == "/api/v1/jellyfin/items/series-1/primary-image"
    assert series[0]["ignored"] is False


def test_jellyfin_ignore_api_persists_across_scan_and_updates_tree_status(tmp_path: Path):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")
    fake_client = FakeJellyfinClient()
    fake_client.items_by_library["movie-lib"] = [
        {
            "id": "movie-1",
            "name": "Movie",
            "type": "Movie",
            "path": str(media),
            "year": 2024,
            "media_streams": [],
        }
    ]
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        _save_jellyfin_settings(client)
        client.post("/api/v1/jellyfin/libraries/movie-lib/scan")
        ignore_response = client.post("/api/v1/jellyfin/items/movie-1/ignore")
        tree_ignored = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")
        items_ignored = client.get("/api/v1/jellyfin/libraries/movie-lib/items")
        client.post("/api/v1/jellyfin/libraries/movie-lib/scan")
        tree_after_scan = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")
        unignore_response = client.post("/api/v1/jellyfin/items/movie-1/unignore")
        tree_unignored = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")

    assert ignore_response.status_code == 200
    assert ignore_response.json() == {
        "item_id": "movie-1",
        "item_type": "Movie",
        "ignored": True,
    }
    assert tree_ignored.json()["movies"][0]["status"] == "ignored"
    assert tree_ignored.json()["movies"][0]["ignored"] is True
    assert items_ignored.json()["items"][0]["subtitle_status"] == "missing"
    assert items_ignored.json()["items"][0]["ignored"] is True
    assert tree_after_scan.json()["movies"][0]["status"] == "ignored"
    assert unignore_response.json()["ignored"] is False
    assert tree_unignored.json()["movies"][0]["status"] == "missing"


def test_jellyfin_ignore_api_accepts_series_and_rejects_episode(tmp_path: Path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="series-1",
            library_id="tv-lib",
            library_name="TV",
            item_type="Series",
            name="Series A",
            path="",
            subtitle_status="unknown",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="episode-1",
            library_id="tv-lib",
            library_name="TV",
            item_type="Episode",
            name="Episode 1",
            series_id="series-1",
            series_name="Series A",
            season=1,
            episode=1,
            path="/media/Series A/S01E01.mkv",
            subtitle_status="missing",
        )
        ignored_series = client.post("/api/v1/jellyfin/items/series-1/ignore")
        tree = client.get("/api/v1/jellyfin/libraries/tv-lib/tree")
        rejected_episode = client.post("/api/v1/jellyfin/items/episode-1/ignore")
        missing = client.post("/api/v1/jellyfin/items/not-found/ignore")

    assert ignored_series.status_code == 200
    assert tree.json()["series"][0]["status"] == "ignored"
    assert tree.json()["series"][0]["ignored"] is True
    assert tree.json()["series"][0]["seasons"][0]["status"] == "missing"
    assert rejected_episode.status_code == 400
    assert rejected_episode.json()["detail"] == "only Movie and Series items can be ignored"
    assert missing.status_code == 404


def test_jellyfin_primary_image_proxy_uses_server_config_and_does_not_leak_api_key(
    tmp_path: Path,
):
    fake_client = FakeJellyfinClient()
    fake_client.primary_images["movie-1"] = (b"image-bytes", "image/jpeg")
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client
    fake_client.primary_images["movie-3"] = 404

    with TestClient(app) as client:
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-1",
            primary_image_tag="poster-a",
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-2",
            primary_image_tag=None,
        )
        _cache_jellyfin_item(
            app,
            jellyfin_item_id="movie-3",
            primary_image_tag="poster-missing",
        )
        unconfigured_response = client.get("/api/v1/jellyfin/items/movie-1/primary-image")
        _save_jellyfin_settings(client, api_key="secret-image-token")
        image_response = client.get("/api/v1/jellyfin/items/movie-1/primary-image")
        missing_item_response = client.get("/api/v1/jellyfin/items/missing/primary-image")
        missing_tag_response = client.get("/api/v1/jellyfin/items/movie-2/primary-image")
        upstream_missing_response = client.get("/api/v1/jellyfin/items/movie-3/primary-image")
        html_response = client.get("/")

    assert unconfigured_response.status_code == 400
    assert image_response.status_code == 200
    assert image_response.content == b"image-bytes"
    assert image_response.headers["content-type"] == "image/jpeg"
    assert image_response.headers["etag"] == '"poster-a"'
    assert image_response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert missing_item_response.status_code == 404
    assert missing_tag_response.status_code == 404
    assert upstream_missing_response.status_code == 404
    assert "secret-image-token" not in html_response.text


def test_jellyfin_primary_image_uses_disk_cache_and_etag(tmp_path: Path):
    fake_client = FakeJellyfinClient()
    fake_client.primary_images["movie-1"] = (b"image-bytes", "image/jpeg")
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.jellyfin_client_factory = lambda config: fake_client

    with TestClient(app) as client:
        _cache_jellyfin_item(app, jellyfin_item_id="movie-1", primary_image_tag="poster-a")
        _save_jellyfin_settings(client)
        first = client.get("/api/v1/jellyfin/items/movie-1/primary-image")
        second = client.get("/api/v1/jellyfin/items/movie-1/primary-image")
        not_modified = client.get(
            "/api/v1/jellyfin/items/movie-1/primary-image",
            headers={"If-None-Match": first.headers["etag"]},
        )

    assert first.content == second.content == b"image-bytes"
    assert fake_client.image_requests == ["movie-1"]
    assert not_modified.status_code == 304
    assert (tmp_path / "cache" / "jellyfin-images").is_dir()
