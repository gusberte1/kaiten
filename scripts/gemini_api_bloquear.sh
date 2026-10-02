#!/usr/bin/env bash
# Cifra la GEMINI_API_KEY (~/.config/sushi-inspector/gemini.env -> gemini.env.gpg) con una frase que sólo sabés vos
# y borra el archivo en claro. Desde ahí NINGÚN agente ni script puede usar la key (cuesta por token) sin que
# la habilites vos con gemini_api_autorizar.sh (que muestra la estimación de costo y pide confirmación).
# Uso: bash /home/admin/Documents/sushi-inspector/agents/scripts/gemini_api_bloquear.sh
set -euo pipefail
D="$HOME/.config/sushi-inspector"; PLAIN="$D/gemini.env"; ENC="$D/gemini.env.gpg"
[ -f "$PLAIN" ] || { echo "No hay $PLAIN en claro (¿ya está bloqueada? existe $ENC: $([ -f "$ENC" ] && echo sí || echo no))."; exit 0; }
echo "Elegí una frase (no la pierdas: sin ella la key no se recupera). Te la pide dos veces."
gpg --batch=false --pinentry-mode loopback --symmetric --cipher-algo AES256 --output "$ENC" "$PLAIN"
chmod 600 "$ENC"
echo "Verificando que se puede descifrar (te pide la frase otra vez)..."
gpg --pinentry-mode loopback --quiet --decrypt "$ENC" 2>/dev/null | grep -q "GEMINI_API_KEY=" || { echo "La verificación falló; NO borré el original." >&2; rm -f "$ENC"; exit 1; }
shred -u "$PLAIN"
echo "Listo: la key quedó cifrada en $ENC y el archivo en claro se borró."
echo "Para usarla de forma puntual: bash $(dirname "$0")/gemini_api_autorizar.sh --llamadas N --motivo '...'"
