from __future__ import annotations

import httpx

from subtitle_sidecar.jellyfin.client import JellyfinClient


def test_jellyfin_client_lists_libraries_and_items() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/Users/user-1/Views":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Id": "movie-lib",
                            "Name": "电影",
                            "CollectionType": "movies",
                        },
                        {
                            "Id": "playlist-lib",
                            "Name": "Playlists",
                            "CollectionType": "playlists",
                        },
                    ]
                },
            )
        if request.url.path == "/Users/user-1/Items":
            return httpx.Response(
                200,
                json={
                    "Items": [
                        {
                            "Id": "movie-1",
                            "Name": "Movie",
                            "Type": "Movie",
                            "Path": "/media/Movie/Movie.mkv",
                            "ProductionYear": 2024,
                            "DateCreated": "2026-07-20T12:34:56.0000000Z",
                            "ProviderIds": {"Tmdb": "123"},
                            "ProductionLocations": ["China"],
                            "ImageTags": {"Primary": "tag-1"},
                            "MediaSources": [
                                {
                                    "MediaStreams": [
                                        {
                                            "Type": "Subtitle",
                                            "Language": "chi",
                                            "IsExternal": False,
                                            "DisplayTitle": "Chi - ASS",
                                        }
                                    ]
                                }
                            ],
                        },
                        {
                            "Id": "series-1",
                            "Name": "Localized Series",
                            "OriginalTitle": "Original Series",
                            "Type": "Series",
                        },
                        {
                            "Id": "episode-1",
                            "Name": "S01E01",
                            "OriginalTitle": "S01E01",
                            "Type": "Episode",
                            "SeriesId": "series-1",
                        },
                    ],
                    "TotalRecordCount": 1,
                },
            )
        if request.url.path == "/Users/user-1/Items/movie-1":
            return httpx.Response(
                200,
                json={
                    "Id": "movie-1",
                    "Name": "Movie",
                    "OriginalTitle": "Original Movie",
                    "Type": "Movie",
                    "Path": "/media/Movie/Movie.mkv",
                    "ProductionYear": 2024,
                    "ProviderIds": {"Imdb": "tt123"},
                },
            )
        return httpx.Response(404)

    client = JellyfinClient(
        server_url="http://jellyfin.test",
        api_key="secret",
        user_id="user-1",
        transport=httpx.MockTransport(handler),
    )

    libraries = client.list_libraries()
    items = client.list_library_items("movie-lib")
    item = client.get_item("movie-1")

    assert libraries == [
        {
            "id": "movie-lib",
            "name": "电影",
            "collection_type": "movies",
        }
    ]
    assert items[0]["id"] == "movie-1"
    assert items[0]["path"] == "/media/Movie/Movie.mkv"
    assert items[0]["provider_ids"] == {"Tmdb": "123"}
    assert items[0]["production_locations"] == ["China"]
    assert items[0]["primary_image_tag"] == "tag-1"
    assert items[0]["date_created"].isoformat() == "2026-07-20T12:34:56+00:00"
    assert items[0]["media_streams"][0]["Language"] == "chi"
    assert items[2]["original_title"] == "Original Series"
    assert item["year"] == 2024
    assert item["original_title"] == "Original Movie"
    assert item["provider_ids"] == {"Imdb": "tt123"}
    library_request = next(url for url in requests if "/Users/user-1/Items?" in url)
    assert "IncludeItemTypes=Movie%2CSeries%2CEpisode" in library_request
    assert "ProductionLocations" in library_request
    assert "DateCreated" in library_request
    assert any("X-Emby-Token=secret" not in url for url in requests)


def test_jellyfin_client_refreshes_single_item() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, str(request.url)))
        if request.method == "POST" and request.url.path == "/Items/movie-1/Refresh":
            return httpx.Response(204)
        return httpx.Response(404)

    client = JellyfinClient(
        server_url="http://jellyfin.test",
        api_key="secret",
        user_id="user-1",
        transport=httpx.MockTransport(handler),
    )

    client.refresh_item("movie-1")

    assert requests == [
        (
            "POST",
            "http://jellyfin.test/Items/movie-1/Refresh?metadataRefreshMode=None&imageRefreshMode=None",
        )
    ]
