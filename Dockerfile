FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m pm \
    && mkdir -p /config/logs /config/reports /plugins /media \
    && chown -R pm /app /config /plugins

USER pm

ENTRYPOINT ["python", "-m", "app.main"]
