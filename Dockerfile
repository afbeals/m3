FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# Install curl (for HEALTHCHECK) and tzdata (required for non-UTC TZ= values on slim images).
# Without tzdata, python:3.12-slim defaults to UTC regardless of the TZ env var.
RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m m3 \
    && mkdir -p /config/logs /config/reports /plugins /media \
    && chown -R m3 /app /config /plugins /media

USER m3

EXPOSE 8765

# Health check: /healthz returns 200 when uvicorn is ready.
# start-period=30s allows time for uvicorn to bind before Docker starts counting failures.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/healthz || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
