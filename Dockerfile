FROM ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 AS uv
FROM node:24.21.0-bookworm-slim@sha256:2fe369e969550cde8e867afc3fe370b260140cab4a23d467074295b42163d553 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

# Layers are ordered by how often they change, so a code-only release ships a
# few megabytes: system packages, locked dependencies, the model, then the app.
FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS deps
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/msu_hub_bot
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project \
    && touch /opt/deps-installed

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS model
COPY src/msu_hub_bot/media/background_model.py /model/background_model.py
RUN python /model/background_model.py fetch

FROM deps AS build
COPY src/ src/
COPY --from=web /web/dist/ src/msu_hub_bot/web/static/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
# Stage only what installing the project added to the dependency environment.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable \
    && mkdir /project \
    && find .venv -newer /opt/deps-installed \( -type f -o -type l \) -print0 | tar --null -cf - -T - | tar -xf - -C /project

FROM python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg tesseract-ocr tesseract-ocr-rus libfribidi0 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hubbot && useradd --uid 10001 --gid 10001 --no-create-home hubbot \
    && mkdir /work && mkdir -m 0700 /data && chown 10001:10001 /work /data
COPY LICENSE THIRD_PARTY_NOTICES.md /opt/msu_hub_bot/
COPY licenses/ /opt/msu_hub_bot/licenses/
COPY --from=deps /opt/msu_hub_bot/.venv /opt/msu_hub_bot/.venv
COPY --from=model /model/_models/ /opt/msu_hub_bot/.venv/lib/python3.14/site-packages/msu_hub_bot/media/_models/
COPY --from=build /project/.venv/ /opt/msu_hub_bot/.venv/
ENV PATH=/opt/msu_hub_bot/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOCKER_MODE=1 DENO_DIR=/tmp/deno
WORKDIR /work
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --start-interval=2s --retries=3 CMD ["python", "-m", "msu_hub_bot.health"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"]
