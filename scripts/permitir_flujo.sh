#!/usr/bin/env bash
# Agrega a .claude/settings.local.json (sólo este proyecto) reglas para que Claude Code pueda correr sin
# preguntar los comandos de LECTURA/ADMIN del flujo (status, audit, team, pause). NO permite lanzar agentes
# ni el dispatcher: `tick`/`daemon`/`decide`/`release --approve` siguen pidiendo permiso.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
python3 - "$REPO/.claude/settings.local.json" <<'PY'
import json, os, sys
p = sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
d = json.load(open(p)) if os.path.exists(p) else {}
allow = d.setdefault("permissions", {}).setdefault("allow", [])
new = ["Bash(python3 agents/flow.py status:*)", "Bash(python3 agents/flow.py team:*)", "Bash(python3 agents/flow.py pause:*)",
       "Bash(python3 agents/flow.py resume:*)", "Bash(python3 agents/flow.py retro:*)",
       "Bash(python3 agents/audit.py:*)", "Bash(cd agents && python3 -m unittest:*)", "Bash(python3 -m unittest:*)"]
for r in new:
    if r not in allow:
        allow.append(r)
json.dump(d, open(p, "w"), indent=2, ensure_ascii=False); open(p, "a").write("\n")
print("Reglas agregadas en", p)
PY
