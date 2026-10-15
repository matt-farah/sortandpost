# syntax=docker/dockerfile:1
# Base images are pinned by digest (docs/security-review.md L2). To update: bump the tag, then
#   docker buildx imagetools inspect <image>:<tag>   -> paste the new "Digest:".
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

# System deps:
#  - exiftool: the only tool allowed to write metadata into photo files
#  - libraw-bin / libheif1: runtime libs for rawpy (RAW) and pillow-heif (HEIC);
#    the wheels normally bundle their own copies, these are a safety net
#  - curl: the compose healthcheck
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        libraw-bin \
        libheif1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv

# Install from uv.lock, like the desktop builds (`uv sync --locked`). Linux torch
# comes from PyTorch's CPU index (pyproject [tool.uv.sources]), not PyPI's CUDA build.
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

# 1) Dependencies only: this layer is reused until pyproject.toml / uv.lock change,
#    so editing the code doesn't re-download torch.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# 2) The app itself (a hatchling package; the build needs src/ + README + LICENSE).
COPY README.md LICENSE alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Runs as an unprivileged user (see docker-entrypoint.sh: PUID / PGID). The
# directories exist so a fresh named volume for /data/local starts out owned by it.
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid app --no-create-home app \
    && mkdir -p /data/local /data/sync \
    && chown app:app /data/local /data/sync
COPY packaging/docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# CLIP weights cache lives on the local/ volume, not in the image.
# HOST=0.0.0.0: inside the container; compose publishes it on 127.0.0.1 by default.
ENV PATH=/app/.venv/bin:$PATH \
    OPEN_CLIP_CACHE_DIR=/data/local/models \
    HF_HOME=/data/local/models \
    HOME=/tmp \
    APP_DATA=/data \
    HOST=0.0.0.0 \
    PORT=8080

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "sortandpost"]
