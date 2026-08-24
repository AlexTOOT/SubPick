from pathlib import Path

from subtitle_sidecar.evaluation import (
    evaluate_subtitle_fingerprints,
    fingerprint_dialogue,
    normalize_dialogue,
    subtitle_dialogue_fingerprints,
)


def test_dialogue_fingerprint_ignores_case_punctuation_and_tags() -> None:
    assert normalize_dialogue("{\\i1}<b>Hello, 世界!</b>") == "hello世界"
    assert fingerprint_dialogue("Hello, 世界!") == fingerprint_dialogue("hello世界")


def test_srt_fingerprint_matches_wrapped_dialogue_without_exposing_text(tmp_path: Path) -> None:
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n这是一个\n测试句子\n",
        encoding="utf-8",
    )
    expected = fingerprint_dialogue("这是一个测试句子")

    result = evaluate_subtitle_fingerprints(subtitle, [expected])

    assert result["matched"] is True
    assert result["matched_fingerprints"] == [expected]
    assert "测试句子" not in str(result)


def test_ass_fingerprint_extracts_dialogue_lines(tmp_path: Path) -> None:
    subtitle = tmp_path / "sample.ass"
    subtitle.write_text(
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,第一行\\N第二行\n",
        encoding="utf-8",
    )

    fingerprints = subtitle_dialogue_fingerprints(subtitle)

    assert fingerprint_dialogue("第一行") in fingerprints
    assert fingerprint_dialogue("第一行第二行") in fingerprints
