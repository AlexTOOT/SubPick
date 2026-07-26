from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_NATIVE_ID_KEYS = (
    "assrt_subtitle_id",
    "subtitle_id",
    "sub_id",
    "sd_id",
    "subdl_subtitle_id",
    "zimuku_subtitle_id",
    "opensubtitles_id",
    "opensubtitlescom_id",
    "id",
)


def candidate_identity(
    *,
    provider: str,
    raw_metadata: Mapping[str, object] | None,
    source_url: str | None,
) -> str | None:
    provider_namespace = _provider_namespace(provider, raw_metadata)
    if not provider_namespace:
        return None

    native_id = _native_id(raw_metadata)
    if native_id is not None:
        return f"{provider_namespace}|id:{native_id}"

    normalized_url = normalize_source_url(source_url)
    if normalized_url:
        return f"{provider_namespace}|url:{normalized_url}"
    return None


def normalize_source_url(source_url: str | None) -> str | None:
    value = str(source_url or "").strip()
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value

    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").casefold()
    if not hostname:
        return value
    try:
        port = parts.port
    except ValueError:
        return value
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, hostname, path, query, ""))


def subtitle_content_identity(path: Path) -> dict[str, str]:
    payload = path.read_bytes()
    identity = {"content_sha256": sha256(payload).hexdigest()}
    text = _decode_subtitle(payload)
    if text is None:
        return identity
    normalized = _normalized_subtitle_text(text)
    if normalized:
        identity["text_fingerprint"] = sha256(normalized.encode("utf-8")).hexdigest()
    return identity


def _decode_subtitle(payload: bytes) -> str | None:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _normalized_subtitle_text(text: str) -> str:
    lines: list[str] = []
    for original_line in text.splitlines():
        line = original_line.strip()
        if not line or re.fullmatch(r"\d+", line):
            continue
        if "-->" in line or re.match(r"^\d+:\d{2}:\d{2}[.,]\d+", line):
            continue
        if line.casefold().startswith("dialogue:"):
            parts = line.split(",", 9)
            line = parts[-1] if len(parts) == 10 else line
        elif line.startswith("[") and line.endswith("]"):
            continue
        elif re.match(r"^[A-Za-z][A-Za-z ]*:", line):
            continue
        line = re.sub(r"\{[^{}]*\}", "", line)
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\s+", "", line).casefold()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _native_id(raw_metadata: Mapping[str, object] | None) -> str | None:
    if not isinstance(raw_metadata, Mapping):
        return None
    for key in _NATIVE_ID_KEYS:
        value = raw_metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _provider_namespace(
    provider: str,
    raw_metadata: Mapping[str, object] | None,
) -> str:
    top_level = str(provider or "").strip().casefold()
    if not top_level or ":" in top_level or not isinstance(raw_metadata, Mapping):
        return top_level
    actual = raw_metadata.get("internal_provider")
    if not isinstance(actual, str):
        return top_level
    normalized_actual = actual.strip().casefold()
    if not normalized_actual or normalized_actual == top_level:
        return top_level
    return f"{top_level}:{normalized_actual}"
