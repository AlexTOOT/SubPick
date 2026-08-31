from importlib.metadata import PackageNotFoundError, version


try:
    __version__ = version("subtitle-sidecar")
except PackageNotFoundError:  # pragma: no cover - source tree without an installed package
    __version__ = "0.0.0"

# These are interface versions rather than upstream package versions. They make
# adapter compatibility visible without coupling the core to a provider's
# release cadence.
ADAPTER_VERSIONS = {
    "subliminal": "1.0.0",
    "assrt": "1.3.0",
    "subdl": "2.0.0",
    "zimuku": "1.2.1",
}

DATABASE_SCHEMA_VERSION = 4
RUNTIME_METADATA_SETTING_KEY = "runtime_metadata"
