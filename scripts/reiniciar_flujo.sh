#!/usr/bin/env bash
# Reinicia el servicio del flujo SIN cortar a un agente que esté trabajando: espera (máx. 30 min) a que no haya
# ejecuciones activas y recién ahí reinicia. Uso: bash /home/admin/Documents/sushi-inspector/agents/scripts/reiniciar_flujo.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
activas() { python3 - "$REPO/.runtime/agent-orchestration.json" <<'PY'
import json, sys
try:
    runs = json.load(open(sys.argv[1]))["runs"].values()
except OSError:
    runs = []
print(sum(1 for r in runs if r["state"] == "executing"))
PY
}
for _ in $(seq 180); do
  n="$(activas)"; [ "$n" = "0" ] && break
  echo "Esperando a que terminen $n ejecución(es) activa(s)..."; sleep 10
done
systemctl --user restart sushi-agent-flow && echo "Servicio reiniciado."
