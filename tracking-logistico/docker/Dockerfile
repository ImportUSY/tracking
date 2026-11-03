# ---- Imagen de producción de Tracking Logístico ----
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias primero (mejor uso de la caché de capas)
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
RUN pip install --no-deps . \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /data/fotos \
    && chown -R app:app /data

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER app
ENV STORAGE_DIR=/data/fotos \
    DB_AUTO_MIGRATE=false \
    APP_ENV=production

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["/entrypoint.sh"]
