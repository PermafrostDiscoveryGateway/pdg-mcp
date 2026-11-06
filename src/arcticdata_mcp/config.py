"""Configuration loading: a TOML file with environment-variable overrides.

Environment variables use the form ``ARCTICDATA_MCP_<SECTION>_<KEY>``, e.g.
``ARCTICDATA_MCP_AUTH_REALM_URL`` or ``ARCTICDATA_MCP_DOWNLOADS_MAX_INLINE_BYTES``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

ENV_PREFIX = "ARCTICDATA_MCP_"


@dataclass
class AuthSettings:
    realm_url: str = ""  # e.g. https://keycloak.software-dev.ncsa.illinois.edu/realms/PDG
    base_url: str = "http://127.0.0.1:8000"  # public URL of this MCP server (used in OAuth discovery)
    audience: str = ""  # expected "aud" claim; empty disables the check
    required_role: str = "mcp-user"  # Keycloak realm role an admin grants to approve a user; empty = any user
    required_scopes: list[str] = field(default_factory=lambda: ["openid"])


@dataclass
class ServerSettings:
    transport: str = "http"
    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"


@dataclass
class ArcticDataSettings:
    base_url: str = "https://arcticdata.io/metacat/d1/mn/v2"
    cn_url: str = "https://cn.dataone.org/cn/v2"
    catalog_url: str = "https://arcticdata.io/catalog/view"
    timeout: float = 60.0


@dataclass
class DownloadSettings:
    max_inline_bytes: int = 10 * 1024 * 1024
    allow_server_save: bool = False
    download_dir: str = "./downloads"


@dataclass
class RateLimitSettings:
    max_concurrent: int = 6  # global cap on in-flight requests to ADC/DataONE; 0 = unlimited
    queue_timeout: float = 30.0  # seconds a request waits for a free slot before failing
    per_user_rate: float = 2.0  # sustained tool calls per second per user (Keycloak "sub"); 0 = unlimited
    per_user_burst: int = 20  # tool calls a user can make back-to-back before the rate applies
    max_retries: int = 3  # retries on 429/502/503/504 from ADC; 0 = no retries
    retry_backoff: float = 1.0  # base delay in seconds, doubled per attempt (Retry-After wins if sent)
    retry_max_delay: float = 30.0  # upper bound on a single retry delay


@dataclass
class WaterSettings:
    # Directory holding lakes.geojson and timeseries.json (see scripts/export_water_fixtures.py).
    # Empty = the small fixture set bundled with the package.
    data_dir: str = ""


@dataclass
class PortalSettings:
    # Public landing page of a portal is <portal_url>/<label>.
    portal_url: str = "https://arcticdata.io/catalog/portals"
    cache_ttl: float = 3600.0  # seconds to keep the portal list and dataset counts; 0 = refetch every call
    exclude_labels: list[str] = field(default_factory=list)  # portal labels to hide (test or spam portals)


@dataclass
class ObservabilitySettings:
    log_level: str = "INFO"  # level for the arcticdata_mcp loggers; tool-call records are logged at INFO
    log_format: str = "json"  # "json" (one object per line, for log shippers) or "text" (human readable)
    log_tool_calls: bool = True  # emit one structured record per tool call
    # Secret key for the HMAC that turns the Keycloak "sub" into the user_hash in logs. Keep it stable and
    # identical across pods so one user keeps one hash; changing it makes past and future hashes unlinkable.
    user_hash_key: str = ""
    # Prometheus metrics are served on their own port, never on the MCP port, so the Service/Ingress that expose
    # the MCP endpoint cannot reach them. Use 0.0.0.0 in a container so a scraper can reach the pod.
    metrics_enabled: bool = True  # http transport only; stdio never opens a port
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 9100


@dataclass
class Settings:
    auth: AuthSettings = field(default_factory=AuthSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    arcticdata: ArcticDataSettings = field(default_factory=ArcticDataSettings)
    downloads: DownloadSettings = field(default_factory=DownloadSettings)
    ratelimit: RateLimitSettings = field(default_factory=RateLimitSettings)
    water: WaterSettings = field(default_factory=WaterSettings)
    portals: PortalSettings = field(default_factory=PortalSettings)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)


def _coerce(value: str, target: type) -> Any:
    if target is bool:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if target is int:
        return int(value)
    if target is float:
        return float(value)
    if target is list:
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from ``path`` (if it exists), then apply env overrides."""
    data: dict[str, dict[str, Any]] = {}
    if path is not None:
        p = Path(path).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Config file not found: {p}")
        data = tomllib.loads(p.read_text())

    settings = Settings()
    for section in fields(settings):
        section_obj = getattr(settings, section.name)
        section_data = data.get(section.name, {})
        for f in fields(section_obj):
            ftype = type(getattr(section_obj, f.name))
            if f.name in section_data:
                setattr(section_obj, f.name, ftype(section_data[f.name]))
            env_key = f"{ENV_PREFIX}{section.name}_{f.name}".upper()
            if env_key in os.environ:
                setattr(section_obj, f.name, _coerce(os.environ[env_key], ftype))
    return settings
