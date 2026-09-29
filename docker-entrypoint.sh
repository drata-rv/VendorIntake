#!/bin/sh
set -eu
if [ "$#" -gt 0 ]; then
  exec "$@"
fi
flask --app app check-schema
exec gunicorn 'app:create_app()' \
  --bind 0.0.0.0:8000 \
  --workers 1 --threads 4 \
  --timeout 75 --graceful-timeout 60 \
  --worker-tmp-dir /dev/shm \
  --access-logfile - --error-logfile - \
  --access-logformat '%(h)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s %(L)s'
