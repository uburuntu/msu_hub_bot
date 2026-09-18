FROM ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 AS uv
FROM node:24.21.0-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/msu_hub_bot
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY --from=web /web/dist/ src/msu_hub_bot/web/static/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses/ licenses/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg tesseract-ocr tesseract-ocr-rus ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hubbot && useradd --uid 10001 --gid 10001 --no-create-home hubbot \
    && mkdir /work && chown 10001:10001 /work
COPY --from=build /opt/msu_hub_bot/.venv /opt/msu_hub_bot/.venv
COPY --from=build /opt/msu_hub_bot/LICENSE /opt/msu_hub_bot/THIRD_PARTY_NOTICES.md /opt/msu_hub_bot/
COPY --from=build /opt/msu_hub_bot/licenses /opt/msu_hub_bot/licenses
ENV PATH=/opt/msu_hub_bot/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOCKER_MODE=1 DENO_DIR=/tmp/deno
WORKDIR /work
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 CMD ["python", "-m", "msu_hub_bot.health"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"]
