from subtitle_sidecar.providers import subliminal_update


class FakeResponse:
    status_code = 200

    def json(self):
        return {"tag_name": "v9.9.9", "html_url": "https://github.test/release"}


class FakeClient:
    def get(self, url, **kwargs):
        assert url == subliminal_update.GITHUB_RELEASE_API
        return FakeResponse()


class RecordingClient:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs["headers"])
        return FakeResponse()


def test_update_check_compares_github_release(monkeypatch) -> None:
    monkeypatch.setattr(subliminal_update, "version", lambda package: "2.6.0")

    result = subliminal_update.check_subliminal_update(FakeClient())

    assert result == {
        "current_version": "2.6.0",
        "latest_version": "9.9.9",
        "update_available": True,
        "status": "ok",
        "release_url": "https://github.test/release",
        "error": None,
    }


def test_dependency_update_checks_use_github_token_without_returning_it(monkeypatch) -> None:
    monkeypatch.setattr(
        subliminal_update,
        "version",
        lambda package: {"subliminal": "2.6.0", "ffsubsync": "0.5.0"}[package],
    )
    client = RecordingClient()

    subliminal = subliminal_update.check_subliminal_update(client, github_token="secret-token")
    ffsubsync = subliminal_update.check_ffsubsync_update(client, github_token="secret-token")

    assert client.urls == [
        subliminal_update.GITHUB_RELEASE_API,
        "https://api.github.com/repos/smacke/ffsubsync/releases/latest",
    ]
    assert all(headers["Authorization"] == "Bearer secret-token" for headers in client.headers)
    assert "secret-token" not in str(subliminal)
    assert "secret-token" not in str(ffsubsync)


def test_missing_package_is_reported_without_contacting_github(monkeypatch) -> None:
    class UnexpectedClient:
        def get(self, *_args, **_kwargs):
            raise AssertionError("GitHub should not be queried for an uninstalled package")

    def missing_version(_package: str) -> str:
        raise subliminal_update.PackageNotFoundError

    monkeypatch.setattr(subliminal_update, "version", missing_version)

    result = subliminal_update.check_ffsubsync_update(UnexpectedClient())

    assert result == {
        "current_version": "not_installed",
        "latest_version": None,
        "update_available": False,
        "status": "unavailable",
        "release_url": "https://github.com/smacke/ffsubsync",
        "error": "package_not_installed",
    }
