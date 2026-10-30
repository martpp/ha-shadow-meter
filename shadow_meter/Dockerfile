# https://contributors.home-assistant.io/apps/guides/dockerfile/
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24
FROM ${BUILD_FROM}

ARG BUILD_VERSION=dev

# Dependencies first (exact versions, cached layer), then the engine itself without re-resolving deps
COPY engine/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY engine /tmp/engine
RUN pip install --no-cache-dir --no-deps /tmp/engine \
    && rm -rf /tmp/engine

COPY rootfs /

# jemalloc ships with the Alpine base images and keeps the memory footprint low
ENV LD_PRELOAD="/usr/local/lib/libjemalloc.so.2"

LABEL \
    io.hass.type="app" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="Home Assistant App: Shadow Meter" \
    org.opencontainers.image.description="Detect unmetered devices from the whole-home power consumption" \
    org.opencontainers.image.source="https://github.com/martpp/ha-shadow-meter" \
    org.opencontainers.image.licenses="MIT"
