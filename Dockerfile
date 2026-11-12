# Serving image. Build context is the repository root.
# Base pinned by digest: python:3.13-slim (Debian 13, Python 3.13.18), built 2026-11-05.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Debian's security fix for zlib (CVE-2026-76844, CVE-2026-85091, CVE-2026-27171), which this
# base predates; pinned to that exact version. Remove once the base image ships it.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
# Hash-locked: every package and version is fixed by uv.lock (scripts/check.py keeps them equal).
COPY requirements/serve.lock requirements/serve.lock
# pip is needed only to install. Removing it leaves no installer in the running container, and
# none of the libraries pip bundles (a vulnerability scan found 8 known issues in those copies).
RUN pip install --require-hashes --no-deps -r requirements/serve.lock \
    && pip uninstall --yes pip

# The package runs from source (PYTHONPATH), so no unpinned build backend is downloaded.
COPY LICENSE README.md ./
COPY src/ src/
# The commit shown in the footer and /healthz; last, so a new commit reuses every layer above.
ARG GIT_COMMIT=local
ENV GIT_COMMIT=$GIT_COMMIT

USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"]
CMD ["python", "-m", "homecare", "serve"]
