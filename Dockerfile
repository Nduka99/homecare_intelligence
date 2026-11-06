# Serving image. Build context is the repository root.
# Base pinned by digest: python:3.13-slim (Debian), checked 2026-11-04.
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

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

USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"]
CMD ["python", "-m", "homecare", "serve"]
