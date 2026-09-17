FROM ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 AS uv
FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84 AS build
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install --no-install-recommends -y build-essential git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/msu_hub_bot
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses/ licenses/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg tesseract-ocr tesseract-ocr-rus libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hubbot && useradd --uid 10001 --gid 10001 --no-create-home hubbot \
    && mkdir /work && chown 10001:10001 /work
COPY --from=build /opt/msu_hub_bot/.venv /opt/msu_hub_bot/.venv
COPY --from=build /opt/msu_hub_bot/LICENSE /opt/msu_hub_bot/THIRD_PARTY_NOTICES.md /opt/msu_hub_bot/
COPY --from=build /opt/msu_hub_bot/licenses /opt/msu_hub_bot/licenses
ENV PATH=/opt/msu_hub_bot/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DOCKER_MODE=1
WORKDIR /work
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 CMD ["python", "-m", "msu_hub_bot.health"]
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/msu_hub_bot/.venv/bin/msu-hub-bot"]
