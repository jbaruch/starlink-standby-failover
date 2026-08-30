FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./
COPY tests/ ./

# State (outage timers, last price, schedule bookkeeping) lives on a volume so a
# container restart mid-outage does not reset the debounce and re-notify.
ENV WATCHDOG_STATE=/data/state.json \
    WATCHDOG_KILL_SWITCH=/data/DISABLED \
    RECON_OUTPUT=/data/recon-output.json \
    STARLINK_SESSION_FILE=/run/secrets/starlink-session \
    UNIFI_API_KEY_FILE=/run/secrets/unifi-api-key
VOLUME ["/data"]

# Must match the host user that owns ./secrets — a bind mount keeps host
# ownership, and those files are 0600. Find yours with `id -u` / `id -g`:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=1000
ARG APP_GID=1000

RUN if ! getent group "${APP_GID}" > /dev/null; then \
      groupadd --gid "${APP_GID}" watchdog; \
    fi \
 && if ! getent passwd "${APP_UID}" > /dev/null; then \
      useradd --system --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home watchdog; \
    fi \
 && mkdir -p /data && chown "${APP_UID}:${APP_GID}" /data
USER ${APP_UID}:${APP_GID}

EXPOSE 8788

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/healthz',timeout=4).status==200 else 1)"

CMD ["python", "watchdog.py"]
