# syntax=docker/dockerfile:1
FROM python:3.12-slim

# System deps:
#  - exiftool: the only tool allowed to write metadata into photo files
#  - libraw + build tools: rawpy wheels usually bundle libraw, but keep headers
#    available for source builds on arm64
#  - libheif: HEIC decode via pillow-heif
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

# Install dependencies first for layer caching
COPY pyproject.toml README.md ./
RUN uv pip install --system --no-cache .

# App source
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN uv pip install --system --no-cache -e . --no-deps

# CLIP weights cache lives on the mounted app-data volume, not in the image
ENV OPEN_CLIP_CACHE_DIR=/data/local/models \
    HF_HOME=/data/local/models \
    APP_DATA=/data

EXPOSE 8080

CMD ["python", "-m", "sortandpost"]
