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

FROM python:3.13-slim-bookworm AS base

ENV ENV=prod \
    PORT=8080 \
    HOST=0.0.0.0 \
    DOCKER=true

# Inference deferred by default + telemetry off. Manifests/env override these.
ENV ENABLE_OLLAMA_API=False \
    ENABLE_OPENAI_API=False \
    SCARF_NO_ANALYTICS=true \
    DO_NOT_TRACK=true

# Caches live under the data volume; no models are baked at build time.
ENV SENTENCE_TRANSFORMERS_HOME="/app/backend/data/cache/embedding/models" \
    HF_HOME="/app/backend/data/cache/embedding/models" \
    WHISPER_MODEL_DIR="/app/backend/data/cache/whisper/models" \
    TIKTOKEN_CACHE_DIR="/app/backend/data/cache/tiktoken"

WORKDIR /app/backend

# Minimal system deps: a build toolchain for the few sdist-only wheels, pandoc
# for document parsing, curl+jq for the healthcheck, ffmpeg/ffprobe for
# utils/audio_ffmpeg.py, which audio.py uses to probe and transcode uploads
# (the STT-forward path to self.transcribe crashed with "No such file or
# directory: 'ffprobe'" on any mp4/AAC browser recording -- this image
# previously shipped with no ffmpeg at all, on the assumption STT was fully
# externalized and wouldn't need it locally; that assumption was wrong, the
# mp4-detection probe runs regardless of engine). These binaries are now
# called DIRECTLY rather than through pydub, which was only ever a wrapper
# around them (#106) -- so ffmpeg is a harder requirement than before, not a
# softer one. Still NO libsm6/libxext6 (those were for OCR/cv2,
# dropped) and NO CUDA -- this stays torch-free.
# TODO(slim): move to a multi-stage build to drop build-essential from runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential gcc python3-dev \
      pandoc netcat-openbsd curl jq ca-certificates ffmpeg && \
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
# The trailing pip force-reinstall + assert are DUCT TAPE for a uv
# install-corruption that broke the R5 force-reaper's in-cluster auth (401 on
# every real pod delete; selfinfra#166 / self.ai!210). `uv pip install` produced
# a MIXED-VERSION kubernetes_asyncio tree: an old `config/incluster_config.py`
# (which writes the SA token as api_key['authorization']) stitched together with
# a newer `client/configuration.py` (whose auth_settings() only emits a bearer
# header when api_key['BearerToken'] is set). The two disagree on the key name,
# so auth_settings() returned {} and the client sent NO Authorization header ->
# 401 -- even though load_incluster_config() had read the token fine (curl with
# the same token = 200). It is NOT a version bug (any single clean version is
# self-consistent), so there is no version pin here. pip's --force-reinstall
# does the clean remove-then-write uv skipped; the assert then drives the REAL
# load_incluster_config()->auth_settings() path against a fake token and FAILS
# THE BUILD if the wiring can't produce a bearer header -- so a mismatched tree
# can never reach a pod. Unit tests inject a fake k8s client and never exercise
# this, which is why it only ever surfaced live (T-007).
#
# `--only-binary numpy` is the regression guard for self.ai#108. numpy publishes
# no 1.x wheel for cp313, so ANY dependency that re-caps numpy<2 silently drops
# a ~50s source compile back into every image build -- slow, and invisible
# unless someone reads the trace. With this flag that situation fails the build
# outright ("numpy has no usable wheels") instead of quietly costing a minute.
# Scoped to numpy alone: langdetect and red-black-tree-mod still build from
# source here (both fast, pure-python sdists) and are deliberately left alone.
# chroma-hnswlib used to be the slow one; it left with the chromadb backend.
RUN pip3 install --no-cache-dir uv && \
    uv pip install --system --no-cache --only-binary numpy -r requirements-api.txt && \
    pip3 install --no-cache-dir --force-reinstall --no-deps "kubernetes_asyncio>=35.0.0" && \
    mkdir -p /tmp/sa-assert && \
    printf 'build-assert-token' > /tmp/sa-assert/token && \
    printf 'x' > /tmp/sa-assert/ca.crt && \
    KUBERNETES_SERVICE_HOST=10.0.0.1 KUBERNETES_SERVICE_PORT=443 python3 -c "import asyncio; import kubernetes_asyncio.config.incluster_config as ic; ic.SERVICE_TOKEN_FILENAME='/tmp/sa-assert/token'; ic.SERVICE_CERT_FILENAME='/tmp/sa-assert/ca.crt'; from kubernetes_asyncio import client, config; config.load_incluster_config(); c=client.Configuration.get_default_copy(); a=asyncio.run(c.auth_settings()); v=(a.get('BearerToken') or {}).get('value'); assert v and 'build-assert-token' in str(v), 'kubernetes_asyncio in-cluster auth wiring broken (mismatched/corrupt install): load_incluster_config wrote the token but auth_settings() did not surface a bearer header. auth_settings keys='+repr(list(a.keys()))+' api_key keys='+repr(list(c.api_key.keys())); print('kubernetes_asyncio in-cluster auth wiring OK')" && \
    rm -rf /tmp/sa-assert

# Backend source: the selfai_ui package, start.sh, alembic + peewee migrations.
COPY . .

EXPOSE 8080

# WEBUI_SECRET_KEY is NOT baked — start.sh self-generates a dev key if unset,
# and the yard manifests inject the real one from OpenBao.

HEALTHCHECK CMD curl --silent --fail http://localhost:${PORT:-8080}/health | jq -ne 'input.status == true' || exit 1

ARG BUILD_HASH=dev-build
ENV WEBUI_BUILD_VERSION=${BUILD_HASH}

CMD [ "bash", "start.sh" ]
