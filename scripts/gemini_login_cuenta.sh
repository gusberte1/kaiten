#!/usr/bin/env bash
# Deja Gemini CLI usando tu CUENTA Google (cuota del plan mensual), nunca una API key (se paga por token).
# Robusto: cierra Gemini solo al detectar el login, reintenta, deja log en .runtime/gemini-login.log.
# Uso: bash /home/admin/Documents/sushi-inspector/agents/scripts/gemini_login_cuenta.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$REPO/.runtime/gemini-login.log"; mkdir -p "$(dirname "$LOG")"; : > "$LOG"
export NVM_DIR="$HOME/.nvm"; [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
S="$HOME/.gemini/settings.json"; ACC="$HOME/.gemini/google_accounts.json"

log()  { echo "$*" | tee -a "$LOG"; }
die()  { echo "✗ $*" | tee -a "$LOG" >&2; echo "  (log: $LOG)" >&2; exit 1; }
# Gemini siempre sin keys en el entorno y forzando login con cuenta
gem()  { env -u GEMINI_API_KEY -u GOOGLE_API_KEY -u GOOGLE_GENAI_USE_VERTEXAI -u GOOGLE_CLOUD_PROJECT \
             GOOGLE_GENAI_USE_GCA=true NO_BROWSER=true gemini "$@"; }
active() { python3 -c "import json,sys; sys.exit(0 if json.load(open('$ACC')).get('active') else 1)" 2>/dev/null; }
account() { python3 -c "import json; print(json.load(open('$ACC')).get('active'))" 2>/dev/null; }

command -v gemini >/dev/null || die "No encuentro el binario 'gemini'."
if pgrep -f "bin/gemini" >/dev/null; then die "Hay un Gemini abierto (otra terminal o una corrida anterior). Cerralo y volvé a correr este script."; fi

# 1) configuración: tipo de auth = cuenta (con backup). Se repite antes de CADA intento porque
#    Gemini lo pisa a "gemini-api-key" cuando un login falla, y entonces pide una API key en vez de la cuenta.
mkdir -p "$HOME/.gemini"
[ -f "$S" ] && cp "$S" "$S.bak-$(date +%Y%m%d-%H%M%S)"
fijar_oauth() {
python3 - "$S" <<'PY' || die "No pude escribir $S"
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("security", {}).setdefault("auth", {})["selectedType"] = "oauth-personal"
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
}
fijar_oauth
log "✓ settings.json en oauth-personal (backup al lado)"

# 2) login (sólo si hace falta)
if active; then
  log "✓ Ya hay una cuenta con sesión: $(account)"
else
  for intento in 1 2 3; do
    fijar_oauth
    clear 2>/dev/null || true
    cat <<'MSG'
══════════ LOGIN DE GEMINI CON TU CUENTA ══════════
 1. Ahora se abre Gemini y muestra una URL larga.
 2. Copiala, abrila en el navegador de tu celu/PC e iniciá sesión
    con la cuenta del plan pago.
 3. Google te muestra un CÓDIGO: copialo.
 4. Volvé acá, pegalo y apretá Enter. Nada más.
 5. Esperá unos segundos: este script detecta el login solo,
    cierra Gemini y sigue con la verificación.
 ⚠ El código sirve SOLO para la URL de ESTA pantalla (cada intento
   genera una nueva). Usá el botón «copiar» de Google y pegá sin espacios.
 ⚠ Si Gemini pide una «API key»: NO la pegues; apretá Esc y volvé
   a intentar desde el script (es lo que aparece cuando el código falla).
 Tenés 10 minutos. Ctrl+C para cancelar.
═══════════════════════════════════════════════════
MSG
    read -r -p "Enter para abrir Gemini... " _
    # vigilante: apenas hay cuenta activa (o pasan 10 min) cierra Gemini, así no hay que salir a mano
    ( for _ in $(seq 300); do sleep 2; if active; then sleep 3; pkill -TERM -f "bin/gemini"; exit 0; fi; done; pkill -TERM -f "bin/gemini" ) &
    WATCHER=$!
    gem                      # en primer plano y con el teclado: sin timeout/pipes que lo dejen en segundo plano (SIGTTIN)
    kill "$WATCHER" 2>/dev/null; wait "$WATCHER" 2>/dev/null
    stty sane 2>/dev/null || true
    active && break
    log "Todavía sin sesión (intento $intento de 3)."
  done
  active || die "El login no se completó (google_accounts.json sigue sin cuenta activa)."
  log "✓ Sesión iniciada: $(account)"
fi

# 3) verificación real: una llamada mínima con la cuenta
log "Verificando con una llamada mínima (usa un poquito de la cuota de tu plan)..."
OUT="$(timeout 120 bash -c "$(declare -f gem); gem -p 'Respondé sólo: ok' --skip-trust" </dev/null 2>&1)"; RC=$?
echo "$OUT" | tail -6 | tee -a "$LOG"
if echo "$OUT" | grep -qiE "401|unauthenticated|api key|invalid_grant|login required"; then die "La verificación falló por autenticación."; fi
[ $RC -eq 0 ] || die "La verificación terminó con error (rc=$RC)."
log
log "✓ LISTO: Gemini usa tu cuenta ($(account)). El flujo ya lo considera disponible."
log "  Comprobar cuando quieras: python3 $REPO/agents/flow.py status"
