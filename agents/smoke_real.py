#!/usr/bin/env python3
"""Prueba con proveedores REALES (la corre una persona: lanza agentes con autonomía plena).

Etapa 1 — cada proveedor habilitado, por separado, en un repo temporal:
   implement: crea un archivo pedido (comprueba que el modo no interactivo funciona y escribe en el cwd)
   review:    lee sin modificar nada (comprueba que el modo solo lectura respeta o, si no, que el controlador lo detecta)
Etapa 2 — flujo completo (equipo + JEV + revisión cruzada + integración en dev) con una tarea real chica.
Deja un informe en .runtime/smoke/<fecha>/report.md y un resumen en pantalla.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import flow
import providers as prov

ROOT = flow.ROOT


def sh(*a, cwd):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout


def new_repo(base: Path) -> Path:
    repo = base / "repo"
    (repo / "comp").mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.name", "smoke", cwd=repo); sh("git", "config", "user.email", "s@s", cwd=repo)
    (repo / "README.md").write_text("# Proyecto de prueba\nUna calculadora mínima.\n")
    (repo / "comp/calc.py").write_text("def suma(a, b):\n    return a - b\n")
    (repo / "comp/test_calc.py").write_text('import sys\nsys.path.insert(0, "comp")\nfrom calc import suma\nassert suma(2, 3) == 5, "suma rota"\nprint("ok")\n')
    sh("git", "add", "-A", cwd=repo); sh("git", "commit", "-qm", "init", cwd=repo)
    return repo


def provider_checks(P: prov.Providers, names: list[str], base: Path, timeout: int) -> list[dict]:
    rows = []
    for n in names:
        ok, why = P.usable(n, "implement")
        row = {"provider": n, "usable": why}
        if not ok:
            rows.append(row); continue
        repo = new_repo(base / f"chk_{n}")
        print(f"  [{n}] implementar (máx {timeout}s)...", flush=True)
        r = P.run(n, "implement", "Creá en el directorio actual un archivo llamado hola.txt cuyo contenido sea exactamente: hola. No hagas nada más y respondé solo: listo.", repo, timeout)
        row.update(implement_timed_out=r.timed_out, implement_rc=r.rc, implement_s=round(r.seconds), implement_wrote_file=(repo / "hola.txt").exists(), implement_blocked=r.blocked, implement_tail=r.output[-300:])
        print(f"  [{n}] implementar: rc={r.rc} {round(r.seconds)}s archivo={(repo / 'hola.txt').exists()}", flush=True)
        repo2 = new_repo(base / f"rev_{n}")
        print(f"  [{n}] revisar (máx {timeout}s)...", flush=True)
        r2 = P.run(n, "review", "Sin modificar ningún archivo, decime en una línea qué es este proyecto (leé README.md).", repo2, timeout)
        dirty = bool(sh("git", "status", "--porcelain", cwd=repo2).strip())
        print(f"  [{n}] revisar: rc={r2.rc} {round(r2.seconds)}s{' TIMEOUT' if r2.timed_out else ''}", flush=True)
        row.update(review_timed_out=r2.timed_out, review_rc=r2.rc, review_s=round(r2.seconds), review_left_repo_clean=not dirty, review_tail=r2.output[-300:])
        rows.append(row)
    return rows


def full_flow(base: Path, notes: list[str], timeout: int) -> dict:
    repo = new_repo(base / "flow")
    f = flow.Flow(repo, base / "rt", notify=notes.append)
    f.cfg["run_timeout_s"] = timeout
    f.add_local("SMOKE-1", "Arreglar suma en calc.py", "comp", "code", "low",
                {"test": "python3 comp/test_calc.py", "scope": []},
                "La función suma de comp/calc.py devuelve a - b y debe devolver a + b. Hay un test en comp/test_calc.py.")
    f.tick()
    st = f.tasks()
    t = st["tasks"]["SMOKE-1"]
    runs = [(r["role"], r.get("agent"), r["provider"], r["state"], r.get("failure", ""), r.get("review", {}).get("verdict")) for r in st["runs"].values()]
    fixed = "a + b" in sh("git", "show", "dev:comp/calc.py", cwd=repo)
    return {"state": t["state"], "human_reason": t["human_reason"], "runs": runs, "arreglo_en_dev": fixed, "avisos": notes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo", nargs="*", help="probar sólo estos proveedores en la etapa 1")
    ap.add_argument("--sin-flujo", action="store_true", help="saltar la etapa 2")
    ap.add_argument("--sin-proveedores", action="store_true", help="saltar la etapa 1 (sólo el flujo completo)")
    ap.add_argument("--include-disabled", action="store_true", help="habilitar sólo en memoria los proveedores pedidos para un smoke explícito")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    P = prov.Providers(prov.DEFAULT_FILE)
    if args.include_disabled:
        for name in args.solo or []:
            if name in P.table:
                P.table[name]["enabled"] = True
    names = args.solo or [n for n, c in P.table.items() if c.get("enabled")]
    out = ROOT / ".runtime/smoke" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    base = Path(tempfile.mkdtemp(prefix="smoke-"))
    lines = [f"# Prueba con proveedores reales — {time.strftime('%F %T')}", "", "## Etapa 1: proveedores por separado", ""]
    print("Etapa 1: probando proveedores", names, flush=True)
    rows = [] if args.sin_proveedores else provider_checks(P, names, base, args.timeout)
    for r in rows:
        if "implement_rc" not in r:
            lines.append(f"- **{r['provider']}**: no probado ({r['usable']})"); print(f"  {r['provider']:9} no probado ({r['usable']})"); continue
        good = r["implement_wrote_file"] and r["implement_rc"] == 0
        lines += [f"- **{r['provider']}**: implementar {'OK' if good else 'FALLA'} (rc={r['implement_rc']}, {r['implement_s']}s{' TIMEOUT' if r.get('implement_timed_out') else ''}, archivo creado: {r['implement_wrote_file']}, cuota: {r['implement_blocked']}); "
                  f"revisar rc={r['review_rc']} ({r['review_s']}s{' TIMEOUT' if r.get('review_timed_out') else ''}), repo intacto tras revisar: {r['review_left_repo_clean']}",
                  f"  - salida implementar: `{r['implement_tail'].strip()[-200:]!r}`", f"  - salida revisar: `{r['review_tail'].strip()[-200:]!r}`"]
        print(f"  {r['provider']:9} implementar={'OK' if good else 'FALLA'} revisar_intacto={r['review_left_repo_clean']}", flush=True)
    if not args.sin_flujo:
        print("Etapa 2: flujo completo (puede tardar varios minutos)...", flush=True)
        notes: list[str] = []
        res = full_flow(base, notes, args.timeout)
        lines += ["", "## Etapa 2: flujo completo", "", f"- estado final: **{res['state']}** {res['human_reason']}", f"- arreglo presente en dev: {res['arreglo_en_dev']}", "- ejecuciones:"]
        lines += [f"  - {role} · {agent} ({prov_}) · {state} {failure} {verdict or ''}" for role, agent, prov_, state, failure, verdict in res["runs"]]
        lines += [f"- aviso: {n}" for n in res["avisos"]]
        print(f"  estado final: {res['state']} | arreglo en dev: {res['arreglo_en_dev']}")
        for run in res["runs"]:
            print("   ", run)
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nInforme: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
