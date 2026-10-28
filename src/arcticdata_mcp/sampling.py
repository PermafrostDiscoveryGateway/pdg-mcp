"""Optional summaries of long free-text metadata via MCP sampling (the client's own LLM writes them).

Sampling is a server-to-client request, so it needs a client that advertises the sampling capability on
a handshake-era protocol (2025-11-25 and earlier). The 2026-07-28 protocol removed the server-to-client
back-channel and deprecated sampling (SEP-2577); on those sessions, and whenever the client declines or
fails, callers fall back to the full text. Keep all sampling here so it is easy to drop later.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from fastmcp import Context
from mcp import types
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

# Sections shorter than this are returned as-is: a summary would save little and risk losing detail.
MIN_CHARS = 4000
# Upper bound on the text sent to the client's model in one request.
MAX_INPUT_CHARS = 100_000
SYSTEM_PROMPT = (
    "You summarize one section of a scientific dataset's metadata for a researcher deciding whether and how "
    "to use the data. Keep exact numbers, units, instrument and model names, dates, places and methods. "
    "Do not add information that is not in the text. Plain prose or short bullet points, no preamble."
)
NOTE = (
    "Model-generated summary written by the client's LLM via MCP sampling. It may omit or misstate details; "
    "call again with summarize=false for the original text."
)


@dataclass
class Summary:
    text: str | None
    status: str
    model: str | None = None
    input_chars: int = 0


def unavailable_reason(context: Context | None) -> str | None:
    """Why this session cannot sample, or None if it can."""
    rc = context.request_context if context else None
    if rc is None:
        return "no_client_session"
    if rc.protocol_version in MODERN_PROTOCOL_VERSIONS:
        return f"protocol_{rc.protocol_version}_has_no_sampling"
    if not context.session.check_client_capability(types.ClientCapabilities(sampling=types.SamplingCapability())):
        return "client_does_not_support_sampling"
    return None


async def summarize(context: Context | None, text: str, *, section: str, pid: str, max_words: int) -> Summary:
    """Ask the client's model for a summary of ``text``; never raises for client-side failures."""
    if reason := unavailable_reason(context):
        return Summary(None, reason)
    excerpt = text[:MAX_INPUT_CHARS]
    message = types.SamplingMessage(
        role="user",
        content=types.TextContent(
            type="text",
            text=f"Summarize the '{section}' section of the metadata for dataset {pid} in at most {max_words} words.\n\n{excerpt}",
        ),
    )
    try:
        # create_message carries a deprecation warning for the 2026-07-28 era; we only reach it on older sessions.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = await context.session.create_message(
                [message],
                max_tokens=max_words * 2 + 100,
                system_prompt=SYSTEM_PROMPT,
                temperature=0,
                related_request_id=context.request_id,
            )
    except MCPError as exc:
        return Summary(None, f"sampling_failed: {exc.message}")
    content = result.content
    if not isinstance(content, types.TextContent) or not content.text.strip():
        return Summary(None, "sampling_returned_no_text")
    return Summary(content.text.strip(), "summarized", result.model, len(excerpt))
