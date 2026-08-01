"use strict";

const state = {
  view: "overview",
  diagnostics: null,
  jobs: [],
  jobTotal: 0,
  taskPage: 1,
  taskPageSize: 50,
  taskSearch: "",
  taskStatus: "all",
  selectedTasks: new Set(),
  taskAnchor: null,
  libraries: [],
  activeLibraryId: null,
  media: [],
  recentMedia: [],
  highlightedMediaId: null,
  mediaSearch: "",
  mediaStatus: "all",
  mediaSort: "date_created",
  mediaSortDirection: "desc",
  selectedMedia: new Set(),
  drawerSeriesId: null,
  drawerMissingOnly: false,
  drawerOpenSeasons: new Set(),
  logs: [],
  logAfterId: 0,
  logSource: null,
  overviewSource: null,
  overviewRefreshTimer: null,
  logsPaused: false,
  jellyfinSettings: null,
  githubSettings: null,
  serverSettings: null,
  pathSettings: null,
  providerOrder: [],
  providerAdapters: [],
  providerSettings: {},
  providerChecks: {},
  openProviders: new Set(),
  draggedProvider: null,
  dependencyUpdates: null,
  healthChecks: [],
  healthRunning: false,
  setupWizard: { initialized: false, page: 0, busy: false, draft: {} },
  toastTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = payload.detail ? `：${typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail)}` : "";
    } catch (_error) {
      detail = "";
    }
    throw new Error(`请求失败 ${response.status}${detail}`);
  }
  if (response.status === 204) return null;
  const payload = await response.json();
  return { payload, response };
}

function showToast(message, error = false) {
  const node = $("#toast");
  window.clearTimeout(state.toastTimer);
  node.textContent = message;
  node.classList.toggle("error", error);
  node.classList.add("show");
  state.toastTimer = window.setTimeout(() => node.classList.remove("show"), 3200);
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function filename(path) {
  return String(path || "").replaceAll("\\", "/").split("/").pop() || "未命名媒体";
}

function statusLabel(status) {
  const labels = {
    pending: "等待中", queued: "排队中", running: "处理中", resolving: "解析中",
    checking_existing: "本地检查", checking_embedded: "内封检查", searching: "搜索中",
    downloading: "下载中", validating: "校验中", syncing: "对轴中", completed: "已完成",
    failed: "失败", interrupted: "已中断", skipped_existing_subtitle: "已跳过（已有字幕）",
    skipped_embedded_subtitle: "已跳过（内封字幕）", has_chinese: "已有",
    partial: "部分缺失", missing: "无字幕", ignored: "已忽略",
  };
  return labels[String(status || "").toLowerCase()] || String(status || "未知");
}

function statusClass(status) {
  const value = String(status || "").toLowerCase();
  if (value === "completed" || value === "has_chinese" || value.startsWith("skipped")) return "ok";
  if (value === "failed" || value === "missing" || value === "interrupted") return "error";
  if (value === "ignored") return "ignored";
  return "active";
}

function badge(status, label = null) {
  return `<span class="badge ${statusClass(status)}">${escapeHtml(label || statusLabel(status))}</span>`;
}

function setHealth(ok) {
  $("#sidebar-health-dot").className = `dot ${ok ? "ok" : "error"}`;
  $("#sidebar-health").textContent = ok ? "服务运行中" : "服务连接异常";
}

async function loadDiagnostics() {
  try {
    const { payload } = await api("/api/v1/diagnostics");
    state.diagnostics = payload;
    setHealth(payload.overall_status === "ok");
    renderHeaderStatus();
    renderDiagnostics();
    if (state.view === "settings" && state.providerOrder.length) renderProviderOrder();
  } catch (error) {
    setHealth(false);
    throw error;
  }
}

function renderHeaderStatus() {
  const diag = state.diagnostics;
  if (!diag) return;
  const enabled = Object.values(diag.providers || {}).filter((item) => item.enabled).length;
  $("#queue-chip").textContent = `队列 ${diag.queue.active_task_id || "-"} / 等待 ${diag.queue.queued_count}`;
  $("#provider-chip").textContent = `Provider ${enabled}`;
  $("#metric-active").textContent = diag.queue.active_task_id ? "1" : "0";
  $("#metric-queued").textContent = String(diag.queue.queued_count);
  $("#metric-interval").textContent = String(Math.ceil(diag.queue.next_provider_ready_seconds || 0));
  $("#metric-providers").textContent = String(enabled);
  $("#service-status").innerHTML = [
    ["Sidecar", `v${diag.version}`, diag.overall_status === "ok"],
    ["Jellyfin", diag.jellyfin.connected ? "已连接" : diag.jellyfin.configured ? "等待测试" : "未配置", diag.jellyfin.connected],
    ["MoviePilot", diag.moviepilot.connected ? "已连接" : diag.moviepilot.token_configured ? "等待验证" : "未配置", diag.moviepilot.connected],
    ["媒体目录", diag.media_dir.status, diag.media_dir.status === "ok"],
    ["数据目录", diag.data_dir.status, diag.data_dir.status === "ok"],
    ["数据库", diag.database.status, diag.database.status === "ok"],
  ].map(([name, value, ok]) => `
    <div class="status-item"><span>${escapeHtml(name)}</span><strong>${badge(ok ? "completed" : "failed", value)}</strong></div>
  `).join("");
  renderSetupStatus();
}

function systemLogCategoryLabel(value) {
  return ({
    system: "系统",
    health: "健康检查",
    configuration: "配置",
    provider: "Provider",
    queue: "队列",
    task: "任务",
  })[String(value || "").toLowerCase()] || value || "—";
}

function providerDiagnosticStatus(value) {
  if (!value?.enabled) return ["未启用", true];
  const labels = {
    ok: "可用",
    failed: "检查失败",
    error: "检查失败",
    unverified: "未验证",
    unconfigured: "未配置",
  };
  const label = labels[value.status] || String(value.status || "未验证");
  if (!value.last_checked_at || !["ok", "failed", "error"].includes(value.status)) {
    return [label, value.status === "ok"];
  }
  const checkedAt = new Date(value.last_checked_at);
  const checkedLabel = Number.isNaN(checkedAt.getTime())
    ? ""
    : checkedAt.toLocaleString("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
    });
  return [`${label}${checkedLabel ? ` · ${checkedLabel}` : ""}`, value.status === "ok"];
}

function renderSetupStatus() {
  const setup = state.diagnostics?.setup;
  if (!setup) return;
  const progress = $("#setup-progress");
  progress.hidden = setup.completed;
  progress.innerHTML = setup.completed ? "" : (setup.steps || []).map((step) => `
    <button class="setup-step ${escapeHtml(step.status)}" type="button"
      data-setup-view="${escapeHtml(step.target_view)}"
      data-setup-section="${escapeHtml(step.target_section || "")}">
      <i aria-hidden="true"></i>
      <span>${escapeHtml(step.label)}</span>
      ${step.help ? `<span class="help-icon" tabindex="0" aria-label="${escapeHtml(step.help)}" data-tooltip="${escapeHtml(step.help)}">?</span>` : ""}
    </button>
  `).join("");
  $$("[data-setup-view]", progress).forEach((button) => button.addEventListener("click", (event) => {
    if (event.target.closest(".help-icon")) return;
    goToSetupTarget(button.dataset.setupView, button.dataset.setupSection);
  }));

  const center = $("#notification-center");
  const notifications = setup.notifications || [];
  center.hidden = notifications.length === 0;
  center.innerHTML = notifications.map((item) => `
    <button class="notification ${escapeHtml(item.level)}" type="button"
      data-notification-view="${escapeHtml(item.target_view)}"
      data-notification-section="${escapeHtml(item.target_section || "")}">
      <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.message)}</small></span>
      <b aria-hidden="true">→</b>
    </button>
  `).join("");
  $$("[data-notification-view]", center).forEach((button) => button.addEventListener("click", () => {
    goToSetupTarget(button.dataset.notificationView, button.dataset.notificationSection);
  }));
  renderSetupDialog();
}

function renderSetupDialog() {
  const setup = state.diagnostics?.setup;
  if (!setup || setup.completed) {
    $("#setup-dialog").hidden = true;
    return;
  }
  if (!state.jellyfinSettings || !state.serverSettings || !Object.keys(state.providerSettings).length) return;
  initializeSetupWizard();
  const wizard = state.setupWizard;
  const pages = ["欢迎", "Jellyfin", "字幕来源", "MoviePilot", "完成"];
  const titles = ["欢迎使用拾幕", "连接 Jellyfin", "选择字幕来源", "连接 MoviePilot", "配置完成"];
  const descriptions = [
    "几步完成必要配置，其余选项以后再调整。",
    "用于读取媒体库、海报、入库信息和字幕状态。",
    "只选择需要的来源；高级参数以后仍可在设置中调整。",
    "使用同一个 Token 保护 MoviePilot 插件回调。",
    "拾幕已经可以开始工作。",
  ];
  $("#setup-title").textContent = titles[wizard.page];
  $("#setup-description").textContent = descriptions[wizard.page];
  $("#setup-wizard-progress").innerHTML = pages.map((label, index) => `
    <li class="${index < wizard.page ? "done" : index === wizard.page ? "current" : ""}">${escapeHtml(label)}</li>
  `).join("");
  $("#setup-dialog-body").innerHTML = setupWizardPageHtml(wizard.page, wizard.draft);
  $("#setup-back").hidden = wizard.page === 0 || wizard.page === pages.length - 1;
  $("#setup-continue").textContent = wizard.page === 0 ? "开始设置" : wizard.page === pages.length - 1 ? "完成" : "下一步";
  $("#setup-continue").disabled = wizard.busy;
  $("#setup-back").disabled = wizard.busy;
  $("#setup-skip").disabled = wizard.busy;
}

function maybeOpenSetupDialog() {
  const setup = state.diagnostics?.setup;
  if (!setup || setup.completed || window.localStorage.getItem("subpick-setup-dismissed-v2")) return;
  $("#setup-dialog").hidden = false;
  renderSetupDialog();
}

function closeSetupDialog() {
  $("#setup-dialog").hidden = true;
}

function randomToken() {
  const bytes = new Uint8Array(32);
  window.crypto.getRandomValues(bytes);
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function initializeSetupWizard() {
  const wizard = state.setupWizard;
  if (wizard.initialized) return;
  const subliminal = state.providerSettings.subliminal || {};
  const opensubtitles = subliminal.authentication?.opensubtitles || {};
  const opensubtitlescom = subliminal.authentication?.opensubtitlescom || {};
  const savedPage = Number(window.localStorage.getItem("subpick-setup-page-v2"));
  wizard.page = Number.isInteger(savedPage) && savedPage >= 0 && savedPage <= 4 ? savedPage : 0;
  wizard.draft = {
    jellyfinUrl: state.jellyfinSettings.server_url || "",
    jellyfinKey: "",
    zimukuEnabled: state.providerSettings.zimuku?.enabled ?? true,
    subliminalEnabled: Boolean(subliminal.enabled),
    opensubtitlesEnabled: !subliminal.enabled || (subliminal.providers || []).includes("opensubtitles"),
    opensubtitlesUsername: opensubtitles.username || "",
    opensubtitlesPassword: "",
    opensubtitlesPasswordConfigured: Boolean(opensubtitles.password_configured),
    opensubtitlescomEnabled: Boolean(subliminal.enabled && (subliminal.providers || []).includes("opensubtitlescom")),
    opensubtitlescomUsername: opensubtitlescom.username || "",
    opensubtitlescomPassword: "",
    opensubtitlescomApiKey: "",
    opensubtitlescomPasswordConfigured: Boolean(opensubtitlescom.password_configured),
    opensubtitlescomApiKeyConfigured: Boolean(opensubtitlescom.apikey_configured),
    assrtEnabled: Boolean(state.providerSettings.assrt?.enabled),
    assrtToken: "",
    assrtTokenConfigured: Boolean(state.providerSettings.assrt?.token_configured),
    subdlEnabled: Boolean(state.providerSettings.subdl?.enabled),
    subdlApiKey: "",
    subdlApiKeyConfigured: Boolean(state.providerSettings.subdl?.api_key_configured),
    moviepilotToken: state.serverSettings.token || randomToken(),
  };
  wizard.initialized = true;
}

function setupSecretHint(configured, required = false) {
  return configured
    ? '<span class="setup-secret-note">已配置，留空保留现有值</span>'
    : `<span class="setup-secret-note">${required ? "必填" : "可选"}</span>`;
}

function setupWizardPageHtml(page, draft) {
  if (page === 0) return `
    <h3>让每一部影片，都有合适的字幕</h3>
    <p>向导只收集运行所需的信息。请求超时、频率限制等高级参数会使用稳妥的默认值。</p>
    <div class="setup-intro">
      <article><strong>读取媒体库</strong><small>连接 Jellyfin，自动识别现有外挂与内嵌字幕。</small></article>
      <article><strong>选择字幕来源</strong><small>Zimuku 默认启用，其他来源由你决定。</small></article>
      <article><strong>接收 MoviePilot</strong><small>生成通信 Token，等待首次成功回调后确认连接。</small></article>
    </div>`;
  if (page === 1) return `
    <form class="setup-form" onsubmit="return false">
      <label>Jellyfin 地址<input id="setup-jellyfin-url" type="url" value="${escapeHtml(draft.jellyfinUrl)}" placeholder="http://jellyfin:8096"></label>
      <label>API Key<input id="setup-jellyfin-key" type="password" autocomplete="new-password" placeholder="${state.jellyfinSettings.api_key_configured ? "已配置，留空保留现有值" : "请输入 Jellyfin API Key"}"></label>
      <p>连接成功后，首次配置会自动扫描一次全部媒体库。无需填写 User ID。</p>
    </form>`;
  if (page === 2) return setupProvidersPageHtml(draft);
  if (page === 3) return `
    <form class="setup-form" onsubmit="return false">
      <label>MoviePilot API Token
        <span class="field-with-action"><input id="setup-moviepilot-token" type="text" value="${escapeHtml(draft.moviepilotToken)}" autocomplete="off" spellcheck="false"><button id="setup-token-generate" type="button">重新生成</button></span>
      </label>
      <p>将这个 Token 填入 MoviePilot 的 ChineseSubFinder 插件。服务地址使用 <code>${escapeHtml(window.location.origin)}</code>。首次成功回调后，运行概览会显示“已连接”。</p>
    </form>`;
  const enabledProviders = [
    draft.zimukuEnabled && "Zimuku",
    draft.subliminalEnabled && "Subliminal",
    draft.assrtEnabled && "ASSRT",
    draft.subdlEnabled && "SubDL",
  ].filter(Boolean).join("、");
  return `
    <h3>基础配置已经保存</h3>
    <p>未完成的连接验证仍会显示在运行概览中，之后可以随时从设置页继续。</p>
    <div class="setup-summary">
      <div><strong>Jellyfin</strong><small>已连接并完成首次扫描</small></div>
      <div><strong>字幕来源</strong><small>${escapeHtml(enabledProviders || "未启用")}</small></div>
      <div><strong>MoviePilot</strong><small>${state.diagnostics?.moviepilot?.connected ? "已连接" : "等待首次成功回调"}</small></div>
    </div>`;
}

function setupProvidersPageHtml(draft) {
  return `
    <div class="setup-provider-list">
      <section class="setup-provider">
        <label><input id="setup-zimuku-enabled" type="checkbox" ${checked(draft.zimukuEnabled)}>Zimuku（推荐）</label>
        <small>无需账号，使用 Compose 内置的验证码识别服务。</small>
      </section>
      <section class="setup-provider">
        <label><input id="setup-subliminal-enabled" type="checkbox" ${checked(draft.subliminalEnabled)}>Subliminal</label>
        <small>通用字幕来源。OpenSubtitles 可匿名使用，但额度较低。</small>
        ${draft.subliminalEnabled ? `<div class="setup-provider-fields">
          <div class="option-row">
            <label><input id="setup-opensubtitles-enabled" type="checkbox" ${checked(draft.opensubtitlesEnabled)}>OpenSubtitles</label>
            <label><input id="setup-opensubtitlescom-enabled" type="checkbox" ${checked(draft.opensubtitlescomEnabled)}>OpenSubtitles.com</label>
          </div>
          ${draft.opensubtitlesEnabled ? `
            <label>OpenSubtitles 用户名（建议填写）<input id="setup-opensubtitles-username" value="${escapeHtml(draft.opensubtitlesUsername)}" autocomplete="off"></label>
            <label>OpenSubtitles 密码<input id="setup-opensubtitles-password" type="password" value="${escapeHtml(draft.opensubtitlesPassword)}" autocomplete="new-password">${setupSecretHint(draft.opensubtitlesPasswordConfigured)}</label>` : ""}
          ${draft.opensubtitlescomEnabled ? `
            <p><a href="https://www.opensubtitles.com/en/users/sign_up" target="_blank" rel="noreferrer">注册 OpenSubtitles.com</a>，并在 <a href="https://www.opensubtitles.com/en/consumers" target="_blank" rel="noreferrer">API Consumers</a> 获取 API Key。该来源必须完成认证。</p>
            <label>OpenSubtitles.com 用户名<input id="setup-opensubtitlescom-username" value="${escapeHtml(draft.opensubtitlescomUsername)}" autocomplete="off"></label>
            <label>OpenSubtitles.com 密码<input id="setup-opensubtitlescom-password" type="password" value="${escapeHtml(draft.opensubtitlescomPassword)}" autocomplete="new-password">${setupSecretHint(draft.opensubtitlescomPasswordConfigured, true)}</label>
            <label>OpenSubtitles.com API Key<input id="setup-opensubtitlescom-apikey" type="password" value="${escapeHtml(draft.opensubtitlescomApiKey)}" autocomplete="new-password">${setupSecretHint(draft.opensubtitlescomApiKeyConfigured, true)}</label>` : ""}
        </div>` : ""}
      </section>
      <section class="setup-provider">
        <label><input id="setup-assrt-enabled" type="checkbox" ${checked(draft.assrtEnabled)}>ASSRT</label>
        <small>中文资源丰富。<a href="https://assrt.net/user/register.xml" target="_blank" rel="noreferrer">注册</a>后在<a href="https://secure.assrt.net/usercp.php" target="_blank" rel="noreferrer">用户面板</a>获取 API Key。</small>
        ${draft.assrtEnabled ? `<div class="setup-provider-fields"><label>API Key<input id="setup-assrt-token" type="password" value="${escapeHtml(draft.assrtToken)}" autocomplete="new-password">${setupSecretHint(draft.assrtTokenConfigured, true)}</label></div>` : ""}
      </section>
      <section class="setup-provider">
        <label><input id="setup-subdl-enabled" type="checkbox" ${checked(draft.subdlEnabled)}>SubDL</label>
        <small>支持 IMDb/TMDb 检索。<a href="https://subdl.com/register" target="_blank" rel="noreferrer">注册</a>后在<a href="https://subdl.com/panel/api" target="_blank" rel="noreferrer">API 面板</a>获取 API Key。</small>
        ${draft.subdlEnabled ? `<div class="setup-provider-fields"><label>API Key<input id="setup-subdl-key" type="password" value="${escapeHtml(draft.subdlApiKey)}" autocomplete="new-password">${setupSecretHint(draft.subdlApiKeyConfigured, true)}</label></div>` : ""}
      </section>
    </div>`;
}

function collectSetupWizardPage() {
  const { page, draft } = state.setupWizard;
  if (page === 1) {
    draft.jellyfinUrl = $("#setup-jellyfin-url")?.value.trim() || "";
    draft.jellyfinKey = $("#setup-jellyfin-key")?.value.trim() || draft.jellyfinKey;
  } else if (page === 2) {
    draft.zimukuEnabled = $("#setup-zimuku-enabled")?.checked === true;
    draft.subliminalEnabled = $("#setup-subliminal-enabled")?.checked === true;
    draft.opensubtitlesEnabled = $("#setup-opensubtitles-enabled")?.checked ?? draft.opensubtitlesEnabled;
    draft.opensubtitlescomEnabled = $("#setup-opensubtitlescom-enabled")?.checked ?? draft.opensubtitlescomEnabled;
    draft.opensubtitlesUsername = $("#setup-opensubtitles-username")?.value.trim() ?? draft.opensubtitlesUsername;
    draft.opensubtitlesPassword = $("#setup-opensubtitles-password")?.value || draft.opensubtitlesPassword;
    draft.opensubtitlescomUsername = $("#setup-opensubtitlescom-username")?.value.trim() ?? draft.opensubtitlescomUsername;
    draft.opensubtitlescomPassword = $("#setup-opensubtitlescom-password")?.value || draft.opensubtitlescomPassword;
    draft.opensubtitlescomApiKey = $("#setup-opensubtitlescom-apikey")?.value || draft.opensubtitlescomApiKey;
    draft.assrtEnabled = $("#setup-assrt-enabled")?.checked === true;
    draft.assrtToken = $("#setup-assrt-token")?.value || draft.assrtToken;
    draft.subdlEnabled = $("#setup-subdl-enabled")?.checked === true;
    draft.subdlApiKey = $("#setup-subdl-key")?.value || draft.subdlApiKey;
  } else if (page === 3) {
    draft.moviepilotToken = $("#setup-moviepilot-token")?.value.trim() || draft.moviepilotToken;
  }
}

function setSetupStatus(message = "", type = "") {
  const node = $("#setup-dialog-status");
  node.className = `setup-dialog-status ${type}`.trim();
  node.textContent = message;
}

async function scanAllJellyfinLibraries() {
  const { payload } = await api("/api/v1/jellyfin/libraries");
  const libraries = payload.libraries || [];
  for (let index = 0; index < libraries.length; index += 1) {
    setSetupStatus(`正在扫描媒体库 ${index + 1}/${libraries.length}：${libraries[index].name}`);
    await api(`/api/v1/jellyfin/libraries/${encodeURIComponent(libraries[index].id)}/scan`, { method: "POST" });
  }
}

async function saveSetupJellyfin() {
  const draft = state.setupWizard.draft;
  if (!draft.jellyfinUrl) throw new Error("请填写 Jellyfin 地址");
  if (!draft.jellyfinKey && !state.jellyfinSettings.api_key_configured) throw new Error("请填写 Jellyfin API Key");
  const wasConfigured = Boolean(state.jellyfinSettings.configured);
  const body = { server_url: draft.jellyfinUrl };
  if (draft.jellyfinKey) body.api_key = draft.jellyfinKey;
  setSetupStatus("正在保存并测试 Jellyfin 连接…");
  const { payload: settings } = await api("/api/v1/jellyfin/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  await api("/api/v1/jellyfin/check", { method: "POST" });
  state.jellyfinSettings = settings;
  draft.jellyfinKey = "";
  if (!wasConfigured) await scanAllJellyfinLibraries();
}

async function saveSetupProviders() {
  const draft = state.setupWizard.draft;
  if (![draft.zimukuEnabled, draft.subliminalEnabled, draft.assrtEnabled, draft.subdlEnabled].some(Boolean)) throw new Error("请至少启用一个字幕来源");
  if (draft.subliminalEnabled && !draft.opensubtitlesEnabled && !draft.opensubtitlescomEnabled) throw new Error("启用 Subliminal 时至少选择一个字幕源");
  if (draft.opensubtitlescomEnabled && draft.subliminalEnabled) {
    const complete = draft.opensubtitlescomUsername
      && (draft.opensubtitlescomPassword || draft.opensubtitlescomPasswordConfigured)
      && (draft.opensubtitlescomApiKey || draft.opensubtitlescomApiKeyConfigured);
    if (!complete) throw new Error("OpenSubtitles.com 必须填写用户名、密码和 API Key");
  }
  if (draft.assrtEnabled && !draft.assrtToken && !draft.assrtTokenConfigured) throw new Error("启用 ASSRT 前请填写 API Key");
  if (draft.subdlEnabled && !draft.subdlApiKey && !draft.subdlApiKeyConfigured) throw new Error("启用 SubDL 前请填写 API Key");

  const currentZimuku = state.providerSettings.zimuku || {};
  setSetupStatus("正在保存并检查 Zimuku…");
  const { payload: zimuku } = await api("/api/v1/providers/zimuku/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      enabled: draft.zimukuEnabled,
      moviepilot_ocr_url: currentZimuku.moviepilot_ocr_url || "http://moviepilot-ocr:9899",
      captcha_debug_capture: Boolean(currentZimuku.captcha_debug_capture),
      base_url: currentZimuku.base_url || "https://srtku.com",
      timeout_seconds: currentZimuku.timeout_seconds || 30,
      request_delay_seconds: currentZimuku.request_delay_seconds ?? 1,
    }),
  });
  state.providerSettings.zimuku = zimuku;
  if (draft.zimukuEnabled) await api("/api/v1/providers/zimuku/ocr-check", { method: "POST" });

  const authentication = {
    opensubtitles: { username: draft.opensubtitlesUsername },
    opensubtitlescom: { username: draft.opensubtitlescomUsername },
  };
  if (draft.opensubtitlesPassword) authentication.opensubtitles.password = draft.opensubtitlesPassword;
  if (draft.opensubtitlescomPassword) authentication.opensubtitlescom.password = draft.opensubtitlescomPassword;
  if (draft.opensubtitlescomApiKey) authentication.opensubtitlescom.apikey = draft.opensubtitlescomApiKey;
  setSetupStatus("正在保存 Subliminal…");
  const { payload: subliminal } = await api("/api/v1/providers/subliminal/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      enabled: draft.subliminalEnabled,
      providers: [draft.opensubtitlesEnabled && "opensubtitles", draft.opensubtitlescomEnabled && "opensubtitlescom"].filter(Boolean),
      languages: ["zh-cn", "zh-hant"],
      authentication,
    }),
  });
  state.providerSettings.subliminal = subliminal;

  setSetupStatus(draft.assrtEnabled ? "正在验证 ASSRT API Key…" : "正在保存 ASSRT…");
  const assrtBody = { enabled: draft.assrtEnabled, timeout_seconds: 15, requests_per_minute: 5 };
  if (draft.assrtToken) assrtBody.token = draft.assrtToken;
  const { payload: assrt } = await api("/api/v1/providers/assrt/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(assrtBody),
  });
  state.providerSettings.assrt = assrt;
  draft.assrtToken = "";
  draft.assrtTokenConfigured = assrt.token_configured;

  setSetupStatus(draft.subdlEnabled ? "正在验证 SubDL API Key…" : "正在保存 SubDL…");
  const subdlBody = { enabled: draft.subdlEnabled, timeout_seconds: 15, requests_per_minute: 20, use_api_key_for_downloads: false };
  if (draft.subdlApiKey) subdlBody.api_key = draft.subdlApiKey;
  const { payload: subdl } = await api("/api/v1/providers/subdl/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(subdlBody),
  });
  state.providerSettings.subdl = subdl;
  draft.subdlApiKey = "";
  draft.subdlApiKeyConfigured = subdl.api_key_configured;
}

async function saveSetupMoviePilot() {
  const token = state.setupWizard.draft.moviepilotToken.trim();
  if (!token) throw new Error("请生成 MoviePilot API Token");
  setSetupStatus("正在保存 MoviePilot 通信 Token…");
  const { payload } = await api("/api/v1/server/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
  });
  state.serverSettings = payload;
}

async function continueSetupWizard() {
  const wizard = state.setupWizard;
  if (wizard.busy) return;
  collectSetupWizardPage();
  if (wizard.page === 4) {
    window.localStorage.setItem("subpick-setup-dismissed-v2", "1");
    window.localStorage.removeItem("subpick-setup-page-v2");
    closeSetupDialog();
    return;
  }
  wizard.busy = true;
  setSetupStatus("");
  renderSetupDialog();
  try {
    if (wizard.page === 1) await saveSetupJellyfin();
    if (wizard.page === 2) await saveSetupProviders();
    if (wizard.page === 3) await saveSetupMoviePilot();
    wizard.page += 1;
    window.localStorage.setItem("subpick-setup-page-v2", String(wizard.page));
    await loadDiagnostics();
    setSetupStatus("");
  } catch (error) {
    setSetupStatus(error.message, "error");
  } finally {
    wizard.busy = false;
    renderSetupDialog();
  }
}

function backSetupWizard() {
  if (state.setupWizard.busy || state.setupWizard.page <= 0) return;
  collectSetupWizardPage();
  state.setupWizard.page -= 1;
  window.localStorage.setItem("subpick-setup-page-v2", String(state.setupWizard.page));
  setSetupStatus("");
  renderSetupDialog();
}

function goToSetupTarget(view, section = "") {
  const targetView = view === "diagnostics" ? "settings" : (view || "settings");
  const targetSection = view === "diagnostics" && !section ? "health" : section;
  switchView(targetView);
  if (!targetSection) return;
  window.setTimeout(() => {
    const target = $(`[data-settings-section="${CSS.escape(targetSection)}"]`);
    if (target instanceof HTMLDetailsElement) target.open = true;
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
    target?.classList.add("attention");
    window.setTimeout(() => target?.classList.remove("attention"), 1600);
  }, 160);
}

async function loadJobs({ overview = false } = {}) {
  const params = new URLSearchParams({
    limit: String(overview ? 6 : state.taskPageSize),
    offset: String(overview ? 0 : (state.taskPage - 1) * state.taskPageSize),
    search: overview ? "" : state.taskSearch,
    status: overview ? "all" : state.taskStatus,
  });
  const response = await fetch(`/api/v1/jobs?${params}`);
  if (!response.ok) throw new Error(`任务请求失败 ${response.status}`);
  const jobs = await response.json();
  const total = Number(response.headers.get("X-Total-Count")) || jobs.length;
  if (overview) {
    renderRecentJobs(jobs);
  } else {
    state.jobs = jobs;
    state.jobTotal = total;
    renderTasks();
  }
}

function flattenJobs(jobs) {
  return jobs.flatMap((job) => (job.video_tasks || []).map((task) => ({ ...task, jobStatus: job.status })));
}

function renderRecentJobs(jobs) {
  const tasks = flattenJobs(jobs).slice(0, 6);
  $("#recent-jobs").innerHTML = tasks.map((task) => `
    <tr data-open-task="${task.id}">
      <td>#${task.id}</td>
      <td class="path" title="${escapeHtml(task.video_path_original)}">${escapeHtml(filename(task.video_path_original))}</td>
      <td>${badge(task.status)}</td>
      <td class="secondary">${escapeHtml(formatTime(task.updated_at))}</td>
    </tr>
  `).join("") || '<tr><td colspan="4" class="empty">暂无任务</td></tr>';
  $$("[data-open-task]", $("#recent-jobs")).forEach((row) => row.addEventListener("click", async () => {
    switchView("tasks");
    await openTask(Number(row.dataset.openTask));
  }));
}

function renderTasks() {
  const tasks = flattenJobs(state.jobs);
  $("#task-rows").innerHTML = tasks.map((task) => `
    <tr data-task-id="${task.id}">
      <td><input class="task-check" type="checkbox" data-task-id="${task.id}" ${state.selectedTasks.has(task.id) ? "checked" : ""} aria-label="选择任务 #${task.id}"></td>
      <td>#${task.id}</td>
      <td>${badge(task.status)}</td>
      <td class="path" title="${escapeHtml(task.video_path_original)}">${escapeHtml(task.video_path_original)}</td>
      <td class="secondary">${escapeHtml(formatTime(task.updated_at))}</td>
    </tr>
  `).join("") || '<tr><td colspan="5" class="empty">没有匹配的任务</td></tr>';
  $$("#task-rows tr[data-task-id]").forEach((row, index) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("input")) return;
      openTask(Number(row.dataset.taskId));
    });
    $(".task-check", row)?.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleTaskSelection(Number(row.dataset.taskId), index, event.shiftKey);
    });
  });
  const totalPages = Math.max(1, Math.ceil(state.jobTotal / state.taskPageSize));
  $("#task-page-summary").textContent = `共 ${state.jobTotal} 条，第 ${state.taskPage}/${totalPages} 页`;
  $("#task-prev").disabled = state.taskPage <= 1;
  $("#task-next").disabled = state.taskPage >= totalPages;
  updateTaskButtons();
}

function toggleTaskSelection(taskId, index, shiftKey) {
  const tasks = flattenJobs(state.jobs);
  const target = tasks[index];
  const checked = $(`.task-check[data-task-id="${taskId}"]`).checked;
  if (shiftKey && state.taskAnchor !== null) {
    const [start, end] = [state.taskAnchor, index].sort((a, b) => a - b);
    tasks.slice(start, end + 1).forEach((task) => checked ? state.selectedTasks.add(task.id) : state.selectedTasks.delete(task.id));
  } else {
    checked ? state.selectedTasks.add(target.id) : state.selectedTasks.delete(target.id);
  }
  state.taskAnchor = index;
  renderTasks();
}

function updateTaskButtons() {
  const count = state.selectedTasks.size;
  $("#task-batch-retry").disabled = count === 0;
  $("#task-batch-delete").disabled = count === 0;
  const tasks = flattenJobs(state.jobs);
  const all = tasks.length > 0 && tasks.every((task) => state.selectedTasks.has(task.id));
  $("#task-select-all").checked = all;
  $("#task-select-all").indeterminate = !all && tasks.some((task) => state.selectedTasks.has(task.id));
}

async function openTask(taskId) {
  try {
    const { payload } = await api(`/api/v1/tasks/${taskId}`);
    openDrawer("任务详情", `#${taskId}`, renderTaskDetail(payload));
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderTaskDetail(task) {
  const candidates = (task.candidates || []).map((candidate) => {
    const title = escapeHtml(candidate.title || "未命名候选");
    const sourceUrl = safeExternalUrl(candidate.source_url);
    const link = sourceUrl
      ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer">${title}</a>`
      : title;
    return `<div class="candidate-row"><strong>${link}</strong><p class="secondary">${escapeHtml(candidate.provider)} · ${escapeHtml(candidate.language)} · 分数 ${escapeHtml(candidate.score ?? "—")}</p>${candidate.last_error_message ? `<p>${escapeHtml(candidate.last_error_message)}</p>` : ""}</div>`;
  }).join("") || '<p class="empty">暂无候选字幕</p>';
  const events = (task.events || []).slice().reverse().map((event) => `
    <div class="event-row"><time>${escapeHtml(formatTime(event.created_at))}</time><strong>${escapeHtml(statusLabel(event.stage))} · ${escapeHtml(statusLabel(event.status))}</strong><p>${escapeHtml(event.message || event.error_code || "无详细信息")}</p></div>
  `).join("") || '<p class="empty">暂无处理日志</p>';
  const artifacts = (task.artifacts || []).map((artifact) => `
    <div class="candidate-row">
      <strong>${escapeHtml(artifact.kind || "字幕文件")}${artifact.is_synced ? " · 已对轴" : ""}</strong>
      <p class="secondary">${escapeHtml(artifact.path || "路径未知")}</p>
    </div>
  `).join("") || '<p class="empty">暂无字幕产物</p>';
  return `
    <div class="actions">
      <button type="button" data-retry-task="${task.id}">重试</button>
      <button class="danger" type="button" data-delete-task="${task.id}">删除</button>
    </div>
    <div class="detail-grid">
      <div><span>状态</span><strong>${escapeHtml(statusLabel(task.status))}</strong></div>
      <div><span>字幕结果</span><strong>${escapeHtml(task.result_subtitle_path || "暂无")}</strong></div>
      <div><span>错误</span><strong>${escapeHtml(task.error_message || "无")}</strong></div>
      <div><span>更新时间</span><strong>${escapeHtml(formatTime(task.updated_at))}</strong></div>
      <div><span>原始路径</span><strong>${escapeHtml(task.video_path_original)}</strong></div>
      <div><span>解析路径</span><strong>${escapeHtml(task.video_path_resolved || "尚未解析")}</strong></div>
    </div>
    <section class="drawer-section"><h3>候选字幕</h3><div class="candidate-list">${candidates}</div></section>
    <section class="drawer-section"><h3>处理日志</h3><div class="event-list">${events}</div></section>
    <section class="drawer-section"><h3>字幕产物</h3><div class="candidate-list">${artifacts}</div></section>
  `;
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

async function retryTasks(taskIds) {
  if (!taskIds.length) return;
  try {
    await api("/api/v1/tasks/batch-retry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_ids: taskIds }),
    });
    state.selectedTasks.clear();
    closeDrawer();
    await loadJobs();
    showToast(`已提交 ${taskIds.length} 个重试任务`);
  } catch (error) { showToast(error.message, true); }
}

function askDelete(message) {
  $("#confirm-message").textContent = message;
  $("#confirm-dialog").hidden = false;
  return new Promise((resolve) => {
    const finish = (value) => {
      $("#confirm-dialog").hidden = true;
      $("#confirm-cancel").onclick = null;
      $("#confirm-tasks").onclick = null;
      $("#confirm-all").onclick = null;
      resolve(value);
    };
    $("#confirm-cancel").onclick = () => finish(null);
    $("#confirm-tasks").onclick = () => finish(false);
    $("#confirm-all").onclick = () => finish(true);
  });
}

async function deleteTasks(taskIds) {
  if (!taskIds.length) return;
  const deleteSubtitles = await askDelete(`确定删除选中的 ${taskIds.length} 个任务吗？\n只有已完成任务会删除由拾幕落库的字幕。`);
  if (deleteSubtitles === null) return;
  try {
    await api("/api/v1/tasks/batch-delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_ids: taskIds, delete_subtitles: deleteSubtitles }),
    });
    state.selectedTasks.clear();
    closeDrawer();
    await loadJobs();
    showToast(`已删除 ${taskIds.length} 个任务`);
  } catch (error) { showToast(error.message, true); }
}

async function deleteAllTasks() {
  const deleteSubtitles = await askDelete("确定删除全部任务及其处理日志吗？\n只有已完成任务会删除由拾幕落库的字幕。");
  if (deleteSubtitles === null) return;
  try {
    await api(`/api/v1/tasks?delete_subtitles=${deleteSubtitles}`, { method: "DELETE" });
    state.selectedTasks.clear();
    state.taskPage = 1;
    await loadJobs();
    showToast("已删除全部任务及日志");
  } catch (error) { showToast(error.message, true); }
}

async function loadLibraries() {
  const { payload } = await api("/api/v1/jellyfin/libraries");
  state.libraries = payload.libraries || [];
  if (!state.activeLibraryId || !state.libraries.some((item) => item.id === state.activeLibraryId)) {
    state.activeLibraryId = state.libraries[0]?.id || null;
  }
  renderLibraryTabs();
  if (!state.activeLibraryId) {
    state.media = [];
    renderMedia();
    return;
  }
  await loadLibraryTree(state.activeLibraryId);
}

async function loadRecentMedia() {
  const { payload } = await api("/api/v1/jellyfin/recent?limit=8");
  state.recentMedia = payload.items || [];
  renderOverviewMedia(state.recentMedia);
}

async function loadLibraryTree(libraryId) {
  const { payload } = await api(`/api/v1/jellyfin/libraries/${encodeURIComponent(libraryId)}/tree`);
  state.media = [...(payload.movies || []), ...(payload.series || [])].map((item) => ({
    ...item,
    kind: (item.seasons || []).length ? "series" : "movie",
  }));
  renderMedia();
  return state.media;
}

function renderLibraryTabs() {
  $("#library-tabs").innerHTML = state.libraries.map((library) => `
    <button class="${library.id === state.activeLibraryId ? "active" : ""}" data-library-id="${escapeHtml(library.id)}" type="button">${escapeHtml(library.name)}</button>
  `).join("") || '<span class="empty">尚未配置或扫描 Jellyfin 媒体库</span>';
  $$("#library-tabs [data-library-id]").forEach((button) => button.addEventListener("click", async () => {
    state.activeLibraryId = button.dataset.libraryId;
    state.selectedMedia.clear();
    renderLibraryTabs();
    try { await loadLibraryTree(state.activeLibraryId); } catch (error) { showToast(error.message, true); }
  }));
  $("#library-scan").disabled = !state.activeLibraryId;
}

function mediaMatches(item) {
  const query = state.mediaSearch.trim().toLowerCase();
  const matchesSearch = !query || `${item.name || ""} ${item.year || ""}`.toLowerCase().includes(query);
  if (!matchesSearch) return false;
  if (state.mediaStatus === "all") return true;
  return String(item.status) === state.mediaStatus;
}

function compareMedia(left, right) {
  const sortKey = state.mediaSort;
  let leftValue;
  let rightValue;
  if (sortKey === "date_created") {
    leftValue = Date.parse(left.date_created || "");
    rightValue = Date.parse(right.date_created || "");
    leftValue = Number.isNaN(leftValue) ? null : leftValue;
    rightValue = Number.isNaN(rightValue) ? null : rightValue;
  } else if (sortKey === "year") {
    leftValue = Number(left.year) || null;
    rightValue = Number(right.year) || null;
  } else {
    leftValue = String(left.name || "");
    rightValue = String(right.name || "");
  }
  if (leftValue === null && rightValue !== null) return 1;
  if (rightValue === null && leftValue !== null) return -1;
  const comparison = typeof leftValue === "string"
    ? leftValue.localeCompare(rightValue, "zh-CN", { numeric: true, sensitivity: "base" })
    : (leftValue || 0) - (rightValue || 0);
  return state.mediaSortDirection === "desc" ? -comparison : comparison;
}

function mediaStatusBadges(item) {
  if (item.ignored || item.status === "ignored") return badge("ignored");
  if (item.kind === "series" || Array.isArray(item.seasons)) return badge(item.status);
  const parts = [];
  if (item.has_embedded_chinese_subtitle) parts.push(badge("completed", "内嵌"));
  if (item.has_external_chinese_subtitle) parts.push(badge("completed", "已下载"));
  if (!parts.length) parts.push(badge(item.status));
  return parts.join(" ");
}

function poster(item, className = "") {
  const fallback = escapeHtml((item.name || "影").slice(0, 1));
  return item.image_url
    ? `<img class="${className}" src="${escapeHtml(item.image_url)}" alt="" loading="lazy">`
    : `<span class="poster-placeholder ${className}" aria-hidden="true">${fallback}</span>`;
}

function renderMedia() {
  const visible = state.media.filter(mediaMatches).sort(compareMedia);
  $("#media-grid").innerHTML = visible.map((item) => `
    <article class="media-card ${state.highlightedMediaId === item.id ? "highlighted" : ""}" data-media-id="${escapeHtml(item.id)}" tabindex="0">
      <label class="media-check"><input class="media-checkbox" type="checkbox" data-media-id="${escapeHtml(item.id)}" ${state.selectedMedia.has(item.id) ? "checked" : ""} aria-label="选择 ${escapeHtml(item.name)}"></label>
      ${poster(item)}
      <h3>${escapeHtml(item.name)}</h3>
      <div class="media-meta"><span>${escapeHtml(item.year || "年份未知")}${item.kind === "series" ? ` · ${(item.seasons || []).length} 季` : ""}</span><span>${mediaStatusBadges(item)}</span></div>
    </article>
  `).join("") || '<p class="empty">没有匹配的媒体</p>';
  $$("#media-grid .media-card").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest(".media-check")) return;
      const item = state.media.find((entry) => entry.id === card.dataset.mediaId);
      if (!item) return;
      if (item.kind === "series") {
        openMedia(item);
      } else {
        toggleMediaSelection(item.id);
      }
    });
    $(".media-checkbox", card).addEventListener("change", (event) => {
      event.stopPropagation();
      event.target.checked ? state.selectedMedia.add(card.dataset.mediaId) : state.selectedMedia.delete(card.dataset.mediaId);
      updateMediaButtons();
    });
    card.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      const item = state.media.find((entry) => entry.id === card.dataset.mediaId);
      if (item?.kind === "series") openMedia(item);
      else if (item) toggleMediaSelection(item.id);
    });
  });
  updateMediaButtons();
  updatePartialFilter();
}

function renderOverviewMedia(items) {
  const list = items.slice(0, 8);
  $("#overview-media").innerHTML = list.map((item) => `
    <article class="poster-card" data-overview-media="${escapeHtml(item.id)}" data-library-id="${escapeHtml(item.library_id)}" tabindex="0">
      ${poster(item)}
      <h3>${escapeHtml(item.name)}</h3>
      <p>${escapeHtml(item.year || "年份未知")} · ${escapeHtml(item.library_name || "媒体库")} · ${escapeHtml(statusLabel(item.status))}</p>
    </article>
  `).join("") || '<p class="empty">媒体库尚无缓存数据，请先在媒体库页面扫描</p>';
  $$("#overview-media [data-overview-media]").forEach((card) => {
    const activate = () => navigateToRecentMedia(card.dataset.libraryId, card.dataset.overviewMedia);
    card.addEventListener("click", activate);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activate();
      }
    });
  });
}

function toggleMediaSelection(itemId) {
  state.selectedMedia.has(itemId) ? state.selectedMedia.delete(itemId) : state.selectedMedia.add(itemId);
  renderMedia();
}

async function navigateToRecentMedia(libraryId, itemId) {
  switchView("library", { refresh: false });
  try {
    if (!state.libraries.length) {
      const { payload } = await api("/api/v1/jellyfin/libraries");
      state.libraries = payload.libraries || [];
    }
    state.activeLibraryId = libraryId;
    state.selectedMedia.clear();
    renderLibraryTabs();
    await loadLibraryTree(libraryId);
    const item = state.media.find((entry) => entry.id === itemId);
    if (!item) return;
    if (item.kind === "series") {
      openMedia(item);
      return;
    }
    state.highlightedMediaId = item.id;
    renderMedia();
    window.setTimeout(() => {
      state.highlightedMediaId = null;
      renderMedia();
    }, 1800);
  } catch (error) {
    showToast(error.message, true);
  }
}

function updateMediaButtons() {
  const taskCount = selectedTaskMediaIds().length;
  const topLevelCount = [...state.selectedMedia].filter((id) => state.media.some((item) => item.id === id)).length;
  $("#library-add").disabled = taskCount === 0;
  $("#library-add").textContent = taskCount ? `添加选中任务 (${taskCount})` : "添加选中任务";
  $("#library-ignore").disabled = topLevelCount === 0;
  $("#library-ignore").textContent = topLevelCount ? `忽略选中 (${topLevelCount})` : "忽略选中";
  $("#library-unignore").disabled = topLevelCount === 0;
  $("#library-unignore").textContent = topLevelCount ? `取消忽略 (${topLevelCount})` : "取消忽略";
}

function updatePartialFilter() {
  const library = state.libraries.find((item) => item.id === state.activeLibraryId);
  const button = $('[data-media-status="partial"]');
  button.disabled = String(library?.collection_type || "").toLowerCase() === "movies";
  if (button.disabled && state.mediaStatus === "partial") {
    state.mediaStatus = "all";
    $$("[data-media-status]").forEach((node) => node.classList.toggle("active", node.dataset.mediaStatus === "all"));
  }
}

function seriesEpisodes(item) {
  return (item.seasons || []).flatMap((season) => season.episodes || []);
}

function missingSeriesEpisodes(item) {
  return seriesEpisodes(item).filter((episode) => String(episode.status) === "missing");
}

function toggleSelectedMediaIds(itemIds, selected) {
  itemIds.forEach((itemId) => selected ? state.selectedMedia.add(itemId) : state.selectedMedia.delete(itemId));
}

function applyAggregateCheckbox(checkbox, itemIds) {
  if (!checkbox) return;
  const selectedCount = itemIds.filter((itemId) => state.selectedMedia.has(itemId)).length;
  checkbox.checked = itemIds.length > 0 && selectedCount === itemIds.length;
  checkbox.indeterminate = selectedCount > 0 && selectedCount < itemIds.length;
}

function syncSeriesDrawerSelection(item) {
  const allEpisodeIds = seriesEpisodes(item).map((episode) => episode.id);
  const seriesCheckbox = $(".drawer-series-check", $("#drawer-content"));
  applyAggregateCheckbox(seriesCheckbox, allEpisodeIds);
  if (seriesCheckbox && state.selectedMedia.has(item.id)) {
    seriesCheckbox.checked = true;
    seriesCheckbox.indeterminate = false;
  }
  $$(".drawer-season-check", $("#drawer-content")).forEach((checkbox) => {
    const season = item.seasons?.[Number(checkbox.dataset.seasonIndex)];
    applyAggregateCheckbox(checkbox, (season?.episodes || []).map((episode) => episode.id));
  });
  $$(".drawer-episode-check", $("#drawer-content")).forEach((checkbox) => {
    checkbox.checked = state.selectedMedia.has(checkbox.dataset.episodeId) || state.selectedMedia.has(item.id);
  });
  const addButton = $("#drawer-add-media");
  const count = selectedTaskMediaIds().length;
  if (addButton) {
    addButton.disabled = count === 0;
    addButton.textContent = count ? `添加选中任务 (${count})` : "添加选中任务";
  }
  updateMediaButtons();
}

function renderSeriesDrawer(item) {
  const seasons = (item.seasons || []).map((season, seasonIndex) => {
    const episodes = state.drawerMissingOnly
      ? (season.episodes || []).filter((episode) => String(episode.status) === "missing")
      : season.episodes || [];
    if (state.drawerMissingOnly && episodes.length === 0) return "";
    const seasonKey = String(season.season ?? seasonIndex);
    return `
      <details class="season-block" data-season-key="${escapeHtml(seasonKey)}"${state.drawerOpenSeasons.has(seasonKey) ? " open" : ""}>
        <summary>
          <span>第 ${escapeHtml(season.season ?? "—")} 季</span>
          ${badge(season.status)}
          <label class="season-select-control">
            <input class="drawer-season-check" type="checkbox" data-season-index="${seasonIndex}" aria-label="选择第 ${escapeHtml(season.season ?? "—")} 季">
            <span>全选本季</span>
          </label>
        </summary>
        <div class="episode-list">${episodes.map((episode) => `
          <label class="episode-row">
            <input class="drawer-episode-check" type="checkbox" data-episode-id="${escapeHtml(episode.id)}">
            <span>S${String(episode.season ?? 0).padStart(2, "0")}E${String(episode.episode ?? 0).padStart(2, "0")}</span>
            <strong title="${escapeHtml(episode.name)}">${escapeHtml(episode.name)}</strong>
            <span>${mediaStatusBadges(episode)}</span>
          </label>
        `).join("")}</div>
      </details>
    `;
  }).join("");
  openDrawer("剧集详情", item.name, `
    <div class="drawer-media-head">
      ${poster(item)}
      <div class="drawer-media-copy">
        <p class="secondary">${escapeHtml(item.year || "年份未知")}</p>
        <p>${mediaStatusBadges(item)}</p>
        <label class="drawer-select-control">
          <input class="drawer-series-check" type="checkbox" aria-label="选择整剧 ${escapeHtml(item.name)}">
          <span>选择整剧</span>
        </label>
      </div>
    </div>
    <div class="drawer-actions-bar">
      <button id="drawer-missing-filter" class="${state.drawerMissingOnly ? "primary" : ""}" type="button">仅看缺失</button>
      <button id="drawer-select-missing" type="button">全选缺失</button>
      <button id="drawer-add-media" class="primary" type="button">添加选中任务</button>
    </div>
    <section class="drawer-section"><h3>季与集</h3><div class="season-list">${seasons || '<p class="empty">暂无匹配剧集</p>'}</div></section>
  `, { seriesId: item.id });

  $(".drawer-series-check", $("#drawer-content"))?.addEventListener("change", (event) => {
    const episodeIds = seriesEpisodes(item).map((episode) => episode.id);
    toggleSelectedMediaIds([item.id, ...episodeIds], event.target.checked);
    syncSeriesDrawerSelection(item);
  });
  $$(".drawer-season-check", $("#drawer-content")).forEach((checkbox) => {
    checkbox.closest("label")?.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      state.selectedMedia.delete(item.id);
      const season = item.seasons?.[Number(checkbox.dataset.seasonIndex)];
      toggleSelectedMediaIds((season?.episodes || []).map((episode) => episode.id), checkbox.checked);
      syncSeriesDrawerSelection(item);
    });
  });
  $$(".drawer-episode-check", $("#drawer-content")).forEach((checkbox) => checkbox.addEventListener("change", () => {
    state.selectedMedia.delete(item.id);
    checkbox.checked ? state.selectedMedia.add(checkbox.dataset.episodeId) : state.selectedMedia.delete(checkbox.dataset.episodeId);
    syncSeriesDrawerSelection(item);
  }));
  $$(".season-block", $("#drawer-content")).forEach((details) => details.addEventListener("toggle", () => {
    const seasonKey = details.dataset.seasonKey;
    if (!seasonKey) return;
    details.open ? state.drawerOpenSeasons.add(seasonKey) : state.drawerOpenSeasons.delete(seasonKey);
  }));
  $("#drawer-missing-filter")?.addEventListener("click", () => {
    state.drawerMissingOnly = !state.drawerMissingOnly;
    renderSeriesDrawer(item);
  });
  $("#drawer-select-missing")?.addEventListener("click", () => {
    state.selectedMedia.delete(item.id);
    toggleSelectedMediaIds(missingSeriesEpisodes(item).map((episode) => episode.id), true);
    syncSeriesDrawerSelection(item);
  });
  $("#drawer-add-media")?.addEventListener("click", addMediaTasks);
  syncSeriesDrawerSelection(item);
}

function openMedia(item) {
  if (item.kind !== "series") return;
  state.drawerMissingOnly = false;
  state.drawerOpenSeasons.clear();
  renderSeriesDrawer(item);
}

function selectedTaskMediaIds() {
  const ids = [];
  for (const id of state.selectedMedia) {
    const top = state.media.find((item) => item.id === id);
    if (top?.kind === "series") {
      (top.seasons || []).forEach((season) => (season.episodes || []).forEach((episode) => ids.push(episode.id)));
    } else {
      ids.push(id);
    }
  }
  return [...new Set(ids)];
}

async function addMediaTasks() {
  const itemIds = selectedTaskMediaIds();
  if (!itemIds.length) return;
  try {
    const { payload } = await api("/api/v1/jellyfin/tasks", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_ids: itemIds }),
    });
    const ok = (payload.results || []).filter((item) => item.ok).length;
    state.selectedMedia.clear();
    renderMedia();
    closeDrawer();
    showToast(`已添加 ${ok}/${itemIds.length} 个任务`);
  } catch (error) { showToast(error.message, true); }
}

async function batchIgnoreMedia(ignored) {
  const topLevel = [...state.selectedMedia].filter((id) => state.media.some((item) => item.id === id));
  if (!topLevel.length) {
    showToast("请在海报墙选择电影或整个剧集", true);
    return;
  }
  try {
    await api("/api/v1/jellyfin/items/batch-ignore", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_ids: topLevel, ignored }),
    });
    state.selectedMedia.clear();
    await loadLibraryTree(state.activeLibraryId);
    showToast(ignored ? "已忽略选中媒体" : "已取消忽略");
  } catch (error) { showToast(error.message, true); }
}

async function scanLibrary() {
  if (!state.activeLibraryId) return;
  try {
    showToast("正在扫描媒体库");
    const { payload } = await api(`/api/v1/jellyfin/libraries/${encodeURIComponent(state.activeLibraryId)}/scan`, { method: "POST" });
    await loadLibraryTree(state.activeLibraryId);
    showToast(`扫描完成：新增 ${payload.created}，更新 ${payload.updated}，移除 ${payload.removed}`);
  } catch (error) { showToast(error.message, true); }
}

async function loadLogs() {
  const params = new URLSearchParams({ after_id: "0", limit: "500" });
  const level = $("#log-level").value;
  const taskId = $("#log-task").value.trim();
  const category = $("#log-category").value;
  if (level) params.set("level", level);
  if (taskId) params.set("task_id", taskId);
  if (category) params.set("category", category);
  try {
    const { payload } = await api(`/api/v1/logs?${params}`);
    state.logs = payload.entries || [];
    state.logAfterId = Math.max(0, ...state.logs.map((entry) => Number(entry.id) || 0));
    renderLogs();
  } catch (error) { showToast(error.message, true); }
}

function renderLogs() {
  const logs = state.logs.slice().sort((a, b) => String(b.ts).localeCompare(String(a.ts))).slice(0, 500);
  $("#log-rows").innerHTML = logs.map((entry) => `
    <tr>
      <td>${escapeHtml(formatTime(entry.ts))}</td>
      <td class="level-${String(entry.level || "").toLowerCase()}">${escapeHtml(entry.level || "INFO")}</td>
      <td>${escapeHtml(systemLogCategoryLabel(entry.category))}</td>
      <td>${escapeHtml(entry.task_id || "—")}</td>
      <td>${escapeHtml(entry.message || entry.error_code || "—")}</td>
    </tr>
  `).join("") || '<tr><td colspan="5" class="empty">暂无系统日志</td></tr>';
}

function connectLogs() {
  disconnectLogs();
  if (state.logsPaused || state.view !== "logs") return;
  const source = new EventSource(`/api/v1/events?after_log_id=${state.logAfterId}`);
  state.logSource = source;
  source.addEventListener("system_event", (event) => {
    const entry = JSON.parse(event.data);
    state.logAfterId = Math.max(state.logAfterId, Number(entry.id) || 0);
    if (logMatches(entry)) {
      state.logs.push(entry);
      state.logs = state.logs.slice(-500);
      renderLogs();
    }
  });
}

function disconnectLogs() {
  state.logSource?.close();
  state.logSource = null;
}

function logMatches(entry) {
  const level = $("#log-level").value;
  const task = $("#log-task").value.trim();
  const category = $("#log-category").value;
  return (!level || entry.level === level)
    && (!task || String(entry.task_id) === task)
    && (!category || entry.category === category);
}

function renderDiagnostics() {
  const diag = state.diagnostics;
  if (!diag) return;
  const componentRows = Object.entries(diag.components || {}).map(([name, value]) => {
    const update = state.dependencyUpdates?.[name];
    if (!update) return [name, value];
    const latest = update.latest_version || "未知";
    return [name, `${update.current_version} → ${latest}（${update.status}）`];
  });
  const sections = [
    ["运行环境", [
      ["配置文件", diag.config_file.status, diag.config_file.status === "ok"],
      ["数据目录", diag.data_dir.status, diag.data_dir.status === "ok"],
      ["缓存目录", diag.cache_dir.status, diag.cache_dir.status === "ok"],
      ["媒体目录", diag.media_dir.status, diag.media_dir.status === "ok"],
      ["数据库", diag.database.status, diag.database.status === "ok"],
    ]],
    ["外部连接", [
      ["MoviePilot", diag.moviepilot.connected ? "已连接" : diag.moviepilot.token_configured ? "等待验证" : "未配置", diag.moviepilot.connected],
      ["Jellyfin", diag.jellyfin.connected ? "已连接" : diag.jellyfin.configured ? "等待测试" : "未配置", diag.jellyfin.connected],
      ...Object.entries(diag.providers || {}).map(([name, value]) => [
        name,
        ...providerDiagnosticStatus(value),
      ]),
    ]],
    ["本地工具", (diag.tools || []).map((tool) => [
      tool.name,
      tool.available ? "可用" : "不可用",
      tool.available,
    ])],
    ["队列状态", [
      ["当前任务", diag.queue.active_task_id || "无", true],
      ["等待数量", diag.queue.queued_count, true],
      ["下个搜索槽位", `${Math.ceil(diag.queue.next_provider_ready_seconds || 0)} 秒`, true],
      ...Object.entries(diag.queue.provider_cooldowns || {}).map(([name, seconds]) => [
        `${name} 冷却`,
        `${Math.ceil(seconds)} 秒`,
        true,
      ]),
    ]],
  ];
  const localIssues = (diag.checks || []).filter((item) => item.status !== "ok").length;
  const connectionIssues = [
    diag.jellyfin.configured && !diag.jellyfin.connected,
    diag.moviepilot.token_configured && !diag.moviepilot.connected,
  ].filter(Boolean).length;
  const providerIssues = Object.values(diag.providers || {}).filter(
    (item) => item.enabled && item.status !== "ok"
  ).length;
  const issueCount = localIssues + connectionIssues + providerIssues;
  const healthStatus = $("#health-settings-status");
  if (healthStatus) {
    healthStatus.className = `badge ${issueCount ? "active" : "ok"}`;
    healthStatus.textContent = issueCount ? `${issueCount} 项待确认` : "运行正常";
  }
  $("#diagnostic-grid").innerHTML = `
    <div class="health-dashboard-head">
      <div class="health-orbit ${issueCount ? "warning" : "ok"}" aria-hidden="true"><i></i></div>
      <div>
        <span class="health-kicker">SUBPICK ${escapeHtml(diag.version)}</span>
        <h2>${issueCount ? "有项目需要确认" : "系统运行正常"}</h2>
        <p>${issueCount ? `${issueCount} 个项目尚未就绪，运行完整检查可查看详情。` : "核心目录、数据库与本地工具均处于可用状态。"}</p>
      </div>
      <div class="health-dashboard-meta">
        <span><b>${Object.values(diag.providers || {}).filter((item) => item.enabled).length}</b> 个 Provider</span>
        <span><b>${diag.logging.retention_days}</b> 天日志保留</span>
      </div>
    </div>
    <div class="health-section-grid">${sections.map(([title, rows]) => `
      <section class="health-section">
        <h3>${escapeHtml(title)}</h3>
        <div>${rows.map(([name, value, ok]) => `
          <span class="health-status-row">
            <i class="${ok ? "ok" : "warning"}" aria-hidden="true"></i>
            <b>${escapeHtml(name)}</b>
            <small>${escapeHtml(value ?? "未知")}</small>
          </span>
        `).join("")}</div>
      </section>
    `).join("")}</div>
    <details class="health-version-details">
      <summary>组件与兼容性信息</summary>
      <dl class="component-list">
        ${componentRows.map(([name, value]) => `<dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value ?? "未知")}</dd>`).join("")}
        <dt>配置版本</dt><dd>${escapeHtml(diag.compatibility.config_version)}</dd>
        <dt>数据库版本</dt><dd>${escapeHtml(diag.compatibility.database_schema_version)}</dd>
        <dt>兼容性</dt><dd>${escapeHtml(diag.compatibility.status)}</dd>
      </dl>
    </details>
  `;
}

function checked(value) {
  return value ? "checked" : "";
}

function capabilitySummary(adapter) {
  const capabilities = adapter?.capabilities || {};
  const mediaLabels = { movie: "电影", episode: "剧集", season_pack: "整季包" };
  const lookupLabels = {
    imdb: "IMDb",
    tmdb: "TMDb",
    title: "标题",
    original_title: "原始标题",
    filename: "文件名",
  };
  const media = (capabilities.media_scopes || []).map((value) => mediaLabels[value] || value).join(" / ");
  const lookups = (capabilities.lookup_keys || []).map((value) => lookupLabels[value] || value).join(" / ");
  return [media, lookups, capabilities.transport, capabilities.supports_archives ? "支持字幕包" : ""]
    .filter(Boolean)
    .join(" · ");
}

function providerStatus(name, adapter) {
  const settings = state.providerSettings[name] || {};
  const diagnostic = state.diagnostics?.providers?.[name] || {};
  const enabled = typeof settings.enabled === "boolean" ? settings.enabled : Boolean(adapter?.enabled);
  if (!enabled) return { enabled, label: "未启用" };
  if (state.providerChecks[name]?.ok === true) return { enabled, label: "已启用 · 可用" };
  if (state.providerChecks[name]?.ok === false) return { enabled, label: "已启用 · 检查失败" };
  if (settings.status === "unconfigured") return { enabled, label: "已启用 · 未配置" };
  if (["unavailable", "error", "failed"].includes(diagnostic.status)) {
    return { enabled, label: "已启用 · 检查失败" };
  }
  return { enabled, label: diagnostic.status === "ok" ? "已启用 · 可用" : "已启用 · 待验证" };
}

function secretStatus(configured) {
  return configured ? '<span class="secret-status">已配置，留空保留</span>' : "";
}

function providerSettingsHtml(name) {
  const settings = state.providerSettings[name] || {};
  if (name === "subliminal") {
    const authentication = settings.authentication || {};
    const opensubtitles = authentication.opensubtitles || {};
    const opensubtitlescom = authentication.opensubtitlescom || {};
    return `
      <div class="provider-form-grid">
        <label class="toggle-row"><input data-setting="enabled" type="checkbox" ${checked(settings.enabled)}>启用 Subliminal</label>
        <fieldset>
          <legend>支持中文的字幕源</legend>
          <div class="option-row">
            <label><input data-subliminal-source="opensubtitles" type="checkbox" ${checked((settings.providers || []).includes("opensubtitles"))}>OpenSubtitles</label>
            <label><input data-subliminal-source="opensubtitlescom" type="checkbox" ${checked((settings.providers || []).includes("opensubtitlescom"))}>OpenSubtitles.com</label>
          </div>
        </fieldset>
        <fieldset>
          <legend>语言</legend>
          <div class="option-row">
            <label><input data-subliminal-language="zh-cn" type="checkbox" ${checked((settings.languages || []).includes("zh-cn"))}>简体中文</label>
            <label><input data-subliminal-language="zh-hant" type="checkbox" ${checked((settings.languages || []).includes("zh-hant"))}>繁体中文</label>
          </div>
        </fieldset>
        <fieldset class="credential-block">
          <legend>OpenSubtitles</legend>
          <div class="compact-grid">
            <label>用户名<input data-auth="opensubtitles-username" value="${escapeHtml(opensubtitles.username || "")}" autocomplete="off"></label>
            <label>密码<input data-auth="opensubtitles-password" type="password" autocomplete="new-password">${secretStatus(opensubtitles.password_configured)}</label>
          </div>
        </fieldset>
        <fieldset class="credential-block">
          <legend>OpenSubtitles.com</legend>
          <div class="compact-grid">
            <label>用户名<input data-auth="opensubtitlescom-username" value="${escapeHtml(opensubtitlescom.username || "")}" autocomplete="off"></label>
            <label>密码<input data-auth="opensubtitlescom-password" type="password" autocomplete="new-password">${secretStatus(opensubtitlescom.password_configured)}</label>
            <label>API Key<input data-auth="opensubtitlescom-apikey" type="password" autocomplete="new-password">${secretStatus(opensubtitlescom.apikey_configured)}</label>
          </div>
          <p class="provider-help">请先<a href="https://www.opensubtitles.com/en/users/sign_up" target="_blank" rel="noreferrer">注册账户</a>，再前往<a href="https://www.opensubtitles.com/en/consumers" target="_blank" rel="noreferrer">API Consumers</a>获取 API Key。当前 Subliminal 仍需要用户名和密码完成账户认证。</p>
        </fieldset>
      </div>
      <div class="provider-actions"><button class="primary" data-provider-save="${name}" type="button">保存 Subliminal</button></div>
    `;
  }
  if (name === "assrt") {
    return `
      <div class="provider-form-grid">
        <label class="toggle-row"><input data-setting="enabled" type="checkbox" ${checked(settings.enabled)}>启用 ASSRT</label>
        <p class="provider-help">字幕服务由 <a href="https://assrt.net/" target="_blank" rel="noreferrer">assrt.net</a> 提供。请先<a href="https://assrt.net/user/register.xml" target="_blank" rel="noreferrer">注册</a>，再从<a href="https://secure.assrt.net/usercp.php" target="_blank" rel="noreferrer">用户面板</a>获取 API Key。</p>
        <div class="compact-grid">
          <label>API Key<input data-setting="token" type="password" autocomplete="new-password">${secretStatus(settings.token_configured)}</label>
          <label>请求超时（秒）<input data-setting="timeout_seconds" type="number" min="1" max="120" value="${escapeHtml(settings.timeout_seconds || 15)}"></label>
          <label>请求上限（次/分钟）<input data-setting="requests_per_minute" type="number" value="5" readonly></label>
        </div>
      </div>
      <p class="provider-result" data-provider-result="${name}">${escapeHtml(state.providerChecks[name]?.message || "实际配额按 5 次/分钟处理。")}</p>
      <div class="provider-actions"><button data-provider-check="assrt-quota" type="button">检查配额</button><button class="primary" data-provider-save="${name}" type="button">保存 ASSRT</button></div>
    `;
  }
  if (name === "subdl") {
    return `
      <div class="provider-form-grid">
        <label class="toggle-row"><input data-setting="enabled" type="checkbox" ${checked(settings.enabled)}>启用 SubDL</label>
        <p class="provider-help">请先在 <a href="https://subdl.com/register" target="_blank" rel="noreferrer">SubDL 注册</a>，再从<a href="https://subdl.com/panel/api" target="_blank" rel="noreferrer">API 面板</a>获取 API Key。</p>
        <div class="compact-grid">
          <label>API Key<input data-setting="api_key" type="password" autocomplete="new-password">${secretStatus(settings.api_key_configured)}</label>
          <label>请求超时（秒）<input data-setting="timeout_seconds" type="number" min="1" max="120" value="${escapeHtml(settings.timeout_seconds || 15)}"></label>
          <label>请求上限（次/分钟）<input data-setting="requests_per_minute" type="number" min="1" max="60" value="${escapeHtml(settings.requests_per_minute || 20)}"></label>
        </div>
        <label class="toggle-row"><input data-setting="use_api_key_for_downloads" type="checkbox" ${checked(settings.use_api_key_for_downloads)}>Pro：下载时携带 API Key</label>
      </div>
      <p class="provider-result" data-provider-result="${name}">${escapeHtml(state.providerChecks[name]?.message || "免费账户默认使用官方匿名下载。")}</p>
      <div class="provider-actions"><button data-provider-check="subdl-usage" type="button">检查额度</button><button class="primary" data-provider-save="${name}" type="button">保存 SubDL</button></div>
    `;
  }
  if (name === "zimuku") {
    return `
      <div class="provider-form-grid">
        <label class="toggle-row"><input data-setting="enabled" type="checkbox" ${checked(settings.enabled)}>启用 Zimuku</label>
        <p class="provider-help">网页检索需要验证码服务。优先使用兼容 <a href="https://github.com/jxxghp/MoviePilot-OCR" target="_blank" rel="noreferrer">MoviePilot OCR</a> 协议的服务，Anti-Captcha 仅作为付费后备。</p>
        <div class="compact-grid">
          <label>MoviePilot OCR 地址<input data-setting="moviepilot_ocr_url" type="url" value="${escapeHtml(settings.moviepilot_ocr_url || "")}" placeholder="http://nas:19899"></label>
          <label>Anti-Captcha API Key<input data-setting="anti_captcha_api_key" type="password" autocomplete="new-password">${secretStatus(settings.anti_captcha_api_key_configured)}</label>
          <label>站点地址<input data-setting="base_url" type="url" value="${escapeHtml(settings.base_url || "https://srtku.com")}"></label>
          <label>请求超时（秒）<input data-setting="timeout_seconds" type="number" min="5" max="180" value="${escapeHtml(settings.timeout_seconds || 30)}"></label>
          <label>站内请求间隔（秒）<input data-setting="request_delay_seconds" type="number" min="0" max="30" step="0.5" value="${escapeHtml(settings.request_delay_seconds ?? 1)}"></label>
        </div>
        <label class="toggle-row"><input data-setting="captcha_debug_capture" type="checkbox" ${checked(settings.captcha_debug_capture)}>保存验证码失败诊断（最多 100 组）</label>
      </div>
      <p class="provider-result" data-provider-result="${name}">${escapeHtml(state.providerChecks[name]?.message || "OCR 服务尚未检查。")}</p>
      <div class="provider-actions"><button data-provider-check="zimuku-ocr" type="button">实图检查 OCR</button><button data-provider-check="zimuku-balance" type="button">检查付费余额</button><button class="primary" data-provider-save="${name}" type="button">保存 Zimuku</button></div>
    `;
  }
  return '<p class="provider-help">这是外部 Provider 适配器。当前适配器尚未声明可由 WebUI 编辑的配置表单。</p>';
}

function renderProviderOrder() {
  const adapters = new Map(state.providerAdapters.map((item) => [item.name, item]));
  $("#settings-provider-order").innerHTML = state.providerOrder.map((name, index) => {
    const adapter = adapters.get(name);
    const status = providerStatus(name, adapter);
    return `
      <li class="provider-order-item" data-provider-name="${escapeHtml(name)}">
        <details class="provider-config-card" ${state.openProviders.has(name) ? "open" : ""}>
          <summary>
            <span class="drag-handle" data-provider-drag="${escapeHtml(name)}" draggable="true" role="button" tabindex="0" aria-label="拖动 ${escapeHtml(adapter?.display_name || name)} 调整顺序" title="拖动调整顺序">⋮⋮</span>
            <b>${index + 1}</b>
            <span class="provider-identity">
              <strong>${escapeHtml(adapter?.display_name || name)}</strong>
              <small>${escapeHtml(capabilitySummary(adapter) || "未声明能力")}</small>
            </span>
            <span class="provider-summary-meta"><small>v${escapeHtml(adapter?.version || "未知")}</small>${badge(status.enabled ? "completed" : "ignored", status.label)}</span>
          </summary>
          <div class="provider-config-body">${providerSettingsHtml(name)}</div>
        </details>
      </li>
    `;
  }).join("") || '<li class="empty">暂无 Provider</li>';
}

async function checkUpdates() {
  try {
    showToast("正在检查组件更新");
    const { payload } = await api("/api/v1/diagnostics/dependency-updates", { method: "POST" });
    state.dependencyUpdates = payload;
    renderDiagnostics();
    const messages = Object.entries(payload).map(([name, item]) => `${name}: ${item.current_version} → ${item.latest_version || "未知"}（${item.status}）`);
    showToast(messages.join("；"));
  } catch (error) { showToast(error.message, true); }
}

async function loadSettings() {
  try {
    const [
      { payload: jellyfin },
      { payload: github },
      { payload: server },
      { payload: paths },
      { payload: providerOrder },
      { payload: subliminal },
      { payload: assrt },
      { payload: subdl },
      { payload: zimuku },
    ] = await Promise.all([
      api("/api/v1/jellyfin/settings"),
      api("/api/v1/github/settings"),
      api("/api/v1/server/settings"),
      api("/api/v1/paths/settings"),
      api("/api/v1/providers/order"),
      api("/api/v1/providers/subliminal/settings"),
      api("/api/v1/providers/assrt/settings"),
      api("/api/v1/providers/subdl/settings"),
      api("/api/v1/providers/zimuku/settings"),
    ]);
    state.jellyfinSettings = jellyfin;
    state.githubSettings = github;
    state.serverSettings = server;
    state.pathSettings = paths;
    state.providerOrder = [...(providerOrder.order || [])];
    state.providerAdapters = [...(providerOrder.adapters || [])];
    state.providerSettings = { subliminal, assrt, subdl, zimuku };
    $("#jellyfin-url").value = jellyfin.server_url || "";
    $("#jellyfin-key").value = "";
    $("#jellyfin-key").placeholder = jellyfin.api_key_configured ? "已配置，留空保留现有值" : "请输入 API Key";
    $("#jellyfin-key-status").textContent = jellyfin.api_key_configured ? "API Key 已配置" : "尚未配置 API Key";
    $("#github-key").value = "";
    $("#github-key").placeholder = github.api_key_configured ? "已配置，留空保留现有值" : "可选";
    $("#github-key-status").textContent = github.api_key_configured
      ? "GitHub Token 已配置，仅用于查询组件更新。"
      : "未配置时使用 GitHub 匿名 API 配额。";
    $("#server-token").value = server.token || "";
    renderPathSettings();
    renderProviderOrder();
    renderSetupDialog();
  } catch (error) { showToast(error.message, true); }
}

function pathMappingRow(mapping = {}) {
  return `
    <div class="path-mapping-row">
      <input data-path-from type="text" value="${escapeHtml(mapping.from_path || "")}" placeholder="/downloads/media">
      <span aria-hidden="true">→</span>
      <input data-path-to type="text" value="${escapeHtml(mapping.to_path || "")}" placeholder="/media">
      <button data-path-remove class="icon-button" type="button" title="删除这条映射" aria-label="删除这条映射">×</button>
    </div>
  `;
}

function currentPathMappings() {
  return $$(".path-mapping-row", $("#path-mapping-rows")).map((row) => ({
    from_path: $("[data-path-from]", row).value.trim(),
    to_path: $("[data-path-to]", row).value.trim(),
  })).filter((item) => item.from_path || item.to_path);
}

function renderPathSettings() {
  const settings = state.pathSettings || {};
  const mappings = settings.mappings || [];
  const latestPath = settings.latest_moviepilot_path || settings.latest_callback_path || "";
  const needsAttention = Boolean(settings.needs_attention || settings.path_issue);
  $("#path-mapping-rows").innerHTML = (mappings.length ? mappings : [{}])
    .map((mapping) => pathMappingRow(mapping))
    .join("");
  $("#path-latest-sample").textContent = latestPath || "尚未收到 MoviePilot 回调";
  const badgeNode = $("#path-mapping-badge");
  badgeNode.className = `badge ${needsAttention ? "error" : mappings.length ? "ok" : "ignored"}`;
  badgeNode.textContent = needsAttention ? "路径不可访问" : mappings.length ? `${mappings.length} 条规则` : "无需设置";
  const details = $("#path-mapping-settings");
  if (needsAttention) details.open = true;
  $("#path-mapping-result").textContent = needsAttention
    ? "最近一次 MoviePilot 路径在拾幕容器内不可访问，请添加映射并测试。"
    : "";
}

function addPathMapping(mapping = {}) {
  $("#path-mapping-rows").insertAdjacentHTML("beforeend", pathMappingRow(mapping));
}

async function testPathMappings() {
  const output = $("#path-mapping-result");
  output.className = "path-mapping-result";
  output.textContent = "正在检查映射后的文件是否存在…";
  try {
    const { payload } = await api("/api/v1/paths/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: currentPathMappings() }),
    });
    output.className = `path-mapping-result ${payload.exists ? "ok" : "error"}`;
    output.innerHTML = payload.exists
      ? `测试通过：<code>${escapeHtml(payload.original_path)}</code> → <code>${escapeHtml(payload.resolved_path)}</code>`
      : `测试失败：转换为 <code>${escapeHtml(payload.resolved_path || payload.original_path || "未知")}</code> 后仍未找到文件。`;
  } catch (error) {
    output.className = "path-mapping-result error";
    output.textContent = error.message;
  }
}

async function savePathMappings() {
  const output = $("#path-mapping-result");
  try {
    const { payload } = await api("/api/v1/paths/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mappings: currentPathMappings() }),
    });
    state.pathSettings = payload;
    renderPathSettings();
    await loadDiagnostics();
    output.className = "path-mapping-result ok";
    output.textContent = "目录映射已保存，后续 MoviePilot 回调与 Jellyfin 路径都会使用这些规则。";
    showToast("目录映射已保存");
  } catch (error) {
    output.className = "path-mapping-result error";
    output.textContent = error.message;
  }
}

function moveProvider(name, direction) {
  const index = state.providerOrder.indexOf(name);
  const target = direction === "up" ? index - 1 : index + 1;
  if (index < 0 || target < 0 || target >= state.providerOrder.length) return;
  [state.providerOrder[index], state.providerOrder[target]] = [state.providerOrder[target], state.providerOrder[index]];
  renderProviderOrder();
  $("#settings-status").textContent = "顺序已调整，保存后生效。";
}

async function saveProviderOrder() {
  try {
    const { payload } = await api("/api/v1/providers/order", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order: state.providerOrder }),
    });
    state.providerOrder = [...(payload.order || [])];
    state.providerAdapters = [...(payload.adapters || [])];
    renderProviderOrder();
    $("#settings-status").textContent = "Provider 搜索顺序已保存。";
    showToast("Provider 搜索顺序已保存");
  } catch (error) { showToast(error.message, true); }
}

async function saveJellyfin(event) {
  event.preventDefault();
  const body = { server_url: $("#jellyfin-url").value.trim() };
  if ($("#jellyfin-key").value.trim()) body.api_key = $("#jellyfin-key").value.trim();
  try {
    const { payload } = await api("/api/v1/jellyfin/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.jellyfinSettings = payload;
    $("#jellyfin-key").value = "";
    $("#jellyfin-key-status").textContent = payload.api_key_configured ? "API Key 已配置" : "尚未配置 API Key";
    await loadDiagnostics();
    showToast("Jellyfin 配置已保存");
  } catch (error) { showToast(error.message, true); }
}

function connectOverviewEvents() {
  if (state.overviewSource || state.view !== "overview") return;
  const source = new EventSource("/api/v1/events");
  state.overviewSource = source;
  source.addEventListener("task_event", () => {
    window.clearTimeout(state.overviewRefreshTimer);
    state.overviewRefreshTimer = window.setTimeout(() => {
      if (state.view !== "overview") return;
      Promise.all([loadDiagnostics(), loadJobs({ overview: true })]).catch(() => {});
    }, 350);
  });
  source.onerror = () => {
    source.close();
    if (state.overviewSource === source) state.overviewSource = null;
  };
}

function disconnectOverviewEvents() {
  window.clearTimeout(state.overviewRefreshTimer);
  state.overviewSource?.close();
  state.overviewSource = null;
}

function healthResult(status, detail) {
  return { status, detail };
}

function localHealthResult(ok, success, failure) {
  return Promise.resolve(healthResult(ok ? "ok" : "error", ok ? success : failure));
}

function buildHealthChecks(diag) {
  const pathChecks = [
    ["config", "配置文件", diag.config_file, "配置文件可读取", "配置文件不可用"],
    ["data", "数据目录", diag.data_dir, "数据目录可写", "数据目录不可用"],
    ["cache", "缓存目录", diag.cache_dir, "缓存目录可写", "缓存目录不可用"],
    ["media", "媒体目录", diag.media_dir, "媒体目录可访问", "媒体目录不可用"],
    ["database", "数据库", diag.database, "SQLite 数据库可用", "数据库不可用"],
  ].map(([id, label, item, success, failure]) => ({
    id,
    group: "运行环境",
    label,
    status: "pending",
    detail: "等待检查",
    run: () => localHealthResult(item.status === "ok", success, `${failure}：${item.status}`),
  }));
  const toolChecks = (diag.tools || []).map((tool) => ({
    id: `tool-${tool.name}`,
    group: "本地工具",
    label: tool.name,
    status: "pending",
    detail: "等待检查",
    run: () => localHealthResult(tool.available, "命令可执行", "未安装或无法执行"),
  }));
  const connectionChecks = [
    {
      id: "moviepilot",
      group: "连接",
      label: "MoviePilot",
      status: "pending",
      detail: "等待检查",
      run: () => Promise.resolve(diag.moviepilot.connected
        ? healthResult("ok", `已验证，最后回调 ${formatTime(diag.moviepilot.last_callback_at)}`)
        : healthResult("warning", diag.moviepilot.token_configured ? "等待首次鉴权回调" : "尚未配置 API Token")),
    },
    {
      id: "jellyfin",
      group: "连接",
      label: "Jellyfin",
      status: "pending",
      detail: "等待检查",
      run: async () => {
        if (!diag.jellyfin.configured) return healthResult("warning", "尚未配置");
        const { payload } = await api("/api/v1/jellyfin/check", { method: "POST" });
        return healthResult("ok", `连接成功，发现 ${payload.library_count} 个媒体库`);
      },
    },
  ];
  const providerChecks = Object.entries(diag.providers || {}).map(([name, diagnostic]) => {
    const settings = state.providerSettings[name] || {};
    const adapter = state.providerAdapters.find((item) => item.name === name);
    const label = adapter?.display_name || name;
    const base = {
      id: `provider-${name}`,
      group: "Provider",
      label,
      status: "pending",
      detail: "等待检查",
    };
    if (!diagnostic.enabled) {
      return { ...base, run: () => Promise.resolve(healthResult("skipped", "未启用")) };
    }
    if (name === "assrt" && settings.token_configured) {
      return { ...base, run: async () => {
        const { payload } = await api("/api/v1/providers/assrt/quota", { method: "POST" });
        return healthResult("ok", `服务可用，当前配额 ${payload.quota}`);
      } };
    }
    if (name === "subdl" && settings.api_key_configured) {
      return { ...base, run: async () => {
        const { payload } = await api("/api/v1/providers/subdl/usage", { method: "POST" });
        return healthResult("ok", `服务可用，搜索额度 ${payload.search_remaining ?? "?"}/${payload.search_limit ?? "?"}`);
      } };
    }
    if (name === "zimuku" && settings.moviepilot_ocr_configured) {
      return { ...base, run: async () => {
        const { payload } = await api("/api/v1/providers/zimuku/ocr-check", { method: "POST" });
        return healthResult("ok", `OCR 实图识别成功，耗时 ${payload.duration_ms} ms`);
      } };
    }
    if (name === "zimuku" && settings.anti_captcha_api_key_configured) {
      return { ...base, run: async () => {
        const { payload } = await api("/api/v1/providers/zimuku/captcha-balance", { method: "POST" });
        return healthResult("ok", `Anti-Captcha 可用，余额 ${Number(payload.balance).toFixed(3)} USD`);
      } };
    }
    return {
      ...base,
      run: () => Promise.resolve(
        diagnostic.status === "ok"
          ? healthResult("ok", "本地适配器可用")
          : healthResult("warning", diagnostic.status === "unconfigured" ? "尚未配置认证信息" : diagnostic.status)
      ),
    };
  });
  return [...pathChecks, ...toolChecks, ...connectionChecks, ...providerChecks];
}

function renderHealthDialog() {
  const checks = state.healthChecks;
  const finished = checks.filter((item) => !["pending", "running"].includes(item.status)).length;
  const percent = checks.length ? Math.round((finished / checks.length) * 100) : 0;
  const ok = checks.filter((item) => item.status === "ok").length;
  const warning = checks.filter((item) => item.status === "warning").length;
  const errors = checks.filter((item) => item.status === "error").length;
  $("#health-run-percent").textContent = `${percent}%`;
  $("#health-progress-bar").style.width = `${percent}%`;
  $("#health-run-state").textContent = state.healthRunning
    ? "正在检查"
    : errors ? "检查完成，有错误" : warning ? "检查完成，有项目待确认" : "检查完成";
  $("#health-count-total").textContent = String(checks.length);
  $("#health-count-ok").textContent = String(ok);
  $("#health-count-warning").textContent = String(warning);
  $("#health-count-error").textContent = String(errors);
  $("#health-check-list").innerHTML = checks.map((item) => `
    <div class="health-check-row ${escapeHtml(item.status)}">
      <i aria-hidden="true"></i>
      <span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.group)}</small></span>
      <p>${escapeHtml(item.detail)}</p>
      <b>${escapeHtml({
        pending: "等待",
        running: "检查中",
        ok: "正常",
        warning: "警告",
        error: "错误",
        skipped: "未启用",
      }[item.status] || item.status)}</b>
    </div>
  `).join("");
  $("#health-dialog-rerun").disabled = state.healthRunning;
  $("#diag-health-check").disabled = state.healthRunning;
}

function closeHealthDialog() {
  $("#health-dialog").hidden = true;
}

async function runHealthCheck() {
  if (state.healthRunning) return;
  $("#health-dialog").hidden = false;
  state.healthRunning = true;
  await loadDiagnostics().catch(() => {});
  state.healthChecks = buildHealthChecks(state.diagnostics);
  renderHealthDialog();
  for (const check of state.healthChecks) {
    check.status = "running";
    check.detail = "正在检查…";
    renderHealthDialog();
    try {
      Object.assign(check, await check.run());
    } catch (error) {
      check.status = "error";
      check.detail = error.message;
    }
    renderHealthDialog();
    await new Promise((resolve) => window.setTimeout(resolve, 80));
  }
  state.healthRunning = false;
  await api("/api/v1/diagnostics/health-runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      checks: state.healthChecks.map(({ label, group, status, detail }) => ({
        name: label,
        group,
        status,
        detail,
      })),
    }),
  }).catch(() => {});
  await loadDiagnostics().catch(() => {});
  renderHealthDialog();
  showToast("健康检查完成");
}

async function checkJellyfin() {
  try {
    const { payload } = await api("/api/v1/jellyfin/check", { method: "POST" });
    await loadDiagnostics();
    showToast(`Jellyfin 连接成功，发现 ${payload.library_count} 个媒体库`);
  } catch (error) {
    await loadDiagnostics().catch(() => {});
    showToast(error.message, true);
  }
}

async function saveGitHub(event) {
  event.preventDefault();
  const githubBody = {};
  if ($("#github-key").value.trim()) githubBody.api_key = $("#github-key").value.trim();
  const serverBody = { token: $("#server-token").value.trim() };
  try {
    const [{ payload: github }, { payload: server }] = await Promise.all([
      api("/api/v1/github/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(githubBody),
      }),
      api("/api/v1/server/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(serverBody),
      }),
    ]);
    state.githubSettings = github;
    state.serverSettings = server;
    $("#server-token").value = server.token || "";
    $("#github-key").value = "";
    $("#github-key-status").textContent = github.api_key_configured
      ? "GitHub Token 已配置，仅用于查询组件更新。"
      : "未配置时使用 GitHub 匿名 API 配额。";
    await loadDiagnostics();
    showToast("系统配置已保存");
  } catch (error) { showToast(error.message, true); }
}

function generateServerToken() {
  $("#server-token").value = randomToken();
  $("#server-token").focus();
}

function exportSettings() {
  window.location.href = "/api/v1/settings/export";
}

async function importSettingsFile(event) {
  const [file] = event.target.files || [];
  event.target.value = "";
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    const { payload: result } = await api("/api/v1/settings/import", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await Promise.all([loadSettings(), loadDiagnostics()]);
    showToast(result.restart_required
      ? "配置已导入；config.yaml 已更新，请重启容器后生效"
      : "配置已导入");
  } catch (error) {
    showToast(`导入失败：${error.message}`, true);
  }
}

function providerCard(name) {
  return $$(".provider-order-item", $("#settings-provider-order"))
    .find((item) => item.dataset.providerName === name) || null;
}

function providerInput(name, key) {
  return $(`[data-setting="${key}"]`, providerCard(name));
}

function providerValue(name, key) {
  return providerInput(name, key)?.value?.trim() || "";
}

function providerNumber(name, key, fallback) {
  return Number(providerValue(name, key)) || fallback;
}

async function saveProviderSettings(name) {
  const card = providerCard(name);
  if (!card) return;
  let body;
  if (name === "subliminal") {
    const authValue = (key) => $(`[data-auth="${key}"]`, card)?.value || "";
    const authentication = {
      opensubtitles: { username: authValue("opensubtitles-username").trim() },
      opensubtitlescom: { username: authValue("opensubtitlescom-username").trim() },
    };
    const secretFields = [
      ["opensubtitles", "password", "opensubtitles-password"],
      ["opensubtitlescom", "password", "opensubtitlescom-password"],
      ["opensubtitlescom", "apikey", "opensubtitlescom-apikey"],
    ];
    secretFields.forEach(([provider, field, selector]) => {
      const value = authValue(selector);
      if (value.trim()) authentication[provider][field] = value;
    });
    body = {
      enabled: providerInput(name, "enabled")?.checked === true,
      providers: $$("[data-subliminal-source]", card).filter((node) => node.checked).map((node) => node.dataset.subliminalSource),
      languages: $$("[data-subliminal-language]", card).filter((node) => node.checked).map((node) => node.dataset.subliminalLanguage),
      authentication,
    };
  } else if (name === "assrt") {
    body = {
      enabled: providerInput(name, "enabled")?.checked === true,
      timeout_seconds: providerNumber(name, "timeout_seconds", 15),
      requests_per_minute: 5,
    };
    if (providerValue(name, "token")) body.token = providerValue(name, "token");
  } else if (name === "subdl") {
    body = {
      enabled: providerInput(name, "enabled")?.checked === true,
      timeout_seconds: providerNumber(name, "timeout_seconds", 15),
      requests_per_minute: providerNumber(name, "requests_per_minute", 20),
      use_api_key_for_downloads: providerInput(name, "use_api_key_for_downloads")?.checked === true,
    };
    if (providerValue(name, "api_key")) body.api_key = providerValue(name, "api_key");
  } else if (name === "zimuku") {
    body = {
      enabled: providerInput(name, "enabled")?.checked === true,
      moviepilot_ocr_url: providerValue(name, "moviepilot_ocr_url"),
      captcha_debug_capture: providerInput(name, "captcha_debug_capture")?.checked === true,
      base_url: providerValue(name, "base_url") || "https://srtku.com",
      timeout_seconds: providerNumber(name, "timeout_seconds", 30),
      request_delay_seconds: Number(providerValue(name, "request_delay_seconds")) || 0,
    };
    if (providerValue(name, "anti_captcha_api_key")) {
      body.anti_captcha_api_key = providerValue(name, "anti_captcha_api_key");
    }
  } else {
    showToast("该外部 Provider 尚未提供 WebUI 配置描述", true);
    return;
  }
  try {
    const { payload } = await api(`/api/v1/providers/${encodeURIComponent(name)}/settings`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.providerSettings[name] = payload;
    delete state.providerChecks[name];
    state.openProviders.add(name);
    await loadDiagnostics();
    renderProviderOrder();
    $("#settings-status").textContent = `${state.providerAdapters.find((item) => item.name === name)?.display_name || name} 配置已保存。`;
    showToast("Provider 配置已保存");
  } catch (error) { showToast(error.message, true); }
}

async function checkProvider(action, name) {
  const output = $(`[data-provider-result="${name}"]`, providerCard(name));
  if (output) output.textContent = "正在检查服务…";
  const endpoints = {
    "assrt-quota": ["/api/v1/providers/assrt/quota", (value) => `服务可用，当前 API 配额：${value.quota}`],
    "subdl-usage": ["/api/v1/providers/subdl/usage", (value) => `服务可用，搜索额度 ${value.search_remaining ?? "?"}/${value.search_limit ?? "?"}，下载额度 ${value.download_remaining ?? "?"}/${value.download_limit ?? "?"}`],
    "zimuku-ocr": ["/api/v1/providers/zimuku/ocr-check", (value) => `OCR 实图识别成功：${value.recognized_answer}，耗时 ${value.duration_ms} ms`],
    "zimuku-balance": ["/api/v1/providers/zimuku/captcha-balance", (value) => `Anti-Captcha 余额：${Number(value.balance).toFixed(3)} USD`],
  };
  const [endpoint, format] = endpoints[action] || [];
  if (!endpoint) return;
  try {
    const { payload } = await api(endpoint, { method: "POST" });
    state.providerChecks[name] = { ok: true, message: format(payload) };
  } catch (error) {
    state.providerChecks[name] = { ok: false, message: `检查失败：${error.message}` };
  }
  await loadDiagnostics().catch(() => {});
  renderProviderOrder();
}

function openDrawer(eyebrow, title, html, { seriesId = null } = {}) {
  state.drawerSeriesId = seriesId;
  $("#drawer-eyebrow").textContent = eyebrow;
  $("#drawer-title").textContent = title;
  $("#drawer-content").innerHTML = html;
  $("#detail-drawer").classList.add("open");
  $("#detail-drawer").setAttribute("aria-hidden", "false");
  $("#drawer-backdrop").hidden = false;
  const retry = $("[data-retry-task]", $("#drawer-content"));
  const remove = $("[data-delete-task]", $("#drawer-content"));
  retry?.addEventListener("click", () => retryTasks([Number(retry.dataset.retryTask)]));
  remove?.addEventListener("click", () => deleteTasks([Number(remove.dataset.deleteTask)]));
}

function closeDrawer() {
  state.drawerSeriesId = null;
  state.drawerMissingOnly = false;
  state.drawerOpenSeasons.clear();
  $("#detail-drawer").classList.remove("open");
  $("#detail-drawer").setAttribute("aria-hidden", "true");
  $("#drawer-backdrop").hidden = true;
}

async function refreshCurrent() {
  try {
    if (state.view === "overview") {
      await Promise.all([loadDiagnostics(), loadJobs({ overview: true })]);
      if (state.diagnostics?.jellyfin?.configured) {
        try { await loadRecentMedia(); } catch (error) { $("#overview-media").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; }
      } else {
        $("#overview-media").innerHTML = '<p class="empty">Jellyfin 尚未配置，请先在设置中完成连接</p>';
      }
      connectOverviewEvents();
    } else if (state.view === "tasks") {
      await Promise.all([loadDiagnostics(), loadJobs()]);
    } else if (state.view === "library") {
      if (!state.diagnostics) await loadDiagnostics();
      if (state.diagnostics?.jellyfin?.configured) {
        await loadLibraries();
      } else {
        state.libraries = [];
        state.media = [];
        renderLibraryTabs();
        $("#media-grid").innerHTML = '<p class="empty">Jellyfin 尚未配置，请先在设置中完成连接</p>';
      }
    } else if (state.view === "logs") {
      await loadLogs();
      connectLogs();
    } else if (state.view === "settings") {
      await Promise.all([loadDiagnostics(), loadSettings()]);
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

const viewCopy = {
  overview: ["运行概览", "让每一部影片，都有合适的字幕"],
  tasks: ["任务工作台", "搜索、重试并排查每一个字幕任务"],
  library: ["媒体库", "浏览 Jellyfin 媒体库字幕状态，并按需创建任务"],
  logs: ["系统日志", "记录服务运行、健康检查、配置变更与任务结果"],
  settings: ["设置", "连接媒体库并管理服务配置"],
};

function switchView(view, { refresh = true } = {}) {
  if (!viewCopy[view]) return;
  if (state.view === "logs" && view !== "logs") disconnectLogs();
  if (state.view === "overview" && view !== "overview") disconnectOverviewEvents();
  state.view = view;
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  $$(".nav-item").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
  $("#page-title").textContent = viewCopy[view][0];
  $("#page-subtitle").textContent = viewCopy[view][1];
  if (refresh) refreshCurrent();
}

function reorderProvider(source, target) {
  const from = state.providerOrder.indexOf(source);
  const to = state.providerOrder.indexOf(target);
  if (from < 0 || to < 0 || from === to) return;
  state.providerOrder.splice(from, 1);
  state.providerOrder.splice(to, 0, source);
  renderProviderOrder();
  $("#settings-status").textContent = "顺序已调整，保存后生效。";
}

function bindProviderOrderEvents() {
  const list = $("#settings-provider-order");
  list.addEventListener("toggle", (event) => {
    const details = event.target.closest(".provider-config-card");
    const name = details?.closest("[data-provider-name]")?.dataset.providerName;
    if (!name) return;
    details.open ? state.openProviders.add(name) : state.openProviders.delete(name);
  }, true);
  list.addEventListener("click", (event) => {
    if (event.target.closest("[data-provider-drag]")) {
      event.preventDefault();
      return;
    }
    const save = event.target.closest("[data-provider-save]");
    if (save) {
      event.preventDefault();
      saveProviderSettings(save.dataset.providerSave);
      return;
    }
    const check = event.target.closest("[data-provider-check]");
    if (check) {
      event.preventDefault();
      const name = check.closest("[data-provider-name]").dataset.providerName;
      checkProvider(check.dataset.providerCheck, name);
    }
  });
  list.addEventListener("keydown", (event) => {
    const handle = event.target.closest("[data-provider-drag]");
    if (!handle || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    moveProvider(handle.dataset.providerDrag, event.key === "ArrowUp" ? "up" : "down");
  });
  list.addEventListener("dragstart", (event) => {
    const handle = event.target.closest("[data-provider-drag]");
    if (!handle) {
      event.preventDefault();
      return;
    }
    state.draggedProvider = handle.dataset.providerDrag;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", state.draggedProvider);
    handle.closest(".provider-order-item").classList.add("dragging");
  });
  list.addEventListener("dragover", (event) => {
    const target = event.target.closest(".provider-order-item");
    if (!target || !state.draggedProvider) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    $$(".provider-order-item.drag-over", list).forEach((item) => item.classList.remove("drag-over"));
    target.classList.add("drag-over");
  });
  list.addEventListener("drop", (event) => {
    const target = event.target.closest(".provider-order-item");
    if (!target || !state.draggedProvider) return;
    event.preventDefault();
    reorderProvider(state.draggedProvider, target.dataset.providerName);
    state.draggedProvider = null;
  });
  list.addEventListener("dragend", () => {
    state.draggedProvider = null;
    $$(".provider-order-item", list).forEach((item) => item.classList.remove("dragging", "drag-over"));
  });
}

function bindEvents() {
  $$(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $$("[data-go]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.go)));
  $("#global-refresh").addEventListener("click", refreshCurrent);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", closeDrawer);
  $("#task-select-all").addEventListener("change", (event) => {
    flattenJobs(state.jobs).forEach((task) => event.target.checked ? state.selectedTasks.add(task.id) : state.selectedTasks.delete(task.id));
    renderTasks();
  });
  let taskSearchTimer;
  $("#task-search").addEventListener("input", (event) => {
    window.clearTimeout(taskSearchTimer);
    taskSearchTimer = window.setTimeout(() => {
      state.taskSearch = event.target.value.trim();
      state.taskPage = 1;
      loadJobs().catch((error) => showToast(error.message, true));
    }, 280);
  });
  $$("[data-task-status]").forEach((button) => button.addEventListener("click", () => {
    state.taskStatus = button.dataset.taskStatus;
    state.taskPage = 1;
    $$("[data-task-status]").forEach((node) => node.classList.toggle("active", node === button));
    loadJobs().catch((error) => showToast(error.message, true));
  }));
  $("#task-page-size").addEventListener("change", (event) => {
    state.taskPageSize = Number(event.target.value);
    state.taskPage = 1;
    loadJobs().catch((error) => showToast(error.message, true));
  });
  $("#task-prev").addEventListener("click", () => { state.taskPage -= 1; loadJobs(); });
  $("#task-next").addEventListener("click", () => { state.taskPage += 1; loadJobs(); });
  $("#task-batch-retry").addEventListener("click", () => retryTasks([...state.selectedTasks]));
  $("#task-batch-delete").addEventListener("click", () => deleteTasks([...state.selectedTasks]));
  $("#task-delete-all").addEventListener("click", deleteAllTasks);
  let mediaSearchTimer;
  $("#media-search").addEventListener("input", (event) => {
    window.clearTimeout(mediaSearchTimer);
    mediaSearchTimer = window.setTimeout(() => { state.mediaSearch = event.target.value.trim(); renderMedia(); }, 180);
  });
  $$("[data-media-status]").forEach((button) => button.addEventListener("click", () => {
    if (button.disabled) return;
    state.mediaStatus = button.dataset.mediaStatus;
    $$("[data-media-status]").forEach((node) => node.classList.toggle("active", node === button));
    renderMedia();
  }));
  $("#media-sort").addEventListener("change", (event) => {
    state.mediaSort = event.target.value;
    renderMedia();
  });
  $("#media-sort-direction").addEventListener("change", (event) => {
    state.mediaSortDirection = event.target.value;
    renderMedia();
  });
  $("#library-scan").addEventListener("click", scanLibrary);
  $("#library-add").addEventListener("click", addMediaTasks);
  $("#library-ignore").addEventListener("click", () => batchIgnoreMedia(true));
  $("#library-unignore").addEventListener("click", () => batchIgnoreMedia(false));
  ["#log-level", "#log-category"].forEach((selector) => $(selector).addEventListener("change", async () => { await loadLogs(); connectLogs(); }));
  let logTaskTimer;
  $("#log-task").addEventListener("input", () => {
    window.clearTimeout(logTaskTimer);
    logTaskTimer = window.setTimeout(async () => { await loadLogs(); connectLogs(); }, 260);
  });
  $("#logs-pause").addEventListener("click", () => {
    state.logsPaused = !state.logsPaused;
    $("#logs-pause").textContent = state.logsPaused ? "继续" : "暂停";
    state.logsPaused ? disconnectLogs() : connectLogs();
  });
  $("#logs-clear").addEventListener("click", () => { state.logs = []; renderLogs(); });
  $("#diag-updates").addEventListener("click", checkUpdates);
  $("#diag-health-check").addEventListener("click", runHealthCheck);
  $("#health-dialog-close").addEventListener("click", closeHealthDialog);
  $("#health-dialog-dismiss").addEventListener("click", closeHealthDialog);
  $("#health-dialog-rerun").addEventListener("click", runHealthCheck);
  $("#diag-export").addEventListener("click", () => { window.location.href = "/api/v1/diagnostics/export"; });
  $("#jellyfin-form").addEventListener("submit", saveJellyfin);
  $("#jellyfin-check").addEventListener("click", checkJellyfin);
  $("#github-form").addEventListener("submit", saveGitHub);
  $("#server-token-generate").addEventListener("click", generateServerToken);
  $("#settings-export").addEventListener("click", exportSettings);
  $("#settings-import").addEventListener("click", () => $("#settings-import-file").click());
  $("#settings-import-file").addEventListener("change", importSettingsFile);
  $("#path-mapping-add").addEventListener("click", () => addPathMapping());
  $("#path-mapping-test").addEventListener("click", testPathMappings);
  $("#path-mapping-save").addEventListener("click", savePathMappings);
  $("#path-mapping-rows").addEventListener("click", (event) => {
    const remove = event.target.closest("[data-path-remove]");
    if (!remove) return;
    remove.closest(".path-mapping-row")?.remove();
    if (!$("#path-mapping-rows").children.length) addPathMapping();
  });
  $("#setup-skip").addEventListener("click", () => {
    collectSetupWizardPage();
    window.localStorage.setItem("subpick-setup-dismissed-v2", "1");
    closeSetupDialog();
  });
  $("#setup-back").addEventListener("click", backSetupWizard);
  $("#setup-continue").addEventListener("click", continueSetupWizard);
  $("#setup-dialog-body").addEventListener("change", (event) => {
    if (!event.target.matches("#setup-zimuku-enabled, #setup-subliminal-enabled, #setup-opensubtitles-enabled, #setup-opensubtitlescom-enabled, #setup-assrt-enabled, #setup-subdl-enabled")) return;
    collectSetupWizardPage();
    renderSetupDialog();
  });
  $("#setup-dialog-body").addEventListener("click", (event) => {
    if (!event.target.closest("#setup-token-generate")) return;
    state.setupWizard.draft.moviepilotToken = randomToken();
    renderSetupDialog();
    $("#setup-moviepilot-token")?.focus();
  });
  bindProviderOrderEvents();
  $("#provider-order-save").addEventListener("click", saveProviderOrder);
}

async function init() {
  bindEvents();
  const minSplash = new Promise((resolve) => window.setTimeout(resolve, 1750));
  const settingsReady = loadSettings();
  await Promise.allSettled([refreshCurrent(), settingsReady, minSplash]);
  $("#splash").classList.add("done");
  window.setTimeout(() => { void loadDiagnostics().catch(() => {}); }, 3000);
  maybeOpenSetupDialog();
}

init();
