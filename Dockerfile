FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m pm \
    && mkdir -p /config/logs /config/reports /plugins /media \
    && chown -R pm /app /config /plugins /media

USER pm

EXPOSE 8765

# Health check: the web dashboard's /healthz endpoint returns 200 when the
# process is alive. No start-period needed — uvicorn starts in seconds.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/healthz || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
