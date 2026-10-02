#!/usr/bin/env bash
# Habilita la GEMINI_API_KEY (paga por token) por un tiempo limitado, para un uso puntual y con tu OK.
# Muestra la estimación de costo, pide que escribas AUTORIZO y la frase de descifrado; pasado el tiempo
# el archivo en claro se borra solo. Cada autorización queda en ~/.config/sushi-inspector/gemini-api-uso.log
# Uso: bash .../gemini_api_autorizar.sh --llamadas 200 [--modelo gemini-2.5-flash] [--tipo imagen|texto] [--minutos 30] --motivo "preetiquetado lote X"
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
D="$HOME/.config/sushi-inspector"; PLAIN="$D/gemini.env"; ENC="$D/gemini.env.gpg"; LOG="$D/gemini-api-uso.log"
LLAMADAS=""; MODELO="gemini-2.5-flash"; TIPO="imagen"; MIN=30; MOTIVO=""
while [ $# -gt 0 ]; do case "$1" in
  --llamadas) LLAMADAS="$2"; shift 2;; --modelo) MODELO="$2"; shift 2;; --tipo) TIPO="$2"; shift 2;;
  --minutos) MIN="$2"; shift 2;; --motivo) MOTIVO="$2"; shift 2;; *) echo "Argumento desconocido: $1" >&2; exit 2;; esac; done
[ -n "$LLAMADAS" ] && [ -n "$MOTIVO" ] || { echo "Faltan --llamadas y --motivo" >&2; exit 2; }
[ -f "$ENC" ] || { echo "La key no está bloqueada ($ENC no existe). Primero: bash $HERE/scripts/gemini_api_bloquear.sh" >&2; exit 1; }

python3 - "$HERE/gemini_precios.json" "$MODELO" "$TIPO" "$LLAMADAS" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1])); m, tipo, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
if m not in cfg["modelos"]:
    raise SystemExit(f"Modelo sin precio en gemini_precios.json: {m}. Modelos: {', '.join(cfg['modelos'])}")
tok = cfg["tokens_por_llamada_" + tipo]; p = cfg["modelos"][m]
per = (tok["in"] * p["in"] + tok["out"] * p["out"]) / 1e6
print(f"\nEstimación ({m}, {tipo}): ~{tok['in']} tokens de entrada + ~{tok['out']} de salida por llamada")
print(f"  por llamada: ~USD {per:.4f}   ·   {n} llamadas: ~USD {per * n:.2f}   (orientativo; verificá precios en ai.google.dev/pricing)")
PY
echo "Motivo: $MOTIVO · ventana: $MIN minutos"
read -r -p "Escribí AUTORIZO para habilitar la key: " OK
[ "$OK" = "AUTORIZO" ] || { echo "Cancelado."; exit 1; }
gpg --pinentry-mode loopback --quiet --decrypt "$ENC" > "$PLAIN"
chmod 600 "$PLAIN"
systemd-run --user --quiet --on-active="${MIN}m" --unit "gemini-api-relock-$(date +%s)" /bin/rm -f "$PLAIN"
printf '%s | motivo=%s | llamadas_estimadas=%s | modelo=%s | minutos=%s\n' "$(date '+%F %T')" "$MOTIVO" "$LLAMADAS" "$MODELO" "$MIN" >> "$LOG"
echo "Key habilitada por $MIN minutos (se borra sola). Si terminás antes: rm $PLAIN"
