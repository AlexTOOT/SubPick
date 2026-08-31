<p align="center">
  <img src="docs/assets/subpick-logo.svg" width="132" alt="拾幕 SubPick Logo">
</p>

<h1 align="center">拾幕 SubPick</h1>

<p align="center"><strong>让每一部影片，都有合适的字幕。</strong></p>

<p align="center">
  <a href="https://github.com/AlexTOOT/SubPick/releases">版本发布</a> ·
  <a href="https://github.com/AlexTOOT/SubPick/pkgs/container/subpick">Docker 镜像</a> ·
  <a href="docs/deployment/nas-docker.md">完整部署说明</a>
</p>

拾幕是面向 MoviePilot + Jellyfin 用户的轻量中文字幕服务，可作为
ChineseSubFinder 的替代方案。它接收 MoviePilot 入库通知，也可以浏览 Jellyfin
媒体库并手动创建字幕任务。影片身份统一读取 MoviePilot 生成的本地 NFO；Jellyfin
只负责媒体库浏览、海报、目录选择、字幕状态和下载后的刷新。

- 支持内嵌、外挂字幕检查及字幕对轴
- 支持电影、整剧、整季和单集批量操作
- 内置 Subliminal、ASSRT、SubDL、Zimuku 字幕来源
- 提供候选排查、任务重试、媒体库状态和系统日志
- 配置、数据库与缓存均自动保存在一个主目录中

> 当前版本仍处于早期阶段，建议先用少量媒体验证后再批量处理。

## 界面预览

![拾幕运行概览](docs/assets/subpick-overview.png)

## 快速部署

1. 在 NAS 上新建一个空目录，例如 `SubPick`。
2. 在 Docker 管理器中新建 Compose 项目，粘贴下面的内容。
3. 修改 `/volume1/SubPick` 和 `/volume1/media` 为 NAS 上的实际目录并启动。

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

也可以下载仓库中的 [compose.yaml](compose.yaml)。首次启动会在主目录内自动创建
`config.yaml`、`data` 和 `cache`，无需提前准备这些文件或目录。

打开 `http://<NAS-IP>:19035/`，按首次使用向导完成以下配置：

1. 填写 Jellyfin 地址和 API Key，并测试连接。
2. 启用需要的 Provider，填写对应凭据并检查可用性。
3. 将拾幕生成的 MoviePilot API Token 填入 ChineseSubFinder 插件。
4. 扫描 Jellyfin 媒体库，确认字幕状态。

ASSRT 会读取候选详情和下载统计以提高排序质量。由于官方 API 限速较低，启用后
一次搜索可能需要更长时间，这是质量优先的正常行为。

## MoviePilot 对接

继续使用 MoviePilot 的 ChineseSubFinder 插件：

- 服务地址：`http://<NAS-IP>:19035`
- API Key：与“拾幕 → 设置 → MoviePilot API Token”保持一致

推荐让 MoviePilot 和拾幕把同一个 NAS 媒体根目录映射为 `/media`。Jellyfin 的容器
路径无需完全相同；拾幕通过自己的 `/media` 读取媒体文件和 NFO，通过 Jellyfin 展示
媒体库并取得手动任务所选文件的目录。
如果 MoviePilot 下发的路径无法访问，运行概览会提示问题，可在“设置 → 系统与更新 →
目录映射”中测试并保存转换规则。

MoviePilot 回调可能早于 NFO 落盘。拾幕会等待 NFO 完成后再搜索字幕；电影优先使用
`movie.nfo`，剧集使用 `tvshow.nfo`，并结合有效分集 NFO 或文件名中的 `SxxExx`。

## 媒体身份识别机制

MoviePilot 通知和 Jellyfin 手动创建任务只有入口不同，取得媒体文件路径后会进入同一条
处理管线：

```text
MoviePilot 回调 ─┐
                 ├─> 解析本地路径 -> 读取 NFO -> 搜索、筛选、校验字幕 -> 写入媒体目录
Jellyfin 手动任务 ┘                                             └-> 刷新 Jellyfin 状态（可选）
```

媒体目录名和 Jellyfin 元数据不参与标题、年份或外部 ID 的补全。目录名只有中文名和年份
也没有关系，只要媒体旁的 NFO 完整，拾幕就能取得统一身份：

- 电影以 `movie.nfo` 为权威来源；MoviePilot 回调到达较早时，最多等待 90 秒让
  `movie.nfo` 取代同名 NFO。最终至少需要标题和年份，可同时读取原始标题以及
  IMDb、TMDb、TVDb ID。
- 剧集以向上查找到的 `tvshow.nfo` 确定剧名、首播年和外部 ID。季号、集号优先取
  分集同名 NFO；缺少有效分集 NFO 时才从媒体文件名的 `SxxExx` 读取。两者明确冲突
  时拒绝任务，不猜测其中一个。
- `season.nfo` 和分集 NFO 中的有效发行年份会作为剧集的备选年份；候选命中剧集
  首播年、当季年份或分集年份中的任意一个都可以继续校验。
- 标题、年份或必要季集信息不完整时，不回退使用 Jellyfin 的同名字段。MoviePilot
  入口会在任务创建后的 2 分钟内每 30 秒等待一次有效 NFO，超过期限后明确失败并
  交给有限自动重试；Jellyfin 手动入口通常发生在入库完成后，因此直接报告 NFO
  问题，便于修正媒体整理结果。

成功解析的 NFO 身份会冻结在当前任务记录中，当前任务的搜索、筛选和落库始终使用
同一份身份。自动或手动重试会创建新任务并重新读取当前 NFO，同时继承候选历史，
以便修正 NFO 后恢复任务，又不会重复使用已经失败的字幕。

## Jellyfin 的角色

Jellyfin 是拾幕的媒体库界面和操作入口，不是媒体身份的权威来源：

- 提供媒体库、海报、层级浏览、最近入库内容以及手动选择电影或分集；
- 向手动任务提供媒体文件路径，拾幕再从该路径读取 NFO；
- 缓存已有内嵌/外挂中文字幕状态，并在字幕写入后刷新对应项目；
- Jellyfin 暂时不可用时，MoviePilot 回调仍可依靠可访问的媒体路径和本地 NFO 完成
  字幕任务，但海报墙、Jellyfin 手动任务和状态刷新会不可用。

因此，MoviePilot 回调携带的 Jellyfin ID 可以用于关联和刷新，但不是开始识别或搜索
字幕的前置条件；也无需等待 Jellyfin 为新媒体生成 ID。

## 字幕筛选与落库规则

拾幕按设置中的字幕源优先级逐个搜索；某个来源找到并成功落库后停止，不把不同来源
的分数混在一起比较。每个来源内部先排除明确错误的候选，再按该站点可提供的质量数据
排序。各站点的具体查询回退顺序见[字幕源搜索策略](docs/providers/search-strategy.md)。

下载前的身份筛选包括：

- 使用站点作品页或 API 返回的稳定作品标题比较中文标题和原始标题；发布组文件名只
  作为辅助信息，避免把通用文件名误当成影片名称；
- 电影候选年份默认允许相差 1 年，以兼容影展、院线和流媒体发行年份差异；超出范围
  的明确年份冲突会被拒绝；
- 剧集候选如果明确标注季号或集号，就必须与目标一致；整季包可以进入后续的单集
  文件选择。明显属于长片的剧集候选以及明显属于剧集的电影候选会被拒绝；
- 排序以字幕源自身的下载量、评分或可信度为主要信号，再考虑季集适配、片源规格和
  语言偏好。整季包略优先于同质量单集；双语加分只在原生质量接近最佳候选时生效，
  不会让低质量双语字幕压过明显更可靠的中文候选。

下载后还会继续检查：

- 压缩包路径和大小安全、字幕扩展名、文本编码、字幕结构以及是否确实包含中文；
- 整季包内文件是否对应目标集，以及同一剧集中不同集数是否错误复用了相同正文；
- 字幕结束时间是否明显超过视频，或只覆盖视频前半段；
- 启用对轴时使用 ffsubsync 检查时间轴质量。对轴分数只表示语音活动时间线是否容易
  对齐，不能证明影片身份，因此它始终位于标题、年份和季集校验之后。

单次任务最多尝试配置数量的候选（默认 4 个）。失败任务按原因分类进行有限自动重试，
例如身份问题、网络错误、无候选和时间轴质量分别使用不同的退避间隔；最多自动重试
3 次，部分错误类别更少，此后停止，等待人工检查或手动重试。手动重试会排除此前
使用过的稳定候选或相同字幕正文，并继续尝试其他候选和字幕源。

## Zimuku 与 OCR

上面的 Compose 会同时启动本地 MoviePilot OCR，拾幕默认通过
`http://moviepilot-ocr:9899` 使用它。启用 Zimuku 后，请在 Provider 设置中执行一次
OCR 实图检查。

## 更新与备份

更新镜像：

```bash
docker compose pull
docker compose up -d
```

配置、任务记录和缓存均位于映射到 `/appdata` 的主目录。备份时至少保留
`config.yaml` 和 `data`；`cache` 可按需重建。WebUI 也支持导入、导出运行配置。

## 更多文档

- [Docker / NAS 部署](docs/deployment/nas-docker.md)
- [字幕源搜索策略](docs/providers/search-strategy.md)
- [Zimuku 与本地 OCR](docs/providers/zimuku.md)
- [字幕校验与对轴流程](docs/workflows/subtitle-alignment.md)
- [Provider Adapter 接口](docs/provider-adapter-api.md)
- [发布与更新策略](docs/architecture/release-and-update-strategy.md)
- [路线图](ROADMAP.md)

## 许可证

拾幕使用 [GNU GPL v3 或更高版本](LICENSE) 发布。字幕内容及字幕站点服务受各自条款
约束，请仅下载和使用你有权访问的内容。

## 参考与致谢

拾幕的设计和实现参考或使用了以下项目与服务：

- 工作流与媒体库：[MoviePilot](https://github.com/jxxghp/MoviePilot)、[MoviePilot Plugins](https://github.com/jxxghp/MoviePilot-Plugins)、[Jellyfin](https://github.com/jellyfin/jellyfin)
- 项目经验：[ChineseSubFinder](https://github.com/ChineseSubFinder/ChineseSubFinder)、[Bazarr](https://github.com/morpheus65535/bazarr)
- 字幕与对轴组件：[Subliminal](https://github.com/Diaoul/subliminal)、[ffsubsync](https://github.com/smacke/ffsubsync)
- 媒体与压缩工具：[FFmpeg](https://github.com/FFmpeg/FFmpeg)、[MKVToolNix](https://gitlab.com/mbunkus/mkvtoolnix)、[7-Zip](https://github.com/ip7z/7zip)、[The Unarchiver](https://github.com/MacPaw/XADMaster)
- 验证码识别：[MoviePilot OCR](https://github.com/jxxghp/MoviePilot-OCR)
- 字幕服务：[ASSRT](https://assrt.net/)、[SubDL](https://subdl.com/)、[Zimuku](https://zimuku.org/)、[OpenSubtitles.com](https://www.opensubtitles.com/)
- 基础组件：[FastAPI](https://github.com/fastapi/fastapi)、[SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy)、[HTTPX](https://github.com/encode/httpx)、[Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/)、[Pillow](https://github.com/python-pillow/Pillow)

感谢这些项目的作者、维护者和字幕贡献者。
