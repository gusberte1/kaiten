#!/usr/bin/env python3
"""Cola cooperativa y auditable para los agentes del repositorio.

No ejecuta trabajos ni sustituye las reglas de git: solamente arbitra quién
puede tomar una tarea. Usa flock, por lo que debe ejecutarse desde la copia
canónica Linux del repositorio (la Raspberry) cuando haya dos agentes.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE = ROOT / "queue.json"
DEFAULT_AGENT_FILE = ROOT / "current_agent"
VALID_STATES = {"pending", "claimed", "blocked", "done"}
VALID_AGENTS = {"claude", "codex", "antigravity", "deepseek"}


def normalize_agent(name: str) -> str:
    cleaned = name.strip().lower()
    if cleaned in {"claude-code", "claude_code", "claude"}:
        return "claude"
    if cleaned in {"codex", "openai"}:
        return "codex"
    if cleaned in {"gemini", "antigravity", "gemini-cli", "agy"}:   # Gemini fue reemplazado por Antigravity (2026-09-26)
        return "antigravity"
    if cleaned in {"deepseek", "deepseek-cli", "aider-deepseek"}:
        return "deepseek"
    return cleaned


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def stamp(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@contextmanager
def locked_queue(path: Path):
    """Serializa lecturas/escrituras concurrentes y entrega la cola cargada."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not path.exists():
                data: dict[str, Any] = {"version": 1, "tasks": []}
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("tasks"), list):
                raise ValueError(f"cola inválida: {path}")
            yield data
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def save(path: Path, data: dict[str, Any]) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                            prefix=".queue-", suffix=".tmp", delete=False) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        temp_name = tmp.name
    os.replace(temp_name, path)


def find_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    matches = [item for item in data["tasks"] if item.get("id") == task_id]
    if len(matches) != 1:
        raise ValueError(f"tarea inexistente: {task_id}")
    return matches[0]


def lease_expired(task: dict[str, Any]) -> bool:
    until = parse_stamp(task.get("lease_until"))
    return until is not None and until <= now()


def write_and_print(path: Path, data: dict[str, Any], task: dict[str, Any]) -> None:
    save(path, data)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def command_add(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        if any(item.get("id") == args.id for item in data["tasks"]):
            raise ValueError(f"id ya existente: {args.id}")
        task = {
            "id": args.id,
            "title": args.title,
            "component": args.component,
            "priority": args.priority,
            "state": "pending",
            "owner": None,
            "lease_until": None,
            "next_step": args.next_step,
            "evidence": None,
            "created_at": stamp(),
            "updated_at": stamp(),
        }
        data["tasks"].append(task)
        write_and_print(args.queue, data, task)


def command_list(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        tasks = data["tasks"]
    if args.state:
        tasks = [task for task in tasks if task.get("state") == args.state]
    if not tasks:
        print("Sin tareas.")
        return
    print("state\tpriority\tid\towner\tlease_until\ttitle")
    for task in sorted(tasks, key=lambda x: (x["state"], x["priority"], x["id"])):
        print("{state}\t{priority}\t{id}\t{owner}\t{lease}\t{title}".format(
            state=task["state"], priority=task["priority"], id=task["id"],
            owner=task.get("owner") or "-", lease=task.get("lease_until") or "-",
            title=task["title"],
        ))


def command_claim(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        task = find_task(data, args.id)
        state = task["state"]
        other_owner = task.get("owner") not in (None, args.agent)
        if state == "claimed" and other_owner and not lease_expired(task):
            raise ValueError(
                f"tarea tomada por {task['owner']} hasta {task['lease_until']}; "
                "no la pises"
            )
        if state in {"done", "blocked"}:
            raise ValueError(f"la tarea está {state}; crear una nueva o reabrirla explícitamente")
        task.update({
            "state": "claimed",
            "owner": args.agent,
            "lease_until": stamp(now() + timedelta(minutes=args.lease_minutes)),
            "updated_at": stamp(),
        })
        write_and_print(args.queue, data, task)


def command_heartbeat(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        task = find_task(data, args.id)
        if task["state"] != "claimed" or task.get("owner") != args.agent:
            raise ValueError("sólo el dueño actual puede renovar la tarea")
        task["lease_until"] = stamp(now() + timedelta(minutes=args.lease_minutes))
        task["updated_at"] = stamp()
        if args.next_step:
            task["next_step"] = args.next_step
        write_and_print(args.queue, data, task)


def close_task(args: argparse.Namespace, state: str) -> None:
    with locked_queue(args.queue) as data:
        task = find_task(data, args.id)
        if task.get("owner") != args.agent:
            raise ValueError("sólo el dueño actual puede cerrar o bloquear la tarea")
        task.update({
            "state": state,
            "lease_until": None,
            "next_step": args.next_step,
            "evidence": args.evidence,
            "updated_at": stamp(),
        })
        write_and_print(args.queue, data, task)


def command_release(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        task = find_task(data, args.id)
        if task.get("owner") != args.agent:
            raise ValueError("sólo el dueño actual puede liberar la tarea")
        task.update({
            "state": "pending",
            "owner": None,
            "lease_until": None,
            "next_step": args.next_step,
            "updated_at": stamp(),
        })
        write_and_print(args.queue, data, task)


def command_show(args: argparse.Namespace) -> None:
    with locked_queue(args.queue) as data:
        task = find_task(data, args.id)
    print(json.dumps(task, ensure_ascii=False, indent=2))


def command_set_agent(args: argparse.Namespace) -> None:
    agent = normalize_agent(args.agent)
    if agent not in VALID_AGENTS:
        raise ValueError(f"agente inválido: {args.agent}. Opciones válidas: {', '.join(sorted(VALID_AGENTS))}")
    args.agent_file.write_text(f"{agent}\n", encoding="utf-8")
    print(f"Agente activo establecido a: {agent}")


def command_current_agent(args: argparse.Namespace) -> None:
    if args.agent_file.exists():
        agent = args.agent_file.read_text(encoding="utf-8").strip()
    else:
        agent = "claude"
    print(agent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE,
                        help="cola JSON (por defecto agents/queue.json)")
    parser.add_argument("--agent-file", type=Path, default=DEFAULT_AGENT_FILE,
                        help="archivo de agente activo (por defecto agents/current_agent)")
    sub = parser.add_subparsers(dest="command", required=True)

    set_ag = sub.add_parser("set-agent", help="definir qué agente tomará el próximo turno")
    set_ag.add_argument("agent", help=f"nombre del agente ({', '.join(sorted(VALID_AGENTS))})")
    set_ag.set_defaults(func=command_set_agent)

    cur_ag = sub.add_parser("current-agent", help="consultar qué agente tiene el turno actual")
    cur_ag.set_defaults(func=command_current_agent)

    add = sub.add_parser("add", help="crear una tarea pendiente")
    add.add_argument("id", help="id estable, por ejemplo obb-mobile-sam-benchmark")
    add.add_argument("--title", required=True)
    add.add_argument("--component", required=True)
    add.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")
    add.add_argument("--next-step", required=True)
    add.set_defaults(func=command_add)

    listing = sub.add_parser("list", help="ver tareas")
    listing.add_argument("--state", choices=sorted(VALID_STATES))
    listing.set_defaults(func=command_list)

    claim = sub.add_parser("claim", help="tomar una tarea con lease")
    claim.add_argument("id")
    claim.add_argument("--agent", required=True, help="por ejemplo codex o claude-code")
    claim.add_argument("--lease-minutes", type=int, default=120)
    claim.set_defaults(func=command_claim)

    beat = sub.add_parser("heartbeat", help="renovar lease y próximo paso")
    beat.add_argument("id")
    beat.add_argument("--agent", required=True)
    beat.add_argument("--lease-minutes", type=int, default=120)
    beat.add_argument("--next-step")
    beat.set_defaults(func=command_heartbeat)

    for name, state, help_text in (
        ("complete", "done", "marcar terminada"),
        ("block", "blocked", "marcar bloqueada"),
    ):
        closing = sub.add_parser(name, help=help_text)
        closing.add_argument("id")
        closing.add_argument("--agent", required=True)
        closing.add_argument("--next-step", required=True)
        closing.add_argument("--evidence", required=True,
                             help="commit, ruta de reporte o motivo verificable")
        closing.set_defaults(func=lambda args, result=state: close_task(args, result))

    release = sub.add_parser("release", help="devolver una tarea pendiente")
    release.add_argument("id")
    release.add_argument("--agent", required=True)
    release.add_argument("--next-step", required=True)
    release.set_defaults(func=command_release)

    show = sub.add_parser("show", help="ver una tarea completa")
    show.add_argument("id")
    show.set_defaults(func=command_show)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
