from pathlib import Path

from fastapi.testclient import TestClient

from subtitle_sidecar.main import create_app


WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "subtitle_sidecar" / "web_v2"


def test_shimu_web_ui_is_the_default_and_v2_compatibility_entry(tmp_path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        root_response = client.get("/")
        path_response = client.get("/v2")
        old_port_response = client.get("/", headers={"host": "sidecar.local:19036"})
        legacy_response = client.get("/legacy")
        js_response = client.get("/web/app.js")
        css_response = client.get("/web/styles.css")

    for response in (root_response, path_response, old_port_response):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "拾幕" in response.text
        assert "让每一部影片，都有合适的字幕" in response.text
        assert "/web/app.js?v=" in response.text
        assert "/web/styles.css?v=" in response.text
    assert legacy_response.status_code == 404
    assert js_response.status_code == 200
    assert js_response.headers["cache-control"] == "no-store"
    assert css_response.status_code == 200
    assert css_response.headers["cache-control"] == "no-store"


def test_shimu_web_assets_are_self_contained_and_feature_complete():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    combined = html + css + js

    assert 'src="https://' not in combined
    assert '<link href="https://' not in combined
    assert 'src="http://' not in combined
    assert 'href="http://' not in combined
    assert "cdn." not in combined
    assert "tailwind" not in combined.lower()
    assert "EventSource" in js
    assert "setInterval" not in js
    assert "/api/v1/diagnostics" in js
    assert "/api/v1/diagnostics/export" in js
    assert "/api/v1/settings/export" in js
    assert "/api/v1/settings/import" in js
    assert "/api/v1/jobs" in js
    assert "/api/v1/tasks/batch-retry" in js
    assert "/api/v1/tasks/batch-delete" in js
    assert "/api/v1/jellyfin/libraries" in js
    assert "/api/v1/jellyfin/check" in js
    assert "/api/v1/jellyfin/recent" in js
    assert "/api/v1/jellyfin/tasks" in js
    assert "/api/v1/jellyfin/items/batch-ignore" in js
    assert "/api/v1/logs" in js
    assert "/api/v1/diagnostics/health-runs" in js
    assert "/api/v1/github/settings" in js
    assert "/api/v1/server/settings" in js
    assert "/api/v1/setup/wizard" in js
    assert "/api/v1/paths/settings" in js
    assert "/api/v1/paths/check" in js
    assert "/api/v1/providers/order" in js
    assert "/api/v1/providers/subliminal/settings" in js
    assert "/api/v1/providers/subdl/settings" in js
    assert "/api/v1/providers/assrt/settings" in js
    assert "/api/v1/providers/zimuku/settings" in js
    assert "/api/v1/providers/zimuku/ocr-check" in js
    assert "data-provider-drag" in js
    assert 'id="media-sort"' in html
    assert 'id="media-sort-direction"' in html
    assert 'id="server-token"' in html
    assert 'id="server-token-generate"' in html
    assert 'id="setup-progress"' in html
    assert 'id="setup-top-action"' in html
    assert 'id="notification-center"' in html
    assert 'id="setup-dialog"' in html
    assert 'id="jellyfin-check"' in html
    assert 'id="diag-health-check"' in html
    assert 'id="path-mapping-settings"' in html
    assert 'data-view="diagnostics"' not in html
    assert 'id="health-dialog"' in html
    assert 'id="health-progress-bar"' in html
    assert 'id="jellyfin-user"' not in html
    assert "buildHealthChecks" in js
    assert "renderHealthDialog" in js
    assert 'type="text" autocomplete="off" spellcheck="false"' in html
    assert "仅看缺失" in js
    assert "全选缺失" in js
    assert "选择整剧" in js
    assert "全选本季" in js
    assert "字幕产物" in js
    assert "providerDiagnosticStatus" in js
    assert "autoCheckProviders" not in js
    assert "drawerOpenSeasons" in js
    assert "getRandomValues" in js
    assert "grid-template-rows: 18px 40px 18px" in css
    assert "scrollbar-gutter: stable" in css
    assert "width: 136px; height: 136px" in css
    assert "window.setTimeout(resolve, 1750)" in js
    assert "safeExternalUrl(candidate.source_url)" in js
    assert "搜索结果与排序" in js
    assert '<details class="drawer-section candidate-discovery-section">' in js
    assert '<details class="drawer-section candidate-discovery-section" open>' not in js
    assert "查看字幕源原始返回顺序" in js
    assert "已尝试候选" in js
    assert 'href="/legacy"' not in html
    assert "清空显示" in html
    assert html.count('<h2 id="library-title">媒体库</h2>') == 0
    assert "系统日志" in html
    assert 'id="log-category"' in html
    assert html.count('<h2 id="settings-title">设置</h2>') == 0
    assert html.count('<h2 id="tasks-title">任务工作台</h2>') == 0
    assert '<div class="section-commandbar">' in html
    assert html.count('class="actions toolbar-actions"') == 2
    assert "浏览 Jellyfin 媒体库字幕状态，并按需创建任务" in js
    assert "animateDisclosure" in js
    assert "showValidationFailure" in js
    assert "validation-shake" in css
    assert "setup-page-forward" in css
    assert "window.location.origin" in js
    assert 'id="setup-subdl-pro"' in js
    assert "use_api_key_for_downloads: draft.subdlPro" in js
    assert "startInitialJellyfinScan" in js
    assert "if (!wasConfigured) startInitialJellyfinScan();" in js
    assert "data-setup-page" in js
    assert "wizard.page = Math.min(restoredPage, wizard.maxPage);" in js
    assert "subpick-setup-dismissed-v2" not in js
    assert "syncSetupProviderFieldStates" in js
    assert "setup-provider-expand" not in css
    assert ".media-grid > .empty { grid-column: 1 / -1; width: 100%; }" in css
    assert '.provider-config-card[open] > summary::after { transform: rotate(90deg); }' in css


def test_shimu_web_assets_do_not_contain_mojibake(tmp_path):
    app = create_app(data_dir=tmp_path, job_processor=lambda task_id: None)

    with TestClient(app) as client:
        html_response = client.get("/")
        js_response = client.get("/web/app.js")

    combined = html_response.text + js_response.text
    for marker in ("闁", "閿", "锟"):
        assert marker not in combined
