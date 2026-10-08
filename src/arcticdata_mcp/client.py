"""Async HTTP client for the DataONE Member Node (Arctic Data Center) and Coordinating Node APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
import xmltodict
from fastmcp.exceptions import ToolError

from arcticdata_mcp.config import Settings

USER_AGENT = "arcticdata-mcp/0.1 (+https://arcticdata.io)"


def quote_pid(pid: str) -> str:
    """URL-encode a DataONE identifier for use as a path segment."""
    return quote(pid, safe="")


def _strip_ns(key: str) -> str:
    if key.startswith("@"):
        return "@" + key[1:].split(":")[-1]
    return key.split(":")[-1]


def clean_xml(obj: Any) -> Any:
    """Remove namespace prefixes and xmlns attributes from an xmltodict structure."""
    if isinstance(obj, dict):
        return {
            _strip_ns(k): clean_xml(v)
            for k, v in obj.items()
            if not (k.startswith("@xmlns") or k == "@xmlns")
        }
    if isinstance(obj, list):
        return [clean_xml(v) for v in obj]
    return obj


def parse_xml(text: str) -> dict[str, Any]:
    """Parse XML into a plain dict, unwrapping the root element."""
    parsed = clean_xml(xmltodict.parse(text, disable_entities=True))
    if isinstance(parsed, dict) and len(parsed) == 1:
        return next(iter(parsed.values())) or {}
    return parsed


def as_list(value: Any) -> list[Any]:
    """xmltodict returns a scalar for single children and a list for many; normalize to list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _raise_for_dataone(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    name, description = f"HTTP {resp.status_code}", resp.text[:500]
    try:
        err = xmltodict.parse(resp.text, disable_entities=True).get("error", {})
        name = err.get("@name", name)
        description = (err.get("description") or description).strip()
    except Exception:
        pass
    raise ToolError(f"DataONE {name} ({resp.status_code}): {description}")


class DataONEClient:
    """Thin wrapper over httpx for the DataONE REST endpoints used by the tools."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.base_url = settings.arcticdata.base_url.rstrip("/")
        self.cn_url = settings.arcticdata.cn_url.rstrip("/")
        self.catalog_url = settings.arcticdata.catalog_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=settings.arcticdata.timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # URL builders ---------------------------------------------------------
    def mn(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def cn(self, path: str) -> str:
        return f"{self.cn_url}/{path.lstrip('/')}"

    def object_url(self, pid: str) -> str:
        return self.mn(f"object/{quote_pid(pid)}")

    def landing_url(self, pid: str) -> str:
        return f"{self.catalog_url}/{quote_pid(pid)}"

    # Requests -------------------------------------------------------------
    async def request(
        self, method: str, url: str, *, raise_errors: bool = True, **kwargs: Any
    ) -> httpx.Response:
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Request to {url} failed: {exc}") from exc
        if raise_errors:
            _raise_for_dataone(resp)
        return resp

    async def get_xml(self, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self.request("GET", url, **kwargs)
        return parse_xml(resp.text) if resp.text.strip() else {}

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.request("GET", url, **kwargs)).text

    async def head(self, url: str) -> httpx.Response:
        return await self.request("HEAD", url)

    async def solr(self, params: list[tuple[str, Any]]) -> dict[str, Any]:
        """Run a Solr query against the MN query endpoint and return the JSON response."""
        params = [*params, ("wt", "json")]
        resp = await self.request("GET", self.mn("query/solr/"), params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolError(f"Solr returned a non-JSON response: {resp.text[:300]}") from exc

    @asynccontextmanager
    async def stream(self, url: str) -> AsyncIterator[httpx.Response]:
        try:
            async with self._http.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    _raise_for_dataone(resp)
                yield resp
        except httpx.HTTPError as exc:
            raise ToolError(f"Request to {url} failed: {exc}") from exc
