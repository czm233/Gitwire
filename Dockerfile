# ---------- 阶段 1：构建前端 ----------
FROM node:22-alpine AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 2：后端 + 前端产物 ----------
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations

COPY --from=fe /fe/dist ./frontend/dist

ENV DATA_DIR=/app/data
RUN useradd --uid 10001 --create-home gitwire \
    && mkdir -p /app/data && chown gitwire:gitwire /app/data
USER gitwire
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
