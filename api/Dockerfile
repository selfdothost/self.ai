# syntax=docker/dockerfile:1
# self.ai API server — FastAPI (selfai_ui), API-only, yard-native slim.
#
# This is NOT the old combined monolith image. It builds the backend only:
# no frontend bundle (the build dir is absent, so selfai_ui logs "Serving API
# only" and traefik routes `/` to the self.chat pod), no torch, no CUDA, no
# build-time embedding/whisper model downloads. Local-ML features (RAG
# embeddings, STT, OCR) are externalized/deferred — see requirements-api.txt.
#
# Result: a few-hundred-MB image, not multi-GB. Build context is this backend
# directory; nothing here builds on the bastion (CI + kaniko -> container.home).

FROM python:3.11-slim-bookworm AS base

ENV ENV=prod \
    PORT=8080 \
    HOST=0.0.0.0 \
    DOCKER=true

# Inference deferred by default + telemetry off. Manifests/env override these.
ENV ENABLE_OLLAMA_API=False \
    ENABLE_OPENAI_API=False \
    SCARF_NO_ANALYTICS=true \
    DO_NOT_TRACK=true \
    ANONYMIZED_TELEMETRY=false

# Caches live under the data volume; no models are baked at build time.
ENV SENTENCE_TRANSFORMERS_HOME="/app/backend/data/cache/embedding/models" \
    HF_HOME="/app/backend/data/cache/embedding/models" \
    WHISPER_MODEL_DIR="/app/backend/data/cache/whisper/models" \
    TIKTOKEN_CACHE_DIR="/app/backend/data/cache/tiktoken"

WORKDIR /app/backend

# Minimal system deps: a build toolchain for the few sdist-only wheels, pandoc
# for document parsing, curl+jq for the healthcheck. Deliberately NO ffmpeg /
# libsm6 / libxext6 (those were for OCR/cv2, dropped) and NO CUDA.
# TODO(slim): move to a multi-stage build to drop build-essential from runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential gcc python3-dev \
      pandoc netcat-openbsd curl jq ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python deps — slim API set (no torch, no model downloads).
# NOTE: the CI build runs kaniko with --cache=false. kaniko keys RUN layers on
# the command STRING, not the content of COPYed files, so a changed
# requirements-api.txt silently reused a stale pip layer — that shipped an api
# pinned to the wrong firecrawl-py and crash-looped on `import firecrawl.v2`.
# --cache-copy-layers did not fix it. Disabling cache is the reliable fix; if
# build time becomes a problem, reintroduce caching with a content-hash build-arg
# referenced in this RUN so the layer key tracks the deps.
COPY requirements-api.txt ./requirements-api.txt
RUN pip3 install --no-cache-dir uv && \
    uv pip install --system --no-cache -r requirements-api.txt

# Backend source: the selfai_ui package, start.sh, alembic + peewee migrations.
COPY . .

EXPOSE 8080

# WEBUI_SECRET_KEY is NOT baked — start.sh self-generates a dev key if unset,
# and the yard manifests inject the real one from OpenBao.

HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1

ARG BUILD_HASH=dev-build
ENV WEBUI_BUILD_VERSION=${BUILD_HASH}

CMD [ "bash", "start.sh" ]
