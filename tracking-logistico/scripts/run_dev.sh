#!/usr/bin/env bash
# Servidor de desarrollo en http://localhost:8000
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/tracking serve --host 0.0.0.0 --port 8000 --reload
