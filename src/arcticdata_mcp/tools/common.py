"""Shared parameter types and annotations for tool definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from arcticdata_mcp.client import DataONEClient
from arcticdata_mcp.config import Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)


@dataclass
class ToolContext:
    settings: Settings
    client: DataONEClient


Rows = Annotated[int, Field(ge=0, le=100, description="Number of results to return (max 100).")]
Start = Annotated[int, Field(ge=0, description="Zero-based offset for paging through results.")]
Sort = Annotated[
    Literal["relevance", "newest", "oldest", "published_newest", "title", "size_desc", "size_asc"],
    Field(description="Result ordering."),
]
LatestOnly = Annotated[
    bool,
    Field(description="Only return the latest version of each object (exclude obsoleted versions)."),
]
Pid = Annotated[
    str,
    Field(description="DataONE identifier (PID), e.g. 'doi:10.18739/A2...' or 'urn:uuid:...'."),
]
DateStr = Annotated[
    str | None,
    Field(description="Date as YYYY, YYYY-MM, YYYY-MM-DD, ISO-8601, or Solr date math like 'NOW-1YEAR'."),
]
