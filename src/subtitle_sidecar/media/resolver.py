from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from subtitle_sidecar.config import PathsSettings


@dataclass(frozen=True)
class ResolveResult:
    original_path: str
    resolved_path: Path | None
    strategy: Literal["direct", "mapping", "not_found"]


class MediaResolver:
    def __init__(self, settings: PathsSettings) -> None:
        self._settings = settings

    def resolve(self, path: str) -> ResolveResult:
        direct_path = Path(path)
        if direct_path.exists():
            return ResolveResult(
                original_path=path,
                resolved_path=direct_path,
                strategy="direct",
            )

        for mapping in self._settings.mappings:
            mapped_path = mapping.rewrite(path)
            if mapped_path is None:
                continue

            candidate = Path(mapped_path)
            if candidate.exists():
                return ResolveResult(
                    original_path=path,
                    resolved_path=candidate,
                    strategy="mapping",
                )

        return ResolveResult(
            original_path=path,
            resolved_path=None,
            strategy="not_found",
        )
