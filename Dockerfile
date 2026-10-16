# syntax=docker/dockerfile:1
#
# Official dtxWiki image. Multi-stage: build the SPA, then install the Python
# package with the built UI bundled into backend/static (mirrors
# `make bundle-frontend` + `pip install .`).

# ---- Stage 1: build the frontend SPA -> frontend/dist ----
FROM node:22-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
COPY frontend ./frontend
RUN npm ci
RUN npm --workspace frontend run build

# ---- Stage 2: python runtime + dtxWiki (with bundled SPA) ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Container deployments historically run a separate worker service. Keep that
# behavior even when an operator upgrades the image with an older Compose file.
# A single-container deployment can opt in with DTXWIKI_WORKER_MODE=managed.
# Keep this default in the image: a rollback to 0.1.2 then also removes the new
# setting, which that version's unknown-environment guard would reject.
ENV DTXWIKI_WORKER_MODE=external

WORKDIR /app

LABEL org.opencontainers.image.title="dtxWiki" \
      org.opencontainers.image.licenses="AGPL-3.0-only OR Elastic-2.0"

RUN groupadd --system dtxwiki \
    && useradd --system --gid dtxwiki --home-dir /var/lib/dtxwiki dtxwiki \
    && mkdir -p /var/lib/dtxwiki /etc/dtxwiki \
    && chown -R dtxwiki:dtxwiki /var/lib/dtxwiki /etc/dtxwiki

# DEPLOYMENT.md is packaged as a data-file (pyproject [tool.setuptools.data-files]),
# so it must be present before `pip install .` or the build fails.
COPY pyproject.toml README.md DEPLOYMENT.md MANIFEST.in ./
COPY LICENSE LICENSE-AGPL-3.0 LICENSE-ELv2 NOTICE ./
COPY backend ./backend
COPY infra/prod ./infra/prod
COPY tests/packaging ./tests/packaging
# Bundled SPA from stage 1 (served by the backend from backend/static).
COPY --from=frontend /build/frontend/dist ./backend/static

# Distribution seam: use one trusted private package index/proxy when set. It
# must also provide dependencies; mixing a private index with public PyPI allows
# pip to select an unintended higher-version package.
ARG PIP_INDEX_URL=""
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        python -m pip install --index-url "$PIP_INDEX_URL" --upgrade pip && \
        python -m pip install --index-url "$PIP_INDEX_URL" . ; \
    else \
        python -m pip install --upgrade pip && \
        python -m pip install . ; \
    fi

COPY infra/prod/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER dtxwiki

EXPOSE 21080

# Entrypoint ensures a stable JWT key + session secret on the data volume and
# runs `dtxwiki bootstrap` once (idempotent) before exec-ing the command.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["dtxwiki", "serve", "--no-open", "--host", "0.0.0.0", "--port", "21080"]
