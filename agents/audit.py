#!/usr/bin/env python3
"""Auditoría por muestreo del flujo de agentes.

  audit.py pending          tareas marcadas como muestra (audit.sample) que aún no revisaste
  audit.py sample [-n 3]    elige n tareas terminadas al azar (prioriza las marcadas)
  audit.py show TAREA       línea de tiempo: quién hizo qué, veredictos, diff, tests, dónde está cada archivo
  audit.py verify           integridad: cadena de hashes de cada tarea + hash de cada bundle de evidencia
  audit.py reviewed TAREA ok|bad [--note ...]   tu veredicto sobre la muestra; "bad" baja la confianza de los agentes
                            que participaron y queda como lección para el equipo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import orchestrator

from paths import ROOT
STATE = ROOT / ".runtime/agent-orchestration.json"
RUNS = ROOT / ".runtime/agent-runs"


def load() -> dict:
    return orchestrator.state(STATE)


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def show(data: dict, tid: str) -> None:
    t = data["tasks"][tid]
    print(f"# {tid} — {t['title']}\nestado={t['state']} tipo={t['type']} riesgo={t['risk']} rondas={t['rounds']} fallos={t['failures']}")
    if t.get("human_reason"):
        print(f"motivo humano: {t['human_reason']}")
    print("\n## Línea de tiempo")
    for e in t["audit"]:
        extra = {k: v for k, v in e.items() if k not in {"at", "event", "prev", "hash"}}
        print(f"{e['at']}  {e['event']:22} {json.dumps(extra, ensure_ascii=False)[:200]}")
    print("\n## Ejecuciones")
    for rid in t["run_ids"]:
        r = data["runs"][rid]
        v = r.get("review", {})
        print(f"- {rid} [{r['role']}/{r.get('agent', r['provider'])}·{r['provider']}] {r['state']} {('veredicto=' + v['verdict']) if v else ''} {r.get('summary', '')[:160]!r}")
        for f in v.get("findings", []):
            print(f"    · {f}")
        b = RUNS / tid / rid
        if b.exists():
            print(f"    archivos: {b}/ ({', '.join(sorted(p.name for p in b.iterdir()))})")


def verify(data: dict) -> int:
    bad = [p for t in data["tasks"].values() for p in orchestrator.verify_chain(t)]
    for t in data["tasks"].values():
        for e in t["audit"]:
            if e.get("event") == "evidence.added" and e.get("kind") == "bundle":
                path, _, digest = e["value"].rpartition(" ")
                meta = Path(path) / "meta.json"
                if not meta.exists() or sha(meta) != digest:
                    bad.append(f"{t['id']}: el bundle {path} no coincide con lo registrado")
                    continue
                info = json.loads(meta.read_text())
                for name in ("prompt", "output", "diff", "tests"):
                    fp = Path(path) / {"prompt": "prompt.md", "output": "output.txt", "diff": "diff.patch", "tests": "tests.log"}[name]
                    if name in info and (not fp.exists() or sha(fp) != info[name]):
                        bad.append(f"{t['id']}: {fp.name} fue modificado después de registrarse")
    print("\n".join(bad) or f"todo íntegro ({len(data['tasks'])} tareas)")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending"); sub.add_parser("verify")
    s = sub.add_parser("sample"); s.add_argument("-n", type=int, default=3)
    sh = sub.add_parser("show"); sh.add_argument("task")
    rv = sub.add_parser("reviewed"); rv.add_argument("task"); rv.add_argument("verdict", choices=["ok", "bad"]); rv.add_argument("--note", default="")
    args = ap.parse_args()
    data = load()
    if args.cmd == "pending":
        for t in data["tasks"].values():
            if t.get("audit_sample") and not t.get("audit_reviewed"):
                print(f"{t['id']}  {t['title']}")
    elif args.cmd == "sample":
        done = [t for t in data["tasks"].values() if t["state"] == "done"]
        picks = [t for t in done if t.get("audit_sample") and not t.get("audit_reviewed")]
        rest = [t for t in done if t not in picks]
        random.shuffle(rest)
        for t in (picks + rest)[:args.n]:
            print(f"{t['id']}  {t['title']}  {'(marcada)' if t in picks else ''}")
    elif args.cmd == "show":
        show(data, args.task)
    elif args.cmd == "reviewed":
        for field, value in (("audit_reviewed", "true"), ("audit_verdict", json.dumps(args.verdict))):
            rc, out = orchestrator.execute(["--state", str(STATE), "mark", args.task, "--event", f"audit.{args.verdict}" if field == "audit_verdict" else "audit.reviewed",
                                            "--field", field, "--value", value, "--actor", "human:" + args.note[:100]])
            if rc:
                print(out, file=sys.stderr); return 2
        print(f"{args.task}: auditoría {args.verdict}" + (" — la confianza de los agentes involucrados baja" if args.verdict == "bad" else ""))
    else:
        return verify(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
