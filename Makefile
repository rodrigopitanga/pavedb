# PaveDB — Makefile
#
# Basic usage:
#   make install                     # default: platform torch wheel
#   make install TORCH_WHEEL_CPU=1   # CPU-only torch wheel (no NVIDIA CUDA)
#
# TORCH_WHEEL_CPU selects the torch wheel for the native sentence-transformers
# embedder only — no effect on OpenAI/Ollama embedders, or on macOS (one
# MPS-capable wheel). Runtime device is separate: embedder.sbert.device.
# All dependency versions live in setup.py (single source of truth).

# GNU Make 4+ and bash 4+ required.
ifeq ($(shell uname -s),Darwin)
_MAKE_HINT := . macOS: `brew install make` and invoke as `gmake`
_BASH_HINT := . macOS: `brew install bash`
endif

_MAKE_MAJOR := $(firstword $(subst ., ,$(MAKE_VERSION)))
ifneq ($(filter 1 2 3,$(_MAKE_MAJOR)),)
$(error GNU Make >= 4 required (found $(MAKE_VERSION))$(_MAKE_HINT))
endif

SHELL := $(shell command -v bash)
ifeq ($(SHELL),)
$(error bash not found$(_BASH_HINT))
endif
_BASH_MAJOR := $(shell $(SHELL) -c 'echo $${BASH_VERSINFO[0]}' 2>/dev/null)
ifneq ($(filter 1 2 3,$(_BASH_MAJOR)),)
$(error bash >= 4 required (found bash $(_BASH_MAJOR).x at $(SHELL))$(_BASH_HINT))
endif

PKG_NAME        := pavedb
PYPI_DIST_NAME  ?= pavedb
PKG_EMOJI	:= 🛣️
PKG_LONGNAME    := $(PKG_EMOJI)  PaveDB
PKG_INTERNAL   	:= pave
REGISTRY_HOST	?= registry.gitlab.com
REGISTRY_GROUP	?= flowlexi

# Newest python3.N on PATH inside the range declared in packaging metadata.
# PYTHON=... overrides. Same block in pavedb, pavedb-sdk, pavedb-extra-benchmarks.
_PY_REQ := $(shell sed -n 's/^requires-python *= *"\([^"]*\)".*/\1/p' pyproject.toml 2>/dev/null | head -1)
ifeq ($(_PY_REQ),)
_PY_REQ := $(shell sed -n 's/.*python_requires *= *"\([^"]*\)".*/\1/p' setup.py 2>/dev/null | head -1)
endif
_PY_MIN := $(shell printf '%s' '$(_PY_REQ)' | sed -n 's/.*>=3\.\([0-9]\{1,\}\).*/\1/p')
_PY_TOP := $(shell printf '%s' '$(_PY_REQ)' | sed -n 's/.*<3\.\([0-9]\{1,\}\).*/\1/p')
_PY_MAX := $(shell echo $$(( $(if $(_PY_TOP),$(_PY_TOP),100) - 1 )))
PYTHON ?= $(shell for n in $$(seq $(_PY_MAX) -1 $(if $(_PY_MIN),$(_PY_MIN),0)); do \
	python3.$$n -m ensurepip --version >/dev/null 2>&1 && command -v "python3.$$n" && exit 0; \
	done; command -v python3)
PIP             ?= $(PYTHON) -m pip
UVICORN         ?= uvicorn

VENV            ?= .venv-$(PKG_INTERNAL)
PYTHON_BIN      ?= $(VENV)/bin/python
PIP_BIN         ?= $(VENV)/bin/pip

DEV		?= 1
WARMUP_DEFAULT  ?= false
BENCH_WARMUP_DEFAULT ?= true
DATA_DIR	?= ./data
CONFIG          ?=
AUTH_MODE	?= none
GLOBAL_KEY      ?= $(CHECK_TOKEN)
HOST            ?= 0.0.0.0
PORT            ?= 8086
WORKERS         ?= 1
RELOAD          ?= 1
LOG_LEVEL       ?= debug
OPS_LOG         ?= null
ACCESS_LOG      ?= null
INSTANCE_NAME   ?= dev

# Native sbert/torch embedder wheel: 0 = platform default (CUDA on Linux),
# 1 = CPU-only wheel (no NVIDIA CUDA). See header note.
TORCH_WHEEL_CPU ?= 0
REQ_MAIN_CPU    ?= requirements-cpu.txt

# Version helpers - pave/version.py source of truth
VERSION         ?= $(shell sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' $(PKG_INTERNAL)/version.py | head -1)
RELEASE_ARTIFACT ?= 0
_TARGET_TAG     := v$(VERSION)
_HEAD_SHORT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null || echo nogit)
_HEAD_EXACT_TAG := $(shell git describe --tags --exact-match HEAD 2>/dev/null || true)
_HEAD_IS_CLEAN  := $(shell if git diff --quiet --ignore-submodules HEAD -- 2>/dev/null && git diff --cached --quiet --ignore-submodules -- 2>/dev/null; then echo 1; else echo 0; fi)
_TARGET_TAG_EXISTS := $(shell git rev-parse -q --verify refs/tags/$(_TARGET_TAG) >/dev/null 2>&1 && echo 1 || echo 0)
_COMMITS_SINCE_TARGET_TAG := $(shell git rev-list --count $(_TARGET_TAG)..HEAD 2>/dev/null || echo 0)
_COMMITS_SINCE_NEAREST_TAG := $(shell d=$$(git describe --tags --long --match 'v[0-9]*' 2>/dev/null | awk -F- '{print $$(NF-1)}'); echo $${d:-0})
_USE_RELEASE_VERSION := $(if $(filter 1,$(RELEASE_ARTIFACT)),1,$(if $(filter $(_TARGET_TAG),$(_HEAD_EXACT_TAG)),$(_HEAD_IS_CLEAN),0))
PUSH_LATEST     ?= $(_USE_RELEASE_VERSION)
_PYPI_DIRTY_SUFFIX := $(if $(filter 1,$(_HEAD_IS_CLEAN)),,.dirty)
_ARCHIVE_DIRTY_SUFFIX := $(if $(filter 1,$(_HEAD_IS_CLEAN)),,-dirty)
# If v$(VERSION) exists, a non-release build is after that release:
# use post0.devN so Python ordering is > VERSION. If the tag does not
# exist, VERSION is a future target: use devN so ordering is < VERSION.
_DEV_PYPI_VERSION := $(if $(filter 1,$(_TARGET_TAG_EXISTS)),$(VERSION).post0.dev$(_COMMITS_SINCE_TARGET_TAG),$(VERSION).dev$(_COMMITS_SINCE_NEAREST_TAG))
_DEV_ARTIFACT_VERSION := $(if $(filter 1,$(_TARGET_TAG_EXISTS)),$(VERSION)-$(_COMMITS_SINCE_TARGET_TAG)-g$(_HEAD_SHORT_SHA),$(VERSION)-dev$(_COMMITS_SINCE_NEAREST_TAG)-g$(_HEAD_SHORT_SHA))
BUILD_VERSION   ?= $(if $(filter 1,$(_USE_RELEASE_VERSION)),$(VERSION),$(_DEV_PYPI_VERSION)+g$(_HEAD_SHORT_SHA)$(_PYPI_DIRTY_SUFFIX))
ARCHIVE_VERSION ?= $(if $(filter 1,$(_USE_RELEASE_VERSION)),$(VERSION),$(_DEV_ARTIFACT_VERSION)$(_ARCHIVE_DIRTY_SUFFIX))
ARCHIVE_BASENAME := $(PKG_NAME)-$(ARCHIVE_VERSION)
DIST_DIR        := dist
BUILD_DIR       := build
ART_DIR         := artifacts

# Docker / publish
DOCKERFILE      ?= Dockerfile
CONTEXT         ?= .
RELEASE_PUBLISH ?= 0
PYPI_BUILD      ?= $(if $(filter 1,$(RELEASE_PUBLISH)),1,0)
DOCKER_BUILD    ?= $(if $(filter 1,$(RELEASE_PUBLISH)),1,0)
# Space-separated remote lists used by `make release`.
# Defaults support canonical `gitlab` and legacy `flowlexi` names.
RELEASE_TAG_REMOTES_FINAL      ?= gitlab flowlexi origin
RELEASE_TAG_REMOTES_PRERELEASE ?= gitlab flowlexi
RELEASE_BRANCH_REMOTES         ?= gitlab flowlexi origin
REGISTRY        ?= $(REGISTRY_HOST)/$(REGISTRY_GROUP)/$(PKG_NAME)
IMAGE_NAME 	?= $(PKG_NAME)

# Release image variants: cpu | cuda | both (default). cpu/cuda also pin the
# torch wheel (so IMAGE_TAG below matches); `both` builds one CPU and one CUDA
# image. CI overrides this.
RELEASE_TORCH ?= both
ifeq ($(RELEASE_TORCH),cpu)
  override TORCH_WHEEL_CPU := 1
else ifeq ($(RELEASE_TORCH),cuda)
  override TORCH_WHEEL_CPU := 0
endif

ifeq ($(TORCH_WHEEL_CPU),1)
    IMAGE_TAG 	:= $(ARCHIVE_VERSION)-cpu
    LATEST_TAG 	:= latest-cpu
else
    IMAGE_TAG 	:= $(ARCHIVE_VERSION)-cuda
    LATEST_TAG 	:= latest-cuda
endif
BUILD_VARIANT 	:= $(if $(filter 1,$(TORCH_WHEEL_CPU)),cpu,cuda)
BUILD_ID 	?= $(shell date -u +%Y%m%d%H%M%S)-$(shell git rev-parse --short HEAD)-$(BUILD_VARIANT)

DOC_REF_TARGETS := docs-ref-cli docs-ref-config docs-ref-openapi
DOC_REF_CHECK_TARGETS := docs-ref-check-cli docs-ref-check-config docs-ref-check-openapi

.ONESHELL:
# With .ONESHELL enabled, keep recipe failure semantics strict so later
# status messages do not run after an earlier command has already failed.
.SHELLFLAGS := -e -o pipefail -c

.PHONY: help
help:
	@B=$$'\033[1m'; R=$$'\033[0m'; \
	echo "$(PKG_LONGNAME) Make Targets"; \
	echo ""; \
	echo "Setup:"; \
	echo "  $${B}install$${R}          Install runtime deps (TORCH_WHEEL_CPU=1 for CPU-only torch)"; \
	echo "  install-dev      Install dev/test deps (TORCH_WHEEL_CPU=1 for CPU-only torch)"; \
	echo "  venv             Create local virtualenv (.venv)"; \
	echo ""; \
	echo "Run:"; \
	echo "  $${B}serve$${R}           Run API server (DEV: PAVEDB_DEV=1, auth=none, loopback)"; \
	echo "  cli              Run CLI (pass ARGS='...')"; \
	echo ""; \
	echo "Build:"; \
	echo "  build            Build sdist+wheel to ./dist (git-suffixed unless release)"; \
	echo "  package          Build source archives to ./artifacts (git-suffixed unless release)"; \
	echo "  docker-build     Build Docker image (VERSION=x.y.z)"; \
	echo "  docker-check     Run alive check against a prebuilt local Docker image"; \
	echo ""; \
	echo "Docs:"; \
	echo "  docs-refresh     Regenerate committed reference files"; \
	echo "  docs-ref-check   Check committed reference files are current"; \
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
	echo "  bench-latency-rate-limited  Latency including tenant rate-limit writes"; \
	echo "  bench-stress     Stress test (STR_LENGTH=seconds, STR_CONCUR, STR_SUITE=base|critical|full)"; \
	echo "  bench-stress-full Full-suite stress test (alias for STR_SUITE=full bench-stress)"; \
	echo "    BENCH_WARMUP_DEFAULT=true warms the benchmark server"; \
	echo "    BENCH_SAVE=1 to save outputs in benchmarks/results/"; \
	echo "    BENCH_TAG=<tag> adds suffix to saved filenames"; \
	echo "  LAT_SLO_P99_MS    Fail bench-latency if p99 > N ms (0=off)"; \
	echo "  STR_MAX_ERROR_PCT Fail bench-stress if error% > N (0=off)"; \
	echo ""; \
	echo "Release:"; \
	echo "  $${B}release$${R}         Bump/test/package/tag; CI builds by default"; \
	echo "    PYPI_BUILD=1 and/or DOCKER_BUILD=1 to build locally"; \
	echo "    RELEASE_PUBLISH=1 to build and publish locally"; \
	echo "  bump             Bump release version in $(PKG_INTERNAL)/version.py"; \
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
.PHONY: _venv-create
_venv-create:
	@if ! command -v $(PYTHON) >/dev/null 2>&1; then echo "ERROR: '$(PYTHON)' not found"; exit 127; fi
	@echo "⏳ Creating virtual environment in $(VENV) using: $(PYTHON)"
	@rm -rf $(VENV)
	@$(PYTHON) -m venv $(VENV) --prompt $(PKG_NAME)
	@$(PIP_BIN) install -q --upgrade pip
	@touch $(VENV)/.created

.PHONY: venv
venv:
	@if [ ! -f "$(VENV)/.created" ] \
	  || ! "$(PYTHON_BIN)" -c 'import sys; print(sys.executable)' \
	      >/dev/null 2>&1 \
	  || ! "$(PYTHON_BIN)" -c 'import sys; raise SystemExit(not ($(_PY_MIN) <= sys.version_info[1] <= $(_PY_MAX)))' >/dev/null 2>&1 \
	  || ! "$(PIP_BIN)" --version >/dev/null 2>&1; then \
	  $(MAKE) _venv-create; \
	fi
	@echo "✅ Virtual env ready 👉 Run: source $(VENV)/bin/activate"

# -------- install --------
define install_main
	@if [ "$(TORCH_WHEEL_CPU)" = "1" ]; then \
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
	@$(PIP_BIN) install -q ".[test]" build twine wheel
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
	  PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(BENCH_WARMUP_DEFAULT) \
	  PAVEDB_DATA_DIR=$(DATA_DIR) \
	  PAVEDB_AUTH__MODE=$(AUTH_MODE) \
	  PAVEDB_AUTH__GLOBAL_KEY=$(GLOBAL_KEY) \
	  PAVEDB_LOG__LEVEL=$(LOG_LEVEL) \
	  PAVEDB_SERVER__HOST=$(HOST) \
	  PAVEDB_SERVER__PORT=$(PORT) \
	  PAVEDB_SERVER__WORKERS=$(WORKERS) \
	  PAVEDB_SERVER__RELOAD=$(RELOAD) \
	  PAVEDB_INSTANCE__NAME=$(INSTANCE_NAME) \
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

.PHONY: docs-refresh $(DOC_REF_TARGETS)
docs-refresh: $(DOC_REF_TARGETS)

docs-ref-cli: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/cli_to_markdown.py

docs-ref-config: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/config_reference.py

docs-ref-openapi: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/openapi_snapshot.py

.PHONY: docs-ref-check $(DOC_REF_CHECK_TARGETS)
docs-ref-check: $(DOC_REF_CHECK_TARGETS)

docs-ref-check-cli: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/cli_to_markdown.py --check

docs-ref-check-config: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/config_reference.py --check

docs-ref-check-openapi: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/openapi_snapshot.py --check

# -------- third-party licenses --------
# Refresh THIRD_PARTY_LICENSES.md from the resolved runtime closure. Tracks
# external resolution (pip picks latest compatible), so it is a point-in-time
# reference refreshed on demand — not a docs-ref-check drift gate.
.PHONY: third-party-licenses
third-party-licenses: venv
	PYTHONPATH=. $(PYTHON_BIN) scripts/gen_third_party.py

# -------- bump --------
.PHONY: bump
bump:
	@if [ -z "$(VERSION)" ]; then \
	  echo "Error: VERSION is not set. Usage: make bump VERSION=0.5.4"; \
	  exit 1; \
	fi
	@echo "Bumping version to $(VERSION)..."

	# pave/version.py: VERSION = "x.y.z"
	@perl -0pi -e 's/^VERSION = ".*"/VERSION = "$(VERSION)"/m' $(PKG_INTERNAL)/version.py
	@echo "✅ Bumped to $(VERSION). Review changes, then commit:"
	@echo "   git add -A && git commit -m \"chore: bump version to $(VERSION)\""

# -------- build / artifacts --------
.PHONY: build
build: install
	@if [ "$(FORCE)" != "1" ] && [ -z "$(_PYPI_DIRTY_SUFFIX)" ] && \
	  ls $(DIST_DIR)/*-$(BUILD_VERSION)*.whl $(DIST_DIR)/*-$(BUILD_VERSION).tar.gz >/dev/null 2>&1; then \
	  echo "dist/ already holds $(BUILD_VERSION) from a clean tree; skipping (FORCE=1 rebuilds)."; \
	  exit 0; \
	fi; \
	$(MAKE) --no-print-directory -o install _build-dist

.PHONY: _build-dist
_build-dist: install
	rm -rf $(DIST_DIR) $(BUILD_DIR)
	$(PYTHON_BIN) -m pip install -q build twine
	@echo "Building Python distributions for $(BUILD_VERSION)"
	PAVEDB_BUILD_VERSION="$(BUILD_VERSION)" $(PYTHON_BIN) -m build
	$(PYTHON_BIN) scripts/check_dist_manifest.py $(DIST_DIR)/*
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
	sdist="$(DIST_DIR)/$(PYPI_DIST_NAME)-$(BUILD_VERSION).tar.gz"; \
	if [ -f "$$sdist" ]; then \
	  cp "$$sdist" $(ART_DIR)/$(ARCHIVE_BASENAME).tar.gz; \
	else \
	  git archive --format=tar.gz --output $(ART_DIR)/$(ARCHIVE_BASENAME).tar.gz HEAD; \
	fi
	@echo "✅ Artifacts available in $(ART_DIR)/"

# -------- docker build/push --------
.PHONY: docker-build
docker-build: install
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION=x.y.z (e.g., 'make docker-build VERSION=0.5.4')"; exit 1; fi
	@echo "Docker image tag: $(IMAGE_TAG)"
	@set -e; if [ "$(RELEASE_TORCH)" = "both" ]; then \
	  $(MAKE) docker-build VERSION=$(VERSION) RELEASE_ARTIFACT=$(RELEASE_ARTIFACT) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cuda; \
	  $(MAKE) docker-build VERSION=$(VERSION) RELEASE_ARTIFACT=$(RELEASE_ARTIFACT) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cpu; \
	elif [ -n "$(REGISTRY)" ]; then \
	  echo "Building $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) from $(DOCKERFILE)"; \
	  DOCKER_BUILDKIT=1 docker build --progress=plain --build-arg TORCH_WHEEL_CPU=$(TORCH_WHEEL_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) -f $(DOCKERFILE) $(CONTEXT); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker tag $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG) $(REGISTRY)/$(IMAGE_NAME):$(LATEST_TAG); fi; \
	else \
	  echo "Building $(IMAGE_NAME):$(IMAGE_TAG) from $(DOCKERFILE)"; \
	  DOCKER_BUILDKIT=1 docker build --progress=plain --build-arg TORCH_WHEEL_CPU=$(TORCH_WHEEL_CPU) --build-arg BUILD_ID=$(BUILD_ID) -t $(IMAGE_NAME):$(IMAGE_TAG) -f $(DOCKERFILE) $(CONTEXT); \
	  if [ "$(PUSH_LATEST)" = "1" ]; then docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(IMAGE_NAME):$(LATEST_TAG); fi; \
	fi

.PHONY: docker-push
docker-push:
	@if [ -z "$(VERSION)" ]; then echo "Set VERSION=x.y.z (e.g., 'make docker-push VERSION=0.5.4')"; exit 1; fi
	@set -e; if [ "$(RELEASE_TORCH)" = "both" ]; then \
	  $(MAKE) docker-push VERSION=$(VERSION) RELEASE_ARTIFACT=$(RELEASE_ARTIFACT) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cuda; \
	  $(MAKE) docker-push VERSION=$(VERSION) RELEASE_ARTIFACT=$(RELEASE_ARTIFACT) REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cpu; \
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
	PKG_WHERE="not published (delegated to CI)"; \
	DOCKER_PUBLISHED="no"; \
	DOCKER_WHERE="not published (delegated to CI)"; \
	IMAGE_REFS=""; \
	BRANCH_REMOTES="$(RELEASE_BRANCH_REMOTES)"; \
	TAG_TOUCHED=0; \
	POST_TAG=0; \
	revert_changes() { \
	  echo "Reverting version bumps and changelog..."; \
	  git restore --staged CHANGELOG.md $(PKG_INTERNAL)/version.py docs/reference/cli.md docs/reference/pavecli.1 openapi.json 2>/dev/null || true; \
	  git checkout -- CHANGELOG.md $(PKG_INTERNAL)/version.py docs/reference/cli.md docs/reference/pavecli.1 openapi.json 2>/dev/null || true; \
	}; \
	trap 'status=$$?; if [ "$$status" -ne 0 ] && [ "$$POST_TAG" = "1" ] && [ "$$TAG_TOUCHED" = "1" ]; then echo "Release failed after tagging; deleting tag v$(VERSION) (keeping bumped commit)."; git tag -d "v$(VERSION)" >/dev/null 2>&1 || true; fi; exit $$status' ERR; \
	case "$(PYPI_BUILD)" in 0|1) ;; *) echo "Invalid PYPI_BUILD=$(PYPI_BUILD); expected 0 or 1."; exit 1 ;; esac; \
	case "$(DOCKER_BUILD)" in 0|1) ;; *) echo "Invalid DOCKER_BUILD=$(DOCKER_BUILD); expected 0 or 1."; exit 1 ;; esac; \
	case "$(RELEASE_PUBLISH)" in 0|1) ;; *) echo "Invalid RELEASE_PUBLISH=$(RELEASE_PUBLISH); expected 0 or 1."; exit 1 ;; esac; \
	if [ "$(RELEASE_PUBLISH)" = "1" ]; then \
	  if [ "$(PYPI_BUILD)" != "1" ]; then \
	    echo "Invalid flags: RELEASE_PUBLISH=1 requires PYPI_BUILD=1; omit PYPI_BUILD to build implicitly."; \
	    exit 1; \
	  fi; \
	  if [ "$(DOCKER_BUILD)" != "1" ]; then \
	    echo "Invalid flags: RELEASE_PUBLISH=1 requires DOCKER_BUILD=1; omit DOCKER_BUILD to build implicitly."; \
	    exit 1; \
	  fi; \
	fi; \
	echo "Bumping to $(VERSION) via 'make bump'..."; \
	$(MAKE) bump VERSION=$(VERSION) || { echo "Version bump failed."; revert_changes; exit 1; }; \
	$(MAKE) changelog-write VERSION=$(VERSION) || { echo "Changelog generation failed."; revert_changes; exit 1; }; \
	$(MAKE) docs-refresh || { echo "Reference doc refresh failed."; revert_changes; exit 1; }; \
	echo "Checking generated reference docs..."; \
	$(MAKE) docs-ref-check || { echo "Reference docs are stale."; revert_changes; exit 1; }; \
	echo "Running tests..."; \
	$(MAKE) test || { echo "Tests failed."; revert_changes; exit 1; }; \
	if [ "$(PYPI_BUILD)" = "1" ]; then \
	  echo "Building dists..."; \
	  $(MAKE) build VERSION=$(VERSION) RELEASE_ARTIFACT=1 || { echo "Build failed."; revert_changes; exit 1; }; \
	  echo "Running build-check runner..."; \
	  $(MAKE) _build-check-run || { echo "build-check failed."; revert_changes; exit 1; }; \
	else \
	  echo "Delegating PyPI build to CI (PYPI_BUILD=0)."; \
	fi; \
	$(MAKE) -o build package VERSION=$(VERSION) RELEASE_ARTIFACT=1 || { echo "Packaging failed."; revert_changes; exit 1; }; \
	if [ -n "$(REGISTRY)" ]; then IMAGE_BASE="$(REGISTRY)/$(IMAGE_NAME)"; else IMAGE_BASE="$(IMAGE_NAME)"; fi; \
	if [ "$(RELEASE_TORCH)" = "both" ]; then \
	  IMAGE_REFS="$(REGISTRY)/$(IMAGE_NAME):$(VERSION)-cuda $(REGISTRY)/$(IMAGE_NAME):$(VERSION)-cpu"; \
	  if [ "$(PUSH_LATEST)" = "1" ]; then IMAGE_REFS="$$IMAGE_REFS $(REGISTRY)/$(IMAGE_NAME):latest-cuda $(REGISTRY)/$(IMAGE_NAME):latest-cpu"; fi; \
	else \
	  if [ "$(TORCH_WHEEL_CPU)" = "1" ]; then IMG_SUFFIX="cpu"; else IMG_SUFFIX="cuda"; fi; \
	  IMAGE_REFS="$(REGISTRY)/$(IMAGE_NAME):$(VERSION)-$$IMG_SUFFIX"; \
	  if [ "$(PUSH_LATEST)" = "1" ]; then IMAGE_REFS="$$IMAGE_REFS $(REGISTRY)/$(IMAGE_NAME):latest-$$IMG_SUFFIX"; fi; \
	fi; \
	if [ "$(DOCKER_BUILD)" = "1" ]; then \
	  if [ "$(RELEASE_PUBLISH)" = "1" ]; then \
	    echo "Building and validating Docker image(s)..."; \
	  else \
	    echo "Building and validating Docker image(s) (no push)..."; \
	  fi; \
	  if [ "$(RELEASE_TORCH)" = "both" ]; then \
	    $(MAKE) docker-build VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cuda || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-cuda" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	    $(MAKE) docker-build VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cpu || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-cpu" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	  else \
	    $(MAKE) docker-build VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" TORCH_WHEEL_CPU=$(TORCH_WHEEL_CPU) || { echo "Docker build failed."; revert_changes; exit 1; }; \
	    $(MAKE) _docker-check-run DOCKER_CHECK_IMAGE="$$IMAGE_BASE:$(VERSION)-$$IMG_SUFFIX" || { echo "docker-check failed."; revert_changes; exit 1; }; \
	  fi; \
	else \
	  echo "Delegating Docker build to CI (DOCKER_BUILD=0)."; \
	fi; \
	git add CHANGELOG.md $(PKG_INTERNAL)/version.py docs/reference/cli.md docs/reference/pavecli.1 openapi.json 2>/dev/null || true; \
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
	if [ "$(RELEASE_PUBLISH)" = "1" ]; then \
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
	if [ "$(RELEASE_PUBLISH)" = "1" ]; then \
	  echo "Publishing Docker image(s)..."; \
	  if [ "$(RELEASE_TORCH)" = "both" ]; then \
	    $(MAKE) docker-push VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cuda; \
	    $(MAKE) docker-push VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" RELEASE_TORCH=cpu; \
	  else \
	    $(MAKE) docker-push VERSION=$(VERSION) RELEASE_ARTIFACT=1 REGISTRY="$(REGISTRY)" IMAGE_NAME="$(IMAGE_NAME)" TORCH_WHEEL_CPU=$(TORCH_WHEEL_CPU); \
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
	if [ "$(PYPI_BUILD)" = "1" ]; then \
	  for f in dist/*; do [ -e "$$f" ] && echo "  - $$f"; done; \
	else \
	  echo "  - local dist build skipped (PYPI_BUILD=0)"; \
	fi; \
	echo "  Published: $$PKG_PUBLISHED"; \
	echo "  Destination: $$PKG_WHERE"; \
	echo ""; \
	echo "Docker images:"; \
	if [ "$(DOCKER_BUILD)" = "1" ]; then \
	  for img in $$IMAGE_REFS; do echo "  - $$img"; done; \
	else \
	  echo "  - local Docker build skipped (DOCKER_BUILD=0)"; \
	fi; \
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
# commit-msg records Reported-by from the closed issue; changelog thanks them.
.PHONY: hooks
hooks:
	@git config core.hooksPath scripts/hooks && echo "git hooks: scripts/hooks"

.PHONY: changelog
changelog: venv
	@if [ -z "$(VERSION)" ]; then echo "VERSION not detected ($(PKG_INTERNAL)/version.py). Set VERSION=x.y.z if needed."; exit 1; fi
	@TMPFILE=$$(mktemp); \
	cp CHANGELOG.md $$TMPFILE; \
	CHANGELOG_SILENT=1 CHANGELOG_PATH=$$TMPFILE $(PYTHON_BIN) scripts/update_changelog.py $(VERSION); \
	awk 'BEGIN{p=0} /^## /{p=1} p{print} /^---/{exit}' $$TMPFILE; \
	rm -f $$TMPFILE

.PHONY: changelog-write
changelog-write: venv
	@if [ -z "$(VERSION)" ]; then echo "VERSION not detected ($(PKG_INTERNAL)/version.py). Set VERSION=x.y.z if needed."; exit 1; fi
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
BUILD_CHECK_OPENAI_MODEL ?= text-embedding-3-small
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
	temp_site=""; \
	check_pythonpath=""; \
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
	temp_site="$$( "$$venv_dir/bin/python" -c 'import site; print(site.getsitepackages()[0])' )"; \
	check_pythonpath="$$temp_site:$$dep_site"; \
	echo "==> Installing local wheel $$wheel (no deps)"; \
	"$$venv_dir/bin/pip" install -q --no-deps "$$wheel"; \
	echo "==> Initializing instance via installed pavecli"; \
	PYTHONPATH="$$check_pythonpath" \
	"$$venv_dir/bin/pavecli" --compact init "$$instance_dir" >/dev/null; \
	test -f "$$instance_dir/config.yml"; \
	test -f "$$instance_dir/tenants.yml"; \
	test -d "$$instance_dir/data"; \
	echo "==> Checking installed CLI bootstrap"; \
	PYTHONPATH="$$check_pythonpath" \
	PAVEDB_DEV=1 \
	PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(WARMUP_DEFAULT) \
	PAVEDB_EMBEDDER__DEFAULT=openai \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__TYPE=openai \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__MODEL=$(BUILD_CHECK_OPENAI_MODEL) \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__API_KEY=$(BUILD_CHECK_OPENAI_KEY) \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__CONFIG__DIM=$(BUILD_CHECK_OPENAI_DIM) \
	PAVEDB_AUTH__MODE=none \
	"$$venv_dir/bin/pavecli" --compact --home "$$instance_dir" list-tenants >/dev/null; \
	echo "==> Starting installed pavesrv on $(BUILD_CHECK_URL)"; \
	PYTHONPATH="$$check_pythonpath" \
	PAVEDB_DEV=1 \
	PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(WARMUP_DEFAULT) \
	PAVEDB_EMBEDDER__DEFAULT=openai \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__TYPE=openai \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__MODEL=$(BUILD_CHECK_OPENAI_MODEL) \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__API_KEY=$(BUILD_CHECK_OPENAI_KEY) \
	PAVEDB_EMBEDDER__INSTANCES__OPENAI__CONFIG__DIM=$(BUILD_CHECK_OPENAI_DIM) \
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
build-check: install-dev
	@$(MAKE) -o venv _build-check-artifacts
	@$(MAKE) -o venv _build-check-run

# -------- docker image smoke test --------
DOCKER_CHECK_HOST ?= 127.0.0.1
DOCKER_CHECK_PORT ?= 18089
DOCKER_CHECK_URL  ?= http://$(DOCKER_CHECK_HOST):$(DOCKER_CHECK_PORT)
DOCKER_CHECK_TIMEOUT_S ?= 45
DOCKER_CHECK_OPENAI_KEY ?= docker-check-dummy-key
DOCKER_CHECK_OPENAI_MODEL ?= text-embedding-3-small
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
	  echo "Build it first with: make docker-build VERSION=$(VERSION) TORCH_WHEEL_CPU=$(TORCH_WHEEL_CPU)"; \
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
	  -e PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(WARMUP_DEFAULT) \
	  -e PAVEDB_AUTH__MODE=none \
	  -e PAVEDB_EMBEDDER__DEFAULT=openai \
	  -e PAVEDB_EMBEDDER__INSTANCES__OPENAI__TYPE=openai \
	  -e PAVEDB_EMBEDDER__INSTANCES__OPENAI__MODEL=$(DOCKER_CHECK_OPENAI_MODEL) \
	  -e PAVEDB_EMBEDDER__INSTANCES__OPENAI__API_KEY=$(DOCKER_CHECK_OPENAI_KEY) \
	  -e PAVEDB_EMBEDDER__INSTANCES__OPENAI__CONFIG__DIM=$(DOCKER_CHECK_OPENAI_DIM) \
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
	  PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(BENCH_WARMUP_DEFAULT) \
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
	  PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(BENCH_WARMUP_DEFAULT) \
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
BENCH_EMBEDDER_MODEL ?= sentence-transformers/all-MiniLM-L6-v2
BENCH_EMBEDDER_RUNTIME ?= auto
BENCH_EMBEDDER_DEVICE ?= auto
BENCH_TENANT_MAX_CONCURRENT ?= -1
STR_LENGTH        ?= 90
STR_CONCUR        ?= 8
STR_SUITE         ?= base
REL_PROFILE       ?= tatoeba-core-xling
REL_MODEL_ID      ?= multi-minilm-l12
BENCH_TAG         ?=
_ts          ?= $(shell date -u +%Y-%m-%d_%H%M%S)
_results_dir ?= benchmarks/results
BENCH_SAVE        ?= 0
LAT_SLO_P99_MS    ?= 0
STR_MAX_ERROR_PCT ?= 0
BENCH_STARTUP_TIMEOUT_S ?= 30
_server_log_level ?= warning

.PHONY: _bench-with-server
_bench-with-server:
	@if [ -z "$(_run_target)" ]; then echo "ERROR: _run_target is not set."; exit 1; fi
	@set -e; \
	if [ -n "$(BENCH_SERVER_URL)" ] && [ "$(BENCH_FORCE_EPHEMERAL)" = "1" ]; then \
	  echo "ERROR: BENCH_SERVER_URL and BENCH_FORCE_EPHEMERAL=1 are mutually exclusive."; \
	  exit 1; \
	fi; \
	bench_auth_mode=none; \
	if [ "$(_bench_rate_limit)" = "1" ]; then \
	  if [ "$(BENCH_FORCE_EPHEMERAL)" != "1" ]; then \
	    echo "ERROR: rate-limit benchmark requires an ephemeral server."; \
	    exit 1; \
	  fi; \
	  bench_auth_mode=static; \
	  export PAVEDB_AUTH__GLOBAL_KEY=bench-admin-token; \
	  export PAVEDB_AUTH__TENANTS_FILE=null; \
	  export PAVEDB_AUTH__API_KEYS='{"bench":"bench-rate-limit-token"}'; \
	  export PAVEDB_TENANTS__DEFAULT_MAX_RPM=$$(($(LAT_LENGTH) + 100)); \
	  export PAVEDB_TENANTS__DEFAULT_MAX_RPH=-1; \
	fi; \
	bench_url="$(_default_url)"; \
	managed=0; \
	data_dir=""; \
	log_file=""; \
	srv_pid=""; \
	keep_data_dir=0; \
	bench_status=0; \
	bench_clock() { \
	  clock_values=$$($(PYTHON) -c 'import time; print( \
	    time.monotonic_ns() // 1000000, time.time_ns() // 1000000)') || return; \
	  read -r now_ms wall_ms <<< "$$clock_values"; \
	}; \
	bench_clock; \
	startup_ms=$$now_ms; startup_wall_ms=$$wall_ms; \
	startup_deadline=$$((startup_ms + $(BENCH_STARTUP_TIMEOUT_S) * 1000)); \
	bench_timing() { \
	  bench_clock; \
	  echo "==> benchmark $$1: elapsed_ms=$$((now_ms - startup_ms))" \
	    "wall_elapsed_ms=$$((wall_ms - startup_wall_ms))" \
	    "wall_unix_ms=$$wall_ms bash_seconds=$$SECONDS"; \
	}; \
	if [ -n "$(BENCH_SERVER_URL)" ]; then \
	  bench_url="$(BENCH_SERVER_URL)"; \
	  if ! curl -q -fsS --connect-timeout 2 --max-time 2 \
	      "$$bench_url/health/live" >/dev/null 2>&1; then \
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
	  PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(BENCH_WARMUP_DEFAULT) \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE="$$bench_auth_mode" \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__MODEL=$(BENCH_EMBEDDER_MODEL) \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__RUNTIME=$(BENCH_EMBEDDER_RUNTIME) \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__DEVICE=$(BENCH_EMBEDDER_DEVICE) \
	  PAVEDB_LOG__LEVEL=$(_server_log_level) \
	  PAVEDB_LOG__OPS_LOG=$(OPS_LOG) \
	  PAVEDB_LOG__ACCESS_LOG=$(ACCESS_LOG) \
	  PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT=$(BENCH_TENANT_MAX_CONCURRENT) \
	  PAVEDB_SERVER__HOST=$(_ephemeral_host) \
	  PAVEDB_SERVER__PORT=$(_ephemeral_port) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	elif curl -q -fsS --connect-timeout 2 --max-time 2 \
	    "$$bench_url/health/live" >/dev/null 2>&1; then \
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
	  PAVEDB_EMBEDDER__WARMUP_DEFAULT=$(BENCH_WARMUP_DEFAULT) \
	  PAVEDB_DATA_DIR="$$data_dir" \
	  PAVEDB_AUTH__MODE=none \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__MODEL=$(BENCH_EMBEDDER_MODEL) \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__RUNTIME=$(BENCH_EMBEDDER_RUNTIME) \
	  PAVEDB_EMBEDDER__INSTANCES__NATIVE__CONFIG__DEVICE=$(BENCH_EMBEDDER_DEVICE) \
	  PAVEDB_LOG__LEVEL=$(_server_log_level) \
	  PAVEDB_LOG__OPS_LOG=$(OPS_LOG) \
	  PAVEDB_LOG__ACCESS_LOG=$(ACCESS_LOG) \
	  PAVEDB_TENANTS__DEFAULT_MAX_CONCURRENT=$(BENCH_TENANT_MAX_CONCURRENT) \
	  PAVEDB_SERVER__HOST=$(_ephemeral_host) \
	  PAVEDB_SERVER__PORT=$(_ephemeral_port) \
	  $(PYTHON_BIN) -m $(PKG_INTERNAL).main >"$$log_file" 2>&1 & \
	  srv_pid=$$!; \
	fi; \
	cleanup() { \
	  exit_status=$$?; \
	  trap - EXIT; \
	  trap '' INT TERM; \
	  if [ "$$managed" = "1" ]; then \
	    if [ -n "$$srv_pid" ] && kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	      bench_timing stopping; \
	      kill "$$srv_pid" >/dev/null 2>&1 || true; \
	      stop_deadline=$$((now_ms + 5000)); \
	      while kill -0 "$$srv_pid" >/dev/null 2>&1 && bench_clock && \
	          [ "$$now_ms" -lt "$$stop_deadline" ]; do sleep 0.1; done; \
	      if kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	        bench_timing killing; \
	        echo "ERROR: benchmark server did not stop in 5s; sending KILL."; \
	        keep_data_dir=1; \
	        if [ "$$exit_status" = "0" ]; then exit_status=1; fi; \
	        kill -KILL "$$srv_pid" >/dev/null 2>&1 || true; \
	        stop_deadline=$$((now_ms + 1000)); \
	        while kill -0 "$$srv_pid" >/dev/null 2>&1 && bench_clock && \
	            [ "$$now_ms" -lt "$$stop_deadline" ]; do sleep 0.1; done; \
	      fi; \
	      if kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	        echo "ERROR: benchmark server PID $$srv_pid still alive after KILL."; \
	      else \
	        wait "$$srv_pid" >/dev/null 2>&1 || true; \
	      fi; \
	    else \
	      srv_status=0; \
	      if [ -n "$$srv_pid" ]; then \
	        wait "$$srv_pid" >/dev/null 2>&1 || srv_status=$$?; \
	      fi; \
	      if [ "$$srv_status" != "0" ]; then \
	        keep_data_dir=1; \
	        if [ "$$exit_status" = "0" ]; then exit_status=$$srv_status; fi; \
	      fi; \
	    fi; \
	    if [ "$$exit_status" != "0" ]; then keep_data_dir=1; fi; \
	    if [ "$$keep_data_dir" = "1" ]; then \
	      echo "==> Preserving ephemeral server state at $$data_dir"; \
	      echo "==> Server log: $$log_file"; \
	      if [ "$(BENCH_SAVE)" = "1" ] && [ -f "$$log_file" ]; then \
	        kept="$(_results_dir)/server-$$(basename "$$data_dir").log"; \
	        if mkdir -p "$(_results_dir)" 2>/dev/null && \
	           cp "$$log_file" "$$kept" 2>/dev/null; then \
	          echo "==> Server log copied to $$kept"; \
	          echo "    (the line above is a path a CI artifact can reach;"; \
	          echo "     $$data_dir dies with the job)"; \
	        fi; \
	      fi; \
	    else \
	      rm -rf "$$data_dir"; \
	    fi; \
	  fi; \
	  exit "$$exit_status"; \
	}; \
	trap cleanup EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	if [ "$$managed" = "1" ]; then \
	  bench_timing starting; \
	  while :; do \
	    if ! kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	      srv_status=0; \
	      wait "$$srv_pid" >/dev/null 2>&1 || srv_status=$$?; \
	      srv_pid=""; \
	      keep_data_dir=1; \
	      echo "ERROR: benchmark server exited before becoming ready" \
	        "(status $$srv_status)."; \
	      tail -n 80 "$$log_file" || true; \
	      if [ "$$srv_status" = "0" ]; then srv_status=1; fi; \
	      exit "$$srv_status"; \
	    fi; \
	    bench_clock; \
	    remaining_ms=$$((startup_deadline - now_ms)); \
	    if [ "$$remaining_ms" -le 0 ]; then break; fi; \
	    if [ "$$remaining_ms" -gt 2000 ]; then remaining_ms=2000; fi; \
	    printf -v probe_timeout '%d.%03d' \
	      $$((remaining_ms / 1000)) $$((remaining_ms % 1000)); \
	    if curl -q -fsS --connect-timeout 2 --max-time "$$probe_timeout" \
	        "$$bench_url/health/live" >/dev/null 2>&1 && \
	        bench_clock && [ "$$now_ms" -lt "$$startup_deadline" ] && \
	        kill -0 "$$srv_pid" >/dev/null 2>&1; then \
	      ready=1; \
	      break; \
	    fi; \
	    bench_clock; \
	    remaining_ms=$$((startup_deadline - now_ms)); \
	    if [ "$$remaining_ms" -gt 0 ]; then \
	      if [ "$$remaining_ms" -gt 1000 ]; then remaining_ms=1000; fi; \
	      printf -v pause '%d.%03d' \
	        $$((remaining_ms / 1000)) $$((remaining_ms % 1000)); \
	      sleep "$$pause"; \
	    fi; \
	  done; \
	  if [ "$${ready:-0}" != "1" ]; then \
	    keep_data_dir=1; \
	    bench_timing startup-timeout; \
	    echo "ERROR: benchmark server did not become ready in $(BENCH_STARTUP_TIMEOUT_S)s."; \
	    tail -n 80 "$$log_file" || true; \
	    exit 1; \
	  fi; \
	  bench_timing ready; \
	fi; \
	$(MAKE) -o install-dev _url="$$bench_url" _data_dir="$$data_dir" \
	  _ts="$(_ts)" $(_run_target) || \
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

.PHONY: bench-latency-rate-limited
bench-latency-rate-limited: install-dev
	@$(MAKE) -o install-dev BENCH_FORCE_EPHEMERAL=1 \
	  BENCH_API_KEY=bench-rate-limit-token BENCH_TENANT_MAX_CONCURRENT=-1 \
	  _bench_rate_limit=1 _latency_suffix=-rate-limited bench-latency

.PHONY: bench-latency bench-latency-run
bench-latency: install-dev
	@summary=$$(mktemp "$${TMPDIR:-/tmp}/pavedb-bench-summary.XXXXXX"); \
	for filt in $$(echo "$(LAT_FILTERS)" | tr ',' ' '); do \
	  echo ""; \
	  echo "==> Latency$(_latency_suffix): filtering=$$filt"; \
	  $(MAKE) -o install-dev \
	    _run_target=bench-latency-run \
	    _filt=$$filt \
	    _summary_file=$$summary \
	    _ts="$(_ts)" \
	    _bench-with-server || { \
	      status=$$?; rm -f "$$summary"; exit "$$status"; \
	    }; \
	done; \
	rows=$$(awk 'END { print NR + 0 }' "$$summary"); \
	if [ "$$rows" -gt 1 ]; then \
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
	fi; \
	rm -f $$summary

bench-latency-run:
	@sha=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown"); \
	tag="$(BENCH_TAG)"; \
	api_arg=""; \
	summary_arg=""; \
	slo_arg=""; \
	rate_arg=""; \
	if [ "$(_bench_rate_limit)" = "1" ]; then \
	  rate_arg="--require-rate-limit"; \
	fi; \
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
	  PYTHONPATH=. $(PYTHON_BIN) -u benchmarks/search_latency.py \
	    --url $(_url) --queries $(LAT_LENGTH) \
	    --concurrency $(LAT_CONCUR) \
	    --filtering $(_filt) \
	    $$api_arg $$summary_arg $$slo_arg $$rate_arg \
	    | tee $(_results_dir)/latency-$(_ts)_$$tag-$(_filt)$(_latency_suffix).txt; \
	else \
	  echo "==> Saving: no (ts=$(_ts), tag=$$tag)"; \
	  PYTHONPATH=. $(PYTHON_BIN) -u benchmarks/search_latency.py \
	    --url $(_url) --queries $(LAT_LENGTH) \
	    --concurrency $(LAT_CONCUR) \
	    --filtering $(_filt) \
	    $$api_arg $$summary_arg $$slo_arg $$rate_arg; \
	fi

.PHONY: bench-stress bench-stress-full bench-stress-run
bench-stress: install-dev
	@$(MAKE) -o install-dev _run_target=bench-stress-run _ts="$(_ts)" _bench-with-server

bench-stress-full:
	@$(MAKE) -o install-dev STR_SUITE=full bench-stress

bench-stress-run:
	@sha=$$(git rev-parse --short HEAD 2>/dev/null || echo "unknown"); \
	tag="$(BENCH_TAG)"; \
	api_arg=""; \
	err_arg=""; \
	if [ -n "$(BENCH_API_KEY)" ]; then api_arg="--api-key $(BENCH_API_KEY)"; fi; \
	if [ "$(STR_MAX_ERROR_PCT)" != "0" ]; then \
	  err_arg="--max-error-pct $(STR_MAX_ERROR_PCT)"; \
	fi; \
	suite_arg="--suite $(STR_SUITE)"; \
	suite_tag=""; \
	if [ "$(STR_SUITE)" != "base" ]; then suite_tag="_$(STR_SUITE)"; fi; \
	if [ -z "$$tag" ]; then \
	  branch=$$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached"); \
	  tag="$$branch-$$sha"; \
	else \
	  tag="$$tag-$$sha"; \
	fi; \
	if [ "$(BENCH_SAVE)" = "1" ]; then \
	  echo "==> Saving: yes (ts=$(_ts), tag=$$tag, suite=$(STR_SUITE))"; \
	  mkdir -p $(_results_dir); \
	  PYTHONPATH=. $(PYTHON_BIN) -u benchmarks/stress.py \
	    --url $(_url) --duration $(STR_LENGTH) \
	    --concurrency $(STR_CONCUR) \
	    $$suite_arg $$api_arg $$err_arg \
	    2>&1 | tee $(_results_dir)/stress-$(_ts)_$$tag$$suite_tag.txt; \
	else \
	  echo "==> Saving: no (ts=$(_ts), tag=$$tag, suite=$(STR_SUITE))"; \
	  PYTHONPATH=. $(PYTHON_BIN) -u benchmarks/stress.py \
	    --url $(_url) --duration $(STR_LENGTH) \
	    --concurrency $(STR_CONCUR) \
	    $$suite_arg $$api_arg $$err_arg; \
	fi

# Chaos + correctness suite (P1-52 Level-1 prototype): drives a server and
# asserts invariants stress.py is blind to (silent corruption, cross-collection
# bleed) under concurrent churn. Exits non-zero on any violation. Reuses the
# ephemeral-server harness; reproducible via CHAOS_SEED.
CHAOS_SEED      ?= 1337
CHAOS_DURATION  ?= 120
CHAOS_CONCUR    ?= 24

.PHONY: chaos chaos-run
chaos: install-dev
	@$(MAKE) -o install-dev _run_target=chaos-run _ts="$(_ts)" _bench-with-server

chaos-run:
	@api_arg=""; \
	if [ -n "$(BENCH_API_KEY)" ]; then api_arg="--api-key $(BENCH_API_KEY)"; fi; \
	data_arg=(); \
	if [ -n "$(_data_dir)" ]; then data_arg=(--data-dir "$(_data_dir)"); fi; \
	PYTHONPATH=. $(PYTHON_BIN) benchmarks/chaos.py \
	  --url $(_url) --seed $(CHAOS_SEED) \
	  --duration $(CHAOS_DURATION) --concurrency $(CHAOS_CONCUR) \
	  $$api_arg "$${data_arg[@]}"

.PHONY: benchmark
benchmark: install-dev
	@ts="$(_ts)"; \
	$(MAKE) -o install-dev _ts="$$ts" bench-latency bench-stress

.PHONY: pypi-push pypitest-push

pypi-push: venv
	$(PYTHON_BIN) -m twine upload --skip-existing $(DIST_DIR)/*

pypitest-push: venv
	$(PYTHON_BIN) -m twine upload --skip-existing --repository testpypi $(DIST_DIR)/*
