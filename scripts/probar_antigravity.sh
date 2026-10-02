#!/usr/bin/env bash
# Smoke real acotado para la CLI oficial agy. No lee ni escribe credenciales:
# agy debe tener una sesión interactiva de cuenta Google ya autenticada.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec python3 "$ROOT/agents/smoke_real.py" --solo antigravity --include-disabled --sin-flujo --timeout "${ANTIGRAVITY_SMOKE_TIMEOUT:-300}"
