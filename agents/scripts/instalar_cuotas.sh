#!/usr/bin/env bash
# Instala el monitor de cuotas (sushi-quota.timer, cada 15 min) como timer de usuario y hace una primera lectura.
# Idempotente. --desinstalar lo saca.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DST="$HOME/.config/systemd/user"

if [[ "${1:-}" == "--desinstalar" ]]; then
  systemctl --user disable --now sushi-quota.timer 2>/dev/null || true
  rm -f "$DST/sushi-quota.service" "$DST/sushi-quota.timer"; systemctl --user daemon-reload
  echo "Desinstalado."; exit 0
fi

mkdir -p "$DST"
NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
sed "s|^\[Service\]|[Service]\nEnvironment=PATH=${NODE_BIN}:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin|" "$REPO/agents/systemd/sushi-quota.service" > "$DST/sushi-quota.service"
cp "$REPO/agents/systemd/sushi-quota.timer" "$DST/sushi-quota.timer"
systemctl --user daemon-reload
systemctl --user enable --now sushi-quota.timer
systemctl --user start sushi-quota.service || true
systemctl --user --no-pager list-timers sushi-quota.timer | head -3
echo
echo "Ver cuotas:   python3 $REPO/agents/quota.py show   |   http://100.75.61.75:8099/cuotas"
echo "Logs:         journalctl --user -u sushi-quota -n 30"
