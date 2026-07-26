from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


class JellyfinClient:
    def __init__(
        self,
        *,
        server_url: str,
        api_key: str,
        user_id: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id or ""
        self.timeout = timeout
        self.transport = transport

    def list_libraries(self) -> list[dict[str, Any]]:
        user_id = self._require_user_id()
        payload = self._get(f"/Users/{user_id}/Views")
        libraries = []
        for item in payload.get("Items", []):
            collection_type = item.get("CollectionType") or ""
            if collection_type not in {"movies", "tvshows"}:
                continue
            libraries.append(
                {
                    "id": item.get("Id", ""),
                    "name": item.get("Name", ""),
                    "collection_type": collection_type,
                }
            )
        return libraries

    def list_library_items(self, library_id: str) -> list[dict[str, Any]]:
        user_id = self._require_user_id()
        items: list[dict[str, Any]] = []
        start_index = 0
        page_size = 200
        while True:
            payload = self._get(
                f"/Users/{user_id}/Items",
                params={
                    "ParentId": library_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Series,Episode",
                    "Fields": ",".join(
                        [
                            "Path",
                            "OriginalTitle",
                            "ProviderIds",
                            "MediaSources",
                            "SeriesName",
                            "SeriesId",
                            "ParentIndexNumber",
                            "IndexNumber",
                            "ProductionYear",
                            "ProductionLocations",
                            "ImageTags",
                            "DateCreated",
                        ]
                    ),
                    "StartIndex": start_index,
                    "Limit": page_size,
                },
            )
            page_items = payload.get("Items", [])
            items.extend(self._normalize_item(item) for item in page_items)
            if start_index + len(page_items) >= payload.get("TotalRecordCount", len(items)):
                break
            if not page_items:
                break
            start_index += len(page_items)
        return _apply_series_original_titles(items)

    def get_primary_image(self, item_id: str) -> tuple[bytes, str]:
        with httpx.Client(
            base_url=self.server_url,
            headers={"X-Emby-Token": self.api_key},
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = client.get(f"/Items/{item_id}/Images/Primary")
            response.raise_for_status()
            return (
                response.content,
                response.headers.get("content-type", "application/octet-stream"),
            )

    def get_item(self, item_id: str) -> dict[str, Any]:
        payload = self._get(
            f"/Users/{self._require_user_id()}/Items/{item_id}",
            params={
                "Fields": ",".join(
                    [
                        "Path",
                        "OriginalTitle",
                        "ProviderIds",
                        "MediaSources",
                        "SeriesName",
                        "SeriesId",
                        "ParentIndexNumber",
                        "IndexNumber",
                        "ProductionYear",
                        "ProductionLocations",
                        "ImageTags",
                        "DateCreated",
                    ]
                )
            },
        )
        return self._normalize_item(payload)

    def refresh_item(
        self,
        item_id: str,
        *,
        metadata_refresh_mode: str = "None",
        image_refresh_mode: str = "None",
    ) -> None:
        with httpx.Client(
            base_url=self.server_url,
            headers={"X-Emby-Token": self.api_key},
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = client.post(
                f"/Items/{item_id}/Refresh",
                params={
                    "metadataRefreshMode": metadata_refresh_mode,
                    "imageRefreshMode": image_refresh_mode,
                },
            )
            response.raise_for_status()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(
            base_url=self.server_url,
            headers={"X-Emby-Token": self.api_key},
            timeout=self.timeout,
            transport=self.transport,
        ) as client:
            response = client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Jellyfin response must be a JSON object")
            return payload

    def _require_user_id(self) -> str:
        if not self.user_id:
            raise ValueError("Jellyfin user_id is required")
        return self.user_id

    def _normalize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        image_tags = item.get("ImageTags") or {}
        media_streams: list[dict[str, Any]] = []
        for source in item.get("MediaSources") or []:
            media_streams.extend(source.get("MediaStreams") or [])
        return {
            "id": item.get("Id", ""),
            "name": item.get("Name", ""),
            "original_title": item.get("OriginalTitle") or None,
            "series_id": item.get("SeriesId") or (
                item.get("Id") if item.get("Type") == "Series" else None
            ),
            "series_name": item.get("SeriesName"),
            "type": item.get("Type", ""),
            "path": item.get("Path") or _first_media_source_path(item),
            "year": item.get("ProductionYear"),
            "season": item.get("ParentIndexNumber"),
            "episode": item.get("IndexNumber"),
            "provider_ids": item.get("ProviderIds") or {},
            "production_locations": [
                str(location).strip()
                for location in item.get("ProductionLocations") or []
                if str(location).strip()
            ],
            "primary_image_tag": image_tags.get("Primary"),
            "media_streams": media_streams,
            "date_created": _parse_jellyfin_datetime(item.get("DateCreated")),
        }


def _first_media_source_path(item: dict[str, Any]) -> str | None:
    for source in item.get("MediaSources") or []:
        path = source.get("Path")
        if path:
            return path
    return None


def _parse_jellyfin_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _apply_series_original_titles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the series' original title for episodes, not its per-episode title."""
    series_titles = {
        item["id"]: item["original_title"]
        for item in items
        if item.get("type") == "Series" and item.get("original_title")
    }
    for item in items:
        if item.get("type") == "Episode":
            series_title = series_titles.get(item.get("series_id"))
            if series_title:
                item["original_title"] = series_title
    return items
