#!/usr/bin/env bash
# Publica en Vercel desde Linux/macOS:  bash scripts/deploy_vercel.sh [nombre-proyecto]
set -euo pipefail
cd "$(dirname "$0")/.."
P="${1:-tracking-logistico}"
V="npx --yes vercel@latest"
$V whoami >/dev/null 2>&1 || $V login
$V link --yes --project "$P"
envs="$($V env ls production 2>&1 || true)"
if ! grep -qE "DATABASE_URL|POSTGRES_URL" <<<"$envs"; then
  $V integration add neon || echo "Conecte Neon en vercel.com > $P > Storage > Create Database > Neon"
  read -rp "Cuando la base esté conectada, presione Enter "
fi
setenv() { $V env rm "$1" production --yes >/dev/null 2>&1 || true; printf '%s' "$2" | $V env add "$1" production >/dev/null; echo "  $1 configurada"; }
grep -q SECRET_KEY <<<"$envs" || setenv SECRET_KEY "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
setenv APP_ENV production
if ! grep -q BOOTSTRAP_ADMIN_PASSWORD <<<"$envs"; then
  read -rsp "Clave para el usuario 'supervisor' (mín. 6): " clave; echo
  setenv BOOTSTRAP_ADMIN_PASSWORD "$clave"
fi
$V deploy --prod --yes
echo "Listo: https://$P.vercel.app"
