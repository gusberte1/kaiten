#!/usr/bin/env bash
# ==============================================================================
# Script de Auto-Actualización Segura de Kaiten
# ==============================================================================
# 1. Comprueba conectividad y estado local limpio en main.
# 2. Descarga referencias de origin/main y compara commits.
# 3. Si hay actualización, corre los tests unitarios antes de tocar nada.
# 4. Si los tests pasan, aplica fast-forward y reinicia servicios si aplica.
# ==============================================================================
set -euo pipefail

KAITEN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$KAITEN_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 1. Verificar rama y estado de Git
ACTUAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$ACTUAL_BRANCH" != "main" ]; then
  log "AVISO: Kaiten está en rama '$ACTUAL_BRANCH' (distinta de main). Se omite auto-actualización."
  exit 0
fi

if [ -n "$(git status --porcelain)" ]; then
  log "ERROR: Existen cambios sin commitear en $KAITEN_DIR. Abortando auto-actualización por seguridad."
  exit 1
fi

# 2. Consultar novedades remotas
log "Consultando origin/main..."
if ! git fetch origin main --tags --quiet 2>/dev/null; then
  log "AVISO: No se pudo contactar origin/main (¿sin red o sin clave SSH?). Reintentará en el próximo ciclo."
  exit 0
fi

LOCAL_HASH="$(git rev-parse HEAD)"
REMOTE_HASH="$(git rev-parse origin/main)"

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
  log "Kaiten ya está actualizado en el último commit de main ($LOCAL_HASH)."
  exit 0
fi

log "Nuevo release detectado en origin/main: $REMOTE_HASH (actual: $LOCAL_HASH)"

# 3. Compuerta de verificación de tests
log "Ejecutando suite de pruebas de validación previa..."
if ! python3 -m unittest discover -s "$KAITEN_DIR" -p "test_*.py" > /tmp/kaiten-test-update.log 2>&1; then
  log "CRÍTICO: La suite de tests falló. No se aplicará la actualización."
  log "Detalle del fallo guardado en /tmp/kaiten-test-update.log"
  tail -n 25 /tmp/kaiten-test-update.log
  exit 1
fi

# 4. Aplicar actualización limpia
log "Tests aprobados con éxito. Aplicando fast-forward merge..."
git merge --ff-only origin/main

NUEVO_TAG="$(git describe --tags --exact-match 2>/dev/null || echo "sin etiqueta")"
log "Actualización completada exitosamente a: $(git rev-parse --short HEAD) ($NUEVO_TAG)"

# 5. Reinicio suave de servicios si están activos
for SERVICIO in "kaiten-flow.service" "sushi-agent-flow.service"; do
  if systemctl --user is-active --quiet "$SERVICIO" 2>/dev/null; then
    log "Reiniciando suavemente $SERVICIO..."
    systemctl --user restart "$SERVICIO" || true
  fi
done

log "Proceso de actualización finalizado correctamente."
exit 0
