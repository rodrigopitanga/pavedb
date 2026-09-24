# syntax=docker/dockerfile:1.4

# PaveDB (pave) Dockerfile

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG BUILD_ID=unknown
# Native sbert/torch embedder wheel: 1 = CPU-only (no NVIDIA CUDA), 0 = default
# (CUDA on Linux). Selects deps only; runtime device is embedder.sbert.device.
ARG TORCH_WHEEL_CPU=0

# make build args available at runtime and in RUN shells
ENV BUILD_ID=${BUILD_ID} \
    PIP_DEFAULT_TIMEOUT=300 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# system deps for building wheels (kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# copy packaging metadata early for good Docker layer caching
COPY setup.py /app/
COPY pyproject.toml /app/
COPY requirements-cpu.txt /app/
COPY LICENSE /app/
COPY scripts/check_sqlite_fts.py /app/scripts/check_sqlite_fts.py

# P1-70 is an indexed-search contract: fail the image build if the selected
# Python base lacks either the FTS5 unicode tokenizer or trigram tokenizer.
RUN python /app/scripts/check_sqlite_fts.py

# create a dummy package so setup.py can resolve deps without full source
RUN mkdir -p /app/pave && touch /app/pave/__init__.py
COPY pave/version.py /app/pave/version.py

# install deps only (cached until setup.py changes)
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "${TORCH_WHEEL_CPU}" = "1" ] || [ "${TORCH_WHEEL_CPU}" = "true" ] ; then \
      echo "=== Installing CPU-only torch wheel ===" ; \
      pip install --progress-bar=off -r requirements-cpu.txt ; \
    else \
      echo "=== Installing default torch wheel (CUDA on Linux) ===" ; \
      pip install --progress-bar=off "." ; \
    fi

# now copy real source and reinstall (deps already satisfied, fast)
COPY pave /app/pave
RUN pip install --no-cache-dir --no-deps --progress-bar=off /app

# Keep installed *.dist-info intact. Those directories carry the license/NOTICE
# files for binary wheels that bundle third-party runtime components
# (torch/scipy/numpy/faiss). They are the redistributed license payload — do
# NOT add a slimming step that deletes *.dist-info (see THIRD_PARTY_LICENSES.md).

# Write build id file and label the image. Use ${BUILD_ID} expansion.
RUN printf "%s\n" "${BUILD_ID}" > /app/BUILD_ID
LABEL org.opencontainers.image.revision=${BUILD_ID} \
      org.opencontainers.image.title="PaveDB" \
      org.opencontainers.image.description="Inspectable retrieval database" \
      org.opencontainers.image.source="https://gitlab.com/flowlexi/pavedb" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Run unprivileged. The instance home (admin.key, tenants.yml, data/) is
# pinned to /data so it does not depend on the runtime user's ~ -- mount a
# volume there to keep it across container replacement. The user keeps a
# real ~ for library caches (the native model download lands in ~/.cache).
RUN useradd --system --uid 1000 --user-group --create-home \
      --home-dir /home/pavedb pavedb && \
    mkdir -p /data && chown pavedb:pavedb /data
ENV PAVEDB_HOME=/data
VOLUME /data
USER pavedb

EXPOSE 8086

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8086/health/ready || exit 1

CMD ["python", "-m", "pave.main"]
