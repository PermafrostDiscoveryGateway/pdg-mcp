FROM python:3.12-slim-bookworm AS build
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The daily usage-archive CronJob (charts/pdg-mcp, report.enabled) runs this script from the app image.
COPY scripts/usage_report.py ./scripts/usage_report.py
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" ARCTICDATA_MCP_SERVER_HOST=0.0.0.0
USER 10001
EXPOSE 8000
CMD ["arcticdata-mcp"]
