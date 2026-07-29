<p align="center">
  <img src="docs/assets/subpick-logo.svg" width="132" alt="拾幕 SubPick Logo">
</p>

<h1 align="center">拾幕 SubPick</h1>

<p align="center"><strong>让每一部影片，都有合适的字幕。</strong></p>

<p align="center">
  <a href="https://github.com/AlexTOOT/SubPick/releases">版本发布</a> ·
  <a href="https://github.com/AlexTOOT/SubPick/pkgs/container/subpick">Docker 镜像</a> ·
  <a href="docs/deployment/nas-docker.md">部署说明</a>
</p>

拾幕是面向 MoviePilot + Jellyfin 用户的轻量中文字幕 Sidecar。它兼容
MoviePilot 的 ChineseSubFinder 通知链路，也能浏览 Jellyfin 媒体库、检查
内嵌与外挂字幕，并按电影、整剧、整季或单集手动创建任务。

当前内置 Subliminal（OpenSubtitles / OpenSubtitles.com）、ASSRT、SubDL 和
Zimuku。任务会依次经过本地检查、搜索、下载、候选校验、对轴和落库，并在
WebUI 中提供任务、候选、实时日志和系统健康状态。

> 当前版本仍属于早期公开预览。建议先在少量媒体上验证，再进行批量操作。

## 界面预览

![拾幕运行概览](docs/assets/subpick-overview.png)

## 快速部署

1. 在 NAS 上新建一个空目录，例如 `SubPick`。
2. 在 Docker 管理器中新建 Compose 项目，粘贴下面的内容。
3. 只修改媒体目录映射中冒号左侧的 NAS 路径。

```yaml
services:
  subpick:
    image: ghcr.io/alextoot/subpick:latest
    container_name: subpick
    ports:
      - "19035:19035"
    volumes:
      # 拾幕主目录：首次启动会自动生成 config.yaml、data/ 和 cache/。
      - ./:/appdata
      # 修改冒号左侧为 MoviePilot 使用的媒体根目录。
      # 该目录应包含电影和剧集的全部媒体，并允许拾幕写入字幕文件。
      - /volume1/media:/media
    environment:
      TZ: Asia/Shanghai
    depends_on:
      - moviepilot-ocr
    restart: unless-stopped

  moviepilot-ocr:
    image: jxxghp/moviepilot-ocr:latest
    container_name: moviepilot-ocr
    restart: unless-stopped
```

也可以直接下载仓库中的 [compose.yaml](compose.yaml)。

启动后目录会自动变成：

```text
SubPick/
├── compose.yaml
├── config.yaml
├── data/
└── cache/
```

打开 `http://<NAS-IP>:19035/`，按照首次使用向导依次完成媒体目录、
MoviePilot、Jellyfin、字幕来源和 OCR 检查。向导可以跳过，未完成项目会继续
显示在运行概览顶部。

## MoviePilot

继续使用 MoviePilot 的 ChineseSubFinder 插件：

- 服务地址：`http://<NAS-IP>:19035`
- API Key：与“拾幕 → 设置 → MoviePilot API Token”保持一致

MoviePilot 与拾幕处于同一 Docker 网络时，也可以使用
`http://subpick:19035`。拾幕只有在成功收到一次鉴权回调后才会把状态从
“等待验证”改为“已连接”。

回调路径必须能在拾幕容器内访问。推荐让 MoviePilot、Jellyfin 和拾幕都把同一
媒体根目录映射为 `/media`。若首次任务的路径无法访问，运行概览会给出持久通知，
再按需要配置 `config.yaml` 中的 `paths.mappings`。

## Jellyfin

在“设置 → Jellyfin 配置”中填写地址、API Key 和 User ID，然后执行“测试连接”。
媒体库扫描只更新字幕覆盖状态，不会自动创建下载任务。

## Zimuku 与 OCR

Compose 默认启动本地 MoviePilot OCR，Zimuku 默认访问
`http://moviepilot-ocr:9899`，无需暴露额外 NAS 端口。启用 Zimuku 后，请在
Provider 设置中执行一次 OCR 实图测试；仅能打开 OCR 根地址不代表识别可用。

## 数据、备份与升级

- `config.yaml`：启动与路径配置
- `data/`：SQLite、任务记录和 WebUI 设置
- `cache/`：海报、整季字幕包等可重建缓存

WebUI 的“设置 → 系统与更新”可以导出或导入运行配置。完整备份建议同时保存
`config.yaml` 与 `data/`。导出的配置包含已保存的 API Key 和 Token，请像密码一样
妥善保管，不要上传到公开仓库或发送给他人。

更新：

```bash
docker compose pull
docker compose up -d
```

回滚时，把 Compose 中的镜像标签从 `latest` 改为指定版本，例如
`ghcr.io/alextoot/subpick:0.6.0`，再重新创建容器。不要在运行中的容器内单独
升级 Subliminal、ffsubsync 或其他 Python 依赖，它们会随拾幕主镜像统一发布。

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

字幕内容及字幕站点服务受各自条款约束。请仅下载和使用你有权访问的内容，并
合理设置请求频率。
