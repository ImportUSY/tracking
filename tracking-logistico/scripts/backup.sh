#!/usr/bin/env bash
# Respaldo manual (fuera de Docker): base PostgreSQL + fotos.
#   DATABASE_URL=postgresql://usuario:clave@host:5432/tracking STORAGE_DIR=./data/fotos bash scripts/backup.sh
set -euo pipefail
destino="${BACKUP_DIR:-./backups}"
fecha="$(date +%Y%m%d_%H%M)"
mkdir -p "$destino"

if [[ "${DATABASE_URL:-}" == postgresql* ]]; then
  pg_dump "${DATABASE_URL}" -Fc -f "$destino/tracking_${fecha}.dump"
elif [[ "${DATABASE_URL:-sqlite:///./data/tracking.db}" == sqlite* ]]; then
  db="${DATABASE_URL:-sqlite:///./data/tracking.db}"; db="${db#sqlite:///}"
  sqlite3 "$db" ".backup '$destino/tracking_${fecha}.db'"
fi
tar -czf "$destino/fotos_${fecha}.tgz" -C "${STORAGE_DIR:-./data/fotos}" .
find "$destino" -type f -mtime +"${BACKUP_RETENTION_DAYS:-14}" -delete
echo "Respaldo listo en $destino"
