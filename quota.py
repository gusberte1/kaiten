#!/usr/bin/env python3
"""Monitor de cuotas de los proveedores de agentes (ventanas de 5 h y semanal).

`collect` lee las tres fuentes (claude /usage, agy /usage, rollouts de Codex), guarda las lecturas
crudas en `.runtime/quota.json` y suma una línea al historial. El resto del sistema (flujo, página
:8099) sólo lee ese archivo con `load_analysis()` y `advise()`, que recalculan tiempos y ritmos al
momento de leer. Cada fuente falla por separado: si una no responde se conserva su última lectura
y el flujo la considera vencida pasada su edad máxima.

Uso: python3 quota.py collect | show | json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
from paths import RUNTIME
QUOTA_FILE = RUNTIME / "quota.json"
HISTORY_FILE = RUNTIME / "quota-history.jsonl"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

DUR_H = {"5h": 5.0, "semanal": 168.0}
MAX_AGE_H = {"claude": 1.0, "agy": 1.0, "codex": 24.0}   # Codex sólo escribe la cuota cuando corre
RATIO_HI, RATIO_LO = 1.15, 0.6      # razón ritmo actual / ritmo sostenible
EXHAUSTED_AVAIL = 3.0               # % disponible por debajo del cual no se asignan tareas
YOUNG = 0.15                        # ventana con menos de este tramo transcurrido: razón poco fiable
HISTORY_MAX_LINES = 6000
BONUS = {"USAR": 0.15, "MANTENER": 0.0, "SIN_DATO": 0.0, "ESPERAR": -0.15, "FRENAR": -0.30, "AGOTADA": -1.0}
SEVERITY = {"AGOTADA": 5, "FRENAR": 4, "ESPERAR": 3, "MANTENER": 2, "USAR": 1, "SIN_DATO": 0}
BINS = [Path.home() / ".local/bin", *sorted((Path.home() / ".nvm/versions/node").glob("*/bin"), reverse=True)]
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
             "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT")
GROUP_LABELS = {"claude": "Claude (suscripción)", "codex": "Codex (plan plus)",
                "agy:Gemini Models": "Antigravity · Gemini", "agy:Claude and GPT models": "Antigravity · Claude y GPT"}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def group_label(group: str) -> str:
    return GROUP_LABELS.get(group, group)


# ---------------------------------------------------------------- lectores
def parse_reset(text: str, tz: str, now: float) -> float:
    """«Sep 30, 8pm» / «Sep 28, 10:10pm» / «10:10pm» en la zona del CLI -> epoch (siempre futuro)."""
    m = re.match(r"(?:([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)$", text.strip(), re.I)
    if not m:
        raise ValueError(f"hora de reinicio ilegible: {text!r}")
    zone = ZoneInfo(tz)
    local_now = datetime.fromtimestamp(now, zone)
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    minute = int(m.group(4) or 0)
    if m.group(1):
        month = datetime.strptime(m.group(1).title(), "%b").month
        cand = datetime(local_now.year, month, int(m.group(2)), hour, minute, tzinfo=zone)
        if cand.timestamp() < now - 86400:
            cand = cand.replace(year=cand.year + 1)
    else:
        cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand.timestamp() < now:
            cand = cand.replace(day=cand.day) + (datetime(2000, 1, 2) - datetime(2000, 1, 1))
    return cand.timestamp()


CLAUDE_RE = re.compile(r"Current (session|week[^:]*):\s*(\d+(?:\.\d+)?)%\s*used\s*·\s*resets\s+(.+?)\s*\(([A-Za-z_]+(?:/[A-Za-z_]+)+)\)")


def parse_claude(text: str, now: float) -> list[dict]:
    found = {}
    for kind, pct, reset, tz in CLAUDE_RE.findall(text):
        window = "5h" if kind == "session" else "semanal"
        if window in found and "all models" not in kind:
            continue
        found[window] = {"group": "claude", "window": window, "used": float(pct),
                         "reset": parse_reset(reset, tz, now), "read": now}
    if not found:
        raise ValueError("claude /usage sin datos reconocibles: " + text.strip()[:120])
    return list(found.values())


def parse_agy(text: str, now: float) -> list[dict]:
    out = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4 or not parts[2].endswith("%"):
            continue
        window = "semanal" if parts[1].lower().startswith("weekly") else "5h" if parts[1].lower().startswith("five") else None
        if not window:
            continue
        reset = datetime.fromisoformat(parts[3].replace("Z", "+00:00")).timestamp()
        out.append({"group": f"agy:{parts[0]}", "window": window, "used": round(100 - float(parts[2][:-1]), 1), "reset": reset, "read": now})
    if not out:
        raise ValueError("agy /usage sin datos reconocibles: " + text.strip()[:120])
    return out


def read_codex(now: float, root: Path = CODEX_SESSIONS, max_files: int = 8) -> list[dict]:
    files = sorted(root.glob("*/*/*/rollout-*.jsonl"), reverse=True)[:max_files]
    for f in files:
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 2_000_000))
            lines = fh.read().decode("utf-8", "ignore").splitlines()
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
                rl = d["payload"]["rate_limits"]
                stamp = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            out = []
            for part in ("primary", "secondary"):
                w = (rl or {}).get(part)
                window = {300: "5h", 10080: "semanal"}.get((w or {}).get("window_minutes"))
                if window:
                    out.append({"group": "codex", "window": window, "used": float(w["used_percent"]),
                                "reset": float(w["resets_at"]), "read": stamp})
            if out:
                return out
    raise ValueError("Codex: sin eventos con rate_limits en las sesiones recientes")


def run_cli(binary: str, args: list[str], timeout: int = 90) -> str:
    exe = shutil.which(binary, path=os.pathsep.join([os.environ.get("PATH", ""), *map(str, BINS)]))
    if not exe:
        raise ValueError(f"binario {binary} no encontrado")
    cwd = RUNTIME / "quota-cwd"          # directorio propio: los transcripts de `claude -p` no ensucian el proyecto
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    r = subprocess.run([exe, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise ValueError(f"{binary} rc={r.returncode}: {(r.stderr or r.stdout).strip()[:150]}")
    return r.stdout


RUNNERS = {
    "claude": lambda now: parse_claude(run_cli("claude", ["-p", "/usage"]), now),
    "agy": lambda now: parse_agy(run_cli("agy", ["-p", "/usage", "--output-format", "text"]), now),
    "codex": lambda now: read_codex(now),
}


# ---------------------------------------------------------------- recolección
def source_of(group: str) -> str:
    return group.split(":")[0]


def load_raw(path: Path = QUOTA_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect(now: float | None = None, runners: dict | None = None, quota_file: Path = QUOTA_FILE,
            history_file: Path = HISTORY_FILE) -> dict:
    now = now or time.time()
    prev = load_raw(quota_file) or {}
    prev_by_source = {}
    for r in prev.get("readings", []):
        prev_by_source.setdefault(source_of(r["group"]), []).append(r)
    sources, readings = {}, []
    for name, fn in (runners or RUNNERS).items():
        try:
            got = fn(now)
            readings += got
            sources[name] = {"ok": True, "error": None, "at": iso(now), "windows": len(got)}
        except Exception as e:   # cada fuente falla por separado
            readings += prev_by_source.get(name, [])
            sources[name] = {"ok": False, "error": str(e)[:200], "at": (prev.get("sources", {}).get(name) or {}).get("at")}
    raw = {"generated_at": iso(now), "sources": sources, "readings": readings}
    quota_file = Path(quota_file)
    quota_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=quota_file.parent, prefix=".quota-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    os.replace(tmp, quota_file)
    append_history(history_file, now, readings)
    return raw


def append_history(path: Path, now: float, readings: list[dict]) -> None:
    path = Path(path)
    line = json.dumps({"t": iso(now), "w": [[r["group"], r["window"], r["used"], iso(r["reset"]), iso(r["read"])] for r in readings]}, ensure_ascii=False)
    with open(path, "a") as f:
        f.write(line + "\n")
    try:
        lines = path.read_text().splitlines()
        if len(lines) > HISTORY_MAX_LINES:
            path.write_text("\n".join(lines[-HISTORY_MAX_LINES // 2:]) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- análisis
def analyze_window(r: dict, now: float) -> dict:
    dur = DUR_H[r["window"]]
    used, avail = float(r["used"]), 100.0 - float(r["used"])
    rem_h = (r["reset"] - now) / 3600
    age_h = max(0.0, (now - r["read"]) / 3600)
    src = source_of(r["group"])
    w = {"group": r["group"], "label": group_label(r["group"]), "window": r["window"], "used": used, "avail": avail,
         "reset": iso(r["reset"]), "read": iso(r["read"]), "age_h": age_h, "fresh": age_h <= MAX_AGE_H.get(src, 1.0),
         "rem_h": max(rem_h, 0.0), "dur_h": dur, "renewed": False, "idle": False, "young": False, "exhausted": False, "eff_avail": avail, "capped": False,
         "elapsed_h": None, "rate": None, "sustainable": None, "ratio": None, "projected": None, "exhaust_h": None, "cupo": None}
    if rem_h <= 0:      # el reinicio ya pasó: la lectura describe una ventana anterior
        w.update(renewed=True, used=0.0, avail=100.0, state="grey")
        return w
    elapsed = min(max(dur - rem_h, 0.01), dur)
    w["idle"] = used == 0 and r["window"] == "5h" and abs((r["reset"] - r["read"]) / 3600 - dur) < 0.2   # el reinicio se mueve: no empezó
    rate, sustainable = used / elapsed, avail / rem_h
    w.update(elapsed_h=elapsed, rate=rate, sustainable=sustainable, ratio=(rate / sustainable) if sustainable > 0 else float("inf"),
             projected=used + rate * rem_h, exhaust_h=(avail / rate) if rate > 0 else None,
             young=(elapsed / dur < YOUNG and used > 0), cupo=(avail / (rem_h / 5)) if r["window"] == "semanal" else None)
    w["exhausted"] = avail <= EXHAUSTED_AVAIL
    w["state"] = ("red" if w["exhausted"] else "grey" if w["idle"] else
                  "red" if w["ratio"] > RATIO_HI else "blue" if w["ratio"] < RATIO_LO else "green")
    return w


def group_verdict(windows: list[dict]) -> dict:
    wk = next((w for w in windows if w["window"] == "semanal"), None)
    f5 = next((w for w in windows if w["window"] == "5h"), None)
    fresh = [w for w in windows if w["fresh"]]
    if not fresh:
        return {"verdict": "SIN_DATO", "reason": "sin lectura reciente", "until": None}
    ex = [w for w in fresh if w["exhausted"] and not w["renewed"]]
    if ex:
        w = max(ex, key=lambda x: x["rem_h"])
        return {"verdict": "AGOTADA", "reason": f"cuota {w['window']} agotada, reinicia en {fmt_h(w['rem_h'])}", "until": w["reset"]}
    if wk and wk["fresh"] and not wk["renewed"]:
        if wk["state"] == "red":
            when = f"se agota en {fmt_h(wk['exhaust_h'])} y reinicia en {fmt_h(wk['rem_h'])}"
            return {"verdict": "FRENAR", "reason": f"semanal {when} (ritmo tope {wk['sustainable']:.2f} %/h)" + (", ventana joven" if wk["young"] else ""), "until": None}
        if wk["state"] == "blue":
            if f5 and f5["fresh"] and f5["state"] == "red":
                return {"verdict": "ESPERAR", "reason": "5 h al límite de ritmo; la semanal tiene margen", "until": f5["reset"]}
            return {"verdict": "USAR", "reason": f"sobrarían {100 - wk['projected']:.0f} % de la semanal al reinicio en {fmt_h(wk['rem_h'])}", "until": None}
        return {"verdict": "MANTENER", "reason": f"semanal con ritmo sano (razón {wk['ratio']:.2f})", "until": None}
    if f5 and f5["fresh"] and f5["state"] == "red":
        return {"verdict": "ESPERAR", "reason": "5 h al límite de ritmo", "until": f5["reset"]}
    return {"verdict": "MANTENER", "reason": "sin lectura semanal, 5 h sin alarma", "until": None}


LEVELS = [(80, "USAR YA"), (60, "CONVIENE"), (42, "NEUTRO"), (25, "CUIDAR"), (0, "PARAR")]


def group_index(windows: list[dict], verdict: dict) -> dict:
    """Índice 0-100 de conveniencia de gastar ahora: 100 = sobra cuota (usar ya), 50 = ritmo justo, 0 = se agota (parar).
    Manda la semanal (razón ritmo/tope); una 5 h casi llena (o acelerada con menos de 40 % libre) lo topa en NEUTRO; ventana joven se acerca a 50."""
    wk = next((w for w in windows if w["window"] == "semanal" and w["fresh"] and not w["renewed"]), None)
    f5 = next((w for w in windows if w["window"] == "5h" and w["fresh"]), None)
    if verdict["verdict"] == "SIN_DATO":
        return {"index": None, "level": "SIN DATO", "why": verdict["reason"]}
    if verdict["verdict"] == "AGOTADA":
        return {"index": 0, "level": "AGOTADA", "why": verdict["reason"]}
    main = wk or (f5 if f5 and not f5["renewed"] and not f5["idle"] else None)
    if not main:
        return {"index": 50, "level": "NEUTRO", "why": "sin ventana activa que medir"}
    ratio = min(main["ratio"], 2.0)
    idx = 50 + 50 * (1 - ratio)
    if main["young"]:
        idx = 50 + (idx - 50) * 0.5
    hot5 = bool(f5 and not f5["renewed"] and not f5["idle"] and ((f5["state"] == "red" and f5["avail"] < 40) or f5["avail"] < 15))
    if hot5:
        idx = min(idx, 45)
    idx = int(round(max(0, min(100, idx))))
    level = next(name for lo, name in LEVELS if idx >= lo)
    if main["ratio"] < RATIO_LO:
        why = f"sobra cuota: al ritmo actual quedaría {100 - main['projected']:.0f} % sin usar al reinicio ({fmt_h(main['rem_h'])})"
    elif main["ratio"] > RATIO_HI:
        why = f"se agota en {fmt_h(main['exhaust_h'])} y faltan {fmt_h(main['rem_h'])} para el reinicio"
    else:
        why = f"ritmo justo para llegar al reinicio ({fmt_h(main['rem_h'])})"
    if main["window"] == "5h":
        why = "5 h: " + why
    if main["young"]:
        why += " (ventana joven, poco fiable)"
    if hot5:
        why += " · 5 h acelerada o casi llena: esperar al reinicio (" + fmt_h(f5["rem_h"]) + ")"
    elif f5 and (f5["renewed"] or f5["idle"]):
        why += " · 5 h libre"
    return {"index": idx, "level": level, "why": why}


def analyze(raw: dict | None, now: float | None = None) -> dict | None:
    if not raw:
        return None
    now = now or time.time()
    windows = [analyze_window(r, now) for r in raw.get("readings", [])]
    groups = {}
    for gid in dict.fromkeys(w["group"] for w in windows):
        ws = [w for w in windows if w["group"] == gid]
        wk = next((w for w in ws if w["window"] == "semanal" and not w["renewed"]), None)
        for w in ws:   # la semanal manda: lo usable de la 5 h es lo que deje la semanal
            w["eff_avail"] = min(w["avail"], wk["avail"]) if wk and w["window"] == "5h" else w["avail"]
            w["capped"] = w["eff_avail"] < w["avail"] - 1
        v = group_verdict(ws)
        groups[gid] = {"group": gid, "label": group_label(gid), **v, "bonus": BONUS[v["verdict"]], **group_index(ws, v)}
    return {"now": iso(now), "generated_at": raw.get("generated_at"), "sources": raw.get("sources", {}),
            "windows": windows, "groups": list(groups.values())}


_cache: dict = {}


def load_analysis(now: float | None = None, path: Path = QUOTA_FILE) -> dict | None:
    """Análisis de las últimas lecturas. Cachea por mtime y minuto para no releer en cada elección."""
    now = now or time.time()
    try:
        key = (str(path), Path(path).stat().st_mtime, int(now // 60))
    except OSError:
        return None
    if key not in _cache:
        _cache.clear()
        _cache[key] = analyze(load_raw(path), now)
    return _cache[key]


def advise(provider: str, table: dict, analysis: dict | None) -> dict:
    """Veredicto de cuota para un proveedor del flujo (usa `quota` de providers.json: lista de grupos)."""
    groups = (table.get(provider) or {}).get("quota") or []
    base = {"provider": provider, "verdict": "SIN_DATO", "usable": True, "reason": "sin fuente de cuota", "until": None,
            "bonus": 0.0, "discourage_heavy": False, "group": None}
    if not groups or not analysis:
        return base
    known = [g for g in analysis["groups"] if g["group"] in groups]
    if not known:
        return {**base, "reason": "sin lectura de cuota"}
    g = max(known, key=lambda x: SEVERITY[x["verdict"]])
    if g["verdict"] == "SIN_DATO":
        return {**base, "reason": g["reason"], "group": g["group"]}
    return {"provider": provider, "verdict": g["verdict"], "usable": g["verdict"] != "AGOTADA", "reason": f"{g['label']}: {g['reason']}",
            "until": g["until"], "bonus": g["bonus"], "discourage_heavy": g["verdict"] in ("AGOTADA", "FRENAR", "ESPERAR"), "group": g["group"]}


def finite(obj):
    """JSON válido: inf/nan (p. ej. razón con cuota disponible 0) pasan a None."""
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [finite(v) for v in obj]
    return obj


def api_payload(providers_file: Path, path: Path = QUOTA_FILE, now: float | None = None) -> dict:
    """Para la página :8099: análisis + qué proveedores del flujo dependen de cada grupo."""
    analysis = analyze(load_raw(path), now) or {"now": iso(now or time.time()), "generated_at": None, "sources": {}, "windows": [], "groups": []}
    try:
        table = json.loads(Path(providers_file).read_text(encoding="utf-8"))["providers"]
    except (OSError, ValueError, KeyError):
        table = {}
    for g in analysis["groups"]:
        g["providers"] = [p for p, c in table.items() if g["group"] in (c.get("quota") or []) and c.get("enabled")]
    return finite(analysis)


# ---------------------------------------------------------------- texto
def fmt_h(h: float | None) -> str:
    if h is None:
        return "—"
    total_min = round(h * 60)
    if total_min < 60:
        return f"{total_min} min"
    if total_min < 48 * 60:
        return f"{total_min // 60} h {total_min % 60} min"
    total_h = round(h)
    return f"{total_h // 24} d {total_h % 24} h"


def render_text(a: dict | None) -> str:
    if not a or not a["windows"]:
        return "Sin lecturas: corré `python3 agents/quota.py collect`."
    out = [f"Lectura {a['generated_at']} · análisis {a['now']}"]
    for name, s in a["sources"].items():
        out.append(f"  fuente {name:7} {'ok ' + str(s['at']) if s['ok'] else 'ERROR ' + str(s['error'])}")
    out.append(f"{'grupo':30} {'vent.':8} {'usado':>6} {'resta':>10} {'ritmo':>7} {'sost.':>7} {'razón':>6}  estado")
    for w in a["windows"]:
        est = "renovada" if w["renewed"] else "sin empezar" if w["idle"] else {"red": "SE AGOTA", "blue": "se pierde", "green": "sano"}[w["state"]]
        f = lambda x: "—" if x is None else f"{x:.2f}"
        out.append(f"{w['label']:30} {w['window']:8} {w['used']:5.0f}% {fmt_h(w['rem_h']):>10} {f(w['rate']):>7} {f(w['sustainable']):>7} {f(w['ratio']):>6}  {est}" + ("" if w["fresh"] else " (vencida)"))
    out.append("Veredictos:")
    for g in a["groups"]:
        out.append(f"  {g['verdict']:9} {g['label']}: {g['reason']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["collect", "show", "json"])
    args = ap.parse_args()
    if args.cmd == "collect":
        raw = collect()
        bad = [f"{k}: {v['error']}" for k, v in raw["sources"].items() if not v["ok"]]
        print(render_text(analyze(raw)))
        if bad:
            print("Fuentes con error -> " + " | ".join(bad), file=sys.stderr)
        return 0 if len(bad) < len(raw["sources"]) else 1
    a = analyze(load_raw())
    print(json.dumps(finite(a), ensure_ascii=False, indent=1) if args.cmd == "json" else render_text(a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
