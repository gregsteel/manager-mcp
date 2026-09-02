# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --frozen --no-dev

ENV MANAGER_MCP_TRANSPORT=http
ENV MANAGER_MCP_HTTP_HOST=0.0.0.0
ENV MANAGER_MCP_HTTP_PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=3)"

CMD ["uv", "run", "manager-mcp"]
