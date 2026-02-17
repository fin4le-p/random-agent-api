#!/usr/bin/env bash
set -e

echo "[api] wait db..."
python - <<'PY'
import os, time
import psycopg
from urllib.parse import urlparse

url = os.environ["DATABASE_URL"]
u = urlparse(url)
host, port = u.hostname, u.port or 5432
user, pwd = u.username, u.password
db = u.path.lstrip("/")

for i in range(60):
    try:
        with psycopg.connect(host=host, port=port, user=user, password=pwd, dbname=db, connect_timeout=2) as conn:
            pass
        print("[api] db ok")
        break
    except Exception as e:
        time.sleep(1)
else:
    raise SystemExit("[api] db not ready")
PY

echo "[api] migrate..."
python manage.py migrate --noinput

echo "[api] collectstatic..."
python manage.py collectstatic --noinput || true

echo "[api] start gunicorn..."
exec gunicorn app.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers 2 \
  --threads 4 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile - \
  --log-level debug \
  --capture-output