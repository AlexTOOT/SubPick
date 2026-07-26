FROM python:3.12.13-slim-bookworm

LABEL org.opencontainers.image.title="SubPick" \
      org.opencontainers.image.description="A lightweight MoviePilot subtitle sidecar" \
      org.opencontainers.image.source="https://github.com/AlexTOOT/SubPick" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        mkvtoolnix \
        7zip \
        unar \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install uv==0.11.31

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY src /app/src
RUN uv sync --frozen --no-dev --no-editable

EXPOSE 19035

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:19035/api/v1/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "subtitle_sidecar.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "19035", "--no-access-log"]
