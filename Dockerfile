# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# tzdata gives glibc's localtime() (and hence `%(asctime)s` in logs) access
# to named zones via the TZ env var; python:3.12-slim doesn't include it.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ src/

RUN uv sync --frozen --no-dev

ENV MANAGER_MCP_TRANSPORT=http
ENV MANAGER_MCP_HTTP_HOST=0.0.0.0
ENV MANAGER_MCP_HTTP_PORT=8080
# feeds_config.py defaults to /secrets/manager/feeds.config, which only
# exists as a writable mount in the compose cluster (shared ./secrets/manager
# bind mount). A standalone container has no such mount, so default the
# bank-feed config into the app's own writable WORKDIR instead; compose.yaml
# overrides this back to the shared secrets path for cluster deployments.
ENV MANAGER_MCP_BANK_FEED_CONFIG_PATH=/app/feeds.config
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python3 -c "import socket; socket.create_connection(('127.0.0.1', 8080), timeout=3)"

CMD ["uv", "run", "manager-mcp"]
