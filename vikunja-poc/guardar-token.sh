#!/usr/bin/env bash
# Guarda el token de API fuera del repo, con permisos 0600 y sin mostrarlo.
set -euo pipefail

CONFIG_DIR="$HOME/.config/sushi-inspector"
TOKEN_FILE="$CONFIG_DIR/vikunja-poc.env"
install -d -m 0700 "$CONFIG_DIR"
read -r -s -p 'Token API Vikunja (oculto): ' TOKEN < /dev/tty
echo
TOKEN="${TOKEN//[[:space:]]/}"
if [[ ! "$TOKEN" =~ ^tk_.{12,}$ ]]; then
  echo 'Formato inesperado: el token de API Vikunja debe empezar por tk_.' >&2
  exit 2
fi
umask 077
printf 'VIKUNJA_API_TOKEN=%s\n' "$TOKEN" > "$TOKEN_FILE"
chmod 0600 "$TOKEN_FILE"
unset TOKEN
echo "Token guardado en $TOKEN_FILE (0600)."
