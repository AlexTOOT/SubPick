# Docker / NAS 部署

拾幕由一个主服务与一个本地 OCR 服务组成：

```text
MoviePilot ──通知──> SubPick ──读写──> 媒体目录
                        │
                        ├──身份──> 本地 NFO
                        ├──展示/刷新──> Jellyfin API
                        └──验证码──> MoviePilot OCR
```

## 推荐方式

在 NAS 上新建一个空目录，例如 `SubPick`，映射给 `/appdata`，作为拾幕的
主目录。然后把仓库根目录的 [compose.yaml](../../compose.yaml) 放进去，或在
Docker 管理器中直接粘贴：

```yaml
services:
  subpick:
    image: ghcr.io/alextoot/subpick:latest
    container_name: subpick
    networks:
      - subpick
    ports:
      - "19035:19035"
    volumes:
      - /volume1/SubPick:/appdata # 拾幕主目录
      # 媒体库根目录，应包含电影和剧集的全部媒体，推荐与 MoviePilot 的 /media 保持一致。
      - /volume1/media:/media
    environment:
      TZ: Asia/Shanghai
    depends_on:
      - moviepilot-ocr
    restart: unless-stopped

  moviepilot-ocr:
    image: jxxghp/moviepilot-ocr:latest
    container_name: moviepilot-ocr
    networks:
      - subpick
    ports:
      - "9899:9899"
    restart: unless-stopped

networks:
  subpick:
    driver: bridge
```

首次启动会自动创建：

```text
SubPick/
├── compose.yaml
├── config.yaml
├── data/
└── cache/
```

无需准备 `.env`，也无需手工创建配置和数据目录。

```bash
docker compose up -d
```

WebUI 地址为 `http://<NAS-IP>:19035/`。

## 首次使用

打开 WebUI 后，向导会依次检查：

1. 配置、数据与缓存目录
2. `/media` 是否可读写
3. MoviePilot API Token 与首次成功回调
4. Jellyfin 实际连接
5. 至少一个字幕来源
6. 启用 Zimuku 时的 OCR 实图识别

向导可以跳过。未完成项目和运行错误会继续显示在运行概览顶部，全部完成后
配置进度会自动隐藏。

## MoviePilot

ChineseSubFinder 插件地址填写：

```text
http://<NAS-IP>:19035
```

如果 MoviePilot 与拾幕加入了同一个 Docker 网络，也可以填写：

```text
http://subpick:19035
```

插件 API Key 与拾幕设置页中的 MoviePilot API Token 保持一致。拾幕只对
`/api/v1/add-job` 回调执行鉴权；WebUI 仍假设运行于可信局域网。

MoviePilot 到拾幕是单向调用。保存 Token 后显示“等待验证”，只有拾幕成功收到
一次鉴权回调后才会显示“已连接”。

回调只需要提供媒体路径。拾幕不会用回调字段或 Jellyfin 元数据决定作品身份，而会在
路径可访问后读取 MoviePilot 已生成的 NFO。由于回调可能早于 NFO 最终落盘，任务会在
队列中等待最多 120 秒；电影优先等待 `movie.nfo`，剧集以 `tvshow.nfo` 为作品身份，
再从有效分集 NFO 或文件名 `SxxExx` 取得季集号。

## 媒体路径

推荐在 MoviePilot、Jellyfin 和拾幕中使用同一个容器内媒体路径 `/media`。
这样 MoviePilot 下发的文件路径可以被拾幕直接访问，不需要额外映射。

如果 MoviePilot 下发的路径不是 `/media/...`，优先直接在 Compose 中把同一个
NAS 媒体目录额外挂载到回调使用的路径。例如回调路径以 `/mnt/media` 开头：

```yaml
    volumes:
      - /volume1/media:/media
      - /volume1/media:/mnt/media
```

只有多个媒体根目录无法通过 Compose 对齐时，才需要编辑 `config.yaml` 中的
`paths.mappings`。首次回调路径无法访问时，拾幕会在运行概览和系统健康页保留
错误通知。

## 本地 OCR

Zimuku 的网页流程可能出现数字验证码。示例 Compose 默认部署
`jxxghp/moviepilot-ocr`，拾幕通过 Compose 内部地址访问：

```text
http://moviepilot-ocr:9899
```

Compose 会把 OCR 的 `9899` 端口暴露到 NAS，拾幕服务本身仍通过内部 bridge
网络访问。启用 Zimuku 后，在设置页执行“实图检查 OCR”；检查会提交一张答案
已知的测试图片，同时验证 HTTP 调用与识别结果。

## 备份与恢复

建议备份：

- `config.yaml`
- `data/`

`cache/` 可重建。WebUI 还提供运行配置导入和导出，但它不能替代完整的
`data/` 备份。

## 更新与回滚

更新：

```bash
docker compose pull
docker compose up -d
```

回滚时把 `ghcr.io/alextoot/subpick:latest` 改为上一个版本标签，再重新创建
容器。不要在容器内执行 `pip install`；依赖与数据库迁移只针对完整镜像组合
进行测试。
