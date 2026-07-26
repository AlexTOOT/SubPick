# 拾幕 SubPick

> 让每一部影片，都有合适的字幕。

拾幕是面向 MoviePilot + Jellyfin 用户的轻量中文字幕 Sidecar。它兼容
MoviePilot 的 ChineseSubFinder 通知链路，也可以浏览 Jellyfin 媒体库并手动
创建字幕任务。

当前内置字幕来源：

- Subliminal（OpenSubtitles / OpenSubtitles.com）
- ASSRT
- SubDL
- Zimuku

拾幕会先检查外挂与内嵌中文字幕，再按 Provider 顺序搜索、下载、校验、尝试
对轴并落库。任务、候选、日志和媒体库状态统一在 WebUI 中查看。

> 当前版本仍属于早期公开预览。请先在少量媒体上验证，再对媒体库批量操作。

## 快速部署

### 1. 准备目录

```text
subpick/
├── docker-compose.yml
├── .env
├── config/
│   └── config.yaml
├── data/
└── cache/
```

下载仓库中的以下文件：

- `docker-compose.example.yml`，保存为 `docker-compose.yml`
- `.env.example`，保存为 `.env`
- `config.example.yaml`，保存为 `config/config.yaml`

编辑 `.env`，把 `MEDIA_PATH` 改为 NAS 上真实的媒体库根目录：

```dotenv
SUBPICK_IMAGE=ghcr.io/alextoot/subpick:latest
MEDIA_PATH=/volume1/media
```

### 2. 配置路径映射

`config/config.yaml` 中的 `paths.mappings` 用于把 MoviePilot 传来的路径转换为
拾幕容器内的路径。例如 MoviePilot 传入 `/moviepilot/media/Movie/A.mkv`，
而媒体库在拾幕容器内挂载为 `/media`：

```yaml
paths:
  mappings:
    - from: /moviepilot/media
      to: /media
```

如果 MoviePilot、Jellyfin 和拾幕看到的媒体路径完全一致，可以删除该映射。
拾幕不会按文件名猜测路径。

### 3. 启动

```bash
docker compose up -d
```

Compose 会启动两个容器：

- `subpick`：WebUI 与字幕任务服务，端口 `19035`
- `moviepilot-ocr`：Zimuku 验证码识别服务，容器端口 `9899`，NAS 端口 `19899`

打开：

```text
http://<NAS-IP>:19035/
```

Zimuku 默认使用 `http://moviepilot-ocr:9899`，同一 Compose 网络内无需改成
NAS IP。建议在“设置 → Provider → Zimuku”中先执行一次“实图检查 OCR”。

## MoviePilot 配置

继续使用 MoviePilot 的 ChineseSubFinder 插件：

- 服务地址：`http://subpick:19035`（同一 Docker 网络）
- 或服务地址：`http://<NAS-IP>:19035`（不同 Docker 网络）
- API Key：与拾幕设置页中的 MoviePilot API Key 保持一致

MoviePilot 回调接口为 `POST /api/v1/add-job`。配置 API Key 后，该接口要求
`Authorization: Bearer <token>`；WebUI 在可信 NAS 局域网内不要求登录。

## Jellyfin 配置

在“设置 → Jellyfin 配置”中填写：

- Jellyfin 地址
- API Key
- 可选 User ID

随后进入“媒体库”加载或扫描。扫描只更新字幕状态，不会自动创建下载任务；
用户可以按电影、整剧、整季或单集手动添加任务。

## 数据与升级

需要备份：

- `config/`：YAML 配置
- `data/`：SQLite 数据库、任务记录和本地设置

`cache/` 可以重建，不要求备份。

升级前备份 `config/` 和 `data/`，然后执行：

```bash
docker compose pull
docker compose up -d
```

回滚时把 `.env` 中的镜像改为上一个版本标签，再重新启动。不要在运行中的
容器内单独升级 Python 依赖；Subliminal、ffsubsync 和内置 Provider 会随拾幕
镜像统一发布。

## 开发

项目使用 Python 3.12、`uv` 和仓库内 `.venv`：

```powershell
.\scripts\bootstrap.ps1
.\scripts\check.ps1
.\scripts\run.ps1
```

依赖锁定在 `uv.lock`。修改 `pyproject.toml` 后运行
`.\scripts\update-dependencies.ps1`，并同时提交两者。

## 文档

- [Docker / NAS 部署](docs/deployment/nas-docker.md)
- [Provider Adapter 接口](docs/provider-adapter-api.md)
- [Zimuku 与本地 OCR](docs/providers/zimuku.md)
- [发布与更新策略](docs/architecture/release-and-update-strategy.md)
- [后续路线图](ROADMAP.md)

## 许可证与声明

拾幕使用 [GNU GPL v3 或更高版本](LICENSE) 发布。

Zimuku 适配器的设计参考了
[Bazarr](https://github.com/morpheus65535/bazarr) 的 Zimuku Provider；Bazarr
同样使用 GPL-3.0。MoviePilot OCR 镜像由
[jxxghp/MoviePilot-OCR](https://github.com/jxxghp/MoviePilot-OCR) 提供。

字幕内容及字幕站点服务受各自条款约束。请仅下载和使用你有权访问的内容，
并合理设置请求频率。
