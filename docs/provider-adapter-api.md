# Provider Adapter API

SubPick keeps its subtitle sources as independent adapters. The core owns task
state, rate-aware scheduling, candidate ranking, validation, syncing and placement; an
adapter owns only provider-specific search and download behavior.

An external adapter may live in its own Git repository and Python package. It registers
one factory through Python packaging:

```toml
[project.entry-points."subtitle_sidecar.providers"]
example = "subtitle_sidecar_example:factory"
```

The exported object implements the public contract in
`subtitle_sidecar.providers.base`:

```python
from collections.abc import Mapping
from typing import Any

from subtitle_sidecar.providers.base import ProviderAdapterMetadata, SubtitleProvider


class ExampleFactory:
    metadata = ProviderAdapterMetadata(
        name="example",
        display_name="Example Subtitles",
        version="1",
        homepage="https://github.com/example/subtitle-sidecar-example",
    )

    def create(self, settings: Mapping[str, Any]) -> SubtitleProvider:
        return ExampleProvider(settings)


factory = ExampleFactory()
```

`SubtitleProvider` must expose a stable `name`, `search(request)` and
`download(candidate, target_dir)`. It should never persist secrets or expiring download
URLs in candidates. Adapters are trusted local Python packages: only install adapters
from sources the NAS administrator trusts. A future core release may add an adapter
manifest for dynamic WebUI form rendering; until then, adapters should document their
own YAML setting schema under `providers.adapters.<adapter-name>`. Built-in adapters may
also offer a dedicated WebUI settings card and secret storage API.
