from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import re
from typing import Any
from datetime import UTC, datetime

import httpx

from subtitle_sidecar import __version__

SUBLIMINAL_REPOSITORY = "Diaoul/subliminal"
FFSUBSYNC_REPOSITORY = "smacke/ffsubsync"
GITHUB_RELEASE_API = f"https://api.github.com/repos/{SUBLIMINAL_REPOSITORY}/releases/latest"
GITHUB_TAGS_API = f"https://api.github.com/repos/{SUBLIMINAL_REPOSITORY}/tags?per_page=20"


def check_subliminal_update(
    client: Any = httpx,
    github_token: str = "",
) -> dict[str, str | bool | None]:
    """Compare the bundled package to the latest GitHub release or version tag.

    This intentionally checks only. Updating a Python dependency inside a running
    Docker container would make the image unreproducible and is handled by a
    normal Sidecar image rebuild.
    """

    return check_package_update(
        "subliminal",
        SUBLIMINAL_REPOSITORY,
        client=client,
        github_token=github_token,
    )


def check_ffsubsync_update(
    client: Any = httpx,
    github_token: str = "",
) -> dict[str, str | bool | None]:
    return check_package_update(
        "ffsubsync",
        FFSUBSYNC_REPOSITORY,
        client=client,
        github_token=github_token,
    )


def check_package_update(
    package_name: str,
    repository: str,
    *,
    client: Any = httpx,
    github_token: str = "",
) -> dict[str, str | bool | None]:
    try:
        current = version(package_name)
    except PackageNotFoundError:
        return {
            "current_version": "not_installed",
            "latest_version": None,
            "update_available": False,
            "status": "unavailable",
            "release_url": f"https://github.com/{repository}",
            "error": "package_not_installed",
        }
    release_api = f"https://api.github.com/repos/{repository}/releases/latest"
    tags_api = f"https://api.github.com/repos/{repository}/tags?per_page=20"
    headers = _github_headers(github_token)
    try:
        release = client.get(
            release_api,
            headers=headers,
            timeout=5.0,
        )
        if release.status_code == 200:
            payload = release.json()
            latest = _normalize_version(payload.get("tag_name"))
            release_url = str(payload.get("html_url") or "")
        elif release.status_code in {403, 429}:
            return _rate_limited_response(current, repository, release)
        else:
            latest, release_url = _latest_tag(client, tags_api, repository, headers)
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return {
            "current_version": current,
            "latest_version": None,
            "update_available": False,
            "status": "unavailable",
            "release_url": f"https://github.com/{repository}",
            "error": error.__class__.__name__,
        }

    if latest is None:
        return {
            "current_version": current,
            "latest_version": None,
            "update_available": False,
            "status": "unavailable",
            "release_url": f"https://github.com/{repository}",
            "error": "version_not_found",
        }
    return {
        "current_version": current,
        "latest_version": latest,
        "update_available": _is_newer(latest, current),
        "status": "ok",
        "release_url": release_url or f"https://github.com/{repository}/releases",
        "error": None,
    }


def _github_headers(github_token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"subtitle-sidecar/{__version__}",
    }
    if github_token.strip():
        headers["Authorization"] = f"Bearer {github_token.strip()}"
    return headers


def _latest_tag(
    client: Any,
    tags_api: str,
    repository: str,
    headers: dict[str, str],
) -> tuple[str | None, str]:
    response = client.get(
        tags_api,
        headers=headers,
        timeout=5.0,
    )
    response.raise_for_status()
    tags = response.json()
    if not isinstance(tags, list):
        return None, ""
    versions = [_normalize_version(tag.get("name")) for tag in tags if isinstance(tag, dict)]
    valid_versions = [item for item in versions if item is not None]
    return (max(valid_versions, key=_version_key), f"https://github.com/{repository}/tags") if valid_versions else (None, "")


def _rate_limited_response(
    current: str,
    repository: str,
    response: Any,
) -> dict[str, str | bool | None]:
    headers = getattr(response, "headers", {}) or {}
    reset_at = _format_reset_at(headers.get("X-RateLimit-Reset"))
    return {
        "current_version": current,
        "latest_version": None,
        "update_available": False,
        "status": "rate_limited",
        "release_url": f"https://github.com/{repository}",
        "error": "github_rate_limited" if reset_at else "github_http_error",
        "retry_at": reset_at,
    }


def _format_reset_at(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _normalize_version(value: Any) -> str | None:
    candidate = str(value or "").strip().removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*", candidate):
        return None
    return candidate


def _is_newer(latest: str, current: str) -> bool:
    latest_key = _version_key(latest)
    current_key = _version_key(current)
    width = max(len(latest_key), len(current_key))
    padded_latest = latest_key + (0,) * (width - len(latest_key))
    padded_current = current_key + (0,) * (width - len(current_key))
    return padded_latest > padded_current


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))
