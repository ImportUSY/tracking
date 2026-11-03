#!/bin/sh
# Aplica migraciones una sola vez (antes de levantar varios workers) y arranca el servidor.
set -e
tracking migrate
exec tracking serve --host 0.0.0.0 --port 8000 --workers "${WEB_CONCURRENCY:-2}"
