from pathlib import Path

from subtitle_sidecar.config import PathMapping, PathsSettings
from subtitle_sidecar.media.resolver import MediaResolver


def test_resolver_uses_direct_existing_path(tmp_path: Path) -> None:
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"fake")
    resolver = MediaResolver(PathsSettings())

    result = resolver.resolve(str(video))

    assert result.resolved_path == video
    assert result.strategy == "direct"


def test_resolver_uses_path_mapping(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    video = media / "Movie.mkv"
    video.write_bytes(b"fake")
    resolver = MediaResolver(
        PathsSettings(
            mappings=[PathMapping(from_path="/moviepilot/media", to_path=str(media))]
        )
    )

    result = resolver.resolve("/moviepilot/media/Movie.mkv")

    assert result.resolved_path == video
    assert result.strategy == "mapping"


def test_resolver_does_not_guess_from_library_basename(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing" / "Movie.mkv"
    resolver = MediaResolver(PathsSettings())

    result = resolver.resolve(str(missing_path))

    assert result.resolved_path is None
    assert result.strategy == "not_found"
