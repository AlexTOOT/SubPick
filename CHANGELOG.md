# Changelog

本项目遵循语义化版本号。公开版本之前的开发提交已整理为首个发布快照。

## 0.6.0 - 2026-07-29

### Added

- 单一 `/appdata` 目录部署，首次启动自动生成配置、数据与缓存目录。
- 首次使用向导、顶部配置进度、动态通知和系统健康检查。
- MoviePilot 首次成功回调验证、Jellyfin 实际连接测试和 Zimuku OCR 状态记录。
- WebUI 运行配置导入与导出。
- 可直接粘贴的 Compose、README Logo 与界面预览入口。

### Changed

- 移除首次部署对 `.env` 和手工创建目录、配置文件的依赖。
- Jellyfin 海报和整季字幕包缓存统一写入 `/appdata/cache`。
- 侧边栏改用功能图标并提高文字可读性。

## 0.5.0 - 2026-07-26

首个公开预览版本。

### Added

- MoviePilot ChineseSubFinder 兼容回调与可选 Bearer Token。
- Jellyfin 媒体库浏览、海报缓存、字幕状态扫描和批量任务创建。
- Subliminal、ASSRT、SubDL、Zimuku Provider。
- 外挂与内嵌中文字幕检查、候选排序、字幕校验、ffsubsync 保守对轴。
- 整季包缓存、候选去重、失败候选排除和 Provider 频率调度。
- 任务工作台、实时日志、诊断、设置和 Provider 优先级管理。
- 拾幕 WebUI、Docker 部署文件与本地 MoviePilot OCR 服务。
