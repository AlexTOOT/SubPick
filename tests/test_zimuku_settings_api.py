from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app
from subtitle_sidecar.observability import clear_log_buffer_for_tests, list_structured_logs


class FakeZimukuProvider:
    def __init__(self, config) -> None:
        self.config = config

    def captcha_balance(self) -> float:
        return 12.345


class FakeOcrSolver:
    def __init__(self, config) -> None:
        self.config = config
        self.last_check_answer = "06394"

    def check_available(self) -> int:
        return 42


class WrongAnswerOcrSolver(FakeOcrSolver):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.last_check_answer = "1234"


def test_zimuku_settings_are_persisted_redacted_and_balance_can_be_checked(tmp_path: Path) -> None:
    clear_log_buffer_for_tests()
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.zimuku_provider_factory = FakeZimukuProvider
    app.state.zimuku_ocr_solver_factory = FakeOcrSolver

    with TestClient(app) as client:
        saved = client.put(
            "/api/v1/providers/zimuku/settings",
            json={
                "enabled": True,
                "anti_captcha_api_key": "anti-captcha-secret",
                "moviepilot_ocr_url": "http://moviepilot-ocr:9899/",
                "captcha_debug_capture": True,
                "base_url": "https://srtku.com/",
                "timeout_seconds": 45,
                "request_delay_seconds": 1.5,
            },
        )
        loaded = client.get("/api/v1/providers/zimuku/settings")
        ocr = client.post("/api/v1/providers/zimuku/ocr-check")
        providers = client.get("/api/v1/logs/providers")
        balance = client.post("/api/v1/providers/zimuku/captcha-balance")
        diagnostics = client.get("/api/v1/diagnostics")
    logs, _ = list_structured_logs(provider="zimuku")

    assert saved.status_code == loaded.status_code == ocr.status_code == providers.status_code == balance.status_code == 200
    assert loaded.json() == {
        "enabled": True,
        "anti_captcha_api_key_configured": True,
        "moviepilot_ocr_url": "http://moviepilot-ocr:9899",
        "moviepilot_ocr_configured": True,
        "captcha_debug_capture": True,
        "captcha_debug_directory": str(tmp_path / "data/diagnostics/captcha/zimuku"),
        "base_url": "https://srtku.com",
        "timeout_seconds": 45.0,
        "request_delay_seconds": 1.5,
        "status": "configured",
    }
    assert ocr.json() == {
        "status": "available",
        "duration_ms": 42,
        "base_url": "http://moviepilot-ocr:9899",
        "recognized_answer": "06394",
        "expected_answer": "06394",
    }
    assert any(
        entry["stage"] == "ocr_check"
        and entry["status"] == "completed"
        and "识别结果 06394" in entry["message"]
        for entry in logs
    )
    assert "zimuku" in providers.json()["providers"]
    assert balance.json() == {"balance": 12.345}
    assert diagnostics.json()["providers"]["zimuku"]["status"] == "configured"
    assert "anti-captcha-secret" not in f"{loaded.json()}{diagnostics.json()}"


def test_zimuku_ocr_check_rejects_wrong_recognition(tmp_path: Path) -> None:
    clear_log_buffer_for_tests()
    app = create_app(data_dir=tmp_path / "data", job_processor=lambda task_id: None)
    app.state.zimuku_ocr_solver_factory = WrongAnswerOcrSolver

    with TestClient(app) as client:
        client.put(
            "/api/v1/providers/zimuku/settings",
            json={"enabled": True, "moviepilot_ocr_url": "http://ocr.test"},
        )
        response = client.post("/api/v1/providers/zimuku/ocr-check")
    logs, _ = list_structured_logs(provider="zimuku")

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "MoviePilot OCR recognition failed: expected 06394, got 1234"
    )
    assert any(
        entry["stage"] == "ocr_check"
        and entry["status"] == "failed"
        and entry["error_code"] == "ocr_answer_mismatch"
        for entry in logs
    )
