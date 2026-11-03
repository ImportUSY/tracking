#!/usr/bin/env bash
# Instalación local en Linux/macOS:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Creando entorno virtual .venv"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .

if [ ! -f .env ]; then
  echo "==> Creando .env de desarrollo"
  secret=$(.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))")
  sed -e 's/^APP_ENV=.*/APP_ENV=development/' \
      -e 's/^LOG_JSON=.*/LOG_JSON=false/' \
      -e "s/^SECRET_KEY=.*/SECRET_KEY=${secret}/" .env.example > .env
  echo "   Edite .env y defina BOOTSTRAP_ADMIN_PASSWORD (o use: tracking crear-supervisor)"
fi

echo "==> Migrando base de datos"
.venv/bin/tracking migrate
echo
echo "Listo. Siguientes pasos:"
echo "  .venv/bin/tracking crear-supervisor"
echo "  bash scripts/run_dev.sh"
