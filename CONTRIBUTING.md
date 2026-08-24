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

## 字幕匹配评测集

`tests/fixtures/matching_evaluation.json` 保存 IMDb 元数据、难度分层的正确/错误候选，
以及不可逆的台词 SHA-256 指纹；仓库不保存字幕正文。常规回归由 pytest 自动执行。
需要核验一份本地字幕时使用：

```powershell
uv run python -m subtitle_sidecar.evaluation `
  --dataset tests/fixtures/matching_evaluation.json `
  --case movie-colony-2026 `
  D:\path\to\candidate.srt
```

退出码 `0` 表示每个输入文件至少命中一个预期指纹，`1` 表示有文件未命中，`2`
表示数据集、文件或格式错误。命令只输出指纹及计数，不输出台词正文。

## 修改范围

- 保持拾幕轻量，优先复用现有接口和模式。
- Provider 特有逻辑放在独立 adapter 中；任务状态、调度、校验与落库由核心负责。
- 不提交 API Key、Token、Cookie、真实 NAS 地址、媒体文件或字幕文件。
- Provider 网络测试应使用最少请求，自动化测试优先使用固定响应。

提交问题时请附上脱敏后的诊断信息、任务 ID、Provider 和错误代码，不要公开凭据。
