from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Iterable

from subtitle_sidecar.pipeline.validator import _read_subtitle_text


def normalize_dialogue(value: str) -> str:
    """Normalize dialogue for an irreversible, formatting-insensitive fingerprint."""
    without_tags = re.sub(r"<[^>]+>|\{[^}]*\}", "", value)
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", without_tags).casefold()
        if character.isalnum()
    )


def fingerprint_dialogue(value: str) -> str:
    return sha256(normalize_dialogue(value).encode("utf-8")).hexdigest()


def subtitle_dialogue_fingerprints(path: Path) -> set[str]:
    content, _ = _read_subtitle_text(path)
    if content is None:
        raise ValueError("subtitle_decode_error")
    units = [value for value in _dialogue_units(path.suffix.casefold(), content) if value]
    normalized: set[str] = set()
    for index in range(len(units)):
        for width in range(1, 4):
            window = units[index : index + width]
            if len(window) != width:
                break
            value = normalize_dialogue(" ".join(window))
            if value:
                normalized.add(value)
    return {sha256(value.encode("utf-8")).hexdigest() for value in normalized}


def evaluate_subtitle_fingerprints(
    path: Path,
    expected_fingerprints: Iterable[str],
) -> dict[str, Any]:
    expected = {str(value).casefold() for value in expected_fingerprints}
    actual = subtitle_dialogue_fingerprints(path)
    matched = sorted(expected & actual)
    return {
        "path": str(path),
        "matched": bool(matched),
        "matched_count": len(matched),
        "expected_count": len(expected),
        "matched_fingerprints": matched,
    }


def _dialogue_units(extension: str, content: str) -> list[str]:
    if extension == ".srt":
        units: list[str] = []
        for block in re.split(r"\r?\n\s*\r?\n", content):
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            text_lines = [
                line
                for line in lines
                if "-->" not in line and not re.fullmatch(r"\d+", line)
            ]
            units.extend(text_lines)
            if len(text_lines) > 1:
                units.append(" ".join(text_lines))
        return units
    if extension in {".ass", ".ssa"}:
        units = []
        for line in content.splitlines():
            if not line.casefold().startswith("dialogue:"):
                continue
            text = line.split(",", 9)[-1]
            parts = [part.strip() for part in re.split(r"\\[Nn]", text) if part.strip()]
            units.extend(parts)
            if len(parts) > 1:
                units.append(" ".join(parts))
        return units
    raise ValueError("unsupported_subtitle_extension")


def _load_case(dataset_path: Path, case_id: str) -> dict[str, Any]:
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    for case in payload.get("cases", []):
        if case.get("id") == case_id:
            return case
    raise ValueError(f"unknown_evaluation_case:{case_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Match subtitle files against opaque dialogue fingerprints.",
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--case", dest="case_id", required=True)
    parser.add_argument("subtitles", type=Path, nargs="+")
    args = parser.parse_args(argv)

    try:
        case = _load_case(args.dataset, args.case_id)
        expected = case["dialogue_reference"]["fingerprints"]
        results = [
            evaluate_subtitle_fingerprints(path, expected)
            for path in args.subtitles
        ]
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2

    print(
        json.dumps(
            {"case_id": args.case_id, "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(result["matched"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
