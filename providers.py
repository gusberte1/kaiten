"""Adaptadores de proveedores de agentes (Claude Code, Codex, Gemini, DeepSeek/aider, ...).

Un proveedor es sólo una línea de comando declarada en `providers.json`: el flujo no
conoce ninguna API. Corre el binario en un directorio dado, con timeout, y devuelve
la salida ya redactada. Detecta cuota agotada y deja al proveedor en cooldown para que
el flujo use otro en vez de fallar.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import quota as quotamod

HERE = Path(__file__).resolve().parent
import paths as _paths
DEFAULT_FILE = _paths.CONFIG / "providers.json"
NVM_BINS = sorted((Path.home() / ".nvm/versions/node").glob("*/bin"), reverse=True)
EXTRA_BINS = [*NVM_BINS, Path.home() / ".local/bin"]
MAX_OUTPUT = 200_000
# «resets 8pm (America/Argentina/Buenos_Aires)» / «resets Sep 30, 10:10pm (UTC)» en el aviso de límite de los CLIs
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?((?:[A-Za-z]{3}[a-z]*\.?\s+\d{1,2},?\s+)?\d{1,2}(?::\d{2})?\s*[ap]m)\s*\(([A-Za-z_]+(?:/[A-Za-z_]+)*)\)", re.I)


@dataclass
class Result:
    rc: int
    output: str
    blocked: bool = False
    timed_out: bool = False
    seconds: float = 0.0
    stderr: str = ""


def redact(text: str, patterns: list[str]) -> str:
    for pat in patterns:
        text = re.sub(pat, "[REDACTED]", text)
    return text


class Providers:
    def __init__(self, path: Path = DEFAULT_FILE, health_file: Path | None = None, quota_file: Path | None = None):
        self.quota_file = quota_file
        self.cfg = json.loads(path.read_text(encoding="utf-8"))
        self.table = self.cfg["providers"]
        self.health_file = health_file
        self.blocked_re = re.compile(self.cfg.get("blocked_regex", "$^"), re.I)
        self._auth_cache: dict[tuple[str, str], tuple[float, tuple[bool, str]]] = {}

    def binary(self, name: str) -> str | None:
        want = self.table[name]["binary"]
        if os.path.isabs(want):
            return want if os.access(want, os.X_OK) else None
        return shutil.which(want, path=os.pathsep.join([os.environ.get("PATH", ""), *map(str, EXTRA_BINS)]))

    def _health_raw(self) -> dict[str, dict]:
        """Cooldowns por proveedor: {"until": epoch, "since": epoch}. Acepta el formato viejo (sólo el epoch de fin)."""
        try:
            data = json.loads(self.health_file.read_text()) if self.health_file else {}
        except (OSError, ValueError):
            return {}
        return {k: (v if isinstance(v, dict) else {"until": float(v), "since": None}) for k, v in data.items()}

    def _health(self) -> dict[str, float]:
        return {k: v["until"] for k, v in self._health_raw().items()}

    def _write_health(self, data: dict[str, dict]) -> None:
        self.health_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.health_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.health_file)

    def _quota_fresh_since(self, name: str, since: float) -> bool:
        """¿Hay evidencia de cuota posterior al bloqueo? Una lectura tomada después, o una ventana cuyo reinicio
        ocurrió después y ya pasó (Codex sólo actualiza su lectura cuando corre: sin esto no volvería nunca)."""
        groups = set(self.table.get(name, {}).get("quota") or [])
        if not (self.quota_file and groups):
            return False
        ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        try:
            analysis = quotamod.load_analysis(path=self.quota_file) or {}
            ws = [w for w in analysis.get("windows", []) if w["group"] in groups and w.get("fresh")]
            return any(ts(w["read"]) > since or (w.get("renewed") and ts(w["reset"]) > since) for w in ws)
        except Exception:   # el monitor jamás debe tumbar la asignación
            return False

    def _cooldown_active(self, name: str) -> bool:
        """El cooldown vale hasta su fin, salvo que una lectura de cuota POSTERIOR al bloqueo diga que el proveedor es usable
        (el aviso del CLI puede traer un reinicio más cercano que el tope, o la ventana ya se renovó)."""
        cd = self._health_raw().get(name)
        if not cd or cd["until"] <= time.time():
            return False
        since = cd.get("since") or (cd["until"] - 5 * 3600)   # formato viejo: se asume el tope de 5 h
        adv = self.quota_advice(name)
        if adv["usable"] and adv["verdict"] != "SIN_DATO" and self._quota_fresh_since(name, since):
            data = self._health_raw()
            data.pop(name, None)
            self._write_health(data)
            return False
        return True

    def quota_advice(self, name: str) -> dict:
        """Veredicto de cuota (quota.advise). Sin archivo de cuota o sin lectura vigente: usable y neutro."""
        neutral = {"provider": name, "verdict": "SIN_DATO", "usable": True, "reason": "sin datos de cuota", "until": None,
                   "bonus": 0.0, "discourage_heavy": False, "group": None}
        if not self.quota_file:
            return neutral
        try:
            return quotamod.advise(name, self.table, quotamod.load_analysis(path=self.quota_file))
        except Exception as e:   # el monitor jamás debe tumbar la asignación
            return {**neutral, "reason": f"error leyendo cuota: {e}"[:120]}

    def cooldown(self, name: str, hours: float, output: str = "") -> float:
        """Pausa al proveedor hasta el reinicio que anuncia su propio aviso («resets 8pm (America/...)»), con un margen de 2 min.
        Si el aviso no trae hora legible se usa el tope `hours`. Devuelve el epoch de fin."""
        now = time.time()
        until = now + hours * 3600
        m = RESET_RE.search(output or "")
        if m:
            try:
                until = min(until, quotamod.parse_reset(m.group(1), m.group(2), now) + 120)
            except (ValueError, KeyError):
                pass
        if self.health_file:
            data = self._health_raw()
            data[name] = {"until": until, "since": now}
            self._write_health(data)
        return until

    def _clean_env(self, cfg: dict) -> dict[str, str]:
        """Entorno sin credenciales facturables, también para prechecks."""
        never = {"GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"}
        env = {k: v for k, v in os.environ.items() if k not in never | set(cfg.get("strip_env", []))}
        env["PATH"] = os.pathsep.join([*map(str, EXTRA_BINS), env.get("PATH", "")])
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _auth_check(self, name: str, cfg: dict, chk: dict) -> tuple[bool, str]:
        """Prechecks declarativos; nunca leen ni pasan secretos al proveedor."""
        fix = chk["fix"]
        # Un chequeo de sesión puede invocar una CLI remota. Cachear sólo el
        # resultado de esta instancia evita repetirlo por cada `team.pick`.
        cache_s = chk.get("cache_s", 0)
        cache_key = (name, json.dumps(chk, sort_keys=True))
        cached = self._auth_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        result: tuple[bool, str]
        if "command" in chk:
            binary = self.binary(name)
            if not binary:
                return False, fix
            argv = [binary if part == "{binary}" else part for part in chk["command"]]
            try:
                run = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                     timeout=chk.get("timeout_s", 15), env=self._clean_env(cfg))
                value = json.loads(run.stdout) if chk.get("path") else run.returncode == 0
                for part in (chk.get("path") or "").split("."):
                    value = value.get(part) if part else value
            except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
                result = False, fix
                if cache_s:
                    self._auth_cache[cache_key] = (time.monotonic() + (cache_s if result[0] else min(cache_s, 20)), result)
                return result
            ok = value == chk["equals"] if "equals" in chk else bool(value)
            result = ok, "ok" if ok else fix
            if cache_s:
                self._auth_cache[cache_key] = (time.monotonic() + (cache_s if result[0] else min(cache_s, 20)), result)
            return result

        f = Path(chk["file"]).expanduser()
        try:
            value = json.loads(f.read_text()) if chk.get("path") else f.exists()
            for part in (chk.get("path") or "").split("."):
                value = value.get(part) if part else value
        except FileNotFoundError:
            if chk.get("allow_missing"):
                return True, "ok"
            return False, fix
        except (OSError, ValueError, AttributeError):
            return False, fix
        if "not_equals" in chk:
            ok = value != chk["not_equals"] if value is not None else bool(chk.get("allow_missing"))
            result = ok, "ok" if ok else fix
            if cache_s:
                self._auth_cache[cache_key] = (time.monotonic() + (cache_s if result[0] else min(cache_s, 20)), result)
            return result
        if not value or ("equals" in chk and value != chk["equals"]):
            result = False, fix
        else:
            result = True, "ok"
        if cache_s:
            self._auth_cache[cache_key] = (time.monotonic() + (cache_s if result[0] else min(cache_s, 20)), result)
        return result

    def usable(self, name: str, mode: str) -> tuple[bool, str]:
        cfg = self.table.get(name)
        if not cfg or not cfg.get("enabled"):
            return False, "deshabilitado"
        if mode not in cfg:
            return False, f"sin plantilla {mode}"
        if not self.binary(name):
            return False, f"binario {cfg['binary']} no encontrado"
        for chk in cfg.get("auth_checks", []):   # p.ej. Gemini con cuenta Google, no con API key (se paga por token)
            ok, why = self._auth_check(name, cfg, chk)
            if not ok:
                return False, why
        missing = [str(Path(f).expanduser()) for f in cfg.get("env_from_file", {}).values() if not Path(f).expanduser().is_file()]
        if missing:
            return False, "falta credencial " + ", ".join(missing)
        if self._cooldown_active(name):
            until = time.strftime("%H:%M", time.localtime(self._health()[name]))
            return False, f"en cooldown por cuota hasta {until}"
        adv = self.quota_advice(name)
        if not adv["usable"]:
            return False, "cuota agotada: " + adv["reason"]
        return True, "ok"

    def choose(self, order: list[str], mode: str, exclude: set[str] = frozenset(), prefer: str | None = None) -> str | None:
        seq = ([prefer] if prefer else []) + [n for n in order if n != prefer] + [n for n in self.table if n not in order]
        for name in seq:
            if name in self.table and name not in exclude and self.usable(name, mode)[0]:
                return name
        return None

    def run(self, name: str, mode: str, prompt: str, cwd: Path, timeout: int, secret_patterns: list[str] = ()) -> Result:
        cfg = self.table[name]
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tmp:
            tmp.write(prompt)
        try:
            subst = {"{prompt}": prompt, "{prompt_file}": tmp.name, "{cwd}": str(cwd)}
            argv = [self.binary(name)]
            for part in cfg[mode]:
                for key, val in subst.items():
                    part = part.replace(key, val)
                argv.append(part)
            env = self._clean_env(cfg)  # ningún agente hereda keys que cobran por token
            for var, fname in cfg.get("env_from_file", {}).items():
                f = Path(fname).expanduser()
                if not f.is_file():
                    return Result(127, f"falta {f} (variable {var})")
                raw = f.read_text()
                found = re.search(rf"^\s*(?:export\s+)?{var}=(.*)$", raw, re.M)   # acepta un archivo `export VAR=valor` o sólo el valor
                env[var] = (found.group(1) if found else raw).strip().strip("\"'")
            env.update(cfg.get("env", {}))
            # La configuración del proveedor no puede reactivar prompts Git.
            env["GIT_TERMINAL_PROMPT"] = "0"
            start = time.time()
            separate_stderr = bool(cfg.get("separate_stderr"))
            proc = subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
                                    start_new_session=True)
            try:
                out, err = proc.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                out, err = proc.communicate()
                timed_out = True
        finally:
            os.unlink(tmp.name)
        out = redact(out or "", list(secret_patterns))
        err = redact(err or "", list(secret_patterns))
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT // 2] + "\n[... recortado ...]\n" + out[-MAX_OUTPUT // 2:]
        if len(err) > MAX_OUTPUT:
            err = err[:MAX_OUTPUT // 2] + "\n[... recortado ...]\n" + err[-MAX_OUTPUT // 2:]
        # cuota agotada: con rc != 0 (miran stdout+stderr), o con rc == 0 pero salida cortita que es sólo el aviso (Claude `-p` sale 0 con «You've hit your session limit»)
        if proc.returncode != 0:
            blocked = bool(self.blocked_re.search((out + "\n" + err)[-3000:]))
        else:
            blocked = bool(out.strip()) and bool(self.blocked_re.search(out[-3000:])) and len(out.strip()) < 600
        return Result(proc.returncode, out, blocked, timed_out, time.time() - start, err)
