# Provider Adapter 接口

拾幕把每个字幕来源实现为独立适配器。主程序只依赖统一协议，不直接依赖站点 API、
网页结构或认证方式。

## 核心协议

适配器需要提供：

- 稳定的 `name`；
- `search(request)`：接收统一媒体信息并返回候选；
- `download(candidate, target_dir)`：下载候选并返回一个或多个字幕文件；
- 可选的 `set_reporter(reporter)`：报告每次检索策略、耗时、数量和错误。

工厂需要提供 `ProviderAdapterMetadata`，声明版本、媒体类型、查询字段、传输方式、
字幕包能力和候选身份是否稳定。内置工厂注册在
`subtitle_sidecar.providers.adapters`，主程序通过注册表按配置顺序加载。

```python
from subtitle_sidecar.providers.base import ProviderAdapterMetadata


class ExampleAdapterFactory:
    metadata = ProviderAdapterMetadata(
        name="example",
        version="1.0.0",
        homepage="https://example.com",
        media_types=("movie", "episode"),
        query_fields=("imdb", "title", "original_title", "filename"),
        transport="api",
        supports_bundles=False,
        stable_candidate_identity=True,
    )

    def create(self, settings):
        return ExampleProvider(settings)
```

## 边界约束

- 适配器自行处理认证、限速、配额和站点错误，但不得绕过付费墙或访问控制。
- API Key、密码和验证码答案不得写入日志、候选元数据或数据库明文字段。
- 候选应尽量提供稳定的站点 ID 或公开详情页 URL，供重试黑名单识别。
- 压缩包必须限制成员数量、总大小并阻止路径穿越。
- 站点特有规则留在适配器内；通用排序、校验、对轴和落库由主流程负责。

当前内置适配器与主镜像一起发布，不提供独立安装或在线更新。
