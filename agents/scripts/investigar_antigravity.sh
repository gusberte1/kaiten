#!/usr/bin/env bash
# Evidencia para el ticket #5 (Antigravity como proveedor). SOLO LECTURA: no instala nada, no cambia servicios
# y no lee credenciales (sólo nombres de archivos y `--help`). Origen: sección 5 del informe de Itamae/Wasabi
# (system-setup/2026-09-26-antigravity-como-proveedor.md). La salida queda en .runtime/antigravity-check.log
# Uso: bash /home/admin/Documents/sushi-inspector/agents/scripts/investigar_antigravity.sh
set -u
LOG="$(cd "$(dirname "$0")/../.." && pwd)/.runtime/antigravity-check.log"
mkdir -p "$(dirname "$LOG")"
D="$HOME/.gemini/antigravity-ide"
{
echo "== fecha =="; date
echo "== arch =="; uname -m
echo "== contenido (solo nombres) =="; ls -la "$D" 2>&1; ls -d "$HOME/.antigravity"* 2>&1
echo "== tipo de binarios =="; for f in "$D"/bin/* "$D"/*; do [ -f "$f" ] && file "$f"; done
echo "== PATH =="; command -v antigravity agy agentapi 2>&1 || echo "(ninguno en PATH)"
echo "== agentapi --help / --version (timeout 10 s, sin stdin) =="
for a in "--help" "--version"; do timeout 10 "$D/bin/agentapi" $a </dev/null 2>&1 | head -60; done
echo "== agentapi server --help =="
timeout 10 "$D/bin/agentapi" server --help </dev/null 2>&1 | head -60
echo "== procesos de Antigravity activos =="; pgrep -af -i antigravity | cut -c1-160
} 2>&1 | tee "$LOG"
echo; echo "Listo. Log en: $LOG"
