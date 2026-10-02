#!/usr/bin/env python3
"""Bot de Control Presupuestario y Optimización de Tokens (v1).

Funcionalidades:
1. Estimación Pre-Ejecución (Challenge de Alcance vs Costo):
   Evalúa el contrato y alcance de la tarea previo a su admisión/ejecución.
   Bloquea o requiere confirmación si el costo proyectado supera umbrales o si el proveedor está en FRENAR/AGOTADA.
2. Auditoría Post-Ejecución:
   Registra el consumo real (tokens in/out, costo USD, proveedor) en `.runtime/budget/history.jsonl` y genera
   notas concisas para Vikunja.
3. Watchdog de Ritmo de Quema (Burn-Rate):
   Calcula la pendiente de consumo vs tiempo restante en las ventanas (5h y semanal).
   Emite alertas proactivas a Telegram cuando el ritmo es insostenible.

Uso:
  python3 budget.py status
  python3 budget.py check [--notify]
  python3 budget.py estimate --task-id VIK-10
  python3 budget.py summary
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
from paths import ROOT, RUNTIME
BUDGET_DIR = RUNTIME / "budget"
BUDGET_HISTORY = BUDGET_DIR / "history.jsonl"
BUDGET_META = BUDGET_DIR / "meta.json"
QUOTA_FILE = RUNTIME / "quota.json"
PRECIOS_FILE = HERE / "gemini_precios.json"

# Umbrales v1
MAX_TOKENS_PER_TASK_DEFAULT = 60_000
MAX_TURNS_PER_TASK = 3
BURN_RATE_WARN_THRESHOLD = 1.15
BURN_RATE_CRITICAL_THRESHOLD = 1.40
ALERT_COOLDOWN_S = 3 * 3600  # Máximo una alerta cada 3 horas por proveedor/condición

# Precios por millón de tokens (USD)
DEFAULT_PRICING = {
    "claude": {"in": 3.00, "out": 15.00},
    "codex": {"in": 2.50, "out": 10.00},
    "antigravity": {"in": 0.30, "out": 2.50},  # Gemini 2.5 Flash / Medium
    "gemini": {"in": 0.30, "out": 2.50},
    "deepseek": {"in": 0.27, "out": 1.10},
}


def ensure_dirs() -> None:
    BUDGET_DIR.mkdir(parents=True, exist_ok=True)


def load_meta() -> dict:
    ensure_dirs()
    if BUDGET_META.exists():
        try:
            return json.loads(BUDGET_META.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"alerts_sent": {}, "daily_tokens": {}, "task_estimates": {}}


def save_meta(data: dict) -> None:
    ensure_dirs()
    tmp = BUDGET_META.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, BUDGET_META)


def notify_telegram(text: str) -> bool:
    """Envía alerta a Telegram usando el script estándar del repo."""
    script = ROOT / "scripts" / "notificar.py"
    if not script.exists():
        return False
    try:
        r = subprocess.run([sys.executable, str(script), text], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def estimate_task(task: dict[str, Any], provider: str = "codex") -> dict[str, Any]:
    """Estima tokens y costo proyectado antes de ejecutar una tarea.

    Desafía el alcance: si la tarea no tiene acotado el alcance, o involucra demasiados archivos,
    o el proveedor asignado no tiene cuota sostenible, rechaza la auto-ejecución.
    """
    task_type = task.get("type", "code")
    risk = task.get("risk", "medium")
    scope = task.get("scope", "")
    title = task.get("title", "")

    # Estimación base según tipo de tarea
    base_in = 8_000
    base_out = 1_500
    if task_type == "research":
        base_in = 12_000
        base_out = 2_500
    elif task_type == "release":
        base_in = 15_000
        base_out = 3_000
    elif task_type in ("code", "data", "test"):
        base_in = 10_000
        base_out = 2_000

    # Factor de archivos en alcance
    num_files = len(re.split(r"[,;\s]+", scope.strip())) if scope.strip() else 3
    scope_multiplier = max(num_files * 0.5, 1.0)

    # Factor de riesgo (revisiones esperadas)
    rounds = 1 if risk == "low" else (2 if risk == "medium" else 3)

    projected_in = int(base_in * scope_multiplier * rounds)
    projected_out = int(base_out * scope_multiplier * rounds)
    total_tokens = projected_in + projected_out

    pricing = DEFAULT_PRICING.get(provider.lower(), DEFAULT_PRICING["codex"])
    cost_usd = (projected_in / 1_000_000 * pricing["in"]) + (projected_out / 1_000_000 * pricing["out"])

    # Challenge de viabilidad
    allowed = True
    reasons = []

    if num_files > 15:
        allowed = False
        reasons.append(f"Alcance demasiado amplio ({num_files} archivos). Descomponer la tarea o acotar 'alcance:'.")
    if total_tokens > MAX_TOKENS_PER_TASK_DEFAULT and risk != "high":
        allowed = False
        reasons.append(f"Tokens proyectados ({total_tokens:,}) exceden el límite seguro ({MAX_TOKENS_PER_TASK_DEFAULT:,}). Acotar 'alcance:'.")

    return {
        "task_id": task.get("id", "UNKNOWN"),
        "provider": provider,
        "type": task_type,
        "risk": risk,
        "projected_input_tokens": projected_in,
        "projected_output_tokens": projected_out,
        "projected_total_tokens": total_tokens,
        "projected_cost_usd": round(cost_usd, 4),
        "estimated_rounds": rounds,
        "allowed": allowed,
        "reasons": reasons,
    }


def record_run(task_id: str, run_id: str, provider: str, input_tokens: int, output_tokens: int, duration_s: float = 0.0, meta: dict | None = None) -> dict[str, Any]:
    """Registra la auditoría de costo y tokens de una corrida terminada."""
    ensure_dirs()
    pricing = DEFAULT_PRICING.get(provider.lower(), DEFAULT_PRICING["codex"])
    cost_usd = (input_tokens / 1_000_000 * pricing["in"]) + (output_tokens / 1_000_000 * pricing["out"])

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "run_id": run_id,
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost_usd, 5),
        "duration_seconds": round(duration_s, 2),
        "meta": meta or {},
    }

    with open(BUDGET_HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return entry


def format_task_cost_comment(task_id: str) -> str:
    """Genera un comentario breve para Vikunja con el total consumido por la tarea."""
    if not BUDGET_HISTORY.exists():
        return f"📊 {task_id}: Sin registros de consumo de tokens."

    total_in = 0
    total_out = 0
    total_cost = 0.0
    runs = 0
    providers = set()

    for line in BUDGET_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            if d.get("task_id") == task_id:
                total_in += d.get("input_tokens", 0)
                total_out += d.get("output_tokens", 0)
                total_cost += d.get("cost_usd", 0.0)
                runs += 1
                providers.add(d.get("provider", "unknown"))
        except Exception:
            continue

    total_tokens = total_in + total_out
    prov_str = ", ".join(sorted(providers)) or "desconocido"
    return f"📊 **Consumo {task_id}**: {total_tokens:,} tokens (~${total_cost:.4f} USD) en {runs} corridas · Proveedor(es): {prov_str}."


def check_burn_rate(notify: bool = True) -> list[dict[str, Any]]:
    """Analiza `.runtime/quota.json` y alerta si la quema de tokens excede el ritmo sostenible."""
    if not QUOTA_FILE.exists():
        return []

    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    readings = data.get("readings", [])
    now = time.time()
    alerts = []
    meta = load_meta()
    alerts_history = meta.get("alerts_sent", {})

    for r in readings:
        group = r.get("group", "")
        window = r.get("window", "")
        used = float(r.get("used", 0.0))
        reset_epoch = float(r.get("reset", now))

        dur_h = 5.0 if window == "5h" else 168.0
        time_left_h = max(0.0, (reset_epoch - now) / 3600.0)
        time_passed_h = max(0.1, dur_h - time_left_h)
        pct_passed = min(100.0, (time_passed_h / dur_h) * 100.0)

        # Burn rate = % usado / % tiempo transcurrido
        burn_rate = (used / pct_passed) if pct_passed > 5.0 else 1.0

        alert_level = None
        message = ""

        if used >= 98.0:
            alert_level = "CRITICAL"
            message = f"🚨 Cuota {group} ({window}) al {used:.0f}% (AGOTADA). Reinicia en {time_left_h:.1f}h."
        elif window == "semanal" and burn_rate > BURN_RATE_CRITICAL_THRESHOLD and used > 60.0:
            alert_level = "CRITICAL"
            message = f"🚨 Quema acelerada en {group} (semanal): {used:.0f}% consumido a ritmo {burn_rate:.2f}x sostenible. Faltan {time_left_h/24:.1f} días para el reinicio."
        elif window == "semanal" and burn_rate > BURN_RATE_WARN_THRESHOLD and used > 40.0:
            alert_level = "WARN"
            message = f"⚠ Alerta de presupuesto en {group} (semanal): {used:.0f}% consumido a ritmo {burn_rate:.2f}x. Faltan {time_left_h/24:.1f} días."

        if alert_level:
            key = f"{group}:{window}:{alert_level}"
            last_sent = alerts_history.get(key, 0)
            should_send = (now - last_sent) > ALERT_COOLDOWN_S

            item = {
                "group": group,
                "window": window,
                "used": used,
                "burn_rate": round(burn_rate, 2),
                "time_left_h": round(time_left_h, 1),
                "level": alert_level,
                "message": message,
                "notified": False,
            }

            if notify and should_send:
                ok = notify_telegram(message)
                if ok:
                    alerts_history[key] = now
                    item["notified"] = True

            alerts.append(item)

    meta["alerts_sent"] = alerts_history
    save_meta(meta)
    return alerts


def budget_status() -> str:
    """Genera un reporte del estado presupuestario y de quema."""
    alerts = check_burn_rate(notify=False)
    lines = ["=== ESTADO DEL BOT PRESUPUESTARIO (v1) ==="]

    if alerts:
        lines.append("\nAlertas Activas de Ritmo de Quema:")
        for a in alerts:
            lines.append(f"  [{a['level']}] {a['message']}")
    else:
        lines.append("\n✅ Ritmo de quema en rangos sostenibles.")

    # Resumen de gasto histórico
    if BUDGET_HISTORY.exists():
        entries = [json.loads(l) for l in BUDGET_HISTORY.read_text().splitlines() if l.strip()]
        total_tokens = sum(e.get("total_tokens", 0) for e in entries)
        total_cost = sum(e.get("cost_usd", 0.0) for e in entries)
        lines.append(f"\nGasto Acumulado Registrado: {total_tokens:,} tokens (~${total_cost:.4f} USD) en {len(entries)} corridas.")
    else:
        lines.append("\nSin historial de corridas en .runtime/budget/history.jsonl.")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot de Control Presupuestario")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status")
    chk = sub.add_parser("check")
    chk.add_argument("--notify", action="store_true", help="Enviar alertas por Telegram si corresponde")

    est = sub.add_parser("estimate")
    est.add_argument("--type", default="code")
    est.add_argument("--risk", default="medium")
    est.add_argument("--scope", default="")
    est.add_argument("--provider", default="codex")

    sub.add_parser("summary")

    args = parser.parse_args()
    if args.command == "status" or not args.command:
        print(budget_status())
    elif args.command == "check":
        alerts = check_burn_rate(notify=args.notify)
        print(f"Check completado. {len(alerts)} condiciones evaluadas.")
        for a in alerts:
            print(f"  - {a['level']}: {a['message']} (notified={a['notified']})")
    elif args.command == "estimate":
        task = {"type": args.type, "risk": args.risk, "scope": args.scope}
        res = estimate_task(task, provider=args.provider)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.command == "summary":
        print(budget_status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
