# TravelDoor Connect - one image serves three roles (see docker/entrypoint.py):
#   web        migrate + collectstatic, then gunicorn on 0.0.0.0:8000 (default)
#   scheduler  migrate, then `python manage.py runscheduler` (background loop)
#   <other>    exec verbatim, e.g. `python manage.py createsuperuser`
#
# Build:  docker build -t traveldoor .
# Run:    docker run --rm -p 8000:8000 traveldoor          # SQLite smoke run
# Stack:  docker compose up --build                        # + PostgreSQL
# Dokploy: docker-compose.dokploy.yml
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DJANGO_SETTINGS_MODULE=traveldoor.settings

WORKDIR /app

# Dependencies first so code changes don't bust the pip cache layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (.dockerignore keeps .env, db.sqlite3 and cruft out of the image).
COPY . .

# Unprivileged runtime user owns the tree (collectstatic writes staticfiles/,
# photo uploads write media/ — both are mounted as volumes at runtime).
RUN useradd --system --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Liveness probe - hits the /health/ endpoint (NFR-1). Sends
# X-Forwarded-Proto: https so the probe is seen as an already-terminated-TLS
# request and is exempt from the production SECURE_SSL_REDIRECT (gunicorn
# itself speaks plain HTTP; TLS terminates at the reverse proxy).
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import os,sys,urllib.request; req=urllib.request.Request('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health/', headers={'X-Forwarded-Proto': 'https'}); sys.exit(0 if urllib.request.urlopen(req, timeout=4).getcode() == 200 else 1)"

ENTRYPOINT ["python", "docker/entrypoint.py"]
CMD ["web"]
