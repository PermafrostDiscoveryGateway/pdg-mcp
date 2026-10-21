FROM python:3.12-slim-bookworm AS build
RUN pip install --no-cache-dir uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" ARCTICDATA_MCP_SERVER_HOST=0.0.0.0
USER 10001
EXPOSE 8000
CMD ["arcticdata-mcp"]
