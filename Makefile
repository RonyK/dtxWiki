.PHONY: install dev-db-up dev-db-down migrate seed backend-dev frontend-dev export-schemas gen-client verify-gen-client verify-generated-schemas bundle-frontend build-wheel docker-image docker-build test-backend test-backend-serial test-backend-benchmarks test-frontend test-e2e test-packaged test-acceptance test-static test-release-gate test lint check-i18n sync-csp

PYTHON ?= python3
VENV ?= .venv
# Backend and release-gate suites distribute over this many processes.
# `auto` is deliberately not the default: it scales with the host core count,
# so the same command would prove something different on every machine.
PYTEST_WORKERS ?= 4
# These modules exercise SQLite write fencing and filesystem-backed media GC
# with their own threads.  They are deterministic in one pytest process, but
# are not safe to run beside xdist workers on Windows.  Keep the split here
# (rather than relying on a reviewer to remember a rerun) so every normal
# `make test-backend` invocation runs this SQLite media lane sequentially.
SQLITE_MEDIA_SERIAL_TESTS := backend/tests/integration/test_media_blob_gc.py backend/tests/integration/test_media_install_workflow.py
SQLITE_MEDIA_PARALLEL_IGNORES := $(foreach test,$(SQLITE_MEDIA_SERIAL_TESTS),--ignore=$(test))
# Modules holding wall-clock budgets (design §19.5.7). Each budget is a
# `*_benchmark_*` test that skips unless DTXWIKI_TEST_BENCHMARKS=1 and refuses
# to run under xdist; `test-backend-benchmarks` below is the only place that
# opts in. A new benchmark's module belongs on this list.
BENCHMARK_TESTS := backend/tests/integration/test_graph_service.py backend/tests/integration/test_graph_analytics_regressions.py backend/tests/unit/test_mcp_chat_service.py
VENV_PYTHON := $(VENV)/bin/python
COMPOSE := docker compose --env-file infra/local/.env.example -f infra/local/docker-compose.yml
DOCKER ?= docker
DOCKERFILE ?= Dockerfile
DOCKER_TAG ?= $(shell git describe --tags --match 'v[0-9]*' --dirty --always 2>/dev/null || git rev-parse --short HEAD)
DOCKER_IMAGE ?= dtxwiki:$(DOCKER_TAG)

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"
	npm install

dev-db-up:
	$(COMPOSE) up -d postgres

dev-db-down:
	$(COMPOSE) down

# The same bootstrap the CLI runs (`dtxwiki bootstrap`): it honours
# DTXWIKI_DATABASE_URL, refuses databases it cannot identify, and records the
# app version. A bare `alembic upgrade head` would instead migrate whatever URL
# alembic.ini names.
migrate:
	$(VENV_PYTHON) -m backend.cli.main bootstrap

seed:
	$(VENV_PYTHON) -m backend.scripts.seed_dev

backend-dev:
	$(VENV_PYTHON) -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port $${DTXWIKI_BACKEND_PORT:-21080}

frontend-dev:
	npm --workspace frontend run dev -- --host 127.0.0.1

export-schemas:
	mkdir -p shared/schemas/rest/v1 shared/schemas/mcp/v1
	$(VENV_PYTHON) -m backend.scripts.export_openapi > shared/schemas/rest/v1/openapi.json
	$(VENV_PYTHON) -m backend.scripts.export_mcp_schemas

gen-client:
	npm --workspace frontend run gen:client

verify-gen-client:
	set -eu; tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
	npm --workspace frontend exec openapi-typescript -- ../shared/schemas/rest/v1/openapi.json -o $$tmp >/dev/null; \
	diff -u --strip-trailing-cr frontend/src/shared/api/types.gen.ts $$tmp

# `git diff --exit-code` ignores untracked files, so a newly registered tool
# whose contract the exporter had just written for the first time slipped
# through as an untracked file and the gate still certified the directory as
# current. `git status --porcelain --untracked-files=all` reports additions,
# modifications and deletions alike, which is the whole drift set.
# tests/static/test_schema_drift.py runs this exact check line against a
# throwaway repo to keep it that way.
verify-generated-schemas:
	$(MAKE) export-schemas
	@drift=$$(git status --porcelain --untracked-files=all -- shared/schemas/rest/v1/openapi.json shared/schemas/mcp/v1) || { printf '%s\n' 'Could not read git status; the generated-artifact gate cannot certify anything.' >&2; exit 1; }; if [ -n "$$drift" ]; then printf '%s\n' 'Generated schema artifacts are not current:' "$$drift" 'Run `make export-schemas` and commit the result.' >&2; exit 1; fi
	$(MAKE) verify-gen-client

# --fresh: this destination becomes a wheel, and a wheel must contain exactly
# the bundle it was built from. Deploying to a directory that is being served
# uses the merging form instead, so an open tab can still fetch old chunks.
bundle-frontend:
	npm --workspace frontend run build
	$(VENV_PYTHON) scripts/deploy_static_bundle.py deploy --fresh frontend/dist backend/static

build-wheel: bundle-frontend
	rm -rf build dtxwiki.egg-info
	# Preserve the prior release artifacts for documented rollback operations.
	@archive=; for artifact in dist/dtxwiki-*.whl dist/dtxwiki-*.tar.gz; do if [ -e "$$artifact" ]; then if [ -z "$$archive" ]; then archive="dist/prev/$$(date -u +%Y%m%dT%H%M%SZ)-$$$$"; mkdir -p "$$archive"; fi; mv "$$artifact" "$$archive"/; fi; done
	$(VENV_PYTHON) -m build

docker-image:
	@echo $(DOCKER_IMAGE)

docker-build:
	$(DOCKER) build -f $(DOCKERFILE) -t $(DOCKER_IMAGE) .

test-backend:
	$(VENV_PYTHON) -m pytest backend/tests -q -n $(PYTEST_WORKERS) --dist loadgroup $(SQLITE_MEDIA_PARALLEL_IGNORES)
	$(VENV_PYTHON) -m pytest $(SQLITE_MEDIA_SERIAL_TESTS) -q

# Parity canary. `--dist loadgroup` keeps the PostgreSQL suites -- which reset
# the database they are pointed at -- on one worker, but distribution should
# never be the only way the suite is run. This target proves the same tests
# pass with no workers at all. It runs the functional suite only: the
# wall-clock budgets are `test-backend-benchmarks`, kept apart on purpose.
test-backend-serial:
	$(VENV_PYTHON) -m pytest backend/tests -q

# A latency measured on a shared runner, or beside xdist workers, cannot tell a
# slow host from a slow change, so the functional lanes assert counted work
# instead and the budgets run here: serially, on request, never in `make test`.
test-backend-benchmarks:
	DTXWIKI_TEST_BENCHMARKS=1 $(VENV_PYTHON) -m pytest $(BENCHMARK_TESTS) -q -k benchmark

test-frontend:
	npm --workspace frontend run test -- --run

test-e2e:
	npm run test:e2e

test-packaged: build-wheel
	$(VENV_PYTHON) tests/packaging/packaged_browser_smoke.py

test-acceptance:
	$(VENV_PYTHON) -m pytest tests/acceptance -q

test-static:
	$(VENV_PYTHON) -m pytest tests/static -q

# Twenty-three of these cases each run the release gate shell script end to
# end, and every run of it starts bash, node and a nested pytest. They own
# separate evidence directories, so distribute per test rather than per file.
test-release-gate:
	$(VENV_PYTHON) -m pytest tests/e2e -q -n $(PYTEST_WORKERS) --dist load

test: test-backend test-acceptance test-static test-release-gate test-frontend test-e2e

lint:
	# scripts/ is in scope because scripts/verify_wheel_artifact.py is
	# release-critical: it is the only thing standing between a broken
	# wheel and PyPI, and a syntax-level regression in it would otherwise
	# surface only when a v* tag is pushed.
	$(VENV_PYTHON) -m ruff check backend tests scripts
	npm --workspace frontend run lint

check-i18n:
	npm run check:i18n

sync-csp:
	$(VENV_PYTHON) -m backend.scripts.sync_csp_templates

-include Makefile.local
