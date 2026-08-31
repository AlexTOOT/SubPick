from sqlalchemy import create_engine, inspect, text

from subtitle_sidecar.db.repository import Repository
from subtitle_sidecar.db.session import create_sqlite_engine, create_tables, session_scope


def test_create_tables_creates_event_tables(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}", future=True)

    create_tables(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "task_events" in table_names
    assert "system_events" in table_names

    task_event_columns = {column["name"] for column in inspector.get_columns("task_events")}
    assert {
        "id",
        "video_task_id",
        "stage",
        "status",
        "message",
        "error_code",
        "details_json",
        "created_at",
    }.issubset(task_event_columns)

    system_event_columns = {column["name"] for column in inspector.get_columns("system_events")}
    assert {
        "id",
        "category",
        "level",
        "event",
        "message",
        "task_id",
        "details_json",
        "created_at",
    }.issubset(system_event_columns)


def test_create_tables_is_idempotent_and_adds_missing_table_to_existing_sqlite_db(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    video_path_resolved TEXT,
                    media_server_id VARCHAR(255),
                    title TEXT,
                    year INTEGER,
                    season INTEGER,
                    episode INTEGER,
                    result_subtitle_path TEXT,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )

    create_tables(engine)
    create_tables(engine)

    inspector = inspect(engine)
    assert "task_events" in set(inspector.get_table_names())


def test_create_tables_adds_safe_missing_columns_without_rewriting_existing_sqlite_data(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs (id, source, raw_payload_json, status)
                VALUES (1, 'moviepilot-csf', '{}', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO video_tasks (id, job_id, video_path_original, status)
                VALUES (1, 1, '/media/A.mkv', 'queued')
                """
            )
        )

    create_tables(engine)

    inspector = inspect(engine)
    video_task_columns = {column["name"] for column in inspector.get_columns("video_tasks")}
    assert "video_path_resolved" in video_task_columns
    assert "media_server_id" in video_task_columns
    assert "result_subtitle_path" in video_task_columns
    assert "error_message" in video_task_columns
    assert "retry_at" in video_task_columns
    assert "auto_retry_count" in video_task_columns
    assert "retry_category" in video_task_columns
    assert "retry_parent_task_id" in video_task_columns

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT video_path_original, status, result_subtitle_path, error_message,
                       retry_at, auto_retry_count, retry_category, retry_parent_task_id
                FROM video_tasks
                WHERE id = 1
                """
            )
        ).one()

    assert row.video_path_original == "/media/A.mkv"
    assert row.status == "queued"
    assert row.result_subtitle_path is None
    assert row.error_message is None
    assert row.retry_at is None
    assert row.auto_retry_count == 0
    assert row.retry_category is None
    assert row.retry_parent_task_id is None


def test_create_tables_adds_missing_default_expression_timestamp_columns_to_legacy_sqlite_table(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE task_events (
                    id INTEGER NOT NULL PRIMARY KEY,
                    video_task_id INTEGER NOT NULL,
                    stage VARCHAR(100) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    message TEXT,
                    error_code VARCHAR(100),
                    details_json JSON,
                    FOREIGN KEY(video_task_id) REFERENCES video_tasks (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs (id, source, raw_payload_json, status)
                VALUES (1, 'moviepilot-csf', '{}', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO video_tasks (id, job_id, video_path_original, status)
                VALUES (1, 1, '/media/A.mkv', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO task_events (id, video_task_id, stage, status, message)
                VALUES (1, 1, 'download', 'started', 'legacy row')
                """
            )
        )

    create_tables(engine)

    inspector = inspect(engine)
    task_event_columns = {column["name"] for column in inspector.get_columns("task_events")}
    assert "created_at" in task_event_columns
    assert "video_task_id" in task_event_columns

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT id, message, created_at
                FROM task_events
                WHERE id = 1
                """
            )
        ).one()

    assert row.id == 1
    assert row.message == "legacy row"


def test_create_tables_preserves_future_timestamp_defaults_for_migrated_task_events(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE task_events (
                    id INTEGER NOT NULL PRIMARY KEY,
                    video_task_id INTEGER NOT NULL,
                    stage VARCHAR(100) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    message TEXT,
                    error_code VARCHAR(100),
                    details_json JSON,
                    FOREIGN KEY(video_task_id) REFERENCES video_tasks (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs (id, source, raw_payload_json, status)
                VALUES (1, 'moviepilot-csf', '{}', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO video_tasks (id, job_id, video_path_original, status)
                VALUES (1, 1, '/media/A.mkv', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO task_events (id, video_task_id, stage, status, message)
                VALUES (1, 1, 'download', 'started', 'legacy row')
                """
            )
        )

    migrated_engine = create_sqlite_engine(f"sqlite:///{database_path}")
    create_tables(migrated_engine)

    with session_scope(migrated_engine) as session:
        repo = Repository(session)
        created = repo.record_task_event(
            video_task_id=1,
            stage="download",
            status="completed",
            message="new row",
        )
        created_id = created.id

    with migrated_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id, message, created_at
                FROM task_events
                ORDER BY id ASC
                """
            )
        ).all()

    assert [row.id for row in rows] == [1, created_id]
    assert rows[0].message == "legacy row"
    assert rows[1].message == "new row"
    assert rows[1].created_at is not None


def test_record_task_event_returns_created_at_immediately_after_migrated_insert(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE task_events (
                    id INTEGER NOT NULL PRIMARY KEY,
                    video_task_id INTEGER NOT NULL,
                    stage VARCHAR(100) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    message TEXT,
                    error_code VARCHAR(100),
                    details_json JSON,
                    FOREIGN KEY(video_task_id) REFERENCES video_tasks (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs (id, source, raw_payload_json, status)
                VALUES (1, 'moviepilot-csf', '{}', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO video_tasks (id, job_id, video_path_original, status)
                VALUES (1, 1, '/media/A.mkv', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO task_events (id, video_task_id, stage, status, message)
                VALUES (1, 1, 'download', 'started', 'legacy row')
                """
            )
        )

    migrated_engine = create_sqlite_engine(f"sqlite:///{database_path}")
    create_tables(migrated_engine)

    with session_scope(migrated_engine) as session:
        repo = Repository(session)
        created = repo.record_task_event(
            video_task_id=1,
            stage="download",
            status="completed",
            message="new row",
        )

        assert created.created_at is not None


def test_create_tables_adds_candidate_attempt_columns_to_legacy_sqlite_db(tmp_path) -> None:
    database_path = tmp_path / "test.sqlite3"
    engine = create_engine(f"sqlite:///{database_path}", future=True)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE jobs (
                    id INTEGER NOT NULL PRIMARY KEY,
                    source VARCHAR(100) NOT NULL,
                    raw_payload_json JSON NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE video_tasks (
                    id INTEGER NOT NULL PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    video_path_original TEXT NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE subtitle_candidates (
                    id INTEGER NOT NULL PRIMARY KEY,
                    video_task_id INTEGER NOT NULL,
                    provider VARCHAR(100) NOT NULL,
                    language VARCHAR(50) NOT NULL,
                    is_bilingual BOOLEAN NOT NULL DEFAULT 0,
                    format VARCHAR(20) NOT NULL,
                    score FLOAT,
                    title TEXT NOT NULL,
                    release_info TEXT,
                    source_url TEXT,
                    raw_metadata_json JSON,
                    download_status VARCHAR(50) NOT NULL DEFAULT 'queued',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    FOREIGN KEY(video_task_id) REFERENCES video_tasks (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO jobs (id, source, raw_payload_json, status)
                VALUES (1, 'moviepilot-csf', '{}', 'queued')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO video_tasks (id, job_id, video_path_original, status, error_message)
                VALUES (1, 1, '/media/A.mkv', 'queued', NULL)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO subtitle_candidates (
                    id, video_task_id, provider, language, is_bilingual, format,
                    score, title, release_info, source_url, raw_metadata_json, download_status
                ) VALUES (
                    1, 1, 'fake', 'zh-cn', 1, 'srt',
                    99.0, 'Legacy candidate', 'WEB-DL', 'https://example.invalid/sub.srt', '{}', 'queued'
                )
                """
            )
        )

    create_tables(engine)

    inspector = inspect(engine)
    candidate_columns = {column["name"] for column in inspector.get_columns("subtitle_candidates")}
    assert "attempt_count" in candidate_columns
    assert "last_attempt_status" in candidate_columns
    assert "last_error_message" in candidate_columns

    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT title, download_status, attempt_count, last_attempt_status, last_error_message
                FROM subtitle_candidates
                WHERE id = 1
                """
            )
        ).one()

    assert row.title == "Legacy candidate"
    assert row.download_status == "queued"
    assert row.attempt_count == 0
    assert row.last_attempt_status is None
    assert row.last_error_message is None
