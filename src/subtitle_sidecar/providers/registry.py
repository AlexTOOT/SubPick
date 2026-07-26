from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from subtitle_sidecar.providers.base import (
    ProviderSearchReport,
    SubtitleCandidate,
    SubtitleProvider,
    SubtitleSearchRequest,
)
from subtitle_sidecar.providers.scheduler import ProviderSearchScheduler, WaitCallback


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    error: str

    def format(self) -> str:
        return f"{self.provider}: {self.error}"


class ProviderRegistry:
    def __init__(
        self,
        providers: list[SubtitleProvider],
        *,
        scheduler: ProviderSearchScheduler | None = None,
    ) -> None:
        self.providers = providers
        self.scheduler = scheduler
        self.failures: list[ProviderFailure] = []
        self.search_reports: list[ProviderSearchReport] = []
        self._reporter = None

    @property
    def errors(self) -> list[str]:
        return [failure.format() for failure in self.failures]

    def search(self, request: SubtitleSearchRequest) -> list[SubtitleCandidate]:
        self.failures = []
        self.search_reports = []
        candidates: list[SubtitleCandidate] = []
        for batch in self.search_batches(request):
            candidates.extend(batch)
        return candidates

    def search_batches(
        self,
        request: SubtitleSearchRequest,
        on_wait: WaitCallback | None = None,
    ) -> Iterator[list[SubtitleCandidate]]:
        self.failures = []
        self.search_reports = []
        remaining_providers = list(self.providers)

        def iterate() -> Iterator[list[SubtitleCandidate]]:
            while remaining_providers:
                provider = self._select_provider(remaining_providers, on_wait)
                if provider is None:
                    return
                remaining_providers.remove(provider)
                batch = self._search_provider(provider, request)
                if batch:
                    yield batch

        return iterate()

    def set_reporter(self, reporter) -> None:
        self._reporter = reporter

    def _select_provider(self, remaining_providers: list[SubtitleProvider], on_wait):
        if self.scheduler is None:
            return remaining_providers[0] if remaining_providers else None
        ordered_names = [provider.name for provider in remaining_providers]
        selected_name = self.scheduler.acquire(ordered_names, on_wait=on_wait)
        if selected_name is None:
            return None
        for provider in remaining_providers:
            if provider.name == selected_name:
                return provider
        return None

    def _search_provider(
        self,
        provider: SubtitleProvider,
        request: SubtitleSearchRequest,
    ) -> list[SubtitleCandidate]:
        set_reporter = getattr(provider, "set_reporter", None)
        if callable(set_reporter):
            set_reporter(self._record_report)
        report_count_before_search = len(self.search_reports)
        try:
            batch = provider.search(request)
        except Exception as error:
            self.failures.append(ProviderFailure(provider=provider.name, error=str(error)))
            emitted_failure = any(
                report.provider == provider.name and report.status == "failed"
                for report in self.search_reports[report_count_before_search:]
            )
            if not emitted_failure:
                self._record_report(
                    ProviderSearchReport(
                        provider=provider.name,
                        status="failed",
                        error=error.__class__.__name__,
                    )
                )
            return []
        finally:
            if self.scheduler is not None:
                self.scheduler.mark_completed(provider.name)
        emitted_completed = any(
            report.provider == provider.name and report.status == "completed"
            for report in self.search_reports[report_count_before_search:]
        )
        if not emitted_completed:
            self._record_report(
                ProviderSearchReport(
                    provider=provider.name,
                    status="completed",
                    candidate_count=len(batch),
                )
            )
        return batch

    def _record_report(self, report: ProviderSearchReport) -> None:
        self.search_reports.append(report)
        if self._reporter is not None:
            self._reporter(report)
