#!/usr/bin/env python3
"""Máquina de estados y política del flujo de agentes (sin red, sin proveedores).

Es la capa que decide qué transiciones son legales: quién puede implementar,
quién puede revisar (siempre otro proveedor), qué evidencia hace falta según
riesgo y cuándo entra una persona. `flow.py` la maneja y ejecuta a los agentes;
esta capa no llama a nada externo. Cada tarea lleva una cadena de auditoría con
hash encadenado (`verify` la comprueba). Estado por defecto en `.runtime/`.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

HERE = Path(__file__).resolve().parent
from paths import RUNTIME
DEFAULT_STATE = RUNTIME / "agent-orchestration.json"
ROLES_FILE = HERE / "orchestration_roles.json"
POLICY_FILE = HERE / "orchestration_policy.json"
TASK_TYPES = {"research", "code", "data", "test", "security", "release"}
RISKS = {"low", "medium", "high"}
FINAL_TASK_STATES = {"rejected", "done"}
GENESIS = "0" * 64


def stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "tasks": {}, "runs": {}}
    data = load_json(path)
    if data.get("version") != 1 or not isinstance(data.get("tasks"), dict) or not isinstance(data.get("runs"), dict):
        raise ValueError(f"estado inválido: {path}")
    for task in data["tasks"].values():
        for key, default in (("rounds", 0), ("failures", 0), ("approvals", []), ("human_reason", "")):
            task.setdefault(key, default)
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        name = tmp.name
    os.replace(name, path)


def entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audit(task: dict[str, Any], event: str, **fields: Any) -> None:
    chain = task.setdefault("audit", [])
    entry = {"at": stamp(), "event": event, **fields, "prev": chain[-1]["hash"] if chain else GENESIS}
    entry["hash"] = entry_hash(entry)
    chain.append(entry)


def verify_chain(task: dict[str, Any]) -> list[str]:
    problems, prev = [], GENESIS
    for i, entry in enumerate(task.get("audit", [])):
        if "hash" not in entry and prev == GENESIS:
            continue  # eventos anteriores a la cadena (formato viejo): no verificables
        if entry.get("prev") != prev or entry.get("hash") != entry_hash(entry):
            problems.append(f"{task['id']}: cadena rota en el evento {i} ({entry.get('event')})")
        prev = entry.get("hash", "")
    return problems


def flow_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    return policy.get("flow", {})


def task_or_fail(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    if task_id not in data["tasks"]:
        raise ValueError(f"tarea inexistente: {task_id}")
    return data["tasks"][task_id]


def cmd_add(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    if args.id in data["tasks"]:
        raise ValueError(f"tarea ya existe: {args.id}")
    if args.type not in TASK_TYPES or args.risk not in RISKS:
        raise ValueError("tipo o riesgo inválido")
    task = {
        "id": args.id, "title": args.title, "component": args.component,
        "type": args.type, "risk": args.risk, "protected_actions": args.protected_action,
        "state": "ready", "run_ids": [], "audit": [], "created_at": stamp(),
        "rounds": 0, "failures": 0, "approvals": [], "human_reason": "",
    }
    audit(task, "task.created", actor=args.actor)
    data["tasks"][args.id] = task


def providers_of(data: dict[str, Any], task: dict[str, Any], reviewer: bool) -> set[str]:
    return {r["provider"] for r in (data["runs"][i] for i in task["run_ids"]) if (r["role"] == "reviewer") == reviewer}


def cmd_start(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    task = task_or_fail(data, args.task_id)
    role = roles["roles"].get(args.role)
    if not role:
        raise ValueError(f"rol inexistente: {args.role}")
    reviewer = bool(role.get("reviews"))
    allowed = {"in_review"} if reviewer else {"ready", "changes_requested"}
    if task["state"] not in allowed:
        raise ValueError(f"no se puede iniciar el rol {args.role} desde {task['state']}")
    if task["type"] not in role["allowed_task_types"]:
        raise ValueError(f"el rol {args.role} no puede ejecutar tareas {task['type']}")
    if any(data["runs"][i]["state"] == "executing" for i in task["run_ids"]):
        raise ValueError("ya hay una ejecución activa en esta tarea")
    if reviewer and flow_cfg(policy).get("cross_provider_review", True):
        # supervisión real: revisa un proveedor distinto del que implementó y de los que ya aprobaron
        if args.provider in providers_of(data, task, reviewer=False):
            raise ValueError(f"{args.provider} implementó esta tarea: no puede revisarla")
        if args.provider in task["approvals"]:
            raise ValueError(f"{args.provider} ya aprobó esta ronda: hace falta otro proveedor")
    run_id = f"run_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"
    run = {
        "id": run_id, "task_id": task["id"], "provider": args.provider, "agent": args.agent or args.provider,
        "role": args.role, "provider_session_ref": args.session_ref,
        "branch": args.branch, "worktree": args.worktree, "state": "executing",
        "prev_task_state": task["state"], "evidence": [], "started_at": stamp(),
    }
    data["runs"][run_id] = run
    task["run_ids"].append(run_id)
    task["state"] = "reviewing" if reviewer else "executing"
    audit(task, "run.started", run_id=run_id, provider=args.provider, agent=args.agent or args.provider, role=args.role)
    print(run_id)


def active_run(data: dict[str, Any], run_id: str) -> dict[str, Any]:
    run = data["runs"].get(run_id)
    if not run or run["state"] != "executing":
        raise ValueError("la ejecución no existe o no está activa")
    return run


def cmd_evidence(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    run = active_run(data, args.run_id)
    run["evidence"].append({"kind": args.kind, "value": args.value, "at": stamp()})
    task = task_or_fail(data, run["task_id"])
    audit(task, "evidence.added", run_id=run["id"], kind=args.kind, value=args.value)


def needs_human(task: dict[str, Any], policy: dict[str, Any]) -> str:
    if task["protected_actions"]:
        return "acción protegida: " + ", ".join(task["protected_actions"])
    if policy["risk_policy"][task["risk"]]["human_review"]:
        return f"riesgo {task['risk']}"
    return ""


def after_reviews(task: dict[str, Any], policy: dict[str, Any]) -> None:
    reason = needs_human(task, policy)
    task["state"] = "human_review" if reason else "validated"
    task["human_reason"] = reason


def cmd_submit(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    run = active_run(data, args.run_id)
    if run["role"] == "reviewer":
        raise ValueError("un revisor cierra con submit-review")
    task = task_or_fail(data, run["task_id"])
    kinds = {item["kind"] for item in run["evidence"]}
    required = set(policy["risk_policy"][task["risk"]]["required_evidence"]) | set(policy["task_type_evidence"][task["type"]])
    missing = sorted(required - kinds)
    if missing:
        raise ValueError("evidencia faltante: " + ", ".join(missing))
    run["state"] = "submitted"
    run["summary"] = args.summary
    task["approvals"] = []          # código nuevo: las aprobaciones anteriores ya no valen
    if policy["risk_policy"][task["risk"]].get("independent_reviews", 0) > 0:
        task["state"] = "in_review"
    else:
        after_reviews(task, policy)
    audit(task, "run.submitted", run_id=run["id"], target_state=task["state"])


def cmd_review(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    run = active_run(data, args.run_id)
    if run["role"] != "reviewer":
        raise ValueError("sólo una ejecución con rol reviewer emite veredicto")
    task = task_or_fail(data, run["task_id"])
    findings = json.loads(Path(args.findings_file).read_text()) if args.findings_file else []
    run["state"], run["summary"] = "submitted", args.summary
    run["review"] = {"verdict": args.verdict, "findings": [str(f)[:500] for f in findings][:20]}
    need = policy["risk_policy"][task["risk"]].get("independent_reviews", 1)
    if args.verdict == "escalate":
        task["state"], task["human_reason"] = "human_review", "el revisor escaló: " + args.summary[:200]
    elif args.verdict == "changes":
        task["rounds"] += 1
        if task["rounds"] >= flow_cfg(policy).get("max_review_rounds", 3):
            task["state"], task["human_reason"] = "human_review", f"{task['rounds']} rondas sin acuerdo entre agentes"
        else:
            task["state"] = "changes_requested"
    else:
        task["approvals"].append(run["provider"])
        if len(task["approvals"]) >= need:
            after_reviews(task, policy)
        else:
            task["state"] = "in_review"
    audit(task, "review.submitted", run_id=run["id"], provider=run["provider"], verdict=args.verdict, target_state=task["state"])


def cmd_fail(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    """Una ejecución no llegó a buen puerto (cuota, timeout, crash): se devuelve la tarea
    a su estado previo; tras N fallos escala a persona en vez de reintentar sin fin."""
    run = active_run(data, args.run_id)
    task = task_or_fail(data, run["task_id"])
    run["state"], run["failure"] = "failed", args.reason[:300]
    task["failures"] += 1 if args.count else 0
    task["state"] = run["prev_task_state"]
    if task["failures"] >= flow_cfg(policy).get("max_run_failures", 3):
        task["state"], task["human_reason"] = "human_review", f"{task['failures']} ejecuciones fallidas: {args.reason[:150]}"
    audit(task, "run.failed", run_id=run["id"], reason=args.reason[:300], target_state=task["state"])


def cmd_escalate(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    task = task_or_fail(data, args.task_id)
    if task["state"] in FINAL_TASK_STATES:
        raise ValueError("la tarea ya está cerrada")
    for run_id in task["run_ids"]:
        if data["runs"][run_id]["state"] == "executing":
            data["runs"][run_id]["state"] = "aborted"
    task["state"], task["human_reason"] = "human_review", args.reason[:1500]
    audit(task, "task.escalated", actor=args.actor, reason=args.reason[:1500])


def cmd_decide(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    task = task_or_fail(data, args.task_id)
    if task["state"] != "human_review":
        raise ValueError("la tarea no está esperando revisión humana")
    if args.decision == "retry":
        task["state"], task["guidance"], task["rounds"], task["failures"] = "ready", args.note, 0, 0
    else:
        task["state"] = "approved" if args.decision == "approve" else "rejected"
    audit(task, "human.decision", human=args.human, decision=args.decision, note=args.note, reason=task["human_reason"])


def cmd_reopen(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    task = task_or_fail(data, args.task_id)
    if task["state"] != "rejected":
        raise ValueError("sólo se puede reabrir una tarea rechazada")
    task["state"], task["guidance"], task["rounds"], task["failures"], task["human_reason"] = "ready", args.note, 0, 0, ""
    audit(task, "human.reopened", human=args.human, note=args.note)


def cmd_complete(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    task = task_or_fail(data, args.task_id)
    if task["state"] not in {"approved", "validated"}:
        raise ValueError("sólo una tarea aprobada o validada puede completarse")
    task["state"] = "done"
    audit(task, "task.completed", actor=args.actor)


def cmd_mark(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    """Anotaciones del controlador que no cambian el estado (merge, muestra de auditoría, notas)."""
    task = task_or_fail(data, args.task_id)
    if args.field:
        task[args.field] = json.loads(args.value)
    audit(task, args.event, actor=args.actor, detail=args.value[:300])


def cmd_verify(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    problems = [p for t in data["tasks"].values() for p in verify_chain(t)]
    print("\n".join(problems) or f"cadenas de auditoría íntegras ({len(data['tasks'])} tareas)")
    if problems:
        raise ValueError(f"{len(problems)} problema(s) de integridad")


def cmd_show(args: argparse.Namespace, data: dict[str, Any], roles: dict[str, Any], policy: dict[str, Any]) -> None:
    if args.task_id:
        task = task_or_fail(data, args.task_id)
        print(json.dumps({"task": task, "runs": [data["runs"][run] for run in task["run_ids"]]}, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add-task")
    add.add_argument("id"); add.add_argument("--title", required=True); add.add_argument("--component", required=True)
    add.add_argument("--type", required=True, choices=sorted(TASK_TYPES)); add.add_argument("--risk", required=True, choices=sorted(RISKS))
    add.add_argument("--protected-action", action="append", default=[]); add.add_argument("--actor", default="orchestrator"); add.set_defaults(func=cmd_add)
    start = sub.add_parser("start-run")
    start.add_argument("task_id"); start.add_argument("--provider", required=True); start.add_argument("--role", required=True)
    start.add_argument("--agent"); start.add_argument("--session-ref", required=True); start.add_argument("--branch", required=True); start.add_argument("--worktree", required=True); start.set_defaults(func=cmd_start)
    ev = sub.add_parser("add-evidence")
    ev.add_argument("run_id"); ev.add_argument("--kind", required=True); ev.add_argument("--value", required=True); ev.set_defaults(func=cmd_evidence)
    submit = sub.add_parser("submit-run"); submit.add_argument("run_id"); submit.add_argument("--summary", required=True); submit.set_defaults(func=cmd_submit)
    review = sub.add_parser("submit-review"); review.add_argument("run_id"); review.add_argument("--verdict", required=True, choices=["approve", "changes", "escalate"])
    review.add_argument("--summary", required=True); review.add_argument("--findings-file"); review.set_defaults(func=cmd_review)
    fail = sub.add_parser("fail-run"); fail.add_argument("run_id"); fail.add_argument("--reason", required=True)
    fail.add_argument("--no-count", dest="count", action="store_false"); fail.set_defaults(func=cmd_fail)
    esc = sub.add_parser("escalate"); esc.add_argument("task_id"); esc.add_argument("--reason", required=True); esc.add_argument("--actor", default="orchestrator"); esc.set_defaults(func=cmd_escalate)
    decide = sub.add_parser("decide")
    decide.add_argument("task_id"); decide.add_argument("--human", required=True); decide.add_argument("--note", required=True)
    group = decide.add_mutually_exclusive_group(required=True)
    for name in ("approve", "reject", "retry"):
        group.add_argument(f"--{name}", dest="decision", action="store_const", const=name)
    decide.set_defaults(func=cmd_decide)
    reopen = sub.add_parser("reopen"); reopen.add_argument("task_id"); reopen.add_argument("--human", required=True); reopen.add_argument("--note", required=True); reopen.set_defaults(func=cmd_reopen)
    done = sub.add_parser("complete"); done.add_argument("task_id"); done.add_argument("--actor", default="orchestrator"); done.set_defaults(func=cmd_complete)
    mark = sub.add_parser("mark"); mark.add_argument("task_id"); mark.add_argument("--event", required=True); mark.add_argument("--actor", default="orchestrator")
    mark.add_argument("--field"); mark.add_argument("--value", default="null"); mark.set_defaults(func=cmd_mark)
    sub.add_parser("verify").set_defaults(func=cmd_verify)
    show = sub.add_parser("show"); show.add_argument("task_id", nargs="?"); show.set_defaults(func=cmd_show)
    return p


READ_ONLY = {"show", "verify"}


def execute(argv: list[str]) -> tuple[int, str]:
    """Corre un comando bajo flock (la CLI, el dispatcher y varios hilos comparten estado)."""
    args = parser().parse_args(argv)
    out = io.StringIO()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with open(args.state.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data, roles, policy = state(args.state), load_json(ROLES_FILE), load_json(POLICY_FILE)
            with contextlib.redirect_stdout(out):
                args.func(args, data, roles, policy)
            if args.command not in READ_ONLY:
                save(args.state, data)
        except (ValueError, json.JSONDecodeError, KeyError) as exc:
            return 2, f"ERROR: {exc}"
    return 0, out.getvalue()


def main() -> int:
    rc, out = execute(sys.argv[1:])
    print(out, end="", file=sys.stderr if rc else sys.stdout)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
