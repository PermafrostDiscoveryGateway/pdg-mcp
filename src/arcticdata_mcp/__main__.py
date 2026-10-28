"""Command-line entry point: ``arcticdata-mcp --config config.toml``."""

from __future__ import annotations

import argparse
import sys

from arcticdata_mcp.config import load_settings
from arcticdata_mcp.server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MCP server for the Arctic Data Center (DataONE) API")
    parser.add_argument("--config", "-c", default=None, help="Path to config.toml (default: ./config.toml if present)")
    parser.add_argument("--transport", choices=["http", "stdio"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--insecure", action="store_true", help="Allow HTTP transport without authentication (NOT recommended)")
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        help="Allow browser calls from this origin, e.g. http://localhost:8080 for the ext-apps basic-host (repeatable)",
    )
    args = parser.parse_args(argv)

    config_path = args.config
    if config_path is None:
        from pathlib import Path

        config_path = "config.toml" if Path("config.toml").is_file() else None
    settings = load_settings(config_path)
    transport = args.transport or settings.server.transport
    host = args.host or settings.server.host
    port = args.port or settings.server.port

    if transport == "stdio":
        create_server(settings, require_auth=False).run(transport="stdio")
        return

    if not settings.auth.realm_url and not args.insecure:
        sys.exit(
            "Refusing to start: no Keycloak realm configured. Set [auth] realm_url in config.toml "
            "or ARCTICDATA_MCP_AUTH_REALM_URL (or pass --insecure for local testing)."
        )
    server = create_server(settings, require_auth=bool(settings.auth.realm_url))
    extra: dict = {}
    if args.cors_origin:
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware

        extra["middleware"] = [
            Middleware(
                CORSMiddleware,
                allow_origins=args.cors_origin,
                allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                allow_headers=["*"],
                expose_headers=["mcp-session-id"],
            )
        ]
        extra["allowed_origins"] = args.cors_origin
    server.run(transport="http", host=host, port=port, path=settings.server.path, **extra)


if __name__ == "__main__":
    main()
