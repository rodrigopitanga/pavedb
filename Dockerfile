# syntax=docker/dockerfile:1.4

# PaveDB (pave) Dockerfile

ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG BUILD_ID=unknown
ARG USE_CPU=0

# make build args available at runtime and in RUN shells
ENV BUILD_ID=${BUILD_ID} \
    PIP_DEFAULT_TIMEOUT=300 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAVEDB_CONFIG=/app/config-base.yml

WORKDIR /app

# system deps for building wheels (kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# copy packaging metadata early for good Docker layer caching
COPY setup.py /app/
COPY pave.toml /app/
COPY requirements-cpu.txt /app/

# create a dummy package so setup.py can resolve deps without full source
RUN mkdir -p /app/pave && touch /app/pave/__init__.py
COPY pave/version.py /app/pave/version.py

# install deps only (cached until setup.py changes)
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ "${USE_CPU}" = "1" ] || [ "${USE_CPU}" = "true" ] ; then \
      echo "=== Installing CPU deps ===" ; \
      pip install --progress-bar=off -r requirements-cpu.txt ; \
      pip install --progress-bar=off "openai>=1.0.0" ; \
    else \
      echo "=== Installing GPU deps ===" ; \
      pip install --progress-bar=off ".[openai]" ; \
    fi

# now copy real source and reinstall (deps already satisfied, fast)
COPY pave /app/pave
RUN pip install --no-cache-dir --no-deps --progress-bar=off /app

# Write build id file and label the image. Use ${BUILD_ID} expansion.
RUN printf "%s\n" "${BUILD_ID}" > /app/BUILD_ID
LABEL org.opencontainers.image.revision=${BUILD_ID}

EXPOSE 8086

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8086/health/ready || exit 1

CMD ["python", "-m", "pave.main"]
