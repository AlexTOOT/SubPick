from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from collections.abc import Iterable


_YEAR_PATTERN = re.compile(r"(?<!\d)(?:18|19|20)\d{2}(?!\d|[pi])", re.IGNORECASE)


@dataclass(frozen=True)
class ReleaseYearEvidence:
    years: frozenset[int]
    matching_years: frozenset[int]

    @property
    def has_conflict(self) -> bool:
        return bool(self.years) and not self.matching_years


def analyze_release_years(
    values: Iterable[str | None],
    *,
    expected_year: int | None,
    expected_titles: Iterable[str | None] = (),
    tolerance: int = 1,
    current_year: int | None = None,
) -> ReleaseYearEvidence:
    """Extract plausible release years while ignoring numbers that belong to the title."""
    if expected_year is None:
        return ReleaseYearEvidence(frozenset(), frozenset())

    now_year = current_year or datetime.now(timezone.utc).year
    upper_bound = max(now_year + 1, expected_year + tolerance)
    title_numbers = {
        int(match.group(0))
        for title in expected_titles
        for match in _YEAR_PATTERN.finditer(str(title or ""))
    }
    years = {
        year
        for value in values
        for match in _YEAR_PATTERN.finditer(str(value or ""))
        if (year := int(match.group(0))) <= upper_bound and year not in title_numbers
    }
    matching = {year for year in years if abs(year - expected_year) <= tolerance}
    return ReleaseYearEvidence(frozenset(years), frozenset(matching))
