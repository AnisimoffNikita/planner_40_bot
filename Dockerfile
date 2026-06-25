# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 meetingbot \
    && useradd --uid 10001 --gid meetingbot --create-home meetingbot

WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install /tmp/*.whl && rm -f /tmp/*.whl
COPY alembic.ini ./
COPY alembic ./alembic
RUN mkdir -p /app/data /app/config \
    && chown -R meetingbot:meetingbot /app

USER meetingbot
CMD ["python", "-m", "meeting_bot", "--app-config", "/app/config/app.yaml", "--meeting-schema", "/app/config/service_schema.yaml"]

FROM runtime AS test
USER root
COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY service_schema.yaml ./service_schema.yaml
RUN python -m pip install ".[dev]"
USER meetingbot
CMD ["pytest"]
