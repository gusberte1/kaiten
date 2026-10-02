#!/usr/bin/env python3
"""Flujo autónomo multi-proveedor: tracker de tickets (Vikunja u otro) -> implementar -> revisión cruzada -> merge.

Un dispatcher (`tick`/`daemon`) mueve las tareas por la máquina de estados de
`orchestrator.py`:

  ready -> implementa el proveedor A (worktree aislado, autonomía plena)
        -> el controlador corre los tests y aplica guardas (alcance, secretos, tamaño, hooks)
        -> revisa un proveedor B != A (solo lectura, verificado) -> approve | changes | escalate
        -> merge a main (sin persona salvo riesgo alto / acción protegida / escalado)

A la persona sólo se la avisa (Telegram + comentario en Vikunja) en decisiones críticas y
responde con `/aprobar`, `/rechazar`, `/reintentar <guía>` en el ticket, o con `flow.py decide`.
Todo queda en `.runtime/agent-runs/<tarea>/<run>/` (prompt, salida redactada, diff, tests, veredicto)
y en la cadena de auditoría de la tarea; `audit.py` muestrea y verifica.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import budget
import jev as jevmod
import learn
import orchestrator
import providers as prov
import session_registry
import team as teammod
import tracker as trackermod
import vikunja_adapter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG_FILE = HERE / "flow_config.json"
TAG = re.compile(r"<[^>]+>")


class FlowError(RuntimeError):
    pass


class Meta(dict):
    base: dict


def merge3(disk, base, new):
    """Fusión de 3 vías: aplica sobre `disk` los cambios que hay entre `base` (lo que leyó el llamador) y `new` (lo que quiere guardar)."""
    if isinstance(new, dict) and isinstance(base, dict) and isinstance(disk, dict):
        out = dict(disk)
        for k in new:
            out[k] = merge3(disk.get(k), base[k], new[k]) if k in base else new[k]
        for k in base:
            if k not in new:
                out.pop(k, None)
        return out
    if isinstance(new, list) and isinstance(base, list) and isinstance(disk, list):
        return disk + [x for x in new[len(base):] if x not in disk] if new[:len(base)] == base else new
    return new if new != base else disk


def sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def clean(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", TAG.sub("", text or "").replace("&nbsp;", " ")).strip()


def glob_match(path: str, pattern: str) -> bool:
    """`**` cruza directorios; `*` no. Un patrón sin `/` casa contra el basename."""
    if "/" not in pattern:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(rx, path) is not None


def any_match(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)


def strip_quotes(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">")).strip()


def parse_contract(description: str) -> dict[str, str]:
    """Líneas `clave: valor` del ticket (markdown tolerado: negritas, backticks, viñetas). Requiere `agente: listo`."""
    out = {}
    text = clean(description).replace("\\*", "*").replace("\\_", "_")   # Vikunja escapa `\*` y junta líneas seguidas en un solo párrafo:
    text = re.sub(r"(?<=\S)[ \t]+((?:agente|tipo|riesgo|componente|test|acciones|alcance|proveedor|timeout|sin-merge)\s*:)", r"\n\1", text, flags=re.I)  # se separan por clave
    for line in text.splitlines():
        m = re.match(r"^[\s>*_`-]*([^:*_`]+?)[*_`]*\s*:\s*(?:\*\*|__)?\s*(.+?)\s*$", line.strip(), re.I)
        if not m:
            continue
        val = m.group(2)
        for mark in ("`", "**", "__"):   # sólo se quita el énfasis que rodea al valor completo (los globs `agents/**` se conservan)
            if len(val) > 2 * len(mark) and val.startswith(mark) and val.endswith(mark):
                val = val[len(mark):-len(mark)].strip()
        out[m.group(1).strip().lower().replace(" ", "-")] = val
    return out


def load_project(repo: Path) -> dict:
    """flow-project.json en la raíz del repo: todo lo que es del proyecto y no del motor (ramas, guardas, hooks, componentes)."""
    try:
        return json.loads((Path(repo) / "flow-project.json").read_text())
    except (OSError, ValueError):
        return {}


# Columnas del tablero del tracker = estados del flujo (el orden es el de izquierda a derecha)
COLUMNS = ["Backlog", "En cola", "Trabajando", "En revisión", "🙋 Tu decisión", "Hecho", "Descartadas"]
LEGACY_COLUMNS = {"To-Do": "Backlog", "Doing": "Trabajando", "Done": "Hecho"}
STATE_COLUMN = {"ready": "En cola", "executing": "Trabajando", "changes_requested": "Trabajando", "validated": "Trabajando", "approved": "Trabajando",
                "in_review": "En revisión", "reviewing": "En revisión", "human_review": "🙋 Tu decisión", "done": "Hecho", "rejected": "Descartadas"}


class Flow:
    def __init__(self, repo: Path = ROOT, runtime: Path | None = None, providers_file: Path = prov.DEFAULT_FILE,
                 policy_file: Path = orchestrator.POLICY_FILE, tracker=None, notify=None, main_branch: str | None = None,
                 team_file: Path = teammod.HERE / "team.json", jev_transport=None, jev_key: str | None = None):
        self.repo = repo
        self.rt = runtime or repo / ".runtime"
        self.state_file = self.rt / "agent-orchestration.json"
        self.meta_file = self.rt / "flow-meta.json"
        self.runs_dir, self.wt_dir = self.rt / "agent-runs", self.rt / "worktrees"
        self.pause_file = self.rt / "flow.pause"
        self.policy_file = Path(policy_file)
        self.policy = orchestrator.load_json(self.policy_file)
        self.project = load_project(repo)
        self.cfg = {**self.policy["flow"], **self.project.get("guards", {})}
        if "hooks_dir" in self.project:
            self.cfg["hooks_dir"] = self.project["hooks_dir"]
        self.provs = prov.Providers(providers_file, self.rt / "provider-health.json", self.rt / "quota.json")
        branches = self.project.get("branches", {})
        self.tracker, self.main = tracker, main_branch or branches.get("main", "main")
        self.integ = branches.get("integration") or self.cfg.get("integration_branch", "dev")   # los tickets se integran en dev; main sólo por release
        self.team = teammod.Team(team_file)
        self.jev = jevmod.Jev(self.rt / "jev", key=jev_key, transport=jev_transport, daily_cap=self.cfg.get("jev_daily_cap", 400))
        self.notify = notify or self._notify_telegram
        self.prompts = HERE / "flow_prompts"
        self.git_email = self.project.get("git_email", "agents@localhost")
        self.component_hint = re.compile(self.project["component_hint"]) if self.project.get("component_hint") else None
        self.component_map = [(re.compile(rx, re.I), comp) for rx, comp in self.project.get("component_map", [])]

    # ---------- utilidades ----------
    def _notify_telegram(self, text: str) -> None:
        cmd = self.project.get("notify_cmd")
        if cmd:
            subprocess.run([sys.executable, str(self.repo / cmd[1]), text] if cmd[0].startswith("python") else [*cmd, text],
                           capture_output=True, timeout=90)

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        r = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True)
        if check and r.returncode:
            raise FlowError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def orch(self, *argv: str) -> str:
        rc, out = orchestrator.execute(["--state", str(self.state_file), *argv])
        if rc:
            raise FlowError(out.strip())
        return out.strip()

    def tasks(self) -> dict:
        return orchestrator.state(self.state_file)

    EMPTY_META = {"tasks": {}, "notified": [], "invalid": {}, "seen": {}, "daily": {}, "proposals": {}}

    def _load_meta(self) -> dict:
        try:
            return {**copy.deepcopy(self.EMPTY_META), **json.loads(self.meta_file.read_text())}
        except (OSError, ValueError):
            return copy.deepcopy(self.EMPTY_META)

    def meta(self) -> "Meta":
        """Lectura con foto (`base`): save_meta aplica sólo lo que ESTE llamador cambió, sobre lo que haya en disco (fusión de 3 vías).
        Así el daemon y la CLI pueden escribir a la vez sin pisarse (antes una tarea recién tomada se perdía)."""
        m = Meta(self._load_meta())
        m.base = copy.deepcopy(dict(m))
        return m

    def save_meta(self, m: dict) -> None:
        self.meta_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.meta_file.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = merge3(self._load_meta(), getattr(m, "base", None), dict(m)) if hasattr(m, "base") else dict(m)
            tmp = self.meta_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1))
            os.replace(tmp, self.meta_file)
        if hasattr(m, "base"):
            m.clear(); m.update(merged); m.base = copy.deepcopy(merged)

    def tmeta(self, tid: str) -> dict:
        return self.meta()["tasks"].setdefault(tid, {})

    def set_tmeta(self, tid: str, **kv) -> None:
        m = self.meta()
        m["tasks"].setdefault(tid, {}).update(kv)
        self.save_meta(m)

    def comment(self, vid: int, text: str) -> dict:
        """Publica el formato acotado también cuando el tracker es un doble de prueba."""
        return self.tracker.comment(vid, vikunja_adapter.brief_comment(text)) or {}

    def say(self, tid: str, text: str) -> int | None:
        """Comentario sanitizado en el ticket; devuelve su id. Nunca rompe el flujo si Vikunja no responde."""
        vid = self.tmeta(tid).get("vik")
        if not (self.tracker and vid):
            return None
        try:
            return self.comment(vid, prov.redact(text, self.cfg["secret_patterns"])).get("id")
        except Exception as exc:  # noqa: BLE001
            print(f"[flow] comentario en el tracker falló ({tid}): {exc}", file=sys.stderr)
            return None

    def where(self, tid: str, comment_id: int | None = None) -> str:
        """Enlace al ticket (y al comentario) en Vikunja para los avisos de Telegram; vacío si la tarea no viene de Vikunja."""
        vid = self.tmeta(tid).get("vik")
        return f"\n🔗 {self.tracker.link(vid, comment_id)}" if (self.tracker and vid) else ""

    def hooks(self, event: str, payload: dict) -> tuple[str, str] | None:
        """Límites enchufables: ejecutables en agents/hooks/<evento>.d/. rc 0 = ok, 2 = escalar, otro = bloquear."""
        d = self.repo / self.cfg.get("hooks_dir", "agents/hooks") / f"{event}.d"
        for h in sorted(d.glob("*")) if d.is_dir() else []:
            if not os.access(h, os.X_OK):
                continue
            r = subprocess.run([str(h)], input=json.dumps(payload), capture_output=True, text=True, timeout=60, cwd=self.repo)
            if r.returncode:
                return ("escalate" if r.returncode == 2 else "block"), f"hook {h.name}: {(r.stdout or r.stderr).strip()[:200]}"
        return None

    def daily_limit_hit(self) -> bool:
        """Tope diario de ejecuciones alcanzado: se avisa UNA vez por día (antes el flujo se quedaba mudo)."""
        # `set-limit` puede ejecutarse desde otra invocación mientras el daemon
        # vive: releer sólo este valor hace efectivo el cambio sin reiniciarlo.
        try:
            self.cfg["daily_run_limit"] = orchestrator.load_json(self.policy_file)["flow"]["daily_run_limit"]
        except (OSError, ValueError, KeyError):
            pass  # se conserva el último límite válido si alguien edita la política de forma incompleta
        count = self.daily_count()
        limit = self.cfg["daily_run_limit"]
        if count >= int(limit * 0.8) and count < limit:
            warn_key = f"dailywarn80:{date.today()}"
            m = self.meta()
            if warn_key not in m["notified"]:
                m["notified"].append(warn_key)
                self.save_meta(m)
                self.notify(f"⚠ Consumo del flujo al 80% ({count}/{limit} ejecuciones hoy). Quedan {limit - count} ejecuciones.")
        if count < limit:
            return False
        key = f"dailylimit:{date.today()}"
        m = self.meta()
        if key not in m["notified"]:
            m["notified"].append(key)
            self.save_meta(m)
            self.notify(f"⏸ El flujo alcanzó el tope diario de {limit} ejecuciones y se pausa hasta mañana. "
                        "Para subirlo: python3 agents/flow.py reset-quota o python3 agents/flow.py set-limit <N>.")
        return True

    def reset_quota(self) -> int:
        """Reinicia el contador diario de ejecuciones y limpia notificaciones de tope."""
        m = self.meta()
        prev = m["daily"].get(str(date.today()), 0)
        m["daily"][str(date.today())] = 0
        m["notified"] = [k for k in m.get("notified", []) if not k.startswith("dailylimit:") and not k.startswith("dailywarn80:")]
        self.save_meta(m)
        return prev

    def set_limit(self, new_limit: int) -> int:
        """Actualiza daily_run_limit en la política de orquestación y en el runtime."""
        if new_limit < 1:
            raise FlowError("el tope diario debe ser un entero mayor que cero")
        policy = json.loads(self.policy_file.read_text(encoding="utf-8"))
        old = policy["flow"].get("daily_run_limit", 30)
        policy["flow"]["daily_run_limit"] = new_limit
        tmp = self.policy_file.with_suffix(self.policy_file.suffix + ".tmp")
        tmp.write_text(json.dumps(policy, indent=2), encoding="utf-8")
        os.replace(tmp, self.policy_file)
        self.cfg["daily_run_limit"] = new_limit
        return old

    def daily_count(self) -> int:
        return self.meta()["daily"].get(str(date.today()), 0)

    def bump_daily(self) -> None:
        m = self.meta()
        m["daily"] = {str(date.today()): m["daily"].get(str(date.today()), 0) + 1}
        self.save_meta(m)

    # ---------- worktrees / git ----------
    def branch_of(self, tid: str) -> str:
        return "agent/" + re.sub(r"[^A-Za-z0-9._-]", "-", tid)

    def ensure_worktree(self, tid: str) -> Path:
        path = self.wt_dir / re.sub(r"[^A-Za-z0-9._-]", "-", tid)
        if not path.exists():
            self.ensure_integration()
            self.wt_dir.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "prune")
            base = self.tmeta(tid).get("base")
            if base and self.git("branch", "--list", self.branch_of(tid)).strip():
                # la rama ya tiene el trabajo del ticket (el worktree se podó para liberar disco): se reengancha, nunca se resetea
                self.git("worktree", "add", str(path), self.branch_of(tid))
            else:
                base = self.git("rev-parse", self.integ).strip()
                self.git("worktree", "add", "-B", self.branch_of(tid), str(path), base)
            self.set_tmeta(tid, base=base, worktree=str(path), branch=self.branch_of(tid))
        return path

    def ensure_integration(self) -> Path:
        """Rama de integración (dev) con su propio worktree: los merges no tocan el checkout de la persona."""
        if not self.git("branch", "--list", self.integ).strip():
            self.git("branch", self.integ, self.main)
        path = self.wt_dir / "_integration"
        if not path.exists():
            self.wt_dir.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", str(path), self.integ)
        return path

    def drop_worktree(self, path: Path) -> None:
        self.git("worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def prune_stale_worktrees(self) -> list[str]:
        """Limpia worktrees de tareas finalizadas o inactivas para ahorrar espacio y evitar basura."""
        pruned = []
        if not self.wt_dir.is_dir():
            return pruned
        tasks = self.tasks().get("tasks", {})
        for entry in self.wt_dir.iterdir():
            if not entry.is_dir() or entry.name == "_integration":
                continue
            tid = entry.name
            t = tasks.get(tid)
            if t and t.get("state") in ("done", "rejected"):
                self.drop_worktree(entry)
                pruned.append(tid)
        return pruned

    MIN_FREE_GB = 5.0

    def prune_idle_worktrees(self, min_free_gb: float | None = None) -> list[str]:
        """Con poco disco, poda los worktrees de tickets que no se están ejecutando (cada checkout pesa ~0,5 GB).
        Es seguro: su trabajo vive en la rama agent/<tarea> y ensure_worktree la reengancha sin resetearla."""
        limit = self.MIN_FREE_GB if min_free_gb is None else min_free_gb
        if not self.wt_dir.is_dir() or shutil.disk_usage(self.wt_dir).free / 1e9 >= limit:
            return []
        tasks, meta, pruned = self.tasks().get("tasks", {}), self.meta()["tasks"], []
        for entry in sorted(self.wt_dir.iterdir()):
            t = tasks.get(entry.name)
            if (not entry.is_dir() or entry.name == "_integration" or not t or t.get("state") == "executing"
                    or not meta.get(entry.name, {}).get("base")
                    or not self.git("branch", "--list", self.branch_of(entry.name)).strip()
                    or self.git("status", "--porcelain", cwd=entry, check=False).strip()):
                continue
            self.drop_worktree(entry)
            pruned.append(entry.name)
        if pruned:
            print(f"[flow] poco disco: podé {len(pruned)} worktrees inactivos ({', '.join(pruned)})", file=sys.stderr)
        return pruned

    def recover_failed_merge(self, wt: Path) -> list[str]:
        """Devuelve los archivos unmerged y deja el worktree listo para el próximo ticket.

        La lista se toma antes de abortar: después del abort/reset Git ya no
        conserva los índices ``U`` que permiten explicar el conflicto.
        """
        conflicts = [p for p in self.git("diff", "--name-only", "--diff-filter=U", cwd=wt, check=False).splitlines() if p]
        self.git("merge", "--abort", cwd=wt, check=False)
        # Aunque merge --abort alcance, estas dos órdenes son intencionales:
        # cubren aborts parciales y archivos no seguidos dejados por el merge.
        self.git("reset", "--hard", "HEAD", cwd=wt, check=False)
        self.git("clean", "-fd", cwd=wt, check=False)
        return conflicts

    @staticmethod
    def merge_failure(prefix: str, output: str, conflicts: list[str]) -> str:
        detail = ", ".join(conflicts) if conflicts else "(Git no marcó archivos unmerged)"
        return f"{prefix}. Archivos en conflicto: {detail}. Salida de Git: {output.strip()[-200:]}"

    def commit_leftovers(self, wt: Path, task: dict, run: dict, provider: str) -> None:
        if not self.git("status", "--porcelain", cwd=wt).strip():
            return
        self.git("add", "-A", "--", ".", ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude).evidencia", cwd=wt)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=wt).returncode == 0:
            return   # sólo había basura ignorada (pycache, etc.)
        trailer = self.provs.table[provider].get("trailer", provider)
        msg = (f"agent({task['id']}): {task['title'][:60]}\n\nTask: {task['id']}\nAgent-Provider: {provider}\n"
               f"Agent-Role: implementer\nAgent-Run-Id: {run}\nCo-Authored-By: {trailer}\n")
        self.git("-c", f"user.name=agent-{provider}", "-c", f"user.email={self.git_email}", "commit", "-q", "-m", msg, cwd=wt)

    # ---------- evidencia ----------
    def bundle(self, tid: str, run_id: str) -> Path:
        d = self.runs_dir / tid / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write(self, d: Path, name: str, text: str) -> str:
        (d / name).write_text(text, encoding="utf-8")
        return sha(text)

    def seal(self, d: Path, run_id: str, meta: dict) -> None:
        """Registra el bundle en la cadena de auditoría con el hash de su meta.json."""
        h = self.write(d, "meta.json", json.dumps(meta, ensure_ascii=False, indent=1, sort_keys=True))
        self.orch("add-evidence", run_id, "--kind", "bundle", "--value", f"{d} {h}")

    # ---------- guardas ----------
    def guards(self, task: dict, files: list[str], diff: str, contract: dict) -> list[tuple[str, str]]:
        out, c = [], self.cfg
        if len(files) > c["max_diff_files"] or diff.count("\n") > c["max_diff_lines"]:
            out.append(("escalate", f"cambio demasiado grande ({len(files)} archivos, {diff.count(chr(10))} líneas)"))
        added = "\n".join(l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        if any(re.search(p, added) for p in c["secret_patterns"]):
            self.set_tmeta(task["id"], no_approve=True)
            out.append(("escalate", "posible secreto en el diff (no se puede aprobar tal cual)"))
        scope = [f"{task['component'].rstrip('/')}/**", *c.get("always_allowed_paths", []), *contract.get("scope", [])]
        outside = [f for f in files if not any_match(f, scope)]
        if outside:
            out.append(("escalate", "fuera de alcance: " + ", ".join(outside[:5])))
        protected = [f for f in files if any_match(f, c["protected_paths"])]
        if protected:
            val = self.jev.validate_escalation(task, "guard.protected_paths", {"files": protected, "diff": diff})
            if val and val.get("safe_to_proceed", 0.0) >= 0.80 and val.get("kind") == "benign_change":
                out.append(("info", "paths protegidos validados como benignos por JEV: " + ", ".join(protected[:3])))
            else:
                desc = f"rutas protegidas ({', '.join(protected[:3])})"
                acts = list(dict.fromkeys([*task["protected_actions"], desc]))
                self.orch("mark", task["id"], "--event", "guard.protected_paths", "--field", "protected_actions",
                          "--value", json.dumps(acts), "--actor", "flow")
                out.append(("info", "toca paths protegidos: " + ", ".join(protected[:5])))
        return out

    @staticmethod
    def normalize_test_cmd(cmd: str) -> str:
        """Normaliza comandos de unittest que apuntan directo a un archivo dentro de directorios con puntos
        (ej. tickets/2026.09.20_foo/test_bar.py), evitando el fallo de Python al intentar importarlo como módulo con puntos."""
        m = re.match(r"^python3\s+-m\s+unittest\s+([A-Za-z0-9_./-]+/test_[A-Za-z0-9_.-]+\.py)$", cmd.strip())
        if m:
            path_str = m.group(1)
            p = Path(path_str)
            if any("." in part for part in p.parent.parts):
                return f"python3 -m unittest discover -s {p.parent.as_posix()} -p {p.name}"
        return cmd

    def run_tests(self, wt: Path, cmd: str, wt_type: str = "code") -> tuple[bool, str]:
        if not cmd:
            return (True, "no aplica (tarea de investigación sin comando de tests)") if wt_type == "research" else (False, "la tarea no declara comando de tests")
        cmd = self.normalize_test_cmd(cmd)
        if not any(cmd.startswith(p) for p in self.cfg["test_cmd_allow_prefixes"]):
            return False, f"comando de tests no permitido por política: {cmd[:60]}"
        try:
            r = subprocess.run(shlex.split(cmd), cwd=wt, capture_output=True, text=True, timeout=self.cfg["test_timeout_s"])
            return r.returncode == 0, prov.redact((r.stdout + r.stderr)[-6000:], self.cfg["secret_patterns"])
        except subprocess.TimeoutExpired:
            return False, "timeout de tests"

    # ---------- pasos ----------
    def implement(self, t: dict) -> str:
        tid, m = t["id"], self.tmeta(t["id"])
        contract = m.get("contract", {})
        if self.daily_limit_hit():
            return "wait"
        state = self.tasks()
        role = teammod.ROLE_FOR[t["type"]]
        pick = self.team.pick("work", t["type"], t["risk"], self.provs, set(), learn.levels(state, self.team), learn.scores(state, t["type"]),
                              prefer=contract.get("prefer"), mode="implement", explore=self.cfg.get("explore", 0.05))
        if not pick:
            return self.waiting(t, self.team.why_none("work", t["type"], t["risk"], learn.levels(state, self.team)))
        agent, provider = pick
        wt = self.ensure_worktree(tid)
        m = self.tmeta(tid)
        blocked = self.hooks("pre_run", {"task": tid, "provider": provider, "agent": agent, "role": role})
        if blocked:
            return self.stop(t, blocked)
        branch = self.branch_of(tid)
        run_id = self.orch("start-run", tid, "--provider", provider, "--agent", agent, "--role", role,
                           "--session-ref", uuid.uuid4().hex[:12], "--branch", branch, "--worktree", str(wt))
        self.bump_daily()
        self.safe(self.sync_boards)   # la tarjeta pasa a «Trabajando» mientras corre el agente
        feedback = "\n\n".join(x for x in (self.feedback(t, m), self.stage_evidence(wt, t, m), learn.lessons(state, t["component"], t["type"])) if x)
        prompt = self.render("implementer", provider=provider, agent=agent, persona=self.team.agents[agent]["persona"], role=role, task_id=tid, title=t["title"], component=t["component"],
                             type=t["type"], risk=t["risk"], branch=branch, description=m.get("description", ""),
                             scope=", ".join([f"{t['component']}/**", *contract.get("scope", [])]),
                             test_cmd=contract.get("test", "(sin comando declarado)"), feedback=feedback)
        d = self.bundle(tid, run_id)
        meta = {"role": role, "agent": agent, "provider": provider, "task": tid, "started": time.strftime("%FT%T"),
                "prompt": self.write(d, "prompt.md", prompt), "quota": self.provs.quota_advice(provider)}
        res = self.provs.run(provider, "implement", prompt, wt, contract.get("timeout", self.cfg["run_timeout_s"]), self.cfg["secret_patterns"])
        meta.update(rc=res.rc, seconds=round(res.seconds), timed_out=res.timed_out, output=self.write(d, "output.txt", res.output),
                    stderr=self.write(d, "stderr.txt", res.stderr))
        if res.blocked:
            until = self.provs.cooldown(provider, self.cfg["quota_cooldown_h"], res.output + "\n" + res.stderr)
            meta["result"] = "blocked_quota"
            self.seal(d, run_id, meta)
            self.orch("fail-run", run_id, "--reason", f"{agent} ({provider}) sin cuota; pausado hasta {time.strftime('%H:%M', time.localtime(until))}", "--no-count")
            return "done"
        self.commit_leftovers(wt, t, run_id, provider)
        base = m["base"]
        files = [f for f in self.git("diff", "--name-only", f"{base}..HEAD", cwd=wt).splitlines() if f]
        raw_diff = self.git("diff", f"{base}..HEAD", cwd=wt)   # las guardas miran el diff crudo; lo guardado va redactado
        diff = prov.redact(raw_diff, self.cfg["secret_patterns"])
        meta.update(files=files, diff=self.write(d, "diff.patch", diff), head=self.git("rev-parse", "HEAD", cwd=wt).strip())
        marks = [m_.start() for m_ in re.finditer(r"^ESCALAR:", res.output, re.M)]
        if marks:   # se conserva TODO el bloque (comandos, rutas): la persona necesita verlo entero, no sólo la primera línea
            block = res.output[marks[-1] + len("ESCALAR:"):].strip()
            return self.finish_fail(t, d, run_id, meta, "escalate", "el agente pidió una persona:\n\n" + block)
        if res.timed_out or (res.rc != 0 and not files):
            return self.finish_fail(t, d, run_id, meta, "fail", "timeout" if res.timed_out else f"el proveedor terminó con rc={res.rc} sin cambios")
        if not files and t["type"] in {"code", "data"}:
            return self.finish_fail(t, d, run_id, meta, "fail", "el agente no produjo cambios")
        for action, why in self.guards(t, files, raw_diff, contract):
            if action == "escalate":
                return self.finish_fail(t, d, run_id, meta, "escalate", why)
        screen = self.jev.screen({"title": t["title"], "component": t["component"], "type": t["type"], "risk": t["risk"]}, files, raw_diff)
        if screen:
            meta["jev_screen"] = screen
            if screen["dangerous"] >= 0.6:
                return self.finish_fail(t, d, run_id, meta, "escalate", f"JEV marcó operaciones peligrosas en el diff (p={screen['dangerous']:.2f})")
            if screen["weakens_tests"] >= 0.6:
                return self.finish_fail(t, d, run_id, meta, "fail", f"JEV detectó tests debilitados o saltados (p={screen['weakens_tests']:.2f}); restaurá los tests")
        hook = self.hooks("post_run", {"task": tid, "provider": provider, "agent": agent, "files": files, "risk": t["risk"]})
        if hook:
            return self.finish_fail(t, d, run_id, meta, "escalate" if hook[0] == "escalate" else "fail", hook[1])
        ok, log = self.run_tests(wt, contract.get("test", ""), t["type"])
        meta["tests"] = self.write(d, "tests.log", log)
        if not ok:
            self.set_tmeta(tid, feedback="Los tests del controlador FALLAN. Salida:\n" + log[-2500:])
            return self.finish_fail(t, d, run_id, meta, "fail", "tests fallan")
        self.set_tmeta(tid, feedback="")
        if files:
            self.orch("add-evidence", run_id, "--kind", "commit", "--value", meta["head"])
            self.orch("add-evidence", run_id, "--kind", "rollback", "--value", f"git revert -m1 <merge de {branch}> o descartar la rama")
        self.orch("add-evidence", run_id, "--kind", "tests", "--value", f"ok; {meta['tests']}")
        self.orch("add-evidence", run_id, "--kind", "report", "--value", meta["output"])
        summary = clean(res.output)[-700:] or "(sin resumen)"
        self.orch("add-evidence", run_id, "--kind", "summary", "--value", summary[:300])
        meta["result"] = "submitted"
        self.seal(d, run_id, meta)
        self.orch("submit-run", run_id, "--summary", summary)
        comp = t.get("component", "agents")
        doc_link = vikunja_adapter.component_doc_link(self.repo, comp)
        sum_words = summary.split()[:20]
        sum_short = " ".join(sum_words) + ("…" if len(summary.split()) > 20 else "")
        self.say(tid, f"**{agent}** ({provider}) implementó (run `{run_id}`, {len(files)} archivos, tests OK). Pasa a revisión cruzada.\n\n{sum_short}\n\n{doc_link}")
        return "done"

    def finish_fail(self, t: dict, d: Path, run_id: str, meta: dict, kind: str, why: str) -> str:
        meta["result"] = f"{kind}: {why}"
        self.seal(d, run_id, meta)
        if kind == "escalate":
            self.orch("escalate", t["id"], "--reason", why, "--actor", "flow")
        else:
            self.orch("fail-run", run_id, "--reason", why)
        why_words = why.split()[:22]
        why_short = " ".join(why_words) + ("…" if len(why.split()) > 22 else "")
        doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
        self.say(t["id"], f"Ejecución `{run_id}` ({meta.get('agent', meta['provider'])}): {why_short}\n\n{doc_link}")
        return "done"

    def stop(self, t: dict, hook: tuple[str, str]) -> str:
        if hook[0] == "escalate":
            self.orch("escalate", t["id"], "--reason", hook[1], "--actor", "flow")
            return "done"
        return self.waiting(t, hook[1])

    def waiting(self, t: dict, why: str) -> str:
        key = f"wait:{t['id']}:{why}"
        m = self.meta()
        if key not in m["notified"]:
            m["notified"].append(key)
            self.save_meta(m)
            doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
            self.say(t["id"], f"En espera: {why}.\n\n{doc_link}")
        return "wait"

    def stage_evidence(self, wt: Path, t: dict, m: dict) -> str:
        """Los agentes corren en un sandbox que no ve el resto del disco: los archivos de .runtime/ que el ticket, la guía o
        los comentarios mencionan por ruta absoluta se copian a <worktree>/.evidencia/ (no se commitean)."""
        text = " ".join([t.get("guidance", ""), m.get("description", ""), m.get("feedback", ""), *[n["text"] for n in m.get("notes", [])], t.get("human_reason", "")])
        found = re.findall(re.escape(str(self.repo)) + r"/\.runtime/[^\s`'\"<>)\\]+", text.replace("\\_", "_"))
        copied = []
        for src in dict.fromkeys(found):
            p = Path(src.rstrip(".,;"))
            if p.is_file() and p.stat().st_size <= 300_000 and "worktrees" not in p.parts:
                (wt / ".evidencia").mkdir(exist_ok=True)
                (wt / ".evidencia" / p.name).write_bytes(p.read_bytes())
                copied.append(p.name)
        return ("## Evidencia disponible\nCopiada para vos a `./.evidencia/` (no la commitees): " + ", ".join(f"`{n}`" for n in copied)) if copied else ""

    def feedback(self, t: dict, m: dict) -> str:
        parts = []
        if t.get("guidance"):
            parts.append("## Guía de la persona\n" + t["guidance"])
        if m.get("feedback"):
            parts.append("## Problema de la ronda anterior\n" + m["feedback"])
        if m.get("notes"):
            parts.append("## Comentarios de la persona en el ticket (tenelos en cuenta)\n" + "\n".join(f"- {n['text']}" for n in m["notes"][-6:]))
        state = self.tasks()
        for rid in reversed(t["run_ids"]):
            run = state["runs"][rid]
            if run["role"] == "reviewer" and run.get("review", {}).get("verdict") == "changes":
                parts.append("## Cambios pedidos por el revisor ({})\n".format(run.get("agent", run["provider"])) + "\n".join("- " + f for f in run["review"]["findings"]))
                break
        try:
            rel = session_registry.find_related(f"{t.get('title', '')} {m.get('description', '')}", component_hint=t.get("component", ""), repo_root=self.repo)
            if rel.get("recent"):
                docs_lines = [f"- **{d['title']}** (`{d['path']}`): {d['summary']}" for d in rel["recent"][:3] if d.get("summary")]
                if docs_lines:
                    parts.append("## Sesiones recientes relacionadas con este tema (contexto previo)\n" + "\n".join(docs_lines))
        except Exception:
            pass
        return "\n\n".join(parts)

    def render(self, name: str, **kv: str) -> str:
        text = (self.prompts / f"{name}.md").read_text(encoding="utf-8")
        for k, v in kv.items():
            text = text.replace("{{" + k + "}}", str(v))
        return text

    def review(self, t: dict) -> str:
        tid, m = t["id"], self.tmeta(t["id"])
        state = self.tasks()
        implementers = {r["provider"] for r in (state["runs"][i] for i in t["run_ids"]) if r["role"] != "reviewer"}
        exclude = implementers | set(t["approvals"]) if self.cfg["cross_provider_review"] else set(t["approvals"])
        pick = self.team.pick("review", t["type"], t["risk"], self.provs, exclude, learn.levels(state, self.team), {}, mode="review")
        if not pick:
            return self.waiting(t, "no hay un revisor independiente disponible: " + self.team.why_none("review", t["type"], t["risk"], learn.levels(state, self.team)))
        agent, provider = pick
        if self.daily_limit_hit():
            return "wait"
        last = next(state["runs"][i] for i in reversed(t["run_ids"]) if state["runs"][i]["role"] != "reviewer" and state["runs"][i]["state"] == "submitted")
        bdir = self.runs_dir / tid / last["id"]
        files = json.loads((bdir / "meta.json").read_text())["files"]
        diff = (bdir / "diff.patch").read_text()
        rev_path = self.wt_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '-', tid)}-rev-{uuid.uuid4().hex[:6]}"
        self.git("worktree", "add", "--detach", str(rev_path), self.branch_of(tid))
        try:
            head = self.git("rev-parse", "HEAD", cwd=rev_path).strip()
            run_id = self.orch("start-run", tid, "--provider", provider, "--agent", agent, "--role", "reviewer",
                               "--session-ref", uuid.uuid4().hex[:12], "--branch", self.branch_of(tid), "--worktree", str(rev_path))
            self.bump_daily()
            self.safe(self.sync_boards)
            prompt = self.render("reviewer", provider=provider, agent=agent, persona=self.team.agents[agent]["persona"],
                                 implementer=", ".join(sorted(f"{r.get('agent', r['provider'])} ({r['provider']})" for r in (state["runs"][i] for i in t["run_ids"]) if r["role"] != "reviewer")), task_id=tid,
                                 title=t["title"], component=t["component"], type=t["type"], risk=t["risk"], branch=self.branch_of(tid),
                                 description=m.get("description", "") + ("\n\n## Comentarios de la persona\n" + "\n".join(f"- {n['text']}" for n in m["notes"][-6:]) if m.get("notes") else ""), summary=last.get("summary", ""),
                                 tests=(bdir / "tests.log").read_text()[-600:] if (bdir / "tests.log").exists() else "ok",
                                 files="\n".join(f"- {f}" for f in files), diff=diff[:60000])
            d = self.bundle(tid, run_id)
            meta = {"role": "reviewer", "agent": agent, "provider": provider, "task": tid, "reviewing": last["id"], "prompt": self.write(d, "prompt.md", prompt)}
            res = self.provs.run(provider, "review", prompt, rev_path, self.cfg["run_timeout_s"], self.cfg["secret_patterns"])
            meta.update(rc=res.rc, seconds=round(res.seconds), output=self.write(d, "output.txt", res.output),
                        stderr=self.write(d, "stderr.txt", res.stderr))
            tampered = bool(self.git("status", "--porcelain", cwd=rev_path).strip()) or self.git("rev-parse", "HEAD", cwd=rev_path).strip() != head
            if res.blocked:
                self.provs.cooldown(provider, self.cfg["quota_cooldown_h"], res.output + "\n" + res.stderr)
                meta["result"] = "blocked_quota"
                self.seal(d, run_id, meta)
                self.orch("fail-run", run_id, "--reason", f"{provider} sin cuota", "--no-count")
                return "done"
            verdict = None if (tampered or res.timed_out) else self.parse_verdict(res.output)
            if not verdict and not (tampered or res.timed_out) and res.output.strip():
                verdict = self.verdict_from_text(res.output, meta)   # JEV rescata reseñas sin JSON válido
            if tampered:
                meta["result"] = "revisor modificó el checkout"
                self.seal(d, run_id, meta)
                self.orch("fail-run", run_id, "--reason", "el revisor modificó archivos (revisión descartada)")
                self.orch("escalate", tid, "--reason", f"{agent} ({provider}) modificó archivos durante una revisión de solo lectura", "--actor", "flow")
                return "done"
            if not verdict:
                meta["result"] = "sin veredicto válido"
                self.seal(d, run_id, meta)
                self.orch("fail-run", run_id, "--reason", "el revisor no devolvió un veredicto JSON válido")
                return "done"
            self.write(d, "review.json", json.dumps(verdict, ensure_ascii=False, indent=1))
            meta["verdict"] = verdict["verdict"]
            meta["result"] = "reviewed"
            self.seal(d, run_id, meta)
            ffile = d / "findings.json"
            ffile.write_text(json.dumps(verdict["findings"]))
            self.orch("submit-review", run_id, "--verdict", verdict["verdict"], "--summary", verdict["summary"], "--findings-file", str(ffile))
            sum_words = verdict['summary'].split()[:15]
            sum_short = " ".join(sum_words) + ("…" if len(verdict['summary'].split()) > 15 else "")
            findings = [f"- {f}" for f in verdict.get("findings", [])[:2]]
            findings_part = ("\n\n" + "\n".join(findings)) if findings else ""
            doc_link = f"[Revisión]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
            self.say(tid, f"**{agent}** ({provider}) revisó: **{verdict['verdict']}** — {sum_short}{findings_part}\n\n{doc_link}")
            return "done"
        finally:
            self.drop_worktree(rev_path)

    def verdict_from_text(self, text: str, meta: dict) -> dict | None:
        """Sin JSON válido: JEV clasifica el texto libre. Sólo se acepta con confianza alta; si no, la revisión falla y se repite."""
        got = self.jev.verdict(text)
        if not got or got[1] < 0.8:
            return None
        meta["verdict_via_jev"] = {"verdict": got[0], "confidence": got[1]}
        return {"verdict": got[0], "summary": f"(veredicto inferido por JEV, confianza {got[1]:.2f}) " + clean(text)[-200:], "findings": []}

    @staticmethod
    def parse_verdict(text: str) -> dict | None:
        def normalize(value: object) -> dict | None:
            if not isinstance(value, dict) or value.get("verdict") not in {"approve", "changes", "escalate"}:
                return None
            value = dict(value)
            if value.get("critical") and value["verdict"] == "approve":
                value["verdict"] = "escalate"
            value["summary"] = str(value.get("summary", "")) or value["verdict"]
            value["findings"] = [str(f) for f in value.get("findings", [])]
            return value

        def from_text(value: str) -> dict | None:
            """Acepta JSON directo y el Markdown que suele devolver un agente."""
            try:
                got = normalize(json.loads(value))
            except ValueError:
                got = None
            if got:
                return got
            blocks = re.findall(r"```json\s*(\{.*?\})\s*```", value, re.S)
            blocks += re.findall(r"(\{[^{}]*\"verdict\"[^{}]*\})", value, re.S)
            for raw in reversed(blocks):
                try:
                    got = normalize(json.loads(raw))
                except ValueError:
                    continue
                if got:
                    return got
            return None

        # `agy --output-format json` envuelve el texto final en `response`.
        # También acepta `structured_output` y el evento terminal stream-json.
        for line in reversed(text.splitlines()):
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            candidates = [envelope]
            if isinstance(envelope, dict):
                candidates += [envelope.get("structured_output"), envelope.get("response")]
                if isinstance(envelope.get("result"), dict):
                    result = envelope["result"]
                    candidates += [result, result.get("structured_output"), result.get("response")]
            for candidate in candidates:
                if isinstance(candidate, str):
                    got = from_text(candidate)
                else:
                    got = normalize(candidate)
                if got:
                    return got
        return from_text(text)

    def finalize(self, t: dict) -> str:
        tid, m = t["id"], self.tmeta(t["id"])
        human_ok = t["state"] == "approved"
        if not human_ok and (t["risk"] not in self.cfg["auto_merge_risks"] or m.get("contract", {}).get("no-merge")):
            return self.waiting(t, "validada; el merge automático no está permitido para esta tarea")
        state = self.tasks()
        impl = [state["runs"][i] for i in t["run_ids"] if state["runs"][i]["role"] != "reviewer" and state["runs"][i]["state"] == "submitted"]
        head_main = self.git("rev-parse", self.main).strip() + self.git("rev-parse", self.integ).strip()
        files = json.loads((self.runs_dir / tid / impl[-1]["id"] / "meta.json").read_text())["files"] if impl else []
        if files:
            if m.get("merge_tried") == head_main:
                return "wait"
            hook = self.hooks("pre_merge", {"task": tid, "files": files, "risk": t["risk"], "human_ok": human_ok})
            if hook:
                return self.stop(t, hook)
            err = self.merge(t, m, files, state, impl)
            if err:
                self.set_tmeta(tid, merge_tried=head_main)
                self.orch("escalate", tid, "--reason", f"falló la integración en {self.integ}: {err}", "--actor", "flow")
                wt_integ = self.wt_dir / "_integration"
                manual_branch = self.main if err.startswith("dev no se pudo sincronizar") else self.branch_of(tid)
                self.say(tid, (
                    f"🚨 Falló la integración automática en `{self.integ}`:\n\n{err}\n\n"
                    f"Para resolver manualmente en el worktree de integración:\n"
                    f"`cd {wt_integ} && git merge {manual_branch}`\n"
                    f"Luego: `python3 agents/flow.py tick`."
                ))
                return "done"
            merged = self.git("rev-parse", self.integ).strip()
            self.orch("mark", tid, "--event", "task.merged", "--actor", "flow", "--value", json.dumps(merged))
        self.orch("complete", tid, "--actor", "flow")
        comp = t.get("component", "agents")
        doc_link = vikunja_adapter.component_doc_link(self.repo, comp)
        cost_note = budget.format_task_cost_comment(tid)
        self.say(tid, "✅ Completada" + (f" e integrada localmente en `{self.integ}`." if files else ".") + f"\n\n{cost_note}\n\n{doc_link}")
        if self.tracker and m.get("vik"):
            try:
                self.tracker.close(m["vik"])
            except Exception as exc:  # noqa: BLE001
                print(f"[flow] no pude cerrar el ticket: {exc}", file=sys.stderr)
        if m.get("worktree"):
            self.drop_worktree(Path(m["worktree"]))
        self.sample(t, impl)
        return "done"

    def merge(self, t: dict, m: dict, files: list[str], state: dict, impl: list[dict]) -> str:
        """Integra la rama del ticket en dev, en un worktree propio (nunca toca el checkout de la persona)."""
        wt = self.ensure_integration()
        if subprocess.run(["git", "merge-base", "--is-ancestor", self.main, self.integ], cwd=wt).returncode:
            sync = subprocess.run(["git", "-c", "user.name=agent-flow", "-c", f"user.email={self.git_email}", "merge", "--no-edit", self.main],
                                  cwd=wt, capture_output=True, text=True)   # main avanzó (loop.sh, la persona): traerlo a dev antes de integrar
            if sync.returncode:
                conflicts = self.recover_failed_merge(wt)
                return self.merge_failure(
                    f"dev no se pudo sincronizar con {self.main}", sync.stdout + sync.stderr, conflicts
                )
        trailers = [f"Task: {t['id']}"]
        for rid in t["run_ids"]:
            r = state["runs"][rid]
            if r["state"] == "submitted":
                trailers.append(f"{'Reviewed' if r['role'] == 'reviewer' else 'Implemented'}-By: {r.get('agent', r['provider'])}/{r['provider']} ({rid})")
        msg = f"merge(agents): {t['id']} {t['title'][:60]}\n\n" + "\n".join(trailers) + "\n"
        r = subprocess.run(["git", "-c", "user.name=agent-flow", "-c", f"user.email={self.git_email}", "merge", "--no-ff", "-m", msg, self.branch_of(t["id"])],
                           cwd=wt, capture_output=True, text=True)
        if r.returncode:
            conflicts = self.recover_failed_merge(wt)
            return self.merge_failure(
                f"no se pudo integrar {self.branch_of(t['id'])}", r.stdout + r.stderr, conflicts
            )
        return ""

    def release(self, approve: bool, human: str = "Gustavo") -> str:
        """dev -> main. Sin --approve sólo arma el resumen; con --approve (decisión humana) mergea y etiqueta."""
        self.ensure_integration()
        commits = self.git("log", "--no-merges", "--format=%h %s", f"{self.main}..{self.integ}").strip()
        merges = [l for l in self.git("log", "--merges", "--format=%s", f"{self.main}..{self.integ}").splitlines() if l.startswith("merge(agents):")]
        state = self.tasks()
        pending = [t["id"] for t in state["tasks"].values() if t.get("audit_sample") and not t.get("audit_reviewed")]
        summary = f"dev tiene {len(merges)} ticket(s) sin release:\n" + "\n".join("  " + l for l in merges) + (f"\n\nMuestras de auditoría sin revisar: {', '.join(pending)}" if pending else "")
        if not merges and not commits:
            return "nada para publicar: dev y main están iguales"
        if not approve:
            return summary + "\n\nPara publicar en main: python3 agents/flow.py release --approve"
        if self.git("rev-parse", "--abbrev-ref", "HEAD").strip() != self.main:
            raise FlowError(f"el checkout principal no está en {self.main}")
        changed = set(self.git("diff", "--name-only", f"{self.main}...{self.integ}").split())
        dirty = {l[3:].split(" -> ")[-1].strip('"') for l in self.git("status", "--porcelain").splitlines()}
        if changed & dirty:
            raise FlowError("el checkout principal tiene cambios sin commitear que chocan con el release: " + ", ".join(sorted(changed & dirty)[:5]))
        tag = "release-" + time.strftime("%Y%m%d-%H%M")
        r = subprocess.run(["git", "-c", "user.name=agent-flow", "-c", f"user.email={self.git_email}", "merge", "--no-ff", "-m", f"release: {tag}\n\n{summary}\n\nApproved-By: {human}", self.integ], cwd=self.repo, capture_output=True, text=True)
        if r.returncode:
            self.git("merge", "--abort", check=False)
            raise FlowError("el release no se pudo mergear: " + (r.stdout + r.stderr).strip()[-200:])
        r_tag = subprocess.run(["git", "-c", "user.name=agent-flow", "-c", f"user.email={self.git_email}",
                                "tag", "-a", tag, "-m", f"release: {tag}\n\n{summary}\n\nApproved-By: {human}"],
                               cwd=self.repo, capture_output=True, text=True)
        if r_tag.returncode:
            raise FlowError(f"no se pudo crear el tag {tag}: " + r_tag.stderr.strip()[:200])
        return f"publicado como {tag}\n{summary}"

    def sample(self, t: dict, impl: list[dict]) -> None:
        provider = impl[-1].get("agent", impl[-1]["provider"]) if impl else "?"
        state = self.tasks()
        done_before = sum(1 for x in state["tasks"].values() if x["state"] == "done" and x["id"] != t["id"]
                          and any(state["runs"][i].get("agent") == provider and state["runs"][i]["role"] != "reviewer" for i in x["run_ids"]))
        if done_before < self.cfg["audit_first_n_per_provider"] or random.random() < self.cfg["audit_sample_rate"]:
            self.orch("mark", t["id"], "--event", "audit.sample", "--field", "audit_sample", "--value", "true", "--actor", "flow")
            self.notify(f"🔎 Muestra para auditar: {t['id']} ({provider}) — python3 agents/audit.py show {t['id']}{self.where(t['id'])}")

    # ---------- humano ----------
    def notify_humans(self) -> None:
        m = self.meta()
        for t in self.tasks()["tasks"].values():
            key = f"human:{t['id']}:{t['human_reason']}:{t['rounds']}:{len(t['run_ids'])}"
            if t["state"] != "human_review" or key in m["notified"]:
                continue
            if t.get("human_reason", "").startswith("falló la integración"):
                # Aviso de error técnico ya registrado en el ticket: no pedir decisión /aprobar
                continue
            val = self.jev.validate_escalation(t, "human_review_notice", {"error": t.get("human_reason", "")})
            if val and val.get("kind") == "technical_fault" and val.get("requires_human", 0.0) < 0.35:
                # Falla técnica clasificada por JEV: no pedir decisión /aprobar
                continue
            m["notified"].append(key)
            self.save_meta(m)
            how = ("Respondé en el ticket: `/aprobar`, `/rechazar motivo` o `/reintentar guía`"
                   if m["tasks"].get(t["id"], {}).get("vik") else
                   f"python3 agents/flow.py decide {t['id']} approve|reject|retry \"nota\"")
            reason_words = t["human_reason"].split()
            reason_short = " ".join(reason_words[:22]) + ("…" if len(reason_words) > 22 else "")
            doc_link = f"[Guía de decisiones]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
            cid = self.say(t["id"], f"🙋 **Necesito una decisión tuya**\n\n{reason_short}\n\n---\n{how}.\n\n{doc_link}")
            self.notify(f"🙋 {t['id']} necesita tu decisión: {t['title'][:80]}\n\n{t['human_reason']}\n\n{how}{self.where(t['id'], cid)}\nEvidencia: .runtime/agent-runs/{t['id']}")

    def decide(self, tid: str, decision: str, note: str, human: str) -> None:
        if decision == "approve" and self.tmeta(tid).get("no_approve"):
            raise FlowError("esta tarea no se puede aprobar tal cual (posible secreto): rechazá o reintentá")
        self.orch("decide", tid, "--human", human, "--note", note or decision, f"--{decision}")
        note_part = (" — " + " ".join(note.split()[:15]) + ("…" if len(note.split()) > 15 else "")) if note else ""
        doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
        self.say(tid, f"Decisión de **{human}**: {decision}{note_part}.\n\n{doc_link}")
        if decision == "reject" and self.tracker:
            vid = self.tmeta(tid).get("vik")
            if vid:
                try:
                    self.tracker.close(vid)
                except Exception:
                    pass

    QUESTION = re.compile(r"@bot|\?|^\s*(dame|dime|decime|cómo|como|qué|que|por qué|explic|mostr|mostrá|cuál|cual|pasame)\b", re.I)

    def ingest_comments(self) -> None:
        """Lee los comentarios de la persona en cada ticket del flujo:
        `/aprobar` `/rechazar` `/reintentar` `/reabrir` -> decisiones · preguntas (@bot, «?») -> las responde un agente ·
        cualquier otro comentario -> nota que llega al prompt de los agentes en su próxima ejecución."""
        if not self.tracker:
            return
        humans = self.tracker.humans
        for t in self.tasks()["tasks"].values():
            info = self.meta()["tasks"].get(t["id"], {})
            vid = info.get("vik")
            if not vid:
                continue
            seen = self.meta()["seen"].get(str(vid), 0)
            new = [c for c in self.tracker.comments(vid) if c["id"] > seen and c["author"].lower() in humans]
            for c in sorted(new, key=lambda x: x["id"]):
                m = self.meta()
                m["seen"][str(vid)] = c["id"]
                self.save_meta(m)
                text = strip_quotes(clean(c["text"]))   # «Responder» en el tracker antepone la cita (> ...): el comando va después
                cmd = re.match(r"\s*/(aprobar|rechazar|reintentar|reabrir)\b\s*(.*)", text, re.S | re.I)
                who = c["author"]
                if cmd:
                    word, note = cmd.group(1).lower(), cmd.group(2).strip()
                    try:
                        if word == "reabrir":
                            self.orch("reopen", t["id"], "--human", who, "--note", note or "reabierta")
                            guide_short = (" Guía: " + " ".join(note.split()[:15]) + ("…" if len(note.split()) > 15 else "")) if note else ""
                            doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
                            self.say(t["id"], f"Reabierta por **{who}**.{guide_short}\n\n{doc_link}")
                        else:
                            if word == "rechazar" and t["state"] not in ("human_review", "done", "rejected"):
                                self.orch("escalate", t["id"], "--reason", "cancelada por la persona", "--actor", who)   # cancelar vale en cualquier estado activo
                            self.decide(t["id"], {"aprobar": "approve", "rechazar": "reject", "reintentar": "retry"}[word], note, who)
                    except FlowError as exc:
                        doc_link = f"[Comandos]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
                        self.say(t["id"], f"No pude aplicar `/{word}`: {exc}.\n\n{doc_link}")
                    continue
                notes = info.get("notes", []) + [{"id": c["id"], "by": who, "text": text[:1500]}]
                is_q = bool(self.QUESTION.search(text))
                self.set_tmeta(t["id"], notes=notes[-20:], **({"questions": info.get("questions", []) + [{"id": c["id"], "text": text[:1500]}]} if is_q else {}))
                info = self.meta()["tasks"][t["id"]]
                if not is_q:
                    hint = ("Para que retomen ya: `/reintentar` (con o sin guía); o `/aprobar` / `/rechazar`." if t["state"] == "human_review"
                            else "Lo verán los agentes en su próxima ejecución.")
                    doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#c%C3%B3mo-funciona)"
                    self.say(t["id"], f"📝 Anotado, {who}. {hint}\n\n{doc_link}")

    def ingest_proposals(self) -> None:
        """`/tomar [clave: valor ...]` sobre un ticket con propuesta de contrato."""
        if not self.tracker:
            return
        humans = self.tracker.humans
        for vid_s, prop in list(self.meta()["proposals"].items()):
            if prop.get("taken") or prop.get("ignored"):
                continue
            seen = self.meta()["seen"].get(f"p{vid_s}", 0)
            for c in sorted(self.tracker.comments(int(vid_s)), key=lambda x: x["id"]):
                if c["id"] <= seen or c["author"].lower() not in humans:
                    continue
                m = self.meta()
                m["seen"][f"p{vid_s}"] = c["id"]
                self.save_meta(m)
                body = strip_quotes(clean(c["text"]))
                if re.match(r"\s*/ignorar\b", body, re.I):
                    m = self.meta()
                    m["proposals"][vid_s]["ignored"] = True
                    self.save_meta(m)
                    doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL}#contrato-del-ticket-descripci%C3%B3n-en-vikunja)"
                    self.comment(int(vid_s), f"Ok, no lo tomo y dejo de recordarlo. Si cambiás de idea: `/tomar`.\n\n{doc_link}")
                    break
                cmd = re.match(r"\s*/tomar\b(.*)", body, re.S | re.I)
                if not cmd:
                    continue
                ov = {k.strip().lower(): v.strip() for k, v in re.findall(r"([a-záéíóúñ-]+)\s*:\s*(.+)", cmd.group(1))}
                try:
                    self.take(int(vid_s), ov, c["author"])
                except FlowError as exc:
                    doc_link = f"[Guía de contratos]({vikunja_adapter.FLOW_DOC_URL}#contrato-del-ticket-descripci%C3%B3n-en-vikunja)"
                    self.comment(int(vid_s), f"No pude tomarlo: {exc}.\n\n{doc_link}")
                break

    def answer_questions(self, limit: int = 2) -> None:
        """Responde en el ticket las preguntas de la persona con un agente en solo lectura (con el contexto de la tarea)."""
        state = self.tasks()
        for t in state["tasks"].values():
            info = self.meta()["tasks"].get(t["id"], {})
            for q in [q for q in info.get("questions", []) if not q.get("done")][:limit]:
                if self.daily_limit_hit():
                    return
                pick = self.team.pick("review", t["type"], "low", self.provs, set(), learn.levels(state, self.team), {}, mode="review")
                if not pick:
                    continue
                agent, provider = pick
                wt = self.wt_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '-', t['id'])}-qa-{uuid.uuid4().hex[:6]}"
                self.ensure_integration()
                branch = self.branch_of(t["id"]) if self.git("branch", "--list", self.branch_of(t["id"])).strip() else self.integ
                self.git("worktree", "add", "--detach", str(wt), branch)
                try:
                    head = self.git("rev-parse", "HEAD", cwd=wt).strip()
                    last_out = ""
                    for rid in reversed(t["run_ids"]):
                        p = self.runs_dir / t["id"] / rid / "output.txt"
                        if p.exists():
                            last_out = p.read_text()[-3500:]
                            break
                    prompt = self.render("responder", agent=agent, provider=provider, persona=self.team.agents[agent]["persona"], task_id=t["id"],
                                         title=t["title"], state=t["state"], reason=t["human_reason"] or "(ninguno)", description=info.get("description", "")[:2500],
                                         last_output=last_out or "(sin salida previa)", question=q["text"],
                                         repo=str(ROOT), worktree=str(self.wt_dir / re.sub(r"[^A-Za-z0-9._-]", "-", t["id"])))
                    self.bump_daily()
                    res = self.provs.run(provider, "review", prompt, wt, self.cfg["run_timeout_s"], self.cfg["secret_patterns"])
                    tampered = bool(self.git("status", "--porcelain", cwd=wt).strip()) or self.git("rev-parse", "HEAD", cwd=wt).strip() != head
                finally:
                    self.drop_worktree(wt)
                answer = clean(res.output) if (res.rc == 0 and not tampered and res.output.strip()) else ""
                q["done"] = True
                m = self.meta()
                m["tasks"][t["id"]]["questions"] = [x if x["id"] != q["id"] else q for x in m["tasks"][t["id"]]["questions"]]
                self.save_meta(m)
                doc_link = f"[Doc]({vikunja_adapter.FLOW_DOC_URL})"
                if answer:
                    ans_words = answer.split()[:40]
                    ans_short = " ".join(ans_words) + ("…" if len(answer.split()) > 40 else "")
                    self.say(t["id"], f"**{agent}** ({provider}) responde:\n\n{ans_short}\n\n{doc_link}")
                else:
                    self.say(t["id"], f"No pude responder ahora ({provider} rc={res.rc}). Probá de nuevo o usá el comando directo.\n\n{doc_link}")

    # ---------- entrada ----------
    def add_local(self, tid: str, title: str, component: str, ttype: str, risk: str, contract: dict, description: str = "", vik: int | None = None) -> None:
        self.orch("add-task", tid, "--title", title, "--component", component, "--type", ttype, "--risk", risk,
                  *[x for a in contract.pop("acciones", []) for x in ("--protected-action", a)], "--actor", "intake")
        self.set_tmeta(tid, contract=contract, description=description, **({"vik": vik} if vik else {}))

    RESEARCH_WORDS = re.compile(
        r"\b(analiz|investig|diagnostic|evalu|revis|benchmark|compar|estudio|relevamiento|por qu[eé]|explic|entender)\w*",
        re.I,
    )
    DATA_WORDS = re.compile(
        r"\b(dataset|etiquet|rotul|fotos?|clips?|exportar|coco|yolo)\w*",
        re.I,
    )

    def infer_component(self, title: str, description: str, declared: str = "") -> str:
        if declared:
            return declared.strip()
        text = f"{title} {description}"
        m = self.component_hint.search(text) if self.component_hint else None
        if m:
            return m.group(1).rstrip("/")
        for rx, comp in self.component_map:
            if rx.search(text) and (self.repo / comp).is_dir():
                return comp
        try:
            rel = session_registry.find_related(text, repo_root=self.repo)
            if rel.get("recent"):
                best = rel["recent"][0]
                if best.get("score", 0) >= 3 and best.get("component"):
                    cand = best["component"]
                    if (self.repo / cand).is_dir():
                        return cand
        except Exception:
            pass
        return "agents"

    def infer_task_type(self, title: str, description: str, tri: dict | None = None) -> str:
        if tri and tri.get("type_conf", 0) >= 0.7:
            return tri["type"]
        text = f"{title} {description}"
        if self.RESEARCH_WORDS.search(text):
            return "research"
        if self.DATA_WORDS.search(text):
            return "data"
        if tri and tri.get("type") in orchestrator.TASK_TYPES:
            return tri["type"]
        return "code"

    def infer_test_cmd(self, component: str, task_type: str) -> str:
        if task_type == "research":
            return ""
        if component == "agents":
            return "python3 -m unittest discover -s agents -p 'test_*.py'"
        comp_dir = self.repo / component
        if comp_dir.is_dir():
            test_files = list(comp_dir.glob("test_*.py"))
            if test_files:
                return f"python3 -m unittest discover -s {component} -p 'test_*.py'"
            if (comp_dir / "tests").is_dir():
                return f"python3 -m unittest discover -s {component}/tests"
        return ""

    def propose(self, vt: dict) -> None:
        """Ticket asignado a un bot sin `agente: listo`: se comenta UNA vez por edición qué falta y se propone un contrato (JEV)."""
        m = self.meta()
        old = m["proposals"].get(str(vt["id"]), {})
        desc = clean(vt.get("description", ""))
        # se compara el contenido (no `updated`): nuestro propio comentario cambia `updated` y provocaba un comentario nuevo por minuto
        sig = hashlib.sha256((vt["title"] + "\0" + desc).encode()).hexdigest()[:16]
        if old and "sig" not in old and not (old.get("ignored") or old.get("taken")):   # propuesta anterior a la firma: se adopta sin volver a comentar
            m["proposals"][str(vt["id"])] = {**old, "sig": sig}
            self.save_meta(m)
            return
        if old.get("ignored") or old.get("taken") or old.get("sig") == sig:
            return
        tri = self.jev.triage(vt["title"], desc, "", self.team.specialties())
        comp = self.infer_component(vt["title"], desc)
        ttype = (tri or {}).get("type") or self.infer_task_type(vt["title"], desc, tri)
        risk = (tri or {}).get("risk") or ("low" if ttype == "research" else "medium")
        test = self.infer_test_cmd(comp, ttype)
        prop = {"tipo": ttype, "riesgo": risk, "componente": comp, "test": test}
        if (tri or {}).get("protected", 0) >= 0.6:
            prop["acciones"] = "jev-protegida"
        m["proposals"][str(vt["id"])] = {"sig": sig, "updated": vt["updated"], "contrato": prop, "titulo": vt["title"], "descripcion": desc}
        self.save_meta(m)
        lines = "\n".join(f"{k}: {v}" for k, v in prop.items() if v)
        test_warn = "" if (test or prop["tipo"] == "research") else "\n\n⚠ Falta el comando de test: `/tomar test: <comando>`."
        doc_link = f"[Guía de contratos]({vikunja_adapter.FLOW_DOC_URL}#contrato-del-ticket-descripci%C3%B3n-en-vikunja)"
        self.comment(vt["id"], (
            f"Me asignaron este ticket pero **no tiene contrato**, así que todavía no lo tomé. "
            f"Propuesta (clasificada por JEV):\n\n```\nagente: listo\n{lines}\n```\n\n"
            f"👉 Respondé **`/tomar`** (o `/tomar riesgo: high`), `/ignorar`, o pegá el contrato."
            f"{test_warn}\n\n{doc_link}"
        ))

    def take(self, vid: int, overrides: dict[str, str] | None = None, human: str = "Gustavo") -> str:
        """Toma un ticket propuesto: aplica el contrato propuesto (+ ajustes de la persona) y lo mete al flujo."""
        prop = self.meta()["proposals"].get(str(vid))
        if not prop:
            raise FlowError(f"no hay una propuesta pendiente para el ticket {vid}")
        k = {**prop["contrato"], **(overrides or {})}
        problems = [f for f in ("tipo", "riesgo", "componente") if not k.get(f)]
        if k.get("tipo") not in orchestrator.TASK_TYPES or k.get("riesgo") not in orchestrator.RISKS:
            problems.append("tipo/riesgo inválido")
        if k.get("tipo") != "research" and not k.get("test"):
            problems.append("test")
        if problems:
            raise FlowError("falta o es inválido: " + ", ".join(problems) + " (usá `/tomar test: <comando>`)")
        contract = {"test": k.get("test", ""), "scope": [x.strip() for x in k.get("alcance", "").split(",") if x.strip()], "prefer": k.get("proveedor"),
                    "no-merge": False, "acciones": [a.strip() for a in k.get("acciones", "").split(",") if a.strip()]}
        tid = f"VIK-{vid}"
        self.add_local(tid, re.sub(r"^\[[^\]]*\]\s*", "", prop["titulo"]), k["componente"], k["tipo"], k["riesgo"], contract, prop["descripcion"], vid)
        m = self.meta()
        m["proposals"][str(vid)]["taken"] = True
        self.save_meta(m)
        doc_link = vikunja_adapter.component_doc_link(self.repo, k["componente"])
        self.say(tid, f"Tomada por el flujo (a pedido de {human}): `{k['tipo']}` / riesgo `{k['riesgo']}` en {doc_link}.")
        return tid

    def intake(self) -> None:
        if not self.tracker:
            return
        known, bots = self.tasks()["tasks"], self.tracker.bots
        vtasks = self.tracker.tasks()
        m0 = self.meta()
        for vt in vtasks:   # la prioridad del tracker (0-5) ordena qué tarea se trabaja primero
            if f"VIK-{vt['id']}" in m0["tasks"] and m0["tasks"][f"VIK-{vt['id']}"].get("priority") != vt.get("priority", 0):
                self.set_tmeta(f"VIK-{vt['id']}", priority=vt.get("priority", 0))
        for vt in vtasks:
            tid = f"VIK-{vt['id']}"
            is_assigned_to_bot = bool({a.lower() for a in vt["assignees"]} & bots)
            if not is_assigned_to_bot:
                continue

            # Sincronización de reapertura desde Vikunja (solo tareas terminadas que se desmarcan)
            if not vt["done"] and tid in known:
                if known[tid]["state"] == "done":
                    self.orch("reopen", tid, "--human", "tracker", "--note", "reabierta desde el tracker")
                    self.say(tid, "Reabierta desde el tracker.")
                continue

            if vt["done"] or tid in known:
                continue

            title = re.sub(r"^\[[^\]]*\]\s*", "", vt["title"])
            desc = clean(vt.get("description", ""))
            k = parse_contract(vt.get("description", ""))
            if k.get("agente", "").lower() in {"no", "ignorar"}:
                continue
            if self.meta().get("proposals", {}).get(str(vt["id"]), {}).get("ignored"):
                continue

            # 1. Comprobar si hay sesiones activas en paralelo para evitar colisiones
            comp_hint = self.infer_component(title, desc, k.get("componente", ""))
            try:
                rel = session_registry.find_related(f"{title} {desc}", component_hint=comp_hint, repo_root=self.repo, registry_file=self.rt / "active_sessions.json")
            except Exception:
                rel = {"active": [], "recent": []}

            if rel.get("active"):
                active_ses = rel["active"][0]
                m = self.meta()
                active_notified = m.setdefault("waiting_session", {})
                if active_notified.get(str(vt["id"])) != active_ses["id"]:
                    active_notified[str(vt["id"])] = active_ses["id"]
                    self.save_meta(m)
                    self.comment(vt["id"], f"⏸ **Sesión activa en curso detectada**: \"{active_ses['topic']}\" ({active_ses['provider']}). Pongo este ticket en pausa para no colisionar.")
                continue

            # 2. Contrato explícito vs auto-intake
            has_explicit_contract = k.get("agente", "").lower() in {"listo", "si", "sí"}
            tri = self.jev.triage(title, desc, comp_hint, self.team.specialties())
            notes = []

            if has_explicit_contract:
                ttype = k.get("tipo")
                risk = k.get("riesgo")
                comp = k.get("componente") or comp_hint
                test = k.get("test", "")
                if tri:
                    if not ttype and tri["type_conf"] >= 0.7:
                        ttype = tri["type"]; notes.append(f"tipo `{ttype}` propuesto por JEV")
                    if not risk:
                        risk = tri["risk"]; notes.append(f"riesgo `{risk}` propuesto por JEV")
                    elif ["low", "medium", "high"].index(tri["risk"]) > ["low", "medium", "high"].index(risk):
                        notes.append(f"JEV subió el riesgo de `{risk}` a `{tri['risk']}` (JEV sólo puede subirlo)"); risk = tri["risk"]
                problems = [f for f, v in (("tipo", ttype), ("riesgo", risk), ("componente", comp)) if not v]
                if not problems and (ttype not in orchestrator.TASK_TYPES or risk not in orchestrator.RISKS):
                    problems = ["tipo/riesgo inválido"]
                if not problems and ttype != "research" and not test:
                    problems = ["test"]
                if not problems and tri and tri["ambiguous"] >= 0.7:
                    problems = ["el pedido es ambiguo (JEV): agregá criterios de aceptación o detalles"]
                if problems:
                    m = self.meta()
                    if m["invalid"].get(str(vt["id"])) != vt["updated"]:
                        m["invalid"][str(vt["id"])] = vt["updated"]
                        self.save_meta(m)
                        probs = ", ".join(problems)
                        doc_link = f"[Guía de contratos]({vikunja_adapter.FLOW_DOC_URL}#contrato-del-ticket-descripci%C3%B3n-en-vikunja)"
                        self.comment(vt["id"], (
                            f"No puedo tomar el ticket todavía: {probs}.\n\n"
                            f"Formato requerido: `agente: listo`, `componente: <carpeta>`, `test: <comando>`.\n\n"
                            f"{doc_link}"
                        ))
            else:
                if not self.cfg.get("auto_intake", True):
                    self.propose(vt)
                    continue
                # Auto-intake inteligente
                ttype = k.get("tipo") or self.infer_task_type(title, desc, tri)
                risk = k.get("riesgo") or ((tri or {}).get("risk") if (tri and tri.get("risk") in orchestrator.RISKS) else ("low" if ttype == "research" else "medium"))
                comp = comp_hint
                test = k.get("test") or self.infer_test_cmd(comp, ttype)
                if tri and tri.get("risk") and ["low", "medium", "high"].index(tri["risk"]) > ["low", "medium", "high"].index(risk):
                    risk = tri["risk"]

                if ttype != "research" and not test:
                    self.propose(vt)
                    continue

                notes.append(f"clasificación automática: tipo `{ttype}`, riesgo `{risk}`, componente `{comp}`")

            k["tipo"], k["riesgo"] = ttype, risk
            contract = {
                "test": test,
                "scope": [s.strip() for s in k.get("alcance", "").split(",") if s.strip()],
                "prefer": k.get("proveedor") or (tri or {}).get("specialty"),
                "no-merge": k.get("sin-merge", "").lower() in {"si", "sí", "true"},
                "acciones": [a.strip() for a in k.get("acciones", "").split(",") if a.strip() and a.strip().lower() != "ninguna"],
            }
            if tri and tri["protected"] >= 0.6 and not contract["acciones"]:
                contract["acciones"] = ["jev-protegida"]
                notes.append("JEV detectó una acción protegida (systemd/cron/secretos/hardware/deploy): decidís vos al final")
            if k.get("timeout", "").isdigit():
                contract["timeout"] = int(k["timeout"]) * 60
            title = re.sub(r"^\[[^\]]*\]\s*", "", vt["title"])
            self.add_local(tid, title, comp, ttype, risk, contract, desc, vt["id"])
            m = self.meta()
            if str(vt["id"]) in m.get("proposals", {}):
                m["proposals"][str(vt["id"])]["taken"] = True
                self.save_meta(m)

            notes_str = "".join(f"\n- {n}" for n in notes)
            extra = "Intervendrá una persona sólo al final (riesgo alto)." if risk == "high" else "Sólo te aviso ante una decisión crítica."
            doc_link = vikunja_adapter.component_doc_link(self.repo, comp)
            auto_label = "Tomada por el flujo de agentes" if has_explicit_contract else "🤖 **Tomada automáticamente por el flujo**"
            self.say(tid, f"{auto_label}: `{ttype}` / riesgo `{risk}` en {doc_link}.{notes_str}\n\n{extra}")

    # ---------- bucle ----------
    def recover_orphans(self) -> None:
        for r in self.tasks()["runs"].values():
            if r["state"] == "executing":
                self.orch("fail-run", r["id"], "--reason", "interrumpida (reinicio del dispatcher)", "--no-count")
        self.prune_stale_worktrees()

    def step(self, skip: set[str]) -> bool:
        table = {"ready": self.implement, "changes_requested": self.implement, "in_review": self.review,
                 "validated": self.finalize, "approved": self.finalize}
        meta = self.meta()["tasks"]
        for t in sorted(self.tasks()["tasks"].values(), key=lambda x: (-meta.get(x["id"], {}).get("priority", 0), x["created_at"])):
            fn = table.get(t["state"])
            if fn and t["id"] not in skip and "contract" in self.meta()["tasks"].get(t["id"], {}):  # sólo lo que entró por el flujo
                try:
                    res = fn(t)
                except (FlowError, OSError, subprocess.SubprocessError) as exc:
                    print(f"[flow] {t['id']} {t['state']}: {exc}", file=sys.stderr)
                    self.orch("mark", t["id"], "--event", "flow.error", "--actor", "flow", "--value", json.dumps(str(exc)[:250]))
                    for rid in t["run_ids"]:   # no dejar ejecuciones colgadas en "executing"
                        if self.tasks()["runs"][rid]["state"] == "executing":
                            self.orch("fail-run", rid, "--reason", "error interno del flujo: " + str(exc)[:150], "--no-count")
                    res = "wait"
                if res == "wait":
                    skip.add(t["id"])
                    continue
                return True
        return False

    def setup_kanban(self) -> str:
        """Deja el kanban con una columna por estado del flujo: renombra las de fábrica (To-Do/Doing/Done) y crea las que faltan."""
        self.tracker.setup_columns(COLUMNS, LEGACY_COLUMNS)
        return "columnas: " + " | ".join(COLUMNS)

    def sync_boards(self) -> None:
        """Mueve cada tarjeta a la columna de su estado (sólo las que cambiaron desde la última sincronización)."""
        if not self.tracker:
            return
        m, cols = self.meta(), None
        for vid, p in m["proposals"].items():   # tickets sin contrato: esperan a la persona (o quedaron ignorados)
            want = "Descartadas" if p.get("ignored") else None if p.get("taken") else "🙋 Tu decisión"
            if not want or p.get("column") == want:
                continue
            cols = cols or self.tracker.columns()
            if want not in cols:
                continue
            try:
                self.tracker.move(int(vid), want)
                m2 = self.meta()
                m2["proposals"][vid]["column"] = want
                self.save_meta(m2)
            except Exception as exc:  # noqa: BLE001
                print(f"[flow] no pude mover la propuesta {vid} a {want}: {exc}", file=sys.stderr)
        m = self.meta()
        for t in self.tasks()["tasks"].values():
            info = m["tasks"].get(t["id"], {})
            want = STATE_COLUMN.get(t["state"])
            if not info.get("vik") or not want or info.get("column") == want:
                continue
            cols = cols or self.tracker.columns()
            if want not in cols:
                continue   # tablero sin configurar (flow.py kanban): no rompe nada
            try:
                self.tracker.move(info["vik"], want)
                self.set_tmeta(t["id"], column=want)
            except Exception as exc:  # noqa: BLE001
                print(f"[flow] no pude mover {t['id']} a {want}: {exc}", file=sys.stderr)

    OWNER = {"ready": ("agente", "espera turno de un agente"), "executing": ("agente", "un agente está trabajando"),
             "reviewing": ("agente", "otro proveedor está revisando"), "in_review": ("agente", "espera revisor independiente"),
             "changes_requested": ("agente", "el autor corrige lo que pidió el revisor"), "validated": ("flujo", "listo para integrar en dev"),
             "approved": ("flujo", "aprobada por vos: integrando"), "human_review": ("VOS", "espera tu decisión"), "done": ("-", "integrada en dev"),
             "rejected": ("-", "rechazada")}

    def board(self) -> str:
        """Vista rigurosa: por ticket, estado real, quién tiene la pelota y cuál es el siguiente paso. Incluye lo que el flujo NO tomó."""
        st, meta, out = self.tasks(), self.meta(), ["TICKET   ESTADO              PELOTA  PRIO  QUÉ SIGUE"]
        for t in sorted(st["tasks"].values(), key=lambda x: -meta["tasks"].get(x["id"], {}).get("priority", 0)):
            if "contract" not in meta["tasks"].get(t["id"], {}):
                continue
            who, nxt = self.OWNER.get(t["state"], ("?", t["state"]))
            if t["state"] == "human_review":
                nxt += f": {t['human_reason'].splitlines()[0][:80]}"
            out.append(f"{t['id']:8} {t['state']:19} {who:7} {meta['tasks'][t['id']].get('priority', 0):<5} {nxt}")
        for vid, p in meta["proposals"].items():
            if not p.get("taken") and not p.get("ignored"):
                out.append(f"VIK-{vid:4} SIN CONTRATO        VOS     -     comentar /tomar en el ticket (propuesta: {p['contrato'].get('tipo')}/{p['contrato'].get('riesgo')}/{p['contrato'].get('componente')})")
        return "\n".join(out)

    def do_retro(self) -> Path:
        """Retrospectiva: métricas por agente, patrones de correcciones y propuestas para la persona."""
        text = learn.retro(self.tasks(), self.team, self.jev, self.rt / "learning" / "labels.json")
        out = HERE / "learning" / "retros" / f"{time.strftime('%Y-%m-%d')}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return out

    def maybe_retro(self) -> None:
        m = self.meta()
        done = sum(1 for t in self.tasks()["tasks"].values() if t["state"] in {"done", "rejected"})
        if done and done - m.get("retro_at", 0) >= self.cfg.get("retro_every_tasks", 5):
            path = self.do_retro()
            m["retro_at"] = done
            self.save_meta(m)
            self.notify(f"📈 Nueva retrospectiva del flujo con propuestas: {path.relative_to(ROOT)}")

    @staticmethod
    def safe(fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001  (un tablero caído no debe frenar el trabajo)
            print(f"[flow] {fn.__name__}: {exc}", file=sys.stderr)

    def tick(self, max_steps: int = 30) -> int:
        if self.pause_file.exists():
            return 0
        self.rt.mkdir(parents=True, exist_ok=True)
        with open(self.rt / "flow.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            self.recover_orphans()
            self.safe(self.prune_idle_worktrees)
            for fn in (self.intake, self.ingest_proposals, self.ingest_comments, self.answer_questions):
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001  (tracker caído no debe frenar lo que ya está en curso)
                    print(f"[flow] {fn.__name__}: {exc}", file=sys.stderr)
            n, skip = 0, set()
            self.safe(self.sync_boards)
            self.safe(lambda: budget.check_burn_rate(notify=True))
            while n < max_steps and self.step(skip):
                n += 1
                self.safe(self.prune_idle_worktrees)   # una pasada puede durar horas: el disco se vigila en cada paso
                self.safe(self.sync_boards)
                self.safe(self.ingest_comments)   # los comandos de la persona no esperan a que termine toda la cadena de pasos
            self.notify_humans()
            try:
                self.maybe_retro()
            except Exception as exc:  # noqa: BLE001
                print(f"[flow] retro: {exc}", file=sys.stderr)
            return n


def load_flow() -> Flow:
    cfg = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    return Flow(tracker=trackermod.load_tracker(cfg.get("tracker")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick", help="una pasada del dispatcher")
    d = sub.add_parser("daemon"); d.add_argument("--every", type=int, default=60)
    sub.add_parser("status"); sub.add_parser("pause"); sub.add_parser("resume")
    sub.add_parser("budget", help="estado del bot presupuestario y ritmo de quema")
    a = sub.add_parser("add", help="crear una tarea local (sin tracker)")
    a.add_argument("id"); a.add_argument("--title", required=True); a.add_argument("--component", required=True)
    a.add_argument("--type", required=True); a.add_argument("--risk", default="low"); a.add_argument("--test", default="")
    a.add_argument("--scope", action="append", default=[]); a.add_argument("--prefer"); a.add_argument("--description", default="")
    a.add_argument("--action", action="append", default=[])
    tk = sub.add_parser("take", help="tomar un ticket del tracker con la propuesta de contrato"); tk.add_argument("vid", type=int); tk.add_argument("ajustes", nargs="*", help="clave=valor (ej. riesgo=high)")
    sub.add_parser("board", help="tablero: cada ticket, en qué está, quién tiene la pelota y qué sigue")
    ro = sub.add_parser("reopen", help="reabrir una tarea rechazada"); ro.add_argument("id"); ro.add_argument("note", nargs="?", default="reabierta"); ro.add_argument("--human", default="Gustavo")
    sub.add_parser("retro", help="generar la retrospectiva ahora")
    sub.add_parser("kanban", help="configurar las columnas del tablero del tracker (una por estado del flujo)")
    sub.add_parser("quota", help="cuota de cada proveedor: veredicto que usa el flujo antes de asignar tareas")
    sub.add_parser("team", help="ver el equipo, su confianza y desempeño")
    sub.add_parser("reset-quota", help="reiniciar el contador diario de ejecuciones")
    sl = sub.add_parser("set-limit", help="cambiar el tope diario de ejecuciones"); sl.add_argument("limit", type=int)
    sub.add_parser("prune", help="limpiar worktrees huérfanos de tareas finalizadas")
    rl = sub.add_parser("release", help="publicar dev en main (sin --approve sólo muestra el resumen)"); rl.add_argument("--approve", action="store_true")
    dc = sub.add_parser("decide"); dc.add_argument("id"); dc.add_argument("decision", choices=["approve", "reject", "retry"])
    dc.add_argument("note", nargs="?", default=""); dc.add_argument("--human", default="Gustavo")
    args = ap.parse_args()
    flow = load_flow()
    try:
        if args.cmd == "tick":
            print(f"pasos ejecutados: {flow.tick()}")
        elif args.cmd == "daemon":
            while True:
                time.sleep(2 if flow.tick() else args.every)
        elif args.cmd == "kanban":
            print(flow.setup_kanban() if flow.tracker else "el tracker no está habilitado en flow_config.json")
        elif args.cmd == "take":
            print("tomada:", flow.take(args.vid, dict(a.split("=", 1) for a in args.ajustes)))
        elif args.cmd == "board":
            print(flow.board())
        elif args.cmd == "reopen":
            print(flow.orch("reopen", args.id, "--human", args.human, "--note", args.note) or "reabierta")
        elif args.cmd == "reset-quota":
            prev = flow.reset_quota()
            print(f"cuota diaria reiniciada (era {prev} ejecuciones hoy)")
        elif args.cmd == "set-limit":
            old = flow.set_limit(args.limit)
            print(f"tope diario actualizado de {old} a {args.limit} ejecuciones")
        elif args.cmd == "prune":
            pruned = flow.prune_stale_worktrees()
            print(f"worktrees limpiados: {', '.join(pruned) if pruned else 'ninguno'}")
        elif args.cmd == "retro":
            print(flow.do_retro())
        elif args.cmd == "release":
            print(flow.release(args.approve))
        elif args.cmd == "team":
            st = flow.tasks(); lv, cs = learn.levels(st, flow.team), learn.cards(st)
            for n, a in flow.team.agents.items():
                c = cs.get(n, {})
                print(f"{n:8} nivel {lv[n]} · {a['title']} · roles {','.join(a['roles'])} · proveedores {','.join(a['providers'])} · limpias {c.get('clean', 0)}/{c.get('attempts', 0)}")
            print(f"Jev      clasificador · {'disponible' if flow.jev.enabled() else 'sin key o sin cupo'} · llamadas hoy {flow.jev.calls_today()}")
        elif args.cmd == "quota":
            for n in flow.provs.table:
                if flow.provs.table[n].get("enabled"):
                    a = flow.provs.quota_advice(n)
                    print(f"{n:11} {a['verdict']:9} {a['reason']}")
        elif args.cmd == "budget":
            print(budget.budget_status())
        elif args.cmd == "pause":
            flow.rt.mkdir(parents=True, exist_ok=True); flow.pause_file.touch(); print("pausado")
        elif args.cmd == "resume":
            flow.pause_file.unlink(missing_ok=True); print("reanudado")
        elif args.cmd == "add":
            flow.add_local(args.id, args.title, args.component, args.type, args.risk,
                           {"test": args.test, "scope": args.scope, "prefer": args.prefer, "acciones": args.action}, args.description)
            print("creada", args.id)
        elif args.cmd == "decide":
            flow.decide(args.id, args.decision, args.note, args.human)
        else:
            print("pausado" if flow.pause_file.exists() else "activo", f"· ejecuciones hoy: {flow.daily_count()}/{flow.cfg['daily_run_limit']}")
            for n in flow.provs.table:
                ok, why = flow.provs.usable(n, "implement")
                print(f"  proveedor {n:9} {'OK' if ok else why} · cuota {flow.provs.quota_advice(n)['verdict']}")
            for t in flow.tasks()["tasks"].values():
                print(f"  {t['id']:14} {t['state']:18} riesgo={t['risk']:6} rondas={t['rounds']} {t['human_reason']}")
    except FlowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
