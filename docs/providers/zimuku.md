# Zimuku 字幕源

Zimuku 是与 Subliminal、ASSRT、SubDL 平级的一级 Provider Adapter。它读取
公开搜索页、字幕详情页和下载页，不修改 Subliminal，也不绕过账号、付费墙或
站点访问权限。

## 默认配置

```yaml
providers:
  zimuku:
    enabled: false
    base_url: https://srtku.com
    moviepilot_ocr_url: http://moviepilot-ocr:9899
    anti_captcha_api_key: ""
    captcha_debug_capture: false
    timeout_seconds: 30
    request_delay_seconds: 1
```

示例 Compose 默认启动本地 MoviePilot OCR。启用 Zimuku 前，请在设置页运行
“实图检查 OCR”；仅能访问根地址不代表识别服务可用。

## 搜索策略

- 电影：依次搜索“中文标题 + 年份”“原始标题 + 年份”“中文标题”“原始标题”，
  找到有效候选后停止回退。
- 剧集：优先搜索“剧名 + Sxx”整季包，再回退到 `SxxExx`。
- 仅返回 SRT、ASS、SSA、VTT 文本字幕。
- 下载页返回 ZIP、RAR 或 7z 时安全提取可用字幕成员；RAR 优先使用
  `lsar + unar`，避免部分 7-Zip 构建不包含 RAR 解码器。
- 支持 UTF-8、GB18030、Big5，以及带 BOM 的 UTF-16、UTF-32 文本字幕。
- 整季包会进入缓存，供同季后续任务复用。

## 验证码

识别顺序：

1. 本地 MoviePilot OCR
2. 可选 Anti-Captcha 付费后备

开启 `captcha_debug_capture` 后，OCR 服务正常但答案为空、格式错误或被站点
拒绝时，拾幕会把图片与脱敏结果保存到：

```text
/appdata/data/diagnostics/captcha/zimuku
```

最多保留 100 组，仅建议排障时开启。

## 已知限制

- Zimuku 没有公开 API，页面或验证码流程变化可能导致 Adapter 失效。
- Zimuku 本身不支持 TMDb/IMDb 查询，检索以标题、年份、季集信息为主；媒体
  ID 仍用于拾幕内部身份和候选校验。
- OCR 模型并非对所有验证码都可靠。失败时应查看结构化日志与诊断样本。

## 来源与许可

实现参考了 GPL-3.0 的
[Bazarr Zimuku Provider](https://github.com/morpheus65535/bazarr)，并针对拾幕
Provider 接口、调度、缓存和日志体系进行了修改。
