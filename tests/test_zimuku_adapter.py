from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image

from subtitle_sidecar.providers.base import SubtitleSearchRequest
from subtitle_sidecar.providers.zimuku_adapter import (
    CaptchaSolverChain,
    FailedCaptchaRecorder,
    MoviePilotOcrSolver,
    ZimukuCaptchaRecognitionError,
    ZimukuError,
    ZimukuProvider,
    _SearchQuery,
    _extract_external_archive,
    _matching_work_pages,
    _parse_lsar_entries,
    _parse_search_results,
    _parse_work_results,
    _response_filename,
    _search_queries,
)


SEARCH_HTML = """
<div class="item prel clearfix">
  <div class="title">
    <p class="tt clearfix"><a href="//zimuku.org/subs/73237.html"><b>新·驯龙高手 How to Train Your Dragon (2025)</b></a></p>
    <div class="sublist"><table><tbody>
      <tr><td class="first">
        <img alt="简体中文字幕 English字幕 双语字幕" src="/flag/china.gif">
        <a href="//zimuku.org/detail/219764.html" title="How.To.Train.Your.Dragon.2025.WEB-DL"><b>subtitle</b></a>
        <span class="label label-info">SRT</span><span class="label label-info">ASS/SSA</span>
      </td><td><i title="字幕质量:9.6分"></i></td><td class="last">5095</td></tr>
    </tbody></table></div>
  </div>
</div>
"""

SEASON_HTML = """
<div class="item prel clearfix"><div class="title">
  <p class="tt clearfix"><a href="/subs/1.html">大楼里只有谋杀 Only Murders in the Building 第一季 (2021)</a></p>
  <div class="sublist"><table><tbody><tr><td class="first">
    <img alt="简体中文字幕 繁體中文字幕 English字幕 双语字幕" src="/flag/jollyroger.gif">
    <a href="/detail/710623.html" title="Only.Murders.in.the.Building.S01.1080p.WEB-DL">season pack</a>
    <span class="label label-info">ASS/SSA</span>
  </td><td><i title="字幕质量:10分"></i></td><td>1.2万</td></tr></tbody></table></div>
</div></div>
"""

MULTI_CANDIDATE_HTML = """
<table><tbody>
  <tr><td class="first"><a href="/detail/219503.html" title="怪奇收割.Strange.Harvest.2025.中英字幕">bilingual</a><span class="label">ASS/SSA</span></td>
    <td class="tac lang"><img alt="双语"></td><td class="tac hidden-xs"><i title="字幕质量:8.9分"></i></td><td class="tac hidden-xs">6138</td><td class="last hidden-xs">25/9/20</td></tr>
  <tr><td class="first"><a href="/detail/219498.html" title="Strange.Harvest.2025.srt">simplified</a><span class="label">SRT</span></td>
    <td class="tac lang"><img alt="简体中文"></td><td class="tac hidden-xs"><i title="字幕质量:9分"></i></td><td class="tac hidden-xs">1753</td><td class="last hidden-xs">25/9/20</td></tr>
  <tr><td class="first"><a href="/detail/219435.html" title="Strange.Harvest.2024.srt">simplified</a><span class="label">SRT</span></td>
    <td class="tac lang"><img alt="简体中文"></td><td class="tac hidden-xs"><i title="字幕质量:6分"></i></td><td class="tac hidden-xs">1225</td><td class="last hidden-xs">25/9/18</td></tr>
  <tr><td class="first"><a href="/detail/219028.html" title="Strange.Harvest.2024.srt">english</a><span class="label">SRT</span></td>
    <td class="tac lang"><img alt="English"></td><td class="tac hidden-xs"><i title="字幕质量:10分"></i></td><td class="tac hidden-xs">506</td><td class="last hidden-xs">25/9/9</td></tr>
</tbody></table>
"""


class FakeCookies:
    def __init__(self) -> None:
        self.values = {}

    def set(self, key, value) -> None:
        self.values[key] = value


class FakeResponse:
    def __init__(self, *, text="", content=b"", url="https://srtku.com/", headers=None, status=200):
        self.text = text
        self.content = content or text.encode()
        self.url = url
        self.headers = headers or {}
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class JsonResponse(FakeResponse):
    def __init__(self, payload, **kwargs):
        super().__init__(**kwargs)
        self.payload = payload

    def json(self):
        return self.payload


class SearchClient:
    def __init__(self, html=SEARCH_HTML) -> None:
        self.html = html
        self.calls = []
        self.cookies = FakeCookies()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(text=self.html, url=str(url))


class FullPageClient(SearchClient):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "/subs/73237.html" not in str(url):
            return FakeResponse(text=SEARCH_HTML, url=str(url))
        second_row = SEARCH_HTML.replace("219764", "217152").replace("Bluray", "WEB-DL")
        rows = "".join(
            part
            for part in (
                SEARCH_HTML.split("<tbody>", 1)[1].split("</tbody>", 1)[0],
                second_row.split("<tbody>", 1)[1].split("</tbody>", 1)[0],
            )
        )
        return FakeResponse(text=f"<table><tbody>{rows}</tbody></table>", url=str(url))


def movie_request() -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        video_path=Path("How.to.Train.Your.Dragon.2025.mkv"),
        title="新·驯龙高手",
        original_title="How to Train Your Dragon",
        year=2025,
        media_type="movie",
        season=None,
        episode=None,
        preferred="bilingual",
        fallback_languages=["zh-cn", "zh-hant"],
        tmdb_id="1087192",
    )


def episode_request() -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        video_path=Path("Only.Murders.in.the.Building.S01E03.mkv"),
        title="大楼里只有谋杀",
        original_title="Only Murders in the Building",
        year=2021,
        media_type="episode",
        season=1,
        episode=3,
        preferred="bilingual",
        fallback_languages=["zh-cn", "zh-hant"],
        series_id="tmdb:107113",
    )


def test_movie_search_uses_title_and_year_and_parses_chinese_candidate() -> None:
    client = SearchClient()
    reports = []
    provider = ZimukuProvider(client=client, request_delay_seconds=0)
    provider.set_reporter(reports.append)

    candidates = provider.search(movie_request())

    assert len(candidates) == 1
    assert candidates[0].source_url == "https://zimuku.org/detail/219764.html"
    assert candidates[0].is_bilingual is True
    assert candidates[0].format == "ass"
    assert client.calls[0][1]["params"] == {"q": "新·驯龙高手 2025"}
    progress = next(report for report in reports if report.status == "progress")
    assert progress.reason == "新·驯龙高手 2025"
    assert progress.search_context["query"] == "新·驯龙高手 2025"
    assert progress.search_context["strategy"] == "title_year"


def test_movie_search_falls_back_to_plain_localized_and_original_titles() -> None:
    queries = _search_queries(movie_request())

    assert [(query.value, query.strategy) for query in queries] == [
        ("新·驯龙高手 2025", "title_year"),
        ("How to Train Your Dragon 2025", "title_year"),
        ("新·驯龙高手", "title"),
        ("How to Train Your Dragon", "title"),
    ]


def test_movie_work_page_accepts_adjacent_release_year_when_title_matches() -> None:
    html = SEARCH_HTML.replace("(2025)", "(2024)")

    pages = _matching_work_pages(
        html,
        request=movie_request(),
        query=_SearchQuery("新·驯龙高手", "新·驯龙高手", "title", "title"),
    )

    assert pages == [
        ("新·驯龙高手 How to Train Your Dragon (2024)", "//zimuku.org/subs/73237.html")
    ]


def test_movie_work_page_rejects_distant_release_year() -> None:
    html = SEARCH_HTML.replace("(2025)", "(2022)")

    pages = _matching_work_pages(
        html,
        request=movie_request(),
        query=_SearchQuery("新·驯龙高手", "新·驯龙高手", "title", "title"),
    )

    assert pages == []


def test_work_results_keep_bilingual_rows_and_parse_real_download_column() -> None:
    request = movie_request()
    query = _SearchQuery("新·驯龙高手", "新·驯龙高手", "title", "title")

    candidates = _parse_work_results(
        MULTI_CANDIDATE_HTML,
        work_title="怪奇收割 Strange Harvest (2024)",
        request=request,
        query=query,
        request_base_url="https://srtku.com",
    )

    assert [candidate.source_url for candidate in candidates] == [
        "https://zimuku.org/detail/219503.html",
        "https://zimuku.org/detail/219498.html",
        "https://zimuku.org/detail/219435.html",
    ]
    assert candidates[0].is_bilingual is True
    assert [candidate.raw_metadata["zimuku_downloads"] for candidate in candidates] == [
        6138,
        1753,
        1225,
    ]
    assert candidates[0].confidence > candidates[1].confidence > candidates[2].confidence


@pytest.mark.parametrize(
    "disposition",
    [
        'attachment; filename="Strange.Harvest.Chs%26amp;Eng.ass"',
        'attachment; filename="Strange.Harvest.Chs%26amp%3BEng.ass"',
    ],
)
def test_response_filename_keeps_extension_after_encoded_html_entity(disposition: str) -> None:
    response = SimpleNamespace(headers={"Content-Disposition": disposition})

    assert _response_filename(response, "https://s.zimuku.org/download/token") == (
        "Strange.Harvest.Chs&Eng.ass"
    )


def test_search_opens_full_work_page_instead_of_using_preview_only() -> None:
    provider = ZimukuProvider(client=FullPageClient(), request_delay_seconds=0)
    candidates = provider.search(movie_request())
    assert [candidate.source_url for candidate in candidates] == [
        "https://zimuku.org/detail/219764.html",
        "https://zimuku.org/detail/217152.html",
    ]


def test_graphic_only_subtitles_are_not_returned_as_text_candidates() -> None:
    html = SEARCH_HTML.replace(
        '<span class="label label-info">SRT</span><span class="label label-info">ASS/SSA</span>',
        '<span class="label label-info">SUP</span>',
    )
    provider = ZimukuProvider(client=SearchClient(html), request_delay_seconds=0)
    assert provider.search(movie_request()) == []


def test_episode_search_prefers_one_season_query_and_returns_bundle() -> None:
    client = SearchClient(SEASON_HTML)
    provider = ZimukuProvider(client=client, request_delay_seconds=0)

    candidates = provider.search(episode_request())

    assert len(candidates) == 1
    assert len(client.calls) == 2
    assert client.calls[0][1]["params"] == {"q": "大楼里只有谋杀 S01"}
    assert client.calls[1][0].endswith("/subs/1.html")
    assert candidates[0].raw_metadata["expected_episode"] == 3


class FixedSolver:
    def __init__(self) -> None:
        self.images = []

    def solve(self, image: bytes) -> str:
        self.images.append(image)
        return "26584"


class ChallengeClient(SearchClient):
    def __init__(self) -> None:
        super().__init__()
        self.challenge_sent = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "security_verify_img" in str(url):
            return FakeResponse(url=str(url), status=404)
        if not self.challenge_sent:
            self.challenge_sent = True
            encoded = "Qk0xMjM="
            html = (
                f'<img src="data:image/bmp;base64,{encoded}">'
                '<script>self.location = "/?security_verify_img=" + stringToHex(code)</script>'
            )
            return FakeResponse(text=html, url=str(url), status=404)
        return FakeResponse(text=SEARCH_HTML, url=str(url))


def test_numeric_challenge_is_solved_and_original_request_is_retried() -> None:
    client = ChallengeClient()
    solver = FixedSolver()
    provider = ZimukuProvider(client=client, captcha_solver=solver, request_delay_seconds=0)

    candidates = provider.search(movie_request())

    assert len(candidates) == 1
    assert solver.images == [b"BM123"]
    assert "srcurl" in client.cookies.values
    assert any("security_verify_img=3236353834" in call[0] for call in client.calls)


def test_numeric_challenge_without_solver_has_clear_error() -> None:
    provider = ZimukuProvider(client=ChallengeClient(), request_delay_seconds=0)
    with pytest.raises(ZimukuError, match="zimuku_captcha_required"):
        provider.search(movie_request())


class OcrClient:
    def __init__(self, result: str) -> None:
        self.result = result
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return JsonResponse({"result": self.result})

    def get(self, url, **kwargs):
        return FakeResponse(text='{"message":"MoviePilot OCR API"}', url=str(url))


def test_moviepilot_ocr_solver_uses_base64_contract() -> None:
    client = OcrClient("26584")
    solver = MoviePilotOcrSolver(base_url="http://moviepilot-ocr:9899/", client=client)

    assert solver.solve(b"BM123") == "26584"
    assert client.posts[0][0] == "http://moviepilot-ocr:9899/captcha/base64"
    assert client.posts[0][1]["json"] == {"base64_img": "Qk0xMjM="}
    assert solver.check_available() >= 0


def test_moviepilot_ocr_invalid_answer_is_recorded_before_fallback(tmp_path: Path) -> None:
    recorder = FailedCaptchaRecorder(tmp_path)
    chain = CaptchaSolverChain(
        [MoviePilotOcrSolver(base_url="http://ocr", client=OcrClient("not-a-number")), FixedSolver()],
        recorder=recorder,
    )

    assert chain.solve(b"BM123") == "26584"
    metadata = list(tmp_path.glob("*.json"))
    assert len(metadata) == 1
    assert (
        '"answer": "raw=not-a-number; preprocessed=not-a-number"'
        in metadata[0].read_text(encoding="utf-8")
    )
    assert '"reason": "invalid_answer"' in metadata[0].read_text(encoding="utf-8")


def test_moviepilot_ocr_retries_with_normalized_image() -> None:
    class SequentialOcrClient(OcrClient):
        def __init__(self) -> None:
            super().__init__("")

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            return JsonResponse({"result": "" if len(self.posts) == 1 else "06394"})

    source = Image.new("RGB", (40, 20), "#222222")
    buffer = BytesIO()
    source.save(buffer, format="BMP")
    client = SequentialOcrClient()
    solver = MoviePilotOcrSolver(base_url="http://ocr", client=client)

    assert solver.solve(buffer.getvalue()) == "06394"
    assert len(client.posts) == 2
    assert client.posts[0][1]["json"]["base64_img"] != client.posts[1][1]["json"]["base64_img"]


def test_moviepilot_ocr_rejects_wrong_length() -> None:
    solver = MoviePilotOcrSolver(base_url="http://ocr", client=OcrClient("123"))

    with pytest.raises(ZimukuCaptchaRecognitionError, match="zimuku_ocr_answer_invalid"):
        solver.solve(b"BM123")


class AlwaysChallengeClient(ChallengeClient):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "security_verify_img" in str(url):
            return FakeResponse(url=str(url), status=404)
        encoded = "Qk0xMjM="
        html = (
            f'<img src="data:image/bmp;base64,{encoded}">'
            '<script>self.location = "/?security_verify_img=" + stringToHex(code)</script>'
        )
        return FakeResponse(text=html, url=str(url), status=404)


def test_rejected_captcha_attempts_are_persisted_when_enabled(tmp_path: Path) -> None:
    provider = ZimukuProvider(
        client=AlwaysChallengeClient(),
        captcha_solver=FixedSolver(),
        captcha_debug_dir=tmp_path,
        request_delay_seconds=0,
    )

    with pytest.raises(ZimukuError, match="zimuku_captcha_rejected"):
        provider.search(movie_request())

    metadata = list(tmp_path.glob("*.json"))
    assert len(metadata) == 3
    assert len(list(tmp_path.glob("*.bmp"))) == 3
    assert all('"answer": "26584"' in path.read_text(encoding="utf-8") for path in metadata)
    assert all(
        '"reason": "rejected_by_zimuku"' in path.read_text(encoding="utf-8")
        for path in metadata
    )


class DownloadClient(SearchClient):
    def __init__(self) -> None:
        super().__init__()
        archive = BytesIO()
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("Series.S01E01.chs.ass", "[Script Info]\n")
            bundle.writestr("Series.S01E02.chs.ass", "[Script Info]\n")
            bundle.writestr("readme.txt", "ignored")
        self.archive = archive.getvalue()

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if str(url).endswith("/detail/710623.html"):
            return FakeResponse(text='<a id="down1" href="/dld/710623.html">download</a>', url=str(url))
        if str(url).endswith("/dld/710623.html"):
            return FakeResponse(text='<a rel="nofollow" href="/download/pack.zip">file</a>', url=str(url))
        if str(url).endswith("/download/pack.zip"):
            return FakeResponse(
                content=self.archive,
                url=str(url),
                headers={"Content-Disposition": 'attachment; filename="season.zip"'},
            )
        raise AssertionError(url)


def test_download_keeps_all_supported_season_members(tmp_path: Path) -> None:
    client = DownloadClient()
    provider = ZimukuProvider(client=client, request_delay_seconds=0)
    candidate = _parse_search_results(
        SEASON_HTML,
        request=episode_request(),
        query=_SearchQuery("大楼里只有谋杀 S01", "大楼里只有谋杀", "title", "season_pack"),
        request_base_url="https://srtku.com",
    )[0]

    downloaded = provider.download(candidate, tmp_path)

    assert [member.filename for member in downloaded.files] == [
        "Series.S01E01.chs.ass",
        "Series.S01E02.chs.ass",
    ]
    assert all(member.path.is_file() for member in downloaded.files)


def test_movie_archive_selects_chinese_bilingual_member_as_primary(tmp_path: Path) -> None:
    client = DownloadClient()
    client.archive = BytesIO()
    with ZipFile(client.archive, "w") as bundle:
        bundle.writestr("Movie.Eng.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        bundle.writestr("Movie.ChsEng.ass", "[Script Info]\n")
    client.archive = client.archive.getvalue()
    candidate = _parse_search_results(
        SEASON_HTML,
        request=episode_request(),
        query=_SearchQuery("大楼里只有谋杀 S01", "大楼里只有谋杀", "title", "season_pack"),
        request_base_url="https://srtku.com",
    )[0]

    downloaded = ZimukuProvider(client=client, request_delay_seconds=0).download(candidate, tmp_path)

    assert downloaded.path.name.endswith("Movie.ChsEng.ass")


def test_external_archive_extracts_nested_unicode_members_in_one_pass(
    monkeypatch, tmp_path: Path
) -> None:
    listing = """
Path = download.rar
Type = Rar

Path = 权力的游戏/权力的游戏.S01E01.chs.ass
Size = 14

Path = 权力的游戏/权力的游戏.S01E02.chs.ass
Size = 14
"""
    calls = []

    def fake_archive_tool(command, *, binary=False):
        calls.append((command, binary))
        if command[1] == "l":
            return SimpleNamespace(stdout=listing)
        output_dir = Path(next(part[2:] for part in command if part.startswith("-o")))
        folder = output_dir / "权力的游戏"
        folder.mkdir(parents=True)
        (folder / "权力的游戏.S01E01.chs.ass").write_bytes(b"[Script Info]\n")
        (folder / "权力的游戏.S01E02.chs.ass").write_bytes(b"[Script Info]\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter.shutil.which",
        lambda name: "7z" if name == "7z" else None,
    )
    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter._run_archive_tool", fake_archive_tool
    )

    members = _extract_external_archive(b"Rar!\x1a\x07test", "season.rar", tmp_path)

    assert [member.filename for member in members] == [
        "权力的游戏.S01E01.chs.ass",
        "权力的游戏.S01E02.chs.ass",
    ]
    assert [call[0][1] for call in calls] == ["l", "x"]


def test_rar_uses_unar_when_7z_cannot_extract_compression_method(
    monkeypatch, tmp_path: Path
) -> None:
    listing = """
Path = download.rar
Type = Rar

Path = 权力的游戏/权力的游戏.S01E03.chs.ass
Size = 14
"""
    calls = []

    def fake_archive_tool(command, *, binary=False):
        calls.append(command)
        if command[1] == "l":
            return SimpleNamespace(stdout=listing)
        if command[0] == "7z":
            raise ZimukuError("zimuku_archive_extract_failed")
        output_dir = Path(command[command.index("-o") + 1])
        folder = output_dir / "权力的游戏"
        folder.mkdir(parents=True)
        (folder / "权力的游戏.S01E03.chs.ass").write_bytes(b"[Script Info]\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter.shutil.which",
        lambda name: name if name in {"7z", "unar"} else None,
    )
    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter._run_archive_tool", fake_archive_tool
    )

    members = _extract_external_archive(b"Rar!\x1a\x07test", "season.rar", tmp_path)

    assert [member.filename for member in members] == ["权力的游戏.S01E03.chs.ass"]
    assert [command[0:2] for command in calls] == [["7z", "l"], ["7z", "x"], ["unar", "-f"]]


def test_rar_prefers_lsar_and_unar_when_available(monkeypatch, tmp_path: Path) -> None:
    listing = json.dumps(
        {
            "lsarFormatVersion": 2,
            "lsarContents": [
                {
                    "XADFileName": "字幕/Her.2013.chs.ass",
                    "XADFileSize": 14,
                }
            ],
        },
        ensure_ascii=False,
    )
    calls = []

    def fake_archive_tool(command, *, binary=False):
        calls.append(command)
        if command[0] == "lsar":
            return SimpleNamespace(stdout=listing)
        output_dir = Path(command[command.index("-o") + 1])
        folder = output_dir / "字幕"
        folder.mkdir(parents=True)
        (folder / "Her.2013.chs.ass").write_bytes(b"[Script Info]\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter.shutil.which",
        lambda name: name if name in {"7zz", "lsar", "unar"} else None,
    )
    monkeypatch.setattr(
        "subtitle_sidecar.providers.zimuku_adapter._run_archive_tool", fake_archive_tool
    )

    members = _extract_external_archive(b"Rar!\x1a\x07test", "season.rar", tmp_path)

    assert [member.filename for member in members] == ["Her.2013.chs.ass"]
    assert [command[0:2] for command in calls] == [["lsar", "-json"], ["unar", "-f"]]


def test_parse_lsar_entries_rejects_invalid_output() -> None:
    with pytest.raises(ZimukuError, match="zimuku_archive_extract_failed"):
        _parse_lsar_entries("not-json")
