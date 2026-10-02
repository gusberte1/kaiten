#!/usr/bin/env bash
# Instala y arranca el flujo autónomo de agentes como servicio de usuario (sushi-agent-flow) y lo suma
# al monitor de servicios :8099. Idempotente. --sin-arrancar sólo instala; --desinstalar lo saca.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_SRC="$REPO/agents/systemd/sushi-agent-flow.service"
UNIT_DST="$HOME/.config/systemd/user/sushi-agent-flow.service"
STATUS="$REPO/system-setup/status-page/status.py"

if [[ "${1:-}" == "--desinstalar" ]]; then
  systemctl --user disable --now sushi-agent-flow 2>/dev/null || true
  rm -f "$UNIT_DST"; systemctl --user daemon-reload
  echo "Desinstalado. (La entrada en status.py queda; el monitor la mostrará caída.)"; exit 0
fi

mkdir -p "$HOME/.config/systemd/user"
NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
sed "s|^\[Service\]|[Service]\nEnvironment=PATH=${NODE_BIN}:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin|" "$UNIT_SRC" > "$UNIT_DST"
systemctl --user daemon-reload
systemctl --user enable sushi-agent-flow >/dev/null

if ! grep -q "sushi-agent-flow.service" "$STATUS"; then
  python3 - "$STATUS" <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
marker = '    ("Esta pagina",'
entry = '    ("Flujo de agentes (dispatcher)", "sushi-agent-flow.service", None, "servicio", "agents/flow.py daemon; pausa: python3 agents/flow.py pause"),\n'
open(p, "w").write(s.replace(marker, entry + marker, 1))
PY
  systemctl --user restart sushi-status
fi

if [[ "${1:-}" != "--sin-arrancar" ]]; then
  systemctl --user restart sushi-agent-flow
  sleep 2
fi
systemctl --user --no-pager status sushi-agent-flow | head -8 || true
echo
echo "Listo. Ver estado:   python3 $REPO/agents/flow.py status"
echo "Pausar / reanudar:   python3 $REPO/agents/flow.py pause | resume"
echo "Logs:                journalctl --user -u sushi-agent-flow -f"
