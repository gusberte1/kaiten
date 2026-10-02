#!/usr/bin/env python3
"""Adaptador API v2 mínimo entre el controlador y Vikunja.

No ejecuta agentes: crea el proyecto/ticket y publica evidencia sanitizada.
El token vive fuera del repo en ~/.config/sushi-inspector/vikunja-poc.env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
from paths import RUNTIME
DEFAULT_STATE = RUNTIME / "agent-orchestration.json"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "sushi-inspector" / "vikunja-poc.env"
BASE_URL = "http://100.75.61.75:3457/api/v2"
REPO_URL = "https://github.com/gusberte1/Sushi-Inspector-v2"
FLOW_DOC_URL = f"{REPO_URL}/blob/main/agents/FLOW.md"
MAX_COMMENT_WORDS = 49
MAX_COMMENT_CHARS = 3500

# Sólo URLs navegables cuentan como documentación. Las rutas de .runtime son
# evidencia local del controlador, no enlaces útiles dentro de Vikunja.
DOC_LINK_RE = re.compile(r'\[[^\]]+\]\((https?://[^)\s]+)\)|https?://\S+')
LOCAL_LINK_RE = re.compile(r'\[([^\]]+)\]\((?!https?://)([^)]+)\)')


def component_doc_link(repo: Path | None, comp: str) -> str:
    """Devuelve sólo enlaces que ya existen en la rama pública ``main``."""
    if not comp or not repo:
        return f"[Flujo de agentes]({FLOW_DOC_URL})"
    clean_comp = comp.strip("/")
    readme = f"{clean_comp}/README.md"
    try:
        published = subprocess.run(
            ["git", "cat-file", "-e", f"main:{readme}"], cwd=repo,
            capture_output=True, text=True,
        ).returncode == 0
    except OSError:
        published = False
    if published:
        return f"[{clean_comp}/README.md]({REPO_URL}/blob/main/{clean_comp}/README.md)"
    # No se enlaza un árbol ni un README que sólo existe en el worktree: el
    # destinatario de Vikunja no puede navegarlo todavía.
    return f"[Flujo de agentes]({FLOW_DOC_URL})"


def brief_comment(text: str, default_url: str = FLOW_DOC_URL) -> str:
    """Hace un comentario breve, navegable y con un límite duro de tamaño.

    Los mensajes del flujo se redactan cortos de origen. Este cierre es una
    barrera para entradas de proveedores o CLI: conserva enlaces HTTP completos
    como trailer y nunca considera ``.runtime`` documentación navegable.
    """
    text = text.strip()
    default_doc_link = f"[agents/FLOW.md]({default_url})"
    if not text:
        return default_doc_link

    links = list(dict.fromkeys(m.group(0) for m in DOC_LINK_RE.finditer(text)))
    # Un enlace de documentación estable siempre queda al final. Así una URL
    # en medio no se corta ni deja el comentario sin documentación.
    if not links:
        links = [default_doc_link]
    trailer = " | ".join(links)
    allowed_words = MAX_COMMENT_WORDS - len(trailer.split())
    if allowed_words <= 0 or len(trailer) > MAX_COMMENT_CHARS:
        return default_doc_link[:MAX_COMMENT_CHARS]

    # Las URLs se mudan al trailer; en el cuerpo sólo queda la información.
    body = DOC_LINK_RE.sub("", text)
    body = LOCAL_LINK_RE.sub(lambda m: f"{m.group(1)}: `{m.group(2)}`", body).strip()
    matches = list(re.finditer(r"\S+", body))
    if len(matches) > allowed_words:
        body = body[:matches[allowed_words - 1].end()].rstrip(".,;:") + "…"
    if body.count("```") % 2:
        body += "\n```"

    separator = "\n\n" if body else ""
    result = f"{body}{separator}{trailer}".strip()
    if len(result) <= MAX_COMMENT_CHARS:
        return result
    # Una palabra sin espacios no elude el tope duro. Conservamos el enlace,
    # que es más útil que un texto gigantesco o una URL cortada.
    budget = MAX_COMMENT_CHARS - len(trailer) - 2
    if budget <= 0:
        return default_doc_link
    if len(body) > budget:
        body = body[:max(budget - 1, 0)].rstrip() + "…"
    return f"{body}\n\n{trailer}"


def token_from(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"falta token: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("VIKUNJA_API_TOKEN="):
            token = line.partition("=")[2].strip()
            if token.startswith("tk_"):
                return token
    raise ValueError("token Vikunja inválido")


class Client:
    def __init__(self, base_url: str, token: str):
        self.base_url, self.token = base_url.rstrip("/"), token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None, content_type: str = "application/json") -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise ValueError(f"Vikunja HTTP {error.code}: {error.read().decode(errors='replace')[:300]}") from error
        return json.loads(body) if body else None


class VikunjaTracker:
    """Implementa `tracker.Tracker` sobre la API v2 de Vikunja (el kanban son los buckets de una vista)."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.client = Client(cfg["base_url"], token_from(Path(cfg["token_file"]).expanduser()))
        self.humans = {h.lower() for h in cfg["human_usernames"]}
        self.bots = {b.lower() for b in cfg["bot_usernames"]}
        self._board = f"/projects/{cfg['project_id']}/views/{cfg['kanban_view_id']}/buckets"

    def tasks(self) -> list[dict]:
        items, page = [], 1
        while True:
            body = self.client.request("GET", f"/projects/{self.cfg['project_id']}/tasks?format=markdown&per_page=50&page={page}")
            items += body.get("items", [])
            if page >= body.get("total_pages", 1):
                break
            page += 1
        return [{"id": t["id"], "title": t["title"], "description": t.get("description", ""), "done": t["done"],
                 "priority": t.get("priority", 0), "assignees": [a["username"] for a in t.get("assignees") or []]} for t in items]

    def comments(self, tid: int) -> list[dict]:
        items = self.client.request("GET", f"/tasks/{tid}/comments?format=markdown&per_page=100").get("items", [])
        return [{"id": c["id"], "author": c["author"]["username"], "text": c["comment"]} for c in items]

    def comment(self, tid: int, text: str) -> dict:
        return self.client.request("POST", f"/tasks/{tid}/comments?format=markdown", {"comment": text}) or {}

    def link(self, tid: int, comment_id: int | None = None) -> str:
        base = self.cfg["base_url"].split("/api/")[0]
        return f"{base}/tasks/{tid}" + (f"#comment-{comment_id}" if comment_id else "")

    def close(self, tid: int) -> None:
        self.client.request("PATCH", f"/tasks/{tid}", {"done": True}, content_type="application/merge-patch+json")

    def _buckets(self) -> dict[str, dict]:
        r = self.client.request("GET", self._board)
        return {b["title"]: b for b in (r if isinstance(r, list) else r.get("items", []))}

    def columns(self) -> list[str]:
        return list(self._buckets())

    def setup_columns(self, wanted: list[str], rename: dict[str, str]) -> None:
        have = self._buckets()
        for old, new in rename.items():
            if old in have and new not in have:
                self.client.request("PUT", f"{self._board}/{have[old]['id']}", {"title": new, "position": 0})
                have[new] = have.pop(old)
        for i, title in enumerate(wanted, 1):
            if title not in have:
                self.client.request("POST", self._board, {"title": title, "position": i * 1000.0})
        have = self._buckets()
        for i, title in enumerate(wanted, 1):
            self.client.request("PUT", f"{self._board}/{have[title]['id']}", {"title": title, "position": i * 1000.0})

    def move(self, tid: int, column: str) -> None:
        bid = self._buckets()[column]["id"]
        self.client.request("PUT", f"{self._board}/{bid}/tasks", {"task_id": tid, "bucket_id": bid})


def load_task(state_path: Path, task_id: str) -> dict[str, Any]:
    data = json.loads(state_path.read_text(encoding="utf-8"))
    try:
        return data["tasks"][task_id]
    except KeyError as error:
        raise ValueError(f"tarea del controlador inexistente: {task_id}") from error


def description(task: dict[str, Any]) -> str:
    actions = ", ".join(task.get("protected_actions", [])) or "ninguna"
    return "\n".join([
        "<!-- sushi-inspector-orchestration -->",
        "## Contrato de ejecución",
        f"- **Task ID:** `{task['id']}`",
        f"- **Componente:** `{task['component']}`",
        f"- **Tipo / riesgo:** `{task['type']}` / `{task['risk']}`",
        f"- **Acciones protegidas:** {actions}",
        "- **Política:** las ejecuciones, evidencia y aprobaciones se registran por el controlador.",
        "",
        "No incluir tokens, secretos ni transcripciones de agentes en este ticket.",
    ])


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = p.add_subparsers(dest="command", required=True)
    projects = sub.add_parser("projects")
    create_project = sub.add_parser("create-project")
    create_project.add_argument("--title", required=True); create_project.add_argument("--description", default="")
    create_task = sub.add_parser("create-task")
    create_task.add_argument("--project-id", type=int, required=True); create_task.add_argument("--task-id", required=True)
    update_task = sub.add_parser("update-task-description")
    update_task.add_argument("--vikunja-task-id", type=int, required=True); update_task.add_argument("--task-id", required=True)
    comment = sub.add_parser("comment")
    comment.add_argument("--vikunja-task-id", type=int, required=True); comment.add_argument("--message", required=True)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        client = Client(args.base_url, token_from(args.token_file))
        if args.command == "projects":
            result = client.request("GET", "/projects")
        elif args.command == "create-project":
            result = client.request("POST", "/projects", {"title": args.title, "description": args.description})
        elif args.command == "create-task":
            task = load_task(args.state, args.task_id)
            result = client.request("POST", f"/projects/{args.project_id}/tasks?format=markdown", {"title": f"[{task['id']}] {task['title']}", "description": description(task)})
        elif args.command == "update-task-description":
            task = load_task(args.state, args.task_id)
            result = client.request("PUT", f"/tasks/{args.vikunja_task_id}?format=markdown", {"title": f"[{task['id']}] {task['title']}", "description": description(task)})
        else:
            result = client.request("POST", f"/tasks/{args.vikunja_task_id}/comments?format=markdown", {"comment": brief_comment(args.message)})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, json.JSONDecodeError, OSError) as error:
        print(f"ERROR: {error}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
