# syntax=docker/dockerfile:1.7

# Image officielle Playwright Python : Ubuntu 24.04 + Python 3.12 + Chromium
# preinstalle + toutes les dependances systeme (libs X, fonts, codecs).
# Version alignee avec celle utilisee en local (playwright 1.60.0).
FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

# Environnement Python : pas de bytecode disk, sortie non bufferisee
# (logs en temps reel via `docker logs`), pas de cache pip.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Installation des dependances Python en couche dediee (cache builder).
# On copie d abord le manifeste pour qu un changement de code ne casse pas
# le cache de pip install.
COPY pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
        "playwright>=1.47.0" \
        "playwright-stealth>=2.0.0" \
        "structlog>=24.1.0" \
        "pendulum>=3.0.0" \
        "pydantic>=2.7.0" \
        "pydantic-settings>=2.4.0" \
        "pyyaml>=6.0.1" \
        "watchdog>=4.0.0" \
        "tenacity>=8.2.3" \
        "streamlit>=1.30.0" \
        "ruamel.yaml>=0.18.0"

# Code source (couche separee : un changement de code ne reinstalle pas les deps)
COPY src/ ./src/
COPY config.yaml ./config.yaml

# Entrypoint : lance le worker en background (si AUTOSTART_WORKER=true) puis
# Streamlit au foreground.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# data/ sera monte en volume par docker-compose. On cree juste le mount point.
RUN mkdir -p /app/data

# Streamlit ecoute sur 8501 a l interieur du container. Le binding au host
# (127.0.0.1:8501) est gere par docker-compose : aucune exposition publique.
EXPOSE 8501

# Healthcheck : le dashboard Streamlit doit repondre sur /_stcore/health.
# Si le container plante, restart: unless-stopped le relance.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
