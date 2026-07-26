# Third-party notices

## Bazarr

Zimuku Provider 的设计与行为参考了
[morpheus65535/bazarr](https://github.com/morpheus65535/bazarr) 中的 Zimuku
Provider。Bazarr 使用 GNU General Public License v3.0。

拾幕的实现已针对独立 Provider Adapter 接口、任务调度、字幕包缓存、日志与
校验管线进行修改。

## MoviePilot OCR

示例 Compose 使用
[jxxghp/MoviePilot-OCR](https://github.com/jxxghp/MoviePilot-OCR) 提供的
`jxxghp/moviepilot-ocr` 镜像。该服务是独立容器，不包含在拾幕镜像中。

## Subtitle services

ASSRT 字幕服务由 [assrt.net](https://assrt.net/) 提供。其他字幕内容与服务由
对应 Provider 提供，并受各自条款约束。
