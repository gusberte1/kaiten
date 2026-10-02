#!/usr/bin/env bash
# Inicializa la POC local. No instala paquetes ni crea servicios systemd.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAIL_IP="${1:-$(tailscale ip -4)}"
ENV_FILE="$HERE/.env"

if [[ ! "$TAIL_IP" =~ ^100\. ]]; then
  echo "IP Tailscale inesperada: $TAIL_IP" >&2
  exit 2
fi
if [[ -e "$ENV_FILE" ]]; then
  echo "Ya existe $ENV_FILE; no se sobreescribe." >&2
  exit 2
fi

install -d -m 0750 "$HERE/db" "$HERE/files"
chown 1000:1000 "$HERE/db" "$HERE/files"
umask 077
SECRET="$(openssl rand -hex 32)"
printf 'VIKUNJA_BIND_IP=%s\nVIKUNJA_SERVICE_PUBLICURL=http://%s:3457/\nVIKUNJA_SERVICE_SECRET=%s\n' \
  "$TAIL_IP" "$TAIL_IP" "$SECRET" > "$ENV_FILE"
unset SECRET

docker compose --project-directory "$HERE" up -d
echo "POC lista en http://$TAIL_IP:3457/"
