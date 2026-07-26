from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from subtitle_sidecar.api.routes import create_api_router
from subtitle_sidecar.db.repository import JellyfinMediaItemData, Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope


def _create_test_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_tables(app.state.engine)
    app.include_router(create_api_router())
    return app


def _cache_item(app: FastAPI, **overrides) -> None:
    data = {
        "jellyfin_item_id": "movie-1",
        "library_id": "movie-lib",
        "library_name": "Movies",
        "item_type": "Movie",
        "name": "Movie",
        "path": "/media/Movie.mkv",
        "subtitle_status": "missing",
    }
    data.update(overrides)
    with session_scope(app.state.engine) as session:
        Repository(session).upsert_jellyfin_media_item(JellyfinMediaItemData(**data))


def test_ignore_and_unignore_movie_updates_media_contract(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    _cache_item(app)

    with TestClient(app) as client:
        ignored = client.post("/api/v1/jellyfin/items/movie-1/ignore")
        tree_ignored = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")
        items_ignored = client.get("/api/v1/jellyfin/libraries/movie-lib/items")
        unignored = client.post("/api/v1/jellyfin/items/movie-1/unignore")
        tree_unignored = client.get("/api/v1/jellyfin/libraries/movie-lib/tree")

    assert ignored.json() == {
        "item_id": "movie-1",
        "item_type": "Movie",
        "ignored": True,
    }
    assert tree_ignored.json()["movies"][0]["status"] == "ignored"
    assert tree_ignored.json()["movies"][0]["ignored"] is True
    assert items_ignored.json()["items"][0]["subtitle_status"] == "missing"
    assert items_ignored.json()["items"][0]["ignored"] is True
    assert unignored.json()["ignored"] is False
    assert tree_unignored.json()["movies"][0]["status"] == "missing"


def test_ignore_accepts_series_but_rejects_episode(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    _cache_item(
        app,
        jellyfin_item_id="series-1",
        library_id="tv-lib",
        library_name="TV",
        item_type="Series",
        name="Series",
        path="",
        subtitle_status="unknown",
    )
    _cache_item(
        app,
        jellyfin_item_id="episode-1",
        library_id="tv-lib",
        library_name="TV",
        item_type="Episode",
        name="Episode",
        series_id="series-1",
        series_name="Series",
        season=1,
        episode=1,
        path="/media/Series/S01E01.mkv",
        subtitle_status="missing",
    )

    with TestClient(app) as client:
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


def test_batch_ignore_and_unignore_movies_and_series(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    _cache_item(app, jellyfin_item_id="movie-1")
    _cache_item(
        app,
        jellyfin_item_id="series-1",
        library_id="tv-lib",
        library_name="TV",
        item_type="Series",
        name="Series",
        path="",
        subtitle_status="unknown",
    )

    with TestClient(app) as client:
        ignored = client.post(
            "/api/v1/jellyfin/items/batch-ignore",
            json={"item_ids": ["movie-1", "series-1", "movie-1"], "ignored": True},
        )
        unignored = client.post(
            "/api/v1/jellyfin/items/batch-ignore",
            json={"item_ids": ["movie-1", "series-1"], "ignored": False},
        )

    assert ignored.status_code == 200
    assert ignored.json() == {
        "items": [
            {"item_id": "movie-1", "item_type": "Movie", "ignored": True},
            {"item_id": "series-1", "item_type": "Series", "ignored": True},
        ]
    }
    assert unignored.status_code == 200
    assert all(item["ignored"] is False for item in unignored.json()["items"])


def test_batch_ignore_rejects_episode_without_partial_update(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    _cache_item(app, jellyfin_item_id="movie-1")
    _cache_item(
        app,
        jellyfin_item_id="episode-1",
        library_id="tv-lib",
        library_name="TV",
        item_type="Episode",
        name="Episode",
        path="/media/Series/S01E01.mkv",
        subtitle_status="missing",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jellyfin/items/batch-ignore",
            json={"item_ids": ["movie-1", "episode-1"], "ignored": True},
        )
        movie = client.get("/api/v1/jellyfin/libraries/movie-lib/items")

    assert response.status_code == 400
    assert response.json()["detail"] == "only Movie and Series items can be ignored"
    assert movie.json()["items"][0]["ignored"] is False
