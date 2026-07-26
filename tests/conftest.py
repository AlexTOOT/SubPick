from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app


@pytest.fixture
def app(tmp_path: Path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)
    app.state.settings.queue.search_interval_seconds = 0
    return app


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def token_app(tmp_path: Path):
    app = create_app(data_dir=tmp_path, token="secret-token", job_processor=lambda task_id: None)
    app.state.settings.queue.search_interval_seconds = 0
    return app


@pytest.fixture
def token_client(token_app) -> Iterator[TestClient]:
    with TestClient(token_app) as test_client:
        yield test_client
