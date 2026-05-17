FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m pm \
    && mkdir -p /config/logs /config/reports /plugins /media \
    && chown -R pm /app /config /plugins /media

USER pm

# Health check: verify the scheduler is still alive by checking that the
# run_latest.txt report has been updated within the last 25 hours.
# This catches a hung or crashed scheduler without requiring an HTTP endpoint.
HEALTHCHECK --interval=1h --timeout=10s --start-period=25h --retries=2 \
    CMD test -f /config/reports/run_latest.txt \
        && test $(( $(date +%s) - $(date -r /config/reports/run_latest.txt +%s) )) -lt 90000 \
        || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
