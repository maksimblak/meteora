# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

COPY --from=node-runtime /usr/local/ /usr/local/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

RUN pip install --no-cache-dir uv==0.12.4

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-extras --no-install-project

COPY executor/package.json executor/package-lock.json ./executor/
RUN npm ci --prefix executor --ignore-scripts

RUN useradd --create-home --uid 10001 app && chown app:app /app /app/executor
COPY --chown=app:app Dockerfile Dockerfile.executor docker-compose.yml .env.example ./
COPY --chown=app:app executor/eslint.config.js executor/tsconfig.json ./executor/
COPY --chown=app:app executor/src ./executor/src
COPY --chown=app:app executor/tests ./executor/tests
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app src ./src
COPY --chown=app:app tests ./tests

USER app

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "src.core.app:app", "--host", "0.0.0.0", "--port", "8000"]
