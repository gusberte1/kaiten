"""Ciclo de aprendizaje: confianza por agente, tarjetas de desempeño, lecciones y retrospectiva.

Todo se deriva del estado del orquestador (runs, veredictos, decisiones humanas, auditorías): no hay
una segunda fuente de verdad que pueda desincronizarse. Cierra el ciclo en tres puntos:
  1. confianza  -> qué riesgo puede tomar cada agente (sube con tareas limpias, baja con auditoría mala)
  2. enrutado   -> a quién se le asigna (tasa de éxito por tipo de tarea)
  3. lecciones  -> lo que los revisores y la persona corrigieron antes en ese componente vuelve al prompt
La retrospectiva propone cambios al flujo/equipo; nunca los aplica sola (los decide la persona).
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path

EXEC_ROLES = {"implementer", "evaluator", "researcher", "release_guard"}


def executor_runs(state: dict, task: dict) -> list[dict]:
    return [state["runs"][i] for i in task["run_ids"] if state["runs"][i]["role"] in EXEC_ROLES]


def review_runs(state: dict, task: dict) -> list[dict]:
    return [state["runs"][i] for i in task["run_ids"] if state["runs"][i]["role"] == "reviewer"]


def clean(state: dict, task: dict) -> bool:
    """Tarea terminada sin rondas de cambios, sin fallos y sin auditoría mala."""
    return (task["state"] == "done" and task["rounds"] == 0 and task["failures"] == 0
            and task.get("audit_verdict") != "bad")


def cards(state: dict) -> dict[str, dict]:
    """Tarjeta por agente: intentos, limpias, cambios pedidos, fallos, revisiones y su tasa de aprobación."""
    out: dict[str, dict] = defaultdict(lambda: {"attempts": 0, "clean": 0, "changes": 0, "failed": 0, "bad_audits": 0, "rejected": 0,
                                               "reviews": 0, "approves": 0, "by_type": defaultdict(lambda: [0, 0]), "seconds": []})
    for task in state["tasks"].values():
        execs = executor_runs(state, task)
        if execs:
            first = execs[0]["agent"] if "agent" in execs[0] else execs[0]["provider"]
            c = out[first]
            c["attempts"] += 1
            ok = clean(state, task)
            c["clean"] += ok
            c["by_type"][task["type"]][0] += ok
            c["by_type"][task["type"]][1] += 1
            c["changes"] += task["rounds"]
            c["rejected"] += task["state"] == "rejected"
        for r in execs:
            out[r.get("agent", r["provider"])]["failed"] += r["state"] == "failed"
        for r in review_runs(state, task):
            c = out[r.get("agent", r["provider"])]
            c["reviews"] += 1
            c["approves"] += r.get("review", {}).get("verdict") == "approve"
        if task.get("audit_verdict") == "bad":
            for r in execs + review_runs(state, task):
                out[r.get("agent", r["provider"])]["bad_audits"] += 1
    return {k: {**v, "by_type": {t: tuple(x) for t, x in v["by_type"].items()}} for k, v in out.items()}


def levels(state: dict, team) -> dict[str, int]:
    """Nivel de confianza = inicial + tareas limpias // promote_every - auditorías malas - rechazos humanos."""
    c = cards(state)
    res = {}
    for name, a in team.agents.items():
        k = c.get(name, {"clean": 0, "bad_audits": 0, "rejected": 0})
        raw = a["initial_trust"] + k["clean"] // team.cfg["promote_every"] - k["bad_audits"] - k["rejected"]
        res[name] = max(0, min(a["ceiling"], raw))
    return res


def scores(state: dict, ttype: str) -> dict[str, float]:
    """Tasa de éxito suavizada (Laplace) por agente para este tipo de tarea."""
    return {n: (v["by_type"].get(ttype, (0, 0))[0] + 1) / (v["by_type"].get(ttype, (0, 0))[1] + 2) for n, v in cards(state).items()}


def lessons(state: dict, component: str, ttype: str, limit: int = 6) -> str:
    """Lo que ya se corrigió en este componente (guía humana primero, luego hallazgos de revisores)."""
    items = []
    for task in sorted(state["tasks"].values(), key=lambda t: t["created_at"], reverse=True):
        if task["component"] != component and task["type"] != ttype:
            continue
        same = task["component"] == component
        for e in task["audit"]:
            if e["event"] == "human.decision" and e.get("decision") in {"retry", "reject"} and e.get("note") and same:
                items.append((0, f"La persona pidió (tarea {task['id']}): {e['note']}"))
        for r in review_runs(state, task):
            if r.get("review", {}).get("verdict") == "changes" and same:
                items += [(1, f"Un revisor observó en {task['id']}: {f}") for f in r["review"]["findings"][:3]]
    seen, out = set(), []
    for _, text in sorted(items, key=lambda x: x[0]):
        if text not in seen:
            seen.add(text)
            out.append("- " + text)
    return "## Lecciones de trabajos anteriores en este componente\n" + "\n".join(out[:limit]) if out else ""


def retro(state: dict, team, jev=None, labels_file: Path | None = None) -> str:
    """Informe de retrospectiva con métricas, patrones y PROPUESTAS que requieren decisión humana."""
    cs, lv = cards(state), levels(state, team)
    tasks = list(state["tasks"].values())
    done = [t for t in tasks if t["state"] == "done"]
    lines = [f"# Retrospectiva del flujo — {time.strftime('%Y-%m-%d')}", "",
             f"Tareas: {len(tasks)} · terminadas: {len(done)} · limpias a la primera: {sum(clean(state, t) for t in tasks)} · "
             f"en revisión humana: {sum(t['state'] == 'human_review' for t in tasks)}", "", "## Equipo", "",
             "| Agente | Nivel | Intentos | Limpias | Cambios pedidos | Fallos | Revisiones (aprueba %) | Auditorías malas |", "|---|---|---|---|---|---|---|---|"]
    for name in team.agents:
        c = cs.get(name)
        if c:
            ap = f"{c['reviews']} ({100 * c['approves'] // max(1, c['reviews'])}%)" if c["reviews"] else "-"
            lines.append(f"| {name} | {lv[name]} | {c['attempts']} | {c['clean']} | {c['changes']} | {c['failed']} | {ap} | {c['bad_audits']} |")
        else:
            lines.append(f"| {name} | {lv[name]} | 0 | - | - | - | - | - |")
    # patrones de hallazgos
    findings = [f for t in tasks for r in review_runs(state, t) if r.get("review", {}).get("verdict") == "changes" for f in r["review"]["findings"]]
    findings += [r["failure"] for t in tasks for r in executor_runs(state, t) if r.get("failure")]
    cats = Counter()
    if findings and jev:
        labels = json.loads(labels_file.read_text()) if labels_file and labels_file.exists() else {}
        todo = [f for f in dict.fromkeys(findings) if f not in labels]
        for f, c in zip(todo, jev.categorize(todo)):
            if c:
                labels[f] = c
        if labels_file:
            labels_file.parent.mkdir(parents=True, exist_ok=True)
            labels_file.write_text(json.dumps(labels, ensure_ascii=False, indent=1))
        cats = Counter(labels.get(f) for f in findings if labels.get(f))
    if cats:
        lines += ["", "## Qué se corrige más (categorizado por JEV)", ""] + [f"- {k}: {v}" for k, v in cats.most_common()]
    # propuestas
    props = []
    for name, c in cs.items():
        if c["reviews"] >= 8 and c["approves"] == c["reviews"]:
            props.append(f"**{name}** aprobó {c['reviews']}/{c['reviews']} revisiones: puede ser blando. Auditá sus aprobaciones o subí `audit_sample_rate` para lo que revisa.")
        if c["attempts"] >= 5 and c["clean"] / c["attempts"] < 0.4:
            props.append(f"**{name}** sólo tuvo {c['clean']}/{c['attempts']} tareas limpias: revisá su persona/proveedor o asignale tareas más acotadas.")
        if c["failed"] >= 3 and c["failed"] > c["attempts"]:
            props.append(f"**{name}** acumula {c['failed']} ejecuciones fallidas (cuota/timeouts): revisá plan del proveedor o `run_timeout_s`.")
    if cats.get("missing_tests", 0) >= 3:
        props.append("Se repite `missing_tests`: exigir en el contrato un comando de test específico y pedir a los revisores que rechacen tests débiles.")
    if cats.get("scope_creep", 0) >= 3:
        props.append("Se repite `scope_creep`: acotar `alcance:` en los contratos y sumar un hook post_run que compare contra el ticket.")
    if cats.get("misunderstood", 0) + cats.get("incomplete", 0) >= 3:
        props.append("Se repiten `misunderstood`/`incomplete`: los tickets llegan flojos. Pedir criterios de aceptación en el contrato (JEV ya marca ambigüedad).")
    esc = Counter(t["human_reason"].split(":")[0] for t in tasks if t["human_reason"])
    for reason, n in esc.most_common(2):
        if n >= 3:
            props.append(f"Escalaste a la persona {n} veces por «{reason}»: si casi siempre aprobás, conviene ajustar esa regla; si rechazás, endurecer el paso previo.")
    lines += ["", "## Propuestas (requieren tu decisión; nada se aplica solo)", ""] + (["- " + p for p in props] or ["- Sin propuestas: todavía no hay datos suficientes o no hay patrones."])
    return "\n".join(lines) + "\n"
