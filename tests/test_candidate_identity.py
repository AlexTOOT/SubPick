from subtitle_sidecar.pipeline.candidate_identity import (
    candidate_identity,
    normalize_source_url,
    subtitle_content_identity,
)


def test_candidate_identity_prefers_native_id_over_source_url() -> None:
    identity = candidate_identity(
        provider="subliminal:OpenSubtitles",
        raw_metadata={"subtitle_id": 12345},
        source_url="https://example.test/subtitle?id=different",
    )

    assert identity == "subliminal:opensubtitles|id:12345"


def test_candidate_identity_keeps_full_provider_namespace() -> None:
    assrt = candidate_identity(
        provider="assrt",
        raw_metadata={"id": "123"},
        source_url=None,
    )
    subdl = candidate_identity(
        provider="subdl",
        raw_metadata={"id": "123"},
        source_url=None,
    )

    assert assrt != subdl


def test_candidate_identity_adds_actual_provider_namespace_from_metadata() -> None:
    identity = candidate_identity(
        provider="subliminal",
        raw_metadata={"internal_provider": "OpenSubtitles", "id": 123},
        source_url=None,
    )

    assert identity == "subliminal:opensubtitles|id:123"


def test_candidate_identity_falls_back_to_normalized_source_url() -> None:
    first = candidate_identity(
        provider="zimuku",
        raw_metadata={"id": {"not": "a scalar"}},
        source_url="HTTPS://ZIMUKU.ORG:443/detail/123/?b=2&a=1#download",
    )
    second = candidate_identity(
        provider="ZIMUKU",
        raw_metadata={},
        source_url="https://zimuku.org/detail/123?a=1&b=2",
    )

    assert first == second
    assert normalize_source_url("https://example.test/path/") == "https://example.test/path"


def test_normalize_source_url_drops_userinfo() -> None:
    with_credentials = candidate_identity(
        provider="private",
        raw_metadata={},
        source_url="https://alice:secret@example.test:8443/subtitle?id=1",
    )
    without_credentials = candidate_identity(
        provider="private",
        raw_metadata={},
        source_url="https://example.test:8443/subtitle?id=1",
    )

    assert with_credentials == without_credentials
    assert "alice" not in str(with_credentials)
    assert "secret" not in str(with_credentials)


def test_subtitle_content_identity_ignores_timing_and_ass_styles(tmp_path) -> None:
    srt = tmp_path / "sample.srt"
    ass = tmp_path / "sample.ass"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好 World\n",
        encoding="utf-8",
    )
    ass.write_text(
        "[Script Info]\n[Events]\n"
        "Dialogue: 0,0:10:01.00,0:10:02.00,Default,,0,0,0,,{\\b1}你好 World\n",
        encoding="utf-8",
    )

    srt_identity = subtitle_content_identity(srt)
    ass_identity = subtitle_content_identity(ass)

    assert srt_identity["content_sha256"] != ass_identity["content_sha256"]
    assert srt_identity["text_fingerprint"] == ass_identity["text_fingerprint"]


def test_subtitle_content_identity_matches_utf16_and_utf8_text(tmp_path) -> None:
    utf8 = tmp_path / "utf8.srt"
    utf16 = tmp_path / "utf16.srt"
    content = "1\n00:00:01,000 --> 00:00:02,000\n你好 World\n"
    utf8.write_text(content, encoding="utf-8")
    utf16.write_text(content, encoding="utf-16")

    utf8_identity = subtitle_content_identity(utf8)
    utf16_identity = subtitle_content_identity(utf16)

    assert utf8_identity["content_sha256"] != utf16_identity["content_sha256"]
    assert utf8_identity["text_fingerprint"] == utf16_identity["text_fingerprint"]
