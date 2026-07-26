# Security

拾幕默认面向可信 NAS 局域网，不应直接暴露到公网。

## 安全边界

- 配置 `server.token` 后，MoviePilot 回调接口要求 Bearer Token。
- WebUI 与管理 API 当前不提供多用户登录。
- 不要把 Docker Socket 挂载给拾幕。
- 不要在 Issue 或日志中公开 API Key、Token、Cookie、NAS 地址或媒体路径。

## 报告问题

发现可能泄露凭据、任意文件读写、路径逃逸或远程代码执行的问题时，请通过
GitHub Security Advisory 私下报告，不要先创建公开 Issue。
