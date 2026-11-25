# All-in-one image: nginx, WebUI, recorder, worker and livebot in one container.
# Built from the repository root. docker-compose.yml does not use it; it keeps
# running the code of the checkout.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg procps supervisor nginx fonts-dejavu-core ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin streamvault

WORKDIR /app
# Code stays root-owned: no program of the container can change it.
COPY . /app
RUN python -m compileall -q core recorder WEBUI \
    && docker/build-config.sh

ENV PYTHONPATH=/app \
    VOD_HOST=127.0.0.1 \
    VOD_PORT=8080 \
    VOD_TRUSTED_PROXIES=127.0.0.1,::1 \
    VOD_BASE_DIR=/app/data \
    RECORDINGS_BASE_DIR=/app/data \
    STREAMRECORDER_LOG_DIR=/app/data/.meta/logs \
    VOD_USE_X_ACCEL=true \
    LIVE_HLS_DIR=/app/.hls_live \
    LIVEBOT_HOST=127.0.0.1 \
    APP_UID=1000

VOLUME ["/app/data"]
EXPOSE 80
# Recordings need time to be finalized: run with --stop-timeout 75.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD curl -fsS http://127.0.0.1/login > /dev/null || exit 1
ENTRYPOINT ["/app/docker/entrypoint.sh"]
