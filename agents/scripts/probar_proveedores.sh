#!/usr/bin/env bash
# Prueba REAL de los proveedores (Claude, Codex, Gemini, DeepSeek) y del flujo completo en un repo temporal.
# Lanza agentes con autonomía plena en /tmp: no toca este repo. Informe: .runtime/smoke/<fecha>/report.md
# Uso: bash agents/scripts/probar_proveedores.sh [--solo claude codex] [--sin-flujo]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
exec python3 -u smoke_real.py "$@"
