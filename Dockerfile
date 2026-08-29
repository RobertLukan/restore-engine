# Build on a machine with internet: dependencies are baked into the image.
# The resulting image runs without pulling anything at runtime (air-gapped lab).

FROM python:3.12-slim-bookworm

ARG RESTORE_ENGINE_VERSION=0.1.0
ARG RESTORE_ENGINE_GIT_REVISION=unknown

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESTORE_ENGINE_CONFIG=/app/config.docker.yaml \
    RESTORE_ENGINE_VERSION=${RESTORE_ENGINE_VERSION} \
    RESTORE_ENGINE_GIT_REVISION=${RESTORE_ENGINE_GIT_REVISION}

WORKDIR /app

# Patch OS packages at build time; rebuild periodically so this layer picks up new fixes.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py worker.py ui.py states.py client_errors.py pbs_client.py pve_client.py sources.py plans.py jobs.py progress_parse.py queue_control.py pbs_wire.py reports.py notifications.py concurrency.py audit.py job_hygiene.py metrics_collect.py ./
COPY static/ ./static/
COPY config.docker.example.yaml /app/config.docker.yaml

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)"
