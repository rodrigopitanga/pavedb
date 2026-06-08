# PaveDB — Makefile
#
# Basic usage:
#   make install            # default: base deps
#   make install USE_CPU=1  # override: CPU-only deps (requirements-cpu.txt → setup.py[cpu])
#
# Base deps by default. Set USE_CPU=1 to install CPU deps instead.
# All dependency versions live in setup.py (single source of truth).

PKG_NAME        := pavedb
PYPI_DIST_NAME  ?= pavedb
PKG_ICON	:= 🛣️
PKG_LONGNAME    := $(PKG_ICON)  PaveDB
PKG_INTERNAL   	:= pave
REGISTRY_HOST	?= registry.gitlab.com
REGISTRY_GROUP	?= flowlexi

PYTHON          ?= python3
PIP             ?= $(PYTHON) -m pip
UVICORN         ?= uvicorn

VENV            ?= .venv-$(PKG_INTERNAL)
PYTHON_BIN      ?= $(VENV)/bin/python
PIP_BIN         ?= $(VENV)/bin/pip

DEV		?= 1
DATA_DIR	?= ./data
CONFIG          ?=
AUTH_MODE	?= none
GLOBAL_KEY      ?= $(CHECK_TOKEN)
HOST            ?= 0.0.0.0
PORT            ?= 8086
WORKERS         ?= 1
RELOAD          ?= 1
LOG_LEVEL       ?= debug

# Requirements flavor (gpu default)
USE_CPU ?= 0
REQ_MAIN_CPU    ?= requirements-cpu.txt

# Version helpers - setup.py source of truth
VERSION         ?= $(shell sed -n 's/^ *version="\([^"]*\)".*/\1/p' setup.py | head -1)
ARCHIVE_BASENAME := $(PKG_NAME)-$(VERSION)
DIST_DIR        := dist
BUILD_DIR       := build
ART_DIR         := artifacts

# Docker / publish
DOCKERFILE      ?= Dockerfile
CONTEXT         ?= .
PUSH_LATEST     ?= 1
RELEASE_PUBLISH ?= 0
SKIP_DOCKER_BUILD ?= 0
SKIP_DOCKER_PUSH  ?= $(if $(filter 1,$(RELEASE_PUBLISH)),0,1)
SKIP_PYPI_BUILD   ?= 0
SKIP_PYPI_PUSH    ?= $(if $(filter 1,$(RELEASE_PUBLISH)),0,1)
# Space-separated remote lists used by `make release`.
# Defaults support canonical `gitlab` and legacy `flowlexi` names.
RELEASE_TAG_REMOTES_FINAL      ?= gitlab flowlexi origin
RELEASE_TAG_REMOTES_PRERELEASE ?= gitlab flowlexi
RELEASE_BRANCH_REMOTES         ?= gitlab flowlexi origin
REGISTRY        ?= $(REGISTRY_HOST)/$(REGISTRY_GROUP)/$(PKG_NAME)
IMAGE_NAME 	?= $(PKG_NAME)
ifeq ($(USE_CPU),1)
    IMAGE_TAG 	:= $(VERSION)-cpu
    LATEST_TAG 	:= latest-cpu
else
    IMAGE_TAG 	:= $(VERSION)-gpu
    LATEST_TAG 	:= latest-gpu
endif
BUILD_VARIANT 	:= $(if $(filter 1,$(USE_CPU)),cpu,gpu)
BUILD_ID 	?= $(shell date -u +%Y%m%d%H%M%S)-$(shell git rev-parse --short HEAD)-$(BUILD_VARIANT)

# Release CPU mode: build both images unless USE_CPU is explicitly set on command line
ifeq ($(origin USE_CPU),command line)
  RELEASE_CPU_MODE := single
else
  RELEASE_CPU_MODE := both
endif

.ONESHELL:
SHELL := /bin/bash
# With .ONESHELL enabled, keep recipe failure semantics strict so later
# status messages do not run after an earlier command has already failed.
.SHELLFLAGS := -e -o pipefail -c

.PHONY: help
help:
	@B=$$'\033[1m'; R=$$'\033[0m'; \
	echo "$(PKG_LONGNAME) Make Targets"; \
	echo ""; \
	echo "Setup:"; \
	echo "  $${B}install$${R}          Install runtime deps (GPU by default; USE_CPU=1 for CPU)"; \
	echo "  install-dev      Install dev/test deps (GPU by default; USE_CPU=1 for CPU)"; \
	echo "  venv             Create local virtualenv (.venv)"; \
	echo ""; \
	echo "Run:"; \
	echo "  $${B}serve$${R}           Run API server (DEV: PAVEDB_DEV=1, auth=none, loopback)"; \
	echo "  cli              Run CLI (pass ARGS='...')"; \
	echo ""; \
	echo "Build:"; \
	echo "  build            Build sdist+wheel to ./dist"; \
	echo "  package          Build release archives (.zip + .tar.gz) to ./artifacts"; \
	echo "  docker-build     Build Docker image (VERSION=x.y.z)"; \
	echo "  docker-check     Run alive check against a prebuilt local Docker image"; \
	echo ""; \
	echo "Verify:"; \
	echo "  $${B}test$${R}            Run pytest (full suite)"; \
	echo "  test-fast        Run pytest (skip slow/real-embedding tests)"; \
	echo "  test-relevance   Opt-in public-corpus retrieval regression checks"; \
	echo "  check            Run end-to-end API check (reuse :8086, else ephemeral; flags: CHECK_FORCE_EPHEMERAL=1, CHECK_SERVER_URL=URL)"; \
	echo "  build-check      Install local wheel in temp venv, init instance, boot installed server, alive test"; \
	echo ""; \
	echo "Benchmarks:"; \
	echo "  $${B}benchmark$${R}       Run latency + stress (reuse :8086, else fresh ephemeral per bench; flags: BENCH_FORCE_EPHEMERAL=1, BENCH_SERVER_URL=URL)"; \
	echo "  bench-latency    Search latency (LAT_LENGTH=queries/variant, LAT_CONCUR, LAT_FILTERS=x,y)"; \
	echo "  bench-stress     Stress test (STR_LENGTH=seconds, STR_CONCUR)"; \
	echo "    BENCH_SAVE=1 to save outputs in benchmarks/results/"; \
	echo "    BENCH_TAG=<tag> adds suffix to saved filenames"; \
	echo "  LAT_SLO_P99_MS    Fail bench-latency if p99 > N ms (0=off)"; \
	echo "  STR_MAX_ERROR_PCT Fail bench-stress if error% > N (0=off)"; \
	echo ""; \
	echo "Release:"; \
	echo "  $${B}release$${R}         Bump/tag/build; flags: SKIP_PYPI_BUILD, SKIP_PYPI_PUSH, SKIP_DOCKER_BUILD, SKIP_DOCKER_PUSH (or RELEASE_PUBLISH=1)"; \
	echo "  bump             Bump release versions in project files"; \
	echo "  changelog        Preview changelog entry for VERSION (no write)"; \
	echo "  changelog-write  Update CHANGELOG.md for VERSION and print new entry"; \
	echo "  pypi-push        Upload existing dist/* to PyPI"; \
	echo "  pypitest-push    Upload existing dist/* to TestPyPI"; \
	echo "  docker-push      Push Docker image (VERSION=x.y.z, REGISTRY=...)"; \
	echo ""; \
	echo "Clean:"; \
	echo "  $${B}clean$${R}           Full clean (except deps)"; \
	echo "  deps-clean       Remove .venv (deps dir)"; \
	echo "  dist-clean       Remove dist/build/caches/artifacts"; \
	echo "  data-clean       Remove local data/indexes"

# -------- venv (robust, idempotent) --------
$(VENV)/.created:
	@if ! command -v $(PYTHON) >/dev/null 2>&1; then echo "ERROR: '$(PYTHON)' not found"; exit 127; fi
	@echo "⏳ Creating virtual environment in $(VENV) using: $(PYTHON)"
	@$(PYTHON) -m venv $(VENV) --prompt $(PKG_NAME)
	@$(PIP_BIN) install -q --upgrade pip
	@touch $@

.PHONY: venv
venv: $(VENV)/.created
	@echo "✅ Virtual env ready 👉 Run: source $(VENV)/bin/activate"

# -------- install --------
define install_main
	@if [ "$(USE_CPU)" = "1" ]; then \
	  echo "Installing CPU deps from $(REQ_MAIN_CPU)"; \
	  $(PIP_BIN) install -q -r $(REQ_MAIN_CPU); \
	else \
	  echo "Installing base deps (setup.py)"; \
	  $(PIP_BIN) install -q .; \
	fi
endef

.PHONY: install
install: venv
	$(install_main)
	@echo "✅ Runtime deps installed."

.PHONY: install-dev
install-dev: install
	@$(PIP_BIN) install -q ".[test]"
	@echo "✅ Dev/test deps installed."

# -------- test / serve / cli --------
.PHONY: test
test: install-dev
	PYTHONPATH=. $(PYTHON_BIN) -m pytest -q

.PHONY: test-fast
test-fast: install-dev
	PYTHONPATH=. $(PYTHON_BIN) -m pytest -q -m "not slow"

.PHONY: test-relevance
test-relevance: install-dev
	PAVETEST_REL=1 \
	PAVETEST_REL_PROFILE="$(REL_PROFILE)" \
	PAVETEST_REL_MODEL_ID="$(REL_MODEL_ID)" \
	PYTHONPATH=. $(PYTHON_BIN) -m pytest -q tests/test_relevance.py \
		-m "slow and relevance" -v --tb=short

.PHONY: serve
serve: install
	@echo "Starting server on $(HOST):$(PORT) [auth.mode=$(AUTH_MODE)]"
	cfg_env=(); \
	if [ -n "$(CONFIG)" ]; then \
	  cfg_env+=(PAVEDB_CONFIG="$(CONFIG)"); \
	fi; \
	env "$${cfg_env[@]}" \
	  PYTHONPATH=. \
	  PAVEDB_DEV=$(DEV) \
	  PAVEDB_DATA_DIR=$(DATA_DIR) \
	  PAVEDB_AUTH__MODE=$(AUTH_MODE) \
	  PAVEDB_AUTH__GLOBAL_KEY=$(GLOBAL_KEY) \
	  PAVEDB_LOG__LEVEL=$(LOG_LEVEL) \
	  PAVEDB_SERVER__HOST=$(HOST) \
	  PAVEDB_SERVER__PORT=$(PORT) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main

.PHONY: cli
cli: install
	cfg_env=(); \
	if [ -n "$(CONFIG)" ]; then \
	  cfg_env+=(PAVEDB_CONFIG="$(CONFIG)"); \
	fi; \
	env "$${cfg_env[@]}" \
	  PYTHONPATH=. \
	  PAVEDB_DATA_DIR=$(DATA_DIR) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).cli $(ARGS)

# -------- bump --------
.PHONY: bump
bump:
	@if [ -z "$(VERSION)" ]; then \
	  echo "Error: VERSION is not set. Usage: make bump VERSION=0.5.4"; \
	  exit 1; \
	fi
	@echo "Bumping version to $(VERSION)..."

	# setup.py: version="x.y.z" or version="x.y.zdevN"
	@if [ -f setup.py ]; then \
	  sed -i -E 's/(version=)\"[^\"]*\"/\1"$(VERSION)"/' setup.py; \
	fi
	# pave/main.py: VERSION = "x.y.z"
	@if [ -f pave/main.py ] && grep -qE '^VERSION\s*=' pave/main.py; then \
	  sed -i -E 's/^VERSION\s*=.*/VERSION = "$(VERSION)"/' pave/main.py; \
	fi
	# Dockerfile: ARG APP_VERSION or LABEL version
	@if [ -f Dockerfile ]; then \
	  if grep -qE '^ARG +APP_VERSION=' Dockerfile; then \
	    sed -i -E 's/^(ARG +APP_VERSION=).*/\1$(VERSION)/' Dockerfile; \
	  fi; \
	  if grep -qE 'LABEL +version=' Dockerfile; then \
	    sed -i -E 's/(LABEL +version=)\"[^\"]*\"/\1"$(VERSION)"/' Dockerfile; \
	  fi; \
	fi
	# docker-compose.yml: image: $(IMAGE_NAME):x.y.z
	@if [ -f docker-compose.yml ] && grep -qE 'image:\s*$(IMAGE_NAME):' docker-compose.yml; then \
	  sed -i -E 's/(image:\s*$(IMAGE_NAME):).*/\1$(VERSION)/' docker-compose.yml; \
	fi
	# README.md example tags for docker build/run
	@if [ -f README.md ] && grep -q 'docker build --progress=plain --build-arg USE_CPU=$(USE_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(IMAGE_NAME):' README.md; then \
	  sed -i -E 's@(docker build --progress=plain --build-arg USE_CPU=$(USE_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(IMAGE_NAME):).*@\1$(VERSION) \.@' README.md; \
	fi
	@if [ -f README.md ] && grep -q 'docker run --rm -p 8086:8086 -v $$\(pwd\)/data:/app/data $(IMAGE_NAME):' README.md; then \
	  sed -i -E 's@(docker run --rm -p 8086:8086 -v \$$\(pwd\)/data:/app/data $(IMAGE_NAME):).*@\1$(VERSION)@' README.md; \
	fi
	@echo "✅ Bumped to $(VERSION). Review changes, then commit:"
	@echo "   git add -A && git commit -m \"chore: bump version to $(VERSION)\""

# -------- build / artifacts --------
.PHONY: build
build: install
	rm -rf $(DIST_DIR) $(BUILD_DIR)
	$(PYTHON_BIN) -m pip install -q build twine
	$(PYTHON_BIN) -m build
	$(PYTHON_BIN) -m twine check $(DIST_DIR)/*

.PHONY: package
package: build
	mkdir -p $(ART_DIR)
	@echo "Creating archives for $(ARCHIVE_BASENAME)"
	@if ! command -v git >/dev/null 2>&1 || ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
	  echo "ERROR: artifacts target requires a git work tree"; \
	  exit 1; \
	fi
	# .zip from tracked files only (explicit, reproducible)
	git archive --format=zip --output $(ART_DIR)/$(ARCHIVE_BASENAME).zip HEAD
	# .tar.gz from sdist if available, else from tracked files only
	if ls $(DIST_DIR)/*.tar.gz >/dev/null 2>&1; then \
	  cp $(DIST_DIR)/*.tar.gz $(ART_DIR)/$(ARCHIVE_BASENAME).tar.gz; \
	else \
	  git archive --format=tar.gz --output $(ART_DIR)/$(ARCHIVE_BASENAME).tar.gz HEAD; \
	fi
	@echo "✅ Artifacts available in $(ART_DIR)/"

# -------- docker build/push --------
.PHONY: docker-build
docker-build: install
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION=x.y.z (e.g., 'make docker-build VERSION=0.5.4')"; exit 1; fi
	@set -e; if [ "$(RELEASE_CPU_MODE)" = "both" ]; then \
	  $(MAKE) docker-build VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=0; \
	  $(MAKE) docker-build VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=1; \
	elif [ -n "$(REGISTRY)" ]; then \
	  echo "Building $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) from $(DOCKERFILE)"; \
	  docker build --progress=plain --build-arg USE_CPU=$(USE_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) -f $(DOCKERFILE) $(CONTEXT); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker tag $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):$(LATEST_TAG); fi; \
	else \
	  echo "Building $(IMAGE_NAME):$(IMAGE_TAG) from $(DOCKERFILE)"; \
	  docker build --progress=plain --build-arg USE_CPU=$(USE_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(IMAGE_NAME):$(IMAGE_TAG) -f $(DOCKERFILE) $(CONTEXT); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(IMAGE_NAME):$(LATEST_TAG); fi; \
	fi

.PHONY: docker-push
docker-push:
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION=x.y.z (e.g., 'make docker-push VERSION=0.5.4')"; exit 1; fi
	@set -e; if [ "$(RELEASE_CPU_MODE)" = "both" ]; then \
	  $(MAKE) docker-push VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=0; \
	  $(MAKE) docker-push VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=1; \
	elif [ -n "$(REGISTRY)" ]; then \
	  echo "Pushing $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)"; \
	  docker push $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker push $(REGISTRY)/$(IMAGE_NAME):$(LATEST_TAG); fi; \
	else \
	  echo "Pushing $(IMAGE_NAME):$(IMAGE_TAG)"; \
	  docker push $(IMAGE_NAME):$(IMAGE_TAG); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker push $(IMAGE_NAME):$(LATEST_TAG); fi; \
	fi

# -------- clean (refactored) --------
.PHONY: dist-clean
dist-clean:
	rm -rf __pycache__ */__pycache__ *.pyc *.pyo *.pyd .pytest_cache .ruff_cache .mypy_cache .pytype
	find . -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf $(DIST_DIR) $(BUILD_DIR) $(ART_DIR)
	-@rm -f uvicorn-demo.log .demo.pid .e2e_ingest.json .e2e_search.json
	@echo "Cleaned build/dist/artifacts."

.PHONY: data-clean
data-clean:
	-@rm -rf data/ var/lib/pavedb/data 2>/dev/null || true
	@echo "Cleaned data/indexes."

.PHONY: clean
clean: dist-clean data-clean
	@echo "Cleaned caches and data."


.PHONY: deps-clean
deps-clean:
	rm -rf $(VENV)
	@echo "Cleaned deps (.venv)."

# -------- release --------
.PHONY: release
release:
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION=x.y.z (e.g., 'make release VERSION=0.5.4')"; exit 1; fi
	@if [ -n "$$(git status --porcelain)" ]; then echo "Working tree not clean"; exit 1; fi
	@set -eE; \
	BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
	LAST_TAG=$$(git describe --tags --abbrev=0 2>/dev/null || true); \
	PKG_PUBLISHED="no"; \
	PKG_WHERE="not published (local build only)"; \
	DOCKER_PUBLISHED="no"; \
	DOCKER_WHERE="not published (local build only)"; \
	IMAGE_REFS=""; \
	BRANCH_REMOTES="$(RELEASE_BRANCH_REMOTES)"; \
	TAG_TOUCHED=0; \
	POST_TAG=0; \
	revert_changes() { \
	  echo "Reverting version bumps and changelog..."; \
	  git restore --staged CHANGELOG.md setup.py README.md Dockerfile docker-compose.yml $(PKG_INTERNAL)/main.py 2>/dev/null || true; \
	  git checkout -- CHANGELOG.md setup.py README.md Dockerfile docker-compose.yml $(PKG_INTERNAL)/main.py 2>/dev/null || true; \
	}; \
	trap 'status=$$?; if [ "$$status" -ne 0 ] && [ "$$POST_TAG" = "1" ] && [ "$$TAG_TOUCHED" = "1" ]; then echo "Release failed after tagging; deleting tag v$(VERSION) (keeping bumped commit)."; git tag -d "v$(VERSION)" >/dev/null 2>&1 || true; fi; exit $$status' ERR; \
	if [ "$(SKIP_BUMP)" != "1" ]; then \
	  echo "Bumping to $(VERSION) via 'make bump'..."; \
	  $(MAKE) bump VERSION=$(VERSION) || { echo "Version bump failed."; revert_changes; exit 1; }; \
	fi; \
	$(MAKE) changelog-write VERSION=$(VERSION) || { echo "Changelog generation failed."; revert_changes; exit 1; }; \
	echo "Running tests..."; \
	$(MAKE) test || { echo "Tests failed."; revert_changes; exit 1; }; \
	if [ "$(SKIP_PYPI_BUILD)" != "1" ]; then \
	  echo "Building dists..."; \
	  $(MAKE) build || { echo "Build failed."; revert_changes; exit 1; }; \
	  echo "Running build-check runner..."; \
	  $(MAKE) _build-check-run || { echo "build-check failed."; revert_changes; exit 1; }; \
	else \
	  echo "Skipping dists build (SKIP_PYPI_BUILD=1)."; \
	fi; \
	$(MAKE) -o build package || { echo "Packaging failed."; revert_changes; exit 1; }; \
	if [ -n "$(REGISTRY)" ]; then IMAGE_BASE="$(REGISTRY)/$(IMAGE_NAME)"; else IMAGE_BASE="$(IMAGE_NAME)"; fi; \
	if [ "$(RELEASE_CPU_MODE)" = "both" ]; then \
	  IMAGE_REFS="$(REGISTRY)/$(IMAGE_NAME):$(VERSION)-gpu $(REGISTRY)/$(IMAGE_NAME):$(VERSION)-cpu"; \
	  if [ "$(PUSH_LATEST)" = "1" ]; then IMAGE_REFS="$$IMAGE_REFS $(REGISTRY)/$(IMAGE_NAME):latest-gpu $(REGISTRY)/$(IMAGE_NAME):latest-cpu"; fi; \
	else \
	  if [ "$(USE_CPU)" = "1" ]; then IMG_SUFFIX="cpu"; else IMG_SUFFIX="gpu"; fi; \
	  IMAGE_REFS="$(REGISTRY)/$(IMAGE_NAME):$(VERSION)-$$IMG_SUFFIX"; \
	  if [ "$(PUSH_LATEST)" = "1" ]; then IMAGE_REFS="$$IMAGE_REFS $(REGISTRY)/$(IMAGE_NAME):latest-$$IMG_SUFFIX"; fi; \
	fi; \
	if [ "$(SKIP_DOCKER_BUILD)" = "1" ] && [ "$(SKIP_DOCKER_PUSH)" != "1" ]; then \
	  echo "Invalid flags: SKIP_DOCKER_BUILD=1 requires SKIP_DOCKER_PUSH=1."; \
	  revert_changes; \
	  exit 1; \
	elif [ "$(SKIP_DOCKER_BUILD)" = "1" ]; then \
	  echo "Skipping Docker build/push (SKIP_DOCKER_BUILD=1)."; \
	else \
	  if [ "$(SKIP_DOCKER_PUSH)" != "1" ]; then \
	    echo "Building and validating Docker image(s)..."; \
	  else \
	    echo "Building and validating Docker image(s) (no push)..."; \
	  fi; \
	  if [ "$(RELEASE_CPU_MODE)" = "both" ]; then \
	    $(MAKE) docker-build VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=0 || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-gpu" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	    $(MAKE) docker-build VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=1 || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-cpu" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	  else \
	    $(MAKE) docker-build VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=$(USE_CPU) || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-$$IMG_SUFFIX" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	  fi; \
	fi; \
	git add CHANGELOG.md setup.py README.md Dockerfile docker-compose.yml $(PKG_INTERNAL)/main.py 2>/dev/null || true; \
	if git diff --cached --quiet; then \
	  echo "Nothing to commit — release commit already exists, skipping."; \
	else \
	  git commit -m "chore(release): v$(VERSION)" || { echo "Release commit failed."; revert_changes; exit 1; }; \
	fi; \
	if git rev-parse "v$(VERSION)" >/dev/null 2>&1; then \
	  printf "Tag v$(VERSION) already exists. Re-tag? [y/N] "; \
	  read RETAG < /dev/tty; \
	  if [ "$$RETAG" = "y" ] || [ "$$RETAG" = "Y" ]; then \
	    git tag -d "v$(VERSION)"; \
	    git tag "v$(VERSION)"; \
	    TAG_TOUCHED=1; \
	  else \
	    echo "Keeping existing tag."; \
	    TAG_TOUCHED=0; \
	  fi; \
	else \
	  git tag "v$(VERSION)"; \
	  TAG_TOUCHED=1; \
	fi; \
	POST_TAG=1; \
	if [ "$(SKIP_PYPI_PUSH)" != "1" ]; then \
	  if printf '%s' "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+$$'; then \
	    echo "Publishing package(s) to TestPyPI..."; \
	    $(MAKE) pypitest-push; \
	    PKG_PUBLISHED="yes"; \
	    PKG_WHERE="TestPyPI (https://test.pypi.org/project/$(PYPI_DIST_NAME)/)"; \
	  else \
	    echo "Publishing package(s) to PyPI..."; \
	    $(MAKE) pypi-push; \
	    PKG_PUBLISHED="yes"; \
	    PKG_WHERE="PyPI (https://pypi.org/project/$(PYPI_DIST_NAME)/)"; \
	  fi; \
	fi; \
	if [ "$(SKIP_DOCKER_BUILD)" != "1" ] && [ "$(SKIP_DOCKER_PUSH)" != "1" ]; then \
	  echo "Publishing Docker image(s)..."; \
	  if [ "$(RELEASE_CPU_MODE)" = "both" ]; then \
	    $(MAKE) docker-push VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=0; \
	    $(MAKE) docker-push VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=1; \
	  else \
	    $(MAKE) docker-push VERSION=$(VERSION) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" USE_CPU=$(USE_CPU); \
	  fi; \
	  DOCKER_PUBLISHED="yes"; \
	  DOCKER_WHERE="$(REGISTRY)/$(IMAGE_NAME)"; \
	fi; \
	echo ""; \
	if printf '%s' "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$$'; then \
	  TAG_REMOTES="$(RELEASE_TAG_REMOTES_FINAL)"; \
	else \
	  TAG_REMOTES="$(RELEASE_TAG_REMOTES_PRERELEASE)"; \
	fi; \
	echo "Pushing tag v$(VERSION) to: $$TAG_REMOTES"; \
	declare -A PUSHED_URLS=(); \
	for remote in $$TAG_REMOTES; do \
	  if url=$$(git remote get-url "$$remote" 2>/dev/null); then \
	    if [ -n "$${PUSHED_URLS[$$url]+x}" ]; then \
	      echo "Skipping duplicate remote URL: $$remote -> $$url"; \
	      continue; \
	    fi; \
	    PUSHED_URLS[$$url]=1; \
	    git push "$$remote" "v$(VERSION)"; \
	  else \
	    echo "Skipping missing remote: $$remote"; \
	  fi; \
	done; \
	echo ""; \
	echo "✅ Release v$(VERSION): tests and builds succeeded."; \
	echo ""; \
	echo "Packages:"; \
	for f in dist/*; do [ -e "$$f" ] && echo "  - $$f"; done; \
	echo "  Published: $$PKG_PUBLISHED"; \
	echo "  Destination: $$PKG_WHERE"; \
	echo ""; \
	echo "Docker images:"; \
	for img in $$IMAGE_REFS; do echo "  - $$img"; done; \
	echo "  Published: $$DOCKER_PUBLISHED"; \
	echo "  Destination: $$DOCKER_WHERE"; \
	echo ""; \
	echo "Next step: push commits to branch remotes"; \
	declare -A SHOWN_URLS=(); \
	for remote in $$BRANCH_REMOTES; do \
	  if url=$$(git remote get-url "$$remote" 2>/dev/null); then \
	    if [ -n "$${SHOWN_URLS[$$url]+x}" ]; then \
	      echo "  # skipping duplicate remote URL: $$remote -> $$url"; \
	      continue; \
	    fi; \
	    SHOWN_URLS[$$url]=1; \
	    echo "  git push $$remote $$BRANCH"; \
	  else \
	    echo "  # skipping missing remote: $$remote"; \
	  fi; \
	done

# -------- changelog --------
.PHONY: changelog
changelog:
	@if [ -z "$(VERSION)" ]; then echo "VERSION not detected (setup.py). Set VERSION=x.y.z if needed."; exit 1; fi
	@TMPFILE=$$(mktemp); \
	cp CHANGELOG.md $$TMPFILE; \
	CHANGELOG_SILENT=1 CHANGELOG_PATH=$$TMPFILE $(PYTHON_BIN) scripts/update_changelog.py $(VERSION); \
	awk 'BEGIN{p=0} /^## /{p=1} p{print} /^---/{exit}' $$TMPFILE; \
	rm -f $$TMPFILE

.PHONY: changelog-write
changelog-write:
	@if [ -z "$(VERSION)" ]; then echo "VERSION not detected (setup.py). Set VERSION=x.y.z if needed."; exit 1; fi
	@$(PYTHON_BIN) scripts/update_changelog.py $(VERSION)
	@awk 'BEGIN{p=0} /^## /{p=1} p{print} /^---/{exit}' CHANGELOG.md

# ------------------- E2E CHECK (server-aware) -------------------
# Reuse active http://127.0.0.1:8086 when available.
# Otherwise start an ephemeral local server with temporary data_dir.

CHECK_DEFAULT_HOST ?= 127.0.0.1
CHECK_DEFAULT_PORT ?= 8086
CHECK_DEFAULT_URL  ?= http://$(CHECK_DEFAULT_HOST):$(CHECK_DEFAULT_PORT)
CHECK_EPHEMERAL_HOST ?= 127.0.0.1
CHECK_EPHEMERAL_PORT ?= 18087
CHECK_EPHEMERAL_URL  ?= http://$(CHECK_EPHEMERAL_HOST):$(CHECK_EPHEMERAL_PORT)
CHECK_URL         ?= $(CHECK_DEFAULT_URL)
CHECK_FORCE_EPHEMERAL ?= 0
CHECK_SERVER_URL  ?=
CHECK_TIMEOUT_S   ?= 45
CHECK_SERVER_LOG_LEVEL ?= warning

# Auth + API params
CHECK_AUTH_MODE   ?= static
CHECK_TOKEN       ?= sekret-token
CHECK_TENANT      ?= demo
CHECK_COLL        ?= books
CHECK_DOCID       ?= DEMO-TXT
CHECK_QUERY       ?= currents
CHECK_K           ?= 7

# Test document (host path)
CHECK_TXT_FILE    ?= ./demo/20k_leagues.txt

# -------- wheel install smoke test --------
BUILD_CHECK_HOST ?= 127.0.0.1
BUILD_CHECK_PORT ?= 18088
BUILD_CHECK_URL  ?= http://$(BUILD_CHECK_HOST):$(BUILD_CHECK_PORT)
BUILD_CHECK_TIMEOUT_S ?= 45
BUILD_CHECK_SERVER_LOG_LEVEL ?= warning
BUILD_CHECK_OPENAI_KEY ?= build-check-dummy-key
BUILD_CHECK_OPENAI_DIM ?= 1536

.PHONY: _build-check-artifacts
_build-check-artifacts: venv
	@set -euo pipefail; \
	if ! $(PYTHON_BIN) -c 'import build, twine' >/dev/null 2>&1; then \
	  echo "Missing build/twine in $(VENV). Run: make install-dev"; \
	  exit 1; \
	fi; \
	echo "==> Building local artifacts without isolation"; \
	rm -rf dist build; \
	$(PYTHON_BIN) -m build --no-isolation; \
	$(PYTHON_BIN) -m twine check dist/*

.PHONY: _build-check-run
_build-check-run: venv
	@set -euo pipefail; \
	if ! command -v curl >/dev/null 2>&1; then echo "curl not found"; exit 127; fi; \
	wheel="$$(ls -1t dist/*.whl 2>/dev/null | head -1)"; \
	[ -n "$$wheel" ] || { echo "No wheel found under dist/"; exit 1; }; \
	tmp_root="$$(mktemp -d "$${TMPDIR:-/tmp}/pavedb-build-check.XXXXXX")"; \
	venv_dir="$$tmp_root/venv"; \
	instance_dir="$$tmp_root/instance"; \
	log_file="$$tmp_root/server.log"; \
	dep_site="$$( $(PYTHON_BIN) -c 'import site; print(site.getsitepackages()[0])' )"; \
	srv_pid=""; \
	cleanup() { \
	  if [ -n "$$srv_pid" ]; then \
	    kill "$$srv_pid" >/dev/null 2>&1 || true; \
	    wait "$$srv_pid" >/dev/null 2>&1 || true; \
	  fi; \
	  rm -rf "$$tmp_root"; \
	}; \
	trap cleanup EXIT INT TERM; \
	echo "==> Creating temp venv at $$venv_dir"; \
	$(PYTHON_BIN) -m venv "$$venv_dir"; \
	"$$venv_dir/bin/python" -m pip install -q --upgrade pip; \
	echo "==> Installing local wheel $$wheel (no deps)"; \
	"$$venv_dir/bin/pip" install -q --no-deps "$$wheel"; \
	echo "==> Initializing instance via installed pavecli"; \
	PYTHONPATH="$$dep_site" \
	"$$venv_dir/bin/pavecli" --compact init "$$instance_dir" >/dev/null; \
	test -f "$$instance_dir/config.yml"; \
	test -f "$$instance_dir/tenants.yml"; \
	test -d "$$instance_dir/data"; \
	echo "==> Checking installed CLI bootstrap"; \
	PYTHONPATH="$$dep_site" \
	PAVEDB_DEV=1 \
	PAVEDB_EMBEDDER__TYPE=openai \
	PAVEDB_EMBEDDER__OPENAI__API_KEY=$(BUILD_CHECK_OPENAI_KEY) \
	PAVEDB_EMBEDDER__OPENAI__DIM=$(BUILD_CHECK_OPENAI_DIM) \
	PAVEDB_AUTH__MODE=none \
	"$$venv_dir/bin/pavecli" --compact list-tenants --home "$$instance_dir" >/dev/null; \
	echo "==> Starting installed pavesrv on $(BUILD_CHECK_URL)"; \
	PYTHONPATH="$$dep_site" \
	PAVEDB_DEV=1 \
	PAVEDB_EMBEDDER__TYPE=openai \
	PAVEDB_EMBEDDER__OPENAI__API_KEY=$(BUILD_CHECK_OPENAI_KEY) \
	PAVEDB_EMBEDDER__OPENAI__DIM=$(BUILD_CHECK_OPENAI_DIM) \
	PAVEDB_AUTH__MODE=none \
	PAVEDB_LOG__LEVEL=$(BUILD_CHECK_SERVER_LOG_LEVEL) \
	PAVEDB_SERVER__HOST=$(BUILD_CHECK_HOST) \
	PAVEDB_SERVER__PORT=$(BUILD_CHECK_PORT) \
	"$$venv_dir/bin/pavesrv" --home "$$instance_dir" >"$$log_file" 2>&1 & \
	srv_pid=$$!; \
	echo "==> Waiting for live $(BUILD_CHECK_URL)/health/live (timeout $(BUILD_CHECK_TIMEOUT_S)s)"; \
	for i in $$(seq 1 $(BUILD_CHECK_TIMEOUT_S)); do \
	  if curl -fsS "$(BUILD_CHECK_URL)/health/live" >/dev/null 2>&1; then \
	    echo "✅ make build-check passed."; \
	    exit 0; \
	  fi; \
	  if ! kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	    echo "Installed server exited early. Log follows:"; \
	    cat "$$log_file"; \
	    exit 1; \
	  fi; \
	  sleep 1; \
	done; \
	echo "Installed server did not become live in $(BUILD_CHECK_TIMEOUT_S)s. Log follows:"; \
	cat "$$log_file"; \
	exit 1

.PHONY: build-check
build-check: venv
	@$(MAKE) -o venv _build-check-artifacts
	@$(MAKE) -o venv _build-check-run

# -------- docker image smoke test --------
DOCKER_CHECK_HOST ?= 127.0.0.1
DOCKER_CHECK_PORT ?= 18089
DOCKER_CHECK_URL  ?= http://$(DOCKER_CHECK_HOST):$(DOCKER_CHECK_PORT)
DOCKER_CHECK_TIMEOUT_S ?= 45
DOCKER_CHECK_OPENAI_KEY ?= docker-check-dummy-key
DOCKER_CHECK_OPENAI_DIM ?= 1536
DOCKER_CHECK_CONTAINER_PREFIX ?= pavedb-docker-check
DOCKER_CHECK_IMAGE ?= $(if $(REGISTRY),$(REGISTRY)/$(IMAGE_NAME):$(VERSION)-cpu,$(IMAGE_NAME):$(VERSION)-cpu)

.PHONY: _docker-check-run
_docker-check-run:
	@set -euo pipefail; \
	if ! command -v docker >/dev/null 2>&1; then echo "docker not found"; exit 127; fi; \
	if ! command -v curl >/dev/null 2>&1; then echo "curl not found"; exit 127; fi; \
	image="$(DOCKER_CHECK_IMAGE)"; \
	if ! docker image inspect "$$image" >/dev/null 2>&1; then \
	  echo "Docker image $$image not found."; \
	  echo "Build it first with: make docker-build VERSION=$(VERSION) USE_CPU=$(USE_CPU)"; \
	  exit 1; \
	fi; \
	container_name="$(DOCKER_CHECK_CONTAINER_PREFIX)-$$(date +%s)-$$RANDOM"; \
	cleanup() { \
	  docker rm -f "$$container_name" >/dev/null 2>&1 || true; \
	}; \
	trap cleanup EXIT INT TERM; \
	echo "==> Starting $$image as $$container_name on $(DOCKER_CHECK_URL)"; \
	docker run -d --name "$$container_name" \
	  -p $(DOCKER_CHECK_HOST):$(DOCKER_CHECK_PORT):8086 \
	  -e PAVEDB_CONFIG= \
	  -e PAVEDB_DEV=1 \
	  -e PAVEDB_AUTH__MODE=none \
	  -e PAVEDB_EMBEDDER__TYPE=openai \
	  -e PAVEDB_EMBEDDER__OPENAI__API_KEY=$(DOCKER_CHECK_OPENAI_KEY) \
	  -e PAVEDB_EMBEDDER__OPENAI__DIM=$(DOCKER_CHECK_OPENAI_DIM) \
	  -e PAVEDB_SERVER__HOST=0.0.0.0 \
	  -e PAVEDB_SERVER__PORT=8086 \
	  "$$image" >/dev/null; \
	echo "==> Waiting for live $(DOCKER_CHECK_URL)/health/live (timeout $(DOCKER_CHECK_TIMEOUT_S)s)"; \
	for i in $$(seq 1 $(DOCKER_CHECK_TIMEOUT_S)); do \
	  if curl -fsS "$(DOCKER_CHECK_URL)/health/live" >/dev/null 2>&1; then \
	    echo "✅ make docker-check passed."; \
	    exit 0; \
	  fi; \
	  status="$$(docker inspect -f '{{.State.Status}}' "$$container_name" 2>/dev/null || echo missing)"; \
	  if [ "$$status" != "running" ]; then \
	    echo "Container exited early (status=$$status). Log follows:"; \
	    docker logs "$$container_name" || true; \
	    exit 1; \
	  fi; \
	  sleep 1; \
	done; \
	echo "Container did not become live in $(DOCKER_CHECK_TIMEOUT_S)s. Log follows:"; \
	docker logs "$$container_name" || true; \
	exit 1

.PHONY: docker-check
docker-check:
	@$(MAKE) _docker-check-run

.PHONY: check-run
check-run: install
	@set -euo pipefail; \
	if ! command -v curl >/dev/null 2>&1; then echo "curl not found"; exit 127; fi; \
	BASE="$(CHECK_URL)"; \
	echo "==> Waiting for live $$BASE/health/live (timeout $(CHECK_TIMEOUT_S)s)"; \
	for i in $$(seq 1 $(CHECK_TIMEOUT_S)); do \
	  if curl -fsS "$$BASE/health/live" >/dev/null 2>&1; then \
	    echo "   Live."; \
	    break; \
	  fi; \
	  sleep 1; \
	  if [ $$i -eq $(CHECK_TIMEOUT_S) ]; then \
	    echo "Timeout waiting for live at $$BASE"; \
	    exit 1; \
	  fi; \
	done; \
	AHDR=""; if [ "$(CHECK_AUTH_MODE)" = "static" ]; then AHDR="Authorization: Bearer $(CHECK_TOKEN)"; fi; \
	echo "==> Create collection: $(CHECK_TENANT)/$(CHECK_COLL)"; \
	if [ -n "$$AHDR" ]; then \
	  curl -fsS -X POST "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)" -H "$$AHDR" -H "Content-Length: 0" >/dev/null; \
	else \
	  curl -fsS -X POST "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)" -H "Content-Length: 0" >/dev/null; \
	fi; \
	[ -f "$(CHECK_TXT_FILE)" ] || { echo "Missing file: $(CHECK_TXT_FILE)"; exit 1; }; \
	echo "==> Ingest: $(CHECK_TXT_FILE) (docid=$(CHECK_DOCID))"; \
	if [ -n "$$AHDR" ]; then \
	  curl -fsS -X POST "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)/documents" -H "$$AHDR" \
	    -F "file=@$(CHECK_TXT_FILE)" \
	    -F "docid=$(CHECK_DOCID)" \
	    -F "metadata={\"lang\":\"en\",\"source\":\"Gutenberg\"}" >/dev/null; \
	else \
	  curl -fsS -X POST "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)/documents" \
	    -F "file=@$(CHECK_TXT_FILE)" \
	    -F "docid=$(CHECK_DOCID)" \
	    -F "metadata={\"lang\":\"en\",\"source\":\"Gutenberg\"}" >/dev/null; \
	fi; \
	echo "==> Search (GET) q='$(CHECK_QUERY)' k=$(CHECK_K)"; \
	ENCQ=$$(printf %s "$(CHECK_QUERY)" | jq -sRr @uri 2>/dev/null || printf %s "$(CHECK_QUERY)"); \
	if [ -n "$$AHDR" ]; then \
	  curl -fsS "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)/search?q=$$ENCQ&k=$(CHECK_K)" -H "$$AHDR" | tee .check_search.json >/dev/null; \
	else \
	  curl -fsS "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)/search?q=$$ENCQ&k=$(CHECK_K)" | tee .check_search.json >/dev/null; \
	fi; \
	$(PYTHON_BIN) -c 'import json,sys; d=json.load(open(".check_search.json")); m=d.get("matches"); (print("Empty search results", file=sys.stderr), sys.exit(1)) if (not isinstance(m, list) or not m) else None; f=m[0] if isinstance(m[0], dict) else {}; reason=f.get("match_reason") or f.get("reason") or ""; latency=d.get("latency_ms"); text=(f.get("text") or "").replace("\n", " "); print(f"==> First match reason: {reason}"); print(f"==> First match text: {text}"); print(f"==> Search latency_ms: {latency}")'; \
	rm -f .check_search.json; \
	echo "==> Cleanup: delete collection $(CHECK_TENANT)/$(CHECK_COLL)"; \
	if [ -n "$$AHDR" ]; then \
	  curl -fsS -X DELETE "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)" -H "$$AHDR" >/dev/null; \
	else \
	  curl -fsS -X DELETE "$$BASE/v1/collections/$(CHECK_TENANT)/$(CHECK_COLL)" >/dev/null; \
	fi; \
	echo "✅ make check passed."

.PHONY: _check-with-server
_check-with-server: install
	@set -e; \
	if ! command -v curl >/dev/null 2>&1; then echo "curl not found"; exit 127; fi; \
	if [ -n "$(CHECK_SERVER_URL)" ] && [ "$(CHECK_FORCE_EPHEMERAL)" = "1" ]; then \
	  echo "ERROR: CHECK_SERVER_URL and CHECK_FORCE_EPHEMERAL=1 are mutually exclusive."; \
	  exit 1; \
	fi; \
	check_url="$(CHECK_DEFAULT_URL)"; \
	managed=0; \
	data_dir=""; \
	log_file=""; \
	srv_pid=""; \
	if [ -n "$(CHECK_SERVER_URL)" ]; then \
	  check_url="$(CHECK_SERVER_URL)"; \
	  echo "==> Using configured check server on $$check_url"; \
	elif [ "$(CHECK_FORCE_EPHEMERAL)" = "1" ]; then \
	  managed=1; \
	  check_url="$(CHECK_EPHEMERAL_URL)"; \
	  data_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/pavedb-check.XXXXXX")"; \
	  log_file="$$data_dir/server.log"; \
	  echo "==> CHECK_FORCE_EPHEMERAL=1; starting ephemeral server on $$check_url (data_dir=$$data_dir)"; \
	  PYTHONPATH=. \
	  PYTHONFAULTHANDLER=1 \
	  PAVEDB_DEV=1 \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE=$(CHECK_AUTH_MODE) \
	  PAVEDB_AUTH__GLOBAL_KEY=$(CHECK_TOKEN) \
	  PAVEDB_LOG__LEVEL=$(CHECK_SERVER_LOG_LEVEL) \
	  PAVEDB_VECTOR_STORE__TYPE=faiss \
	  PAVEDB_SERVER__HOST=$(CHECK_EPHEMERAL_HOST) \
	  PAVEDB_SERVER__PORT=$(CHECK_EPHEMERAL_PORT) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	elif curl -fsS "$$check_url/health/live" >/dev/null 2>&1; then \
	  echo "==> Using active server on $$check_url"; \
	else \
	  managed=1; \
	  check_url="$(CHECK_EPHEMERAL_URL)"; \
	  data_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/pavedb-check.XXXXXX")"; \
	  log_file="$$data_dir/server.log"; \
	  echo "==> No active server on $(CHECK_DEFAULT_URL); starting ephemeral server on $$check_url (data_dir=$$data_dir)"; \
	  PYTHONPATH=. \
	  PYTHONFAULTHANDLER=1 \
	  PAVEDB_DEV=1 \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE=$(CHECK_AUTH_MODE) \
	  PAVEDB_AUTH__GLOBAL_KEY=$(CHECK_TOKEN) \
	  PAVEDB_LOG__LEVEL=$(CHECK_SERVER_LOG_LEVEL) \
	  PAVEDB_VECTOR_STORE__TYPE=faiss \
	  PAVEDB_SERVER__HOST=$(CHECK_EPHEMERAL_HOST) \
	  PAVEDB_SERVER__PORT=$(CHECK_EPHEMERAL_PORT) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	fi; \
	cleanup() { \
	  if [ "$$managed" = "1" ]; then \
	    kill "$$srv_pid" >/dev/null 2>&1 || true; \
	    wait "$$srv_pid" >/dev/null 2>&1 || true; \
	    rm -rf "$$data_dir"; \
	  fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	if [ "$$managed" = "1" ]; then \
	  for i in $$(seq 1 $(CHECK_TIMEOUT_S)); do \
	    if curl -fsS "$$check_url/health/live" >/dev/null 2>&1; then \
	      ready=1; \
	      break; \
	    fi; \
	    sleep 1; \
	  done; \
	  if [ "$${ready:-0}" != "1" ]; then \
	    echo "ERROR: check server did not become ready in $(CHECK_TIMEOUT_S)s."; \
	    tail -n 80 "$$log_file" || true; \
	    exit 1; \
	  fi; \
	fi; \
	$(MAKE) -o install CHECK_URL="$$check_url" check-run

.PHONY: check
check: install
	@$(MAKE) -o install _check-with-server

# -------- benchmarks --------
_default_host ?= 127.0.0.1
_default_port ?= 8086
_default_url  ?= http://$(_default_host):$(_default_port)
_ephemeral_host ?= 127.0.0.1
_ephemeral_port ?= 18086
_ephemeral_url  ?= http://$(_ephemeral_host):$(_ephemeral_port)
_url         ?= $(_default_url)
BENCH_FORCE_EPHEMERAL ?= 0
BENCH_SERVER_URL  ?=
BENCH_API_KEY     ?=
LAT_LENGTH        ?= 1200
LAT_CONCUR        ?= 42
LAT_FILTERS       ?= none,exact,wildcard,mixed
STR_LENGTH        ?= 90
STR_CONCUR        ?= 8
REL_PROFILE       ?= tatoeba-core-xling
REL_MODEL_ID      ?= multi-minilm-l12
BENCH_TAG         ?=
_ts          ?= $(shell date -u +%Y-%m-%d_%H%M%S)
_results_dir ?= benchmarks/results
BENCH_SAVE        ?= 0
LAT_SLO_P99_MS    ?= 0
STR_MAX_ERROR_PCT ?= 0
_startup_timeout_s ?= 30
_server_log_level ?= warning

.PHONY: _bench-with-server
_bench-with-server:
	@if [ -z "$(_run_target)" ]; then echo "ERROR: _run_target is not set."; exit 1; fi
	@set -e; \
	if [ -n "$(BENCH_SERVER_URL)" ] && [ "$(BENCH_FORCE_EPHEMERAL)" = "1" ]; then \
	  echo "ERROR: BENCH_SERVER_URL and BENCH_FORCE_EPHEMERAL=1 are mutually exclusive."; \
	  exit 1; \
	fi; \
	bench_url="$(_default_url)"; \
	managed=0; \
	data_dir=""; \
	log_file=""; \
	srv_pid=""; \
	keep_data_dir=0; \
	bench_status=0; \
	if [ -n "$(BENCH_SERVER_URL)" ]; then \
	  bench_url="$(BENCH_SERVER_URL)"; \
	  if ! curl -fsS "$$bench_url/health/live" >/dev/null 2>&1; then \
	    echo "ERROR: BENCH_SERVER_URL is not reachable: $$bench_url"; \
	    exit 1; \
	  fi; \
	  echo "==> Using configured benchmark server on $$bench_url"; \
	elif [ "$(BENCH_FORCE_EPHEMERAL)" = "1" ]; then \
	  managed=1; \
	  bench_url="$(_ephemeral_url)"; \
	  data_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/pavedb-bench.XXXXXX")"; \
	  log_file="$$data_dir/server.log"; \
	  echo "==> BENCH_FORCE_EPHEMERAL=1; starting ephemeral server on $$bench_url (data_dir=$$data_dir)"; \
	  PYTHONPATH=. \
	  PYTHONFAULTHANDLER=1 \
	  PAVEDB_DEV=1 \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE=none \
	  PAVEDB_LOG__LEVEL=$(_server_log_level) \
	  PAVEDB_SERVER__HOST=$(_ephemeral_host) \
	  PAVEDB_SERVER__PORT=$(_ephemeral_port) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	elif curl -fsS "$$bench_url/health/live" >/dev/null 2>&1; then \
	  echo "==> Using active server on $$bench_url"; \
	else \
	  managed=1; \
	  bench_url="$(_ephemeral_url)"; \
	  data_dir="$$(mktemp -d "$${TMPDIR:-/tmp}/pavedb-bench.XXXXXX")"; \
	  log_file="$$data_dir/server.log"; \
	  echo "==> No active server on $(_default_url); starting ephemeral server on $$bench_url (data_dir=$$data_dir)"; \
	  PYTHONPATH=. \
	  PYTHONFAULTHANDLER=1 \
	  PAVEDB_DEV=1 \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE=none \
	  PAVEDB_LOG__LEVEL=$(_server_log_level) \
	  PAVEDB_SERVER__HOST=$(_ephemeral_host) \
	  PAVEDB_SERVER__PORT=$(_ephemeral_port) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	fi; \
	cleanup() { \
	  if [ "$$managed" = "1" ]; then \
	    if kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	      kill "$$srv_pid" >/dev/null 2>&1 || true; \
	      wait "$$srv_pid" >/dev/null 2>&1 || true; \
	    else \
	      srv_status=0; \
	      wait "$$srv_pid" >/dev/null 2>&1 || srv_status=$$?; \
	      if [ "$$srv_status" != "0" ]; then \
	        keep_data_dir=1; \
	      fi; \
	    fi; \
	    if [ "$$keep_data_dir" = "1" ]; then \
	      echo "==> Preserving ephemeral server state at $$data_dir"; \
	      echo "==> Server log: $$log_file"; \
	    else \
	      rm -rf "$$data_dir"; \
	    fi; \
	  fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	if [ "$$managed" = "1" ]; then \
	  for i in $$(seq 1 $(_startup_timeout_s)); do \
	    if curl -fsS "$$bench_url/health/live" >/dev/null 2>&1; then \
	      ready=1; \
	      break; \
	    fi; \
	    sleep 1; \
	  done; \
	  if [ "$${ready:-0}" != "1" ]; then \
	    keep_data_dir=1; \
	    echo "ERROR: benchmark server did not become ready in $(_startup_timeout_s)s."; \
	    tail -n 80 "$$log_file" || true; \
	    exit 1; \
	  fi; \
	fi; \
	$(MAKE) -o install-dev _url="$$bench_url" _ts="$(_ts)" $(_run_target) || \
	  bench_status=$$?; \
	if [ "$$managed" = "1" ] && [ -n "$$srv_pid" ] && \
	   ! kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	  srv_status=0; \
	  wait "$$srv_pid" >/dev/null 2>&1 || srv_status=$$?; \
	  srv_pid=""; \
	  keep_data_dir=1; \
	  if [ "$$srv_status" != "0" ]; then \
	    if [ "$$bench_status" = "0" ]; then bench_status=$$srv_status; fi; \
	  elif [ "$$bench_status" = "0" ]; then \
	    bench_status=1; \
	  fi; \
	  echo "ERROR: benchmark server exited during run (status $$srv_status)."; \
	fi; \
	if [ "$$bench_status" != "0" ]; then \
	  keep_data_dir=1; \
	  exit "$$bench_status"; \
	fi

.PHONY: bench-latency bench-latency-run
bench-latency: install-dev
	@summary=$$(mktemp /tmp/pavedb-bench-summary.XXXXXX); \
	for filt in $$(echo "$(LAT_FILTERS)" | tr ',' ' '); do \
	  echo ""; \
	  echo "==> Latency: filtering=$$filt"; \
	  $(MAKE) -o install-dev \
	    _run_target=bench-latency-run \
	    _filt=$$filt \
	    _summary_file=$$summary \
	    _ts="$(_ts)" \
	    _bench-with-server; \
	done; \
	awk -F'|' ' \
	  BEGIN { \
	    s=""; for(i=1;i<=108;i++) s=s"="; print "\n" s; \
	    print "  SEARCH LATENCY — consolidated"; print s; \
	    h="%-18s %6s %6s %8s %11s %9s %9s %9s %9s %9s %9s\n"; \
	    printf h,"Variant","Count","OK","Hits","Err (%)","Min","p50","p95","p99","Max","Ops/s"; \
	    gsub(/=/,"-",s); print s \
	  } \
	  { e=sprintf("%d (%.1f%%)",$$5,$$6); \
	    printf "%-18s %6d %6d %8d %11s %8.1fms %8.1fms %8.1fms %8.1fms %8.1fms %8.1f\n", \
	      $$1,$$2,$$3,$$4,e,$$7,$$8,$$9,$$10,$$11,$$12 } \
	  END { s=""; for(i=1;i<=108;i++) s=s"-"; print s }' \
	  $$summary; \
	rm -f $$summary

bench-latency-run:
	@sha=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown"); \
	tag="$(BENCH_TAG)"; \
	api_arg=""; \
	summary_arg=""; \
	slo_arg=""; \
	if [ -n "$(BENCH_API_KEY)" ]; then api_arg="--api-key $(BENCH_API_KEY)"; fi; \
	if [ -n "$(_summary_file)" ]; then \
	  summary_arg="--summary-line $(_summary_file)"; \
	fi; \
	if [ "$(LAT_SLO_P99_MS)" != "0" ]; then \
	  slo_arg="--slo-p99-ms $(LAT_SLO_P99_MS)"; \
	fi; \
	if [ -z "$$tag" ]; then \
	  branch=$$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached"); \
	  tag="$$branch-$$sha"; \
	else \
	  tag="$$tag-$$sha"; \
	fi; \
	if [ "$(BENCH_SAVE)" = "1" ]; then \
	  echo "==> Saving: yes (ts=$(_ts), tag=$$tag)"; \
	  mkdir -p $(_results_dir); \
	  PYTHONPATH=. $(PYTHON_BIN) benchmarks/search_latency.py \
	    --url $(_url) --queries $(LAT_LENGTH) \
	    --concurrency $(LAT_CONCUR) \
	    --filtering $(_filt) \
	    $$api_arg $$summary_arg $$slo_arg \
	    | tee $(_results_dir)/latency-$(_ts)_$$tag-$(_filt).txt; \
	else \
	  echo "==> Saving: no (ts=$(_ts), tag=$$tag)"; \
	  PYTHONPATH=. $(PYTHON_BIN) benchmarks/search_latency.py \
	    --url $(_url) --queries $(LAT_LENGTH) \
	    --concurrency $(LAT_CONCUR) \
	    --filtering $(_filt) \
	    $$api_arg $$summary_arg $$slo_arg; \
	fi

.PHONY: bench-stress bench-stress-run
bench-stress: install-dev
	@$(MAKE) -o install-dev _run_target=bench-stress-run _ts="$(_ts)" _bench-with-server

bench-stress-run:
	@sha=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown"); \
	tag="$(BENCH_TAG)"; \
	api_arg=""; \
	err_arg=""; \
	if [ -n "$(BENCH_API_KEY)" ]; then api_arg="--api-key $(BENCH_API_KEY)"; fi; \
	if [ "$(STR_MAX_ERROR_PCT)" != "0" ]; then \
	  err_arg="--max-error-pct $(STR_MAX_ERROR_PCT)"; \
	fi; \
	if [ -z "$$tag" ]; then \
	  branch=$$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached"); \
	  tag="$$branch-$$sha"; \
	else \
	  tag="$$tag-$$sha"; \
	fi; \
	if [ "$(BENCH_SAVE)" = "1" ]; then \
	  echo "==> Saving: yes (ts=$(_ts), tag=$$tag)"; \
	  mkdir -p $(_results_dir); \
	  PYTHONPATH=. $(PYTHON_BIN) benchmarks/stress.py \
	    --url $(_url) --duration $(STR_LENGTH) \
	    --concurrency $(STR_CONCUR) \
	    $$api_arg $$err_arg \
	    | tee $(_results_dir)/stress-$(_ts)_$$tag.txt; \
	else \
	  echo "==> Saving: no (ts=$(_ts), tag=$$tag)"; \
	  PYTHONPATH=. $(PYTHON_BIN) benchmarks/stress.py \
	    --url $(_url) --duration $(STR_LENGTH) \
	    --concurrency $(STR_CONCUR) \
	    $$api_arg $$err_arg; \
	fi

.PHONY: benchmark
benchmark: install-dev
	@ts="$(_ts)"; \
	$(MAKE) -o install-dev _ts="$$ts" bench-latency bench-stress

.PHONY: pypi-push pypitest-push

pypi-push:
	$(PYTHON_BIN) -m twine upload --skip-existing $(DIST_DIR)/*

pypitest-push:
	$(PYTHON_BIN) -m twine upload --skip-existing --repository testpypi $(DIST_DIR)/*
