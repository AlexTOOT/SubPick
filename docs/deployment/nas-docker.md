# Docker / NAS 部署

拾幕由一个主服务与一个本地 OCR 服务组成：

```text
MoviePilot ──通知──> SubPick ──读取──> 媒体目录
                        │
                        ├──读取──> Jellyfin API
                        └──验证码──> MoviePilot OCR
```

## 推荐 Compose

使用仓库根目录的 `docker-compose.example.yml`。将其保存为
`docker-compose.yml`，并从 `.env.example` 创建 `.env`。

```dotenv
SUBPICK_IMAGE=ghcr.io/alextoot/subpick:latest
MEDIA_PATH=/volume1/media
```

`MEDIA_PATH` 是 NAS 上的真实媒体库根目录。容器内统一挂载为 `/media`。

```bash
docker compose up -d
```

服务端口：

| 服务 | 容器端口 | 默认 NAS 端口 |
| --- | ---: | ---: |
| SubPick | 19035 | 19035 |
| MoviePilot OCR | 9899 | 19899 |

WebUI 地址为 `http://<NAS-IP>:19035/`。

## 配置文件

拾幕启动时读取 `/config/config.yaml`。从 `config.example.yaml` 创建
`config/config.yaml`，不要把真实配置提交到 Git。

持久化目录：

- `config/`：YAML 配置
- `data/`：SQLite、任务、设置和诊断样本
- `cache/`：可重建缓存

升级前备份 `config/` 与 `data/`。

## MoviePilot

MoviePilot 与拾幕位于同一 Docker 网络时，ChineseSubFinder 插件地址填写：

```text
http://subpick:19035
```

不在同一网络时填写：

```text
http://<NAS-IP>:19035
```

插件 API Key 与拾幕设置页中的 MoviePilot API Key 保持一致。拾幕只对
`/api/v1/add-job` 回调执行该鉴权；WebUI 仍假设运行于可信局域网。

## 路径映射

MoviePilot 传入的文件路径必须能映射到拾幕容器内的真实文件。例如：

```yaml
paths:
  mappings:
    - from: /moviepilot/media
      to: /media
```

不要配置固定的电影库或剧集库目录。拾幕使用 MoviePilot/Jellyfin 提供的完整
媒体路径，不按文件名扫描或猜测。

## 本地 OCR

Zimuku 的网页流程可能出现数字验证码。MoviePilot 官方默认的在线 OCR 地址
`https://movie-pilot.org` 在实际测试中可能超时，因此示例 Compose 默认部署
独立的 `jxxghp/moviepilot-ocr` 容器。

拾幕与 OCR 位于同一 Compose 网络，Zimuku 的默认 OCR 地址为：

```text
http://moviepilot-ocr:9899
```

只有从 NAS 浏览器或其他容器直接访问 OCR 时才使用：

```text
http://<NAS-IP>:19899
```

启用 Zimuku 前，在设置页执行“实图检查 OCR”。检查会提交一张答案已知的测试
图片，并同时验证 HTTP 调用和识别结果。

## 更新与回滚

更新：

```bash
docker compose pull
docker compose up -d
```

回滚：

1. 在 `.env` 中把 `SUBPICK_IMAGE` 改为上一个版本标签。
2. 再次执行 `docker compose pull && docker compose up -d`。

不要在容器内执行 `pip install`。镜像中的依赖和数据库迁移只针对完整版本组合
进行测试。
