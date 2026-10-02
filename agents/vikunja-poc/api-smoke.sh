#!/usr/bin/env bash
# Verifica el token sin imprimirlo. No crea ni modifica proyectos/tareas.
set -euo pipefail

CONFIG_FILE="$HOME/.config/sushi-inspector/vikunja-poc.env"
[[ -r "$CONFIG_FILE" ]] || { echo "Falta $CONFIG_FILE; corre guardar-token.sh." >&2; exit 2; }
set -a
source "$CONFIG_FILE"
set +a
python3 - "$VIKUNJA_API_TOKEN" <<'PY'
import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

request = Request(
    "http://100.75.61.75:3457/api/v2/projects",
    headers={"Authorization": f"Bearer {sys.argv[1]}", "Accept": "application/json"},
)
try:
    with urlopen(request, timeout=10) as response:
        body = json.load(response)
    print(f"API token OK; proyectos visibles: {body.get('total', len(body.get('items', [])))}")
except HTTPError as error:
    print(f"Token rechazado: HTTP {error.code}", file=sys.stderr)
    raise SystemExit(1)
PY
