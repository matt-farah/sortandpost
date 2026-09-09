# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System deps:
#  - exiftool: the only tool allowed to write metadata into photo files
#  - libraw-bin / libheif1: runtime libs for rawpy (RAW) and pillow-heif (HEIC);
#    the wheels normally bundle their own copies, these are a safety net
#  - curl: kept for container debugging / a future compose healthcheck
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        libraw-bin \
        libheif1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# The project is a hatchling package (packages = ["src/sortandpost"]), so
# building its wheel needs src/ present *before* the install -- the previous
# ordering ran `uv pip install .` with only pyproject.toml copied and failed.
# One combined install step means editing any file busts the dependency layer
# on rebuild; that's an acceptable tradeoff for a single-container app.
# --torch-backend=cpu keeps the image off the ~2 GB CUDA torch wheel (this app
# is CPU-only per CLAUDE.md). If an older uv rejects that flag, swap in:
#   --extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN uv pip install --system --no-cache --torch-backend=cpu .

# CLIP weights cache lives on the mounted app-data volume, not in the image
ENV OPEN_CLIP_CACHE_DIR=/data/local/models \
    HF_HOME=/data/local/models \
    APP_DATA=/data

EXPOSE 8080

CMD ["python", "-m", "sortandpost"]
