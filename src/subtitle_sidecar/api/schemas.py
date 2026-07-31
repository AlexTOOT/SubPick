from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AddJobRequest(BaseModel):
    video_type: str | int | None = None
    physical_video_file_full_path: str
    task_priority_level: int | None = None
    media_server_inside_video_id: str | None = None
    is_bluray: bool | None = None


class AddJobResponse(BaseModel):
    job_id: int
    status: str


class RetryTaskResponse(BaseModel):
    job_id: int
    task_id: int
    status: str


class BatchTaskRequest(BaseModel):
    task_ids: list[int]


class BatchDeleteTasksRequest(BatchTaskRequest):
    delete_subtitles: bool = False


class BatchRetryTaskResultResponse(BaseModel):
    task_id: int
    ok: bool
    job_id: int | None
    new_task_id: int | None
    status: str
    error: str | None


class BatchRetryTasksResponse(BaseModel):
    results: list[BatchRetryTaskResultResponse]


class BatchDeleteTaskResultResponse(BaseModel):
    task_id: int
    ok: bool
    deleted: bool
    subtitle_deleted: bool = False
    error: str | None


class BatchDeleteTasksResponse(BaseModel):
    results: list[BatchDeleteTaskResultResponse]


class JellyfinSettingsRequest(BaseModel):
    server_url: str
    api_key: str | None = None
    user_id: str = ""


class JellyfinSettingsResponse(BaseModel):
    server_url: str
    user_id: str
    configured: bool
    api_key_configured: bool


class JellyfinConnectionCheckResponse(BaseModel):
    connected: bool
    library_count: int


class GitHubSettingsRequest(BaseModel):
    api_key: str | None = None


class GitHubSettingsResponse(BaseModel):
    api_key_configured: bool


class ServerSettingsRequest(BaseModel):
    token: str = ""


class ServerSettingsResponse(BaseModel):
    token: str


class PathMappingRequest(BaseModel):
    from_path: str = Field(alias="from")
    to_path: str = Field(alias="to")

    model_config = ConfigDict(populate_by_name=True)


class PathMappingResponse(BaseModel):
    from_path: str
    to_path: str


class PathSettingsRequest(BaseModel):
    mappings: list[PathMappingRequest] = Field(default_factory=list)


class PathMappingTestRequest(PathSettingsRequest):
    sample_path: str = ""


class PathMappingTestResponse(BaseModel):
    original_path: str
    resolved_path: str | None
    strategy: str
    exists: bool


class PathSettingsResponse(BaseModel):
    mappings: list[PathMappingResponse]
    latest_moviepilot_path: str | None = None
    path_issue: dict[str, str] | None = None
    needs_attention: bool


class SubliminalProviderAuthenticationRequest(BaseModel):
    username: str = ""
    password: str | None = None
    apikey: str | None = None


class SubliminalProviderSettingsRequest(BaseModel):
    enabled: bool
    providers: list[str]
    languages: list[str]
    authentication: dict[str, SubliminalProviderAuthenticationRequest] = Field(
        default_factory=dict
    )


class SubliminalProviderAuthenticationResponse(BaseModel):
    username: str
    password_configured: bool
    apikey_configured: bool


class SubliminalProviderSettingsResponse(BaseModel):
    enabled: bool
    providers: list[str]
    languages: list[str]
    authentication: dict[str, SubliminalProviderAuthenticationResponse]


class AssrtProviderSettingsRequest(BaseModel):
    enabled: bool
    token: str | None = None
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    requests_per_minute: int = Field(default=5, ge=1, le=5)


class AssrtProviderSettingsResponse(BaseModel):
    enabled: bool
    token_configured: bool
    timeout_seconds: float
    requests_per_minute: int
    status: str


class AssrtQuotaResponse(BaseModel):
    quota: int


class SubdlProviderSettingsRequest(BaseModel):
    enabled: bool
    api_key: str | None = None
    timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    requests_per_minute: int = Field(default=20, ge=1, le=60)
    use_api_key_for_downloads: bool = False


class SubdlProviderSettingsResponse(BaseModel):
    enabled: bool
    api_key_configured: bool
    timeout_seconds: float
    requests_per_minute: int
    use_api_key_for_downloads: bool
    status: str


class SubdlUsageResponse(BaseModel):
    plan_name: str
    is_pro: bool
    search_remaining: int | None
    search_limit: int | None
    download_remaining: int | None
    download_limit: int | None
    reset_at: str | None


class ZimukuProviderSettingsRequest(BaseModel):
    enabled: bool
    anti_captcha_api_key: str | None = None
    moviepilot_ocr_url: str = ""
    captcha_debug_capture: bool = False
    base_url: str = "https://srtku.com"
    timeout_seconds: float = Field(default=30.0, ge=5.0, le=180.0)
    request_delay_seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class ZimukuProviderSettingsResponse(BaseModel):
    enabled: bool
    anti_captcha_api_key_configured: bool
    moviepilot_ocr_url: str
    moviepilot_ocr_configured: bool
    captcha_debug_capture: bool
    captcha_debug_directory: str
    base_url: str
    timeout_seconds: float
    request_delay_seconds: float
    status: str


class ZimukuCaptchaBalanceResponse(BaseModel):
    balance: float


class ZimukuOcrCheckResponse(BaseModel):
    status: str
    duration_ms: int
    base_url: str
    recognized_answer: str
    expected_answer: str


class LogProvidersResponse(BaseModel):
    providers: list[str]


class ProviderCapabilitiesResponse(BaseModel):
    media_scopes: list[str]
    lookup_keys: list[str]
    transport: str
    requires_auth: bool
    requires_captcha: bool
    supports_archives: bool
    recommended_interval_seconds: float
    stable_candidate_identity: bool


class ProviderAdapterResponse(BaseModel):
    name: str
    display_name: str
    version: str
    enabled: bool
    capabilities: ProviderCapabilitiesResponse


class ProviderOrderRequest(BaseModel):
    order: list[str]


class ProviderOrderResponse(BaseModel):
    order: list[str]
    adapters: list[ProviderAdapterResponse]


class SubliminalUpdateCheckResponse(BaseModel):
    current_version: str
    latest_version: str | None
    update_available: bool
    status: str
    release_url: str
    error: str | None = None
    retry_at: str | None = None


class DependencyUpdateChecksResponse(BaseModel):
    subliminal: SubliminalUpdateCheckResponse
    ffsubsync: SubliminalUpdateCheckResponse


class JellyfinLibraryResponse(BaseModel):
    id: str
    name: str
    collection_type: str


class JellyfinLibrariesResponse(BaseModel):
    libraries: list[JellyfinLibraryResponse]


class JellyfinMediaItemResponse(BaseModel):
    jellyfin_item_id: str
    library_id: str
    library_name: str
    item_type: str
    name: str
    original_title: str | None
    series_id: str | None
    series_name: str | None
    year: int | None
    season: int | None
    episode: int | None
    path: str
    provider_ids: dict[str, Any] | None
    production_locations: list[str] = Field(default_factory=list)
    primary_image_tag: str | None
    subtitle_status: str
    has_external_chinese_subtitle: bool
    has_embedded_chinese_subtitle: bool
    has_bilingual_subtitle: bool
    ignored: bool
    last_scanned_at: datetime | None


class JellyfinMediaItemsResponse(BaseModel):
    items: list[JellyfinMediaItemResponse]


class JellyfinTreeMediaCardResponse(BaseModel):
    id: str
    name: str
    year: int | None
    status: str
    has_external_chinese_subtitle: bool
    has_embedded_chinese_subtitle: bool
    production_locations: list[str] = Field(default_factory=list)
    path: str
    primary_image_tag: str | None
    image_url: str | None
    ignored: bool
    date_created: datetime | None = None


class JellyfinTreeEpisodeResponse(JellyfinTreeMediaCardResponse):
    season: int | None
    episode: int | None


class JellyfinTreeSeasonResponse(BaseModel):
    season: int | None
    status: str
    has_external_chinese_subtitle: bool
    has_embedded_chinese_subtitle: bool
    episodes: list[JellyfinTreeEpisodeResponse]


class JellyfinTreeSeriesResponse(BaseModel):
    id: str
    name: str
    year: int | None
    status: str
    has_external_chinese_subtitle: bool
    has_embedded_chinese_subtitle: bool
    production_locations: list[str] = Field(default_factory=list)
    primary_image_tag: str | None
    image_url: str | None
    ignored: bool
    date_created: datetime | None = None
    seasons: list[JellyfinTreeSeasonResponse]


class JellyfinLibraryTreeResponse(BaseModel):
    library_id: str
    library_name: str
    collection_type: str
    movies: list[JellyfinTreeMediaCardResponse]
    series: list[JellyfinTreeSeriesResponse]


class JellyfinRecentMediaResponse(BaseModel):
    id: str
    library_id: str
    library_name: str
    item_type: str
    name: str
    year: int | None
    status: str
    has_external_chinese_subtitle: bool
    has_embedded_chinese_subtitle: bool
    image_url: str | None
    ignored: bool
    date_created: datetime | None


class JellyfinRecentMediaItemsResponse(BaseModel):
    items: list[JellyfinRecentMediaResponse]


class JellyfinScanResponse(BaseModel):
    library_id: str
    library_name: str
    scanned_count: int
    created: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0


class JellyfinCreateTasksRequest(BaseModel):
    item_ids: list[str]


class JellyfinIgnoreResponse(BaseModel):
    item_id: str
    item_type: str
    ignored: bool


class JellyfinBatchIgnoreRequest(BaseModel):
    item_ids: list[str]
    ignored: bool


class JellyfinBatchIgnoreResponse(BaseModel):
    items: list[JellyfinIgnoreResponse]


class JellyfinCreateTaskResultResponse(BaseModel):
    item_id: str
    ok: bool
    job_id: int | None
    task_id: int | None
    status: str
    error: str | None


class JellyfinCreateTasksResponse(BaseModel):
    results: list[JellyfinCreateTaskResultResponse]


class VideoTaskSummaryResponse(BaseModel):
    id: int
    job_id: int
    status: str
    video_path_original: str
    result_subtitle_path: str | None
    created_at: datetime
    updated_at: datetime


class JobResponse(BaseModel):
    job_id: int
    status: str


class JobListItemResponse(JobResponse):
    created_at: datetime
    updated_at: datetime
    video_tasks: list[VideoTaskSummaryResponse]


class TaskEventResponse(BaseModel):
    id: int
    video_task_id: int
    stage: str
    status: str
    message: str | None
    error_code: str | None
    details: dict[str, Any] | None
    created_at: datetime


class SubtitleCandidateResponse(BaseModel):
    id: int
    provider: str
    language: str
    is_bilingual: bool
    format: str
    score: float | None
    title: str
    release_info: str | None
    source_url: str | None
    download_status: str
    attempt_count: int = 0
    last_attempt_status: str | None = None
    last_error_message: str | None = None
    raw_metadata: dict[str, Any] | None
    created_at: datetime


class SubtitleArtifactResponse(BaseModel):
    id: int
    candidate_id: int | None
    kind: str
    path: str
    is_synced: bool
    created_at: datetime


class VideoTaskDetailResponse(BaseModel):
    id: int
    job_id: int
    status: str
    error_message: str | None
    video_path_original: str
    video_path_resolved: str | None
    result_subtitle_path: str | None
    created_at: datetime
    updated_at: datetime
    candidates: list[SubtitleCandidateResponse]
    artifacts: list[SubtitleArtifactResponse]
    events: list[TaskEventResponse]


class StructuredLogEntryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    ts: str
    level: str
    event: str
    job_id: int | None = None
    task_id: int | None = None
    stage: str | None = None
    status: str | None = None
    provider: str | None = None
    candidate_id: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    message: str | None = None


class StructuredLogsResponse(BaseModel):
    entries: list[StructuredLogEntryResponse]
    next_after_id: int


class DiagnosticCheckResponse(BaseModel):
    name: str
    status: str


class CompatibilityDiagnosticResponse(BaseModel):
    status: str
    config_version: int
    database_schema_version: int
    supported_database_schema_version: int


class LoggingDiagnosticResponse(BaseModel):
    retention_days: int
    max_task_events: int


class PathDiagnosticResponse(BaseModel):
    path: str
    exists: bool
    readable: bool
    writable: bool
    status: str


class ToolDiagnosticResponse(BaseModel):
    name: str
    executable: str
    available: bool
    status: str


class QueueDiagnosticResponse(BaseModel):
    active_task_id: int | None
    queued_count: int
    search_interval_seconds: float
    provider_cooldowns: dict[str, float] = Field(default_factory=dict)
    next_provider_ready_seconds: float = 0


class SubliminalProviderDiagnosticResponse(BaseModel):
    enabled: bool
    status: str


class AssrtProviderDiagnosticResponse(BaseModel):
    enabled: bool
    status: str


class SubdlProviderDiagnosticResponse(BaseModel):
    enabled: bool
    status: str


class ZimukuProviderDiagnosticResponse(BaseModel):
    enabled: bool
    status: str


class ProviderDiagnosticsResponse(BaseModel):
    subliminal: SubliminalProviderDiagnosticResponse
    assrt: AssrtProviderDiagnosticResponse
    subdl: SubdlProviderDiagnosticResponse
    zimuku: ZimukuProviderDiagnosticResponse


class JellyfinDiagnosticResponse(BaseModel):
    configured: bool
    connected: bool = False
    last_checked_at: str | None = None


class MoviePilotDiagnosticResponse(BaseModel):
    token_configured: bool
    connected: bool
    last_callback_at: str | None = None
    last_received_path: str | None = None


class SetupStepResponse(BaseModel):
    id: str
    label: str
    status: str
    target_view: str
    target_section: str | None = None
    help: str | None = None


class SetupNotificationResponse(BaseModel):
    id: str
    level: str
    title: str
    message: str
    target_view: str
    target_section: str | None = None


class SetupStatusResponse(BaseModel):
    completed: bool
    steps: list[SetupStepResponse]
    notifications: list[SetupNotificationResponse]


class DiagnosticsResponse(BaseModel):
    version: str
    components: dict[str, str | None]
    compatibility: CompatibilityDiagnosticResponse
    overall_status: str
    queue: QueueDiagnosticResponse
    providers: ProviderDiagnosticsResponse
    jellyfin: JellyfinDiagnosticResponse
    moviepilot: MoviePilotDiagnosticResponse
    setup: SetupStatusResponse
    tools: list[ToolDiagnosticResponse]
    config_file: PathDiagnosticResponse
    data_dir: PathDiagnosticResponse
    cache_dir: PathDiagnosticResponse
    media_dir: PathDiagnosticResponse
    database: PathDiagnosticResponse
    logging: LoggingDiagnosticResponse
    checks: list[DiagnosticCheckResponse]
