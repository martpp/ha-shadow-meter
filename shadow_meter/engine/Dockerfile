# Standalone image: run the Shadow Meter engine on any machine (see docker-compose.example.yml).
# The Home Assistant app uses ../Dockerfile instead.
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHADOW_METER_DATA=/data \
    SHADOW_METER_OPTIONS=/config/options.json \
    SHADOW_METER_HOST=0.0.0.0 \
    SHADOW_METER_PORT=8099

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY . /tmp/engine
RUN pip install --no-cache-dir --no-deps /tmp/engine && rm -rf /tmp/engine \
    && useradd --system --uid 10001 --home-dir /data shadowmeter \
    && mkdir -p /data /config && chown shadowmeter /data

USER shadowmeter
VOLUME ["/data"]
EXPOSE 8099
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8099), 3)"
CMD ["python", "-m", "shadow_meter"]
