# Contributing

感谢参与拾幕开发。

## 开发环境

- Python 3.12
- `uv`
- Docker（验证镜像时）

```powershell
.\scripts\bootstrap.ps1
.\scripts\check.ps1
```

提交前必须保证 `check.ps1` 通过。修改依赖时同时提交 `pyproject.toml` 与
`uv.lock`，不要使用系统 Python 或全局 `pip` 修改项目环境。

## 修改范围

- 保持拾幕轻量，优先复用现有接口和模式。
- Provider 特有逻辑放在独立 adapter 中；任务状态、调度、校验与落库由核心负责。
- 不提交 API Key、Token、Cookie、真实 NAS 地址、媒体文件或字幕文件。
- Provider 网络测试应使用最少请求，自动化测试优先使用固定响应。

提交问题时请附上脱敏后的诊断信息、任务 ID、Provider 和错误代码，不要公开凭据。
