#!/usr/bin/env python3
"""Registro y protocolo liviano de sesiones para agentes y sesiones humanas.

Permite:
1. Registrar cuándo se inicia y termina una sesión interactiva o de agente
   (para evitar colisiones entre sesiones en paralelo).
2. Consultar si ya existe una sesión activa o reciente sobre un tema antes
   de que el despachador autónomo (flow.py) empiece a trabajar en un ticket.
3. Buscar sesiones documentadas recientes en docs/sessions/ a lo largo del repo.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import ROOT as REPO_ROOT, RUNTIME as RUNTIME_DIR
ACTIVE_SESSIONS_FILE = RUNTIME_DIR / "active_sessions.json"
MAX_ACTIVE_AGE_HOURS = 8

STOP_WORDS = {
    "a", "al", "algo", "ante", "bajo", "cabe", "cada", "como", "con", "contra",
    "de", "del", "desde", "durante", "e", "el", "ella", "ellas", "ello", "ellos",
    "en", "entre", "era", "erais", "eran", "eras", "eres", "es", "esa", "esas",
    "ese", "eso", "esos", "esta", "estaba", "estabais", "estaban", "estabas",
    "estad", "estada", "estadas", "estado", "estados", "estamos", "estando",
    "estar", "estaremos", "estará", "estaran", "estaras", "estare", "estareis",
    "estaria", "estariais", "estariamos", "estarian", "estarias", "estas", "este",
    "estemos", "esto", "estos", "estoy", "estuve", "estuviera", "estuvierais",
    "estuvieran", "estuvieras", "estuvieron", "estuviese", "estuvieseis",
    "estuviesen", "estuvieses", "estuvimos", "estuviste", "estuvisteis",
    "estuvo", "fue", "fuera", "fuerais", "fueran", "fueras", "fueron", "fuese",
    "fueseis", "fuesen", "fueses", "fui", "fuimos", "fuiste", "fuisteis", "ha",
    "habida", "habidas", "habido", "habidos", "habiendo", "habremos", "habra",
    "habran", "habras", "habre", "habreis", "habria", "habriais", "habriamos",
    "habrian", "habrias", "habeis", "habia", "habiais", "habiamos", "habian",
    "habias", "han", "has", "hasta", "hay", "haya", "hayamos", "hayan",
    "hayas", "hayais", "he", "hemos", "hube", "hubiera", "hubierais", "hubieran",
    "hubieras", "hubieron", "hubiese", "hubieseis", "hubiesen", "hubieses",
    "hubimos", "hubiste", "hubisteis", "hubo", "la", "las", "le", "les", "lo",
    "los", "me", "mi", "mis", "mucho", "muchos", "muy", "mas", "mia", "mias",
    "mio", "mios", "nada", "ni", "no", "nos", "nosotras", "nosotros", "nuestra",
    "nuestras", "nuestro", "nuestros", "o", "os", "otra", "otras", "otro",
    "otros", "para", "pero", "poco", "por", "porque", "que", "quedo", "quien",
    "quienes", "que", "se", "sea", "seamos", "sean", "seas", "sentid", "sentida",
    "sentidas", "sentido", "sentidos", "siente", "sintiendo", "sin", "sobre",
    "sois", "somos", "son", "soy", "su", "sus", "suya", "suyas", "suyo",
    "suyos", "si", "tambien", "tanto", "te", "tendremos", "tendra", "tendran",
    "tendras", "tendre", "tendreis", "tendria", "tendriais", "tendriamos",
    "tendrian", "tendrias", "tened", "tenemos", "tenga", "tengamos", "tengan",
    "tengas", "tengo", "tengais", "tenida", "tenidas", "tenido", "tenidos",
    "teniendo", "teneis", "tenia", "teniais", "teniamos", "tenian", "tenias",
    "ti", "tiene", "tienen", "tienes", "todo", "todos", "tu", "tus", "tuve",
    "tuviera", "tuvierais", "tuvieran", "tuvieras", "tuvieron", "tuviese",
    "tuvieseis", "tuviesen", "tuvieses", "tuvimos", "tuviste", "tuvisteis",
    "tuvo", "tuya", "tuyas", "tuyo", "tuyos", "tu", "un", "una", "unas", "uno",
    "unos", "vosotras", "vosotros", "vuestra", "vuestras", "vuestro", "vuestros",
    "y", "ya", "yo", "el", "eramos"
}


def normalize_text(text: str) -> str:
    """Elimina acentos y pasa a minúsculas para comparaciones semánticas robustas."""
    norm = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in norm if not unicodedata.combining(c))


def tokenize(text: str) -> set[str]:
    """Extrae palabras clave normalizadas ignorando stop words y caracteres especiales."""
    normalized = normalize_text(text)
    words = re.findall(r"[a-z0-9_]{3,}", normalized)
    # Conservamos palabras de 3+ letras y números de 4+ dígitos (ej: 8094, 8090, v13, v15)
    return {
        w for w in words
        if w not in STOP_WORDS and not (w.isdigit() and len(w) < 4)
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _read_registry(file_path: Path = ACTIVE_SESSIONS_FILE) -> dict[str, Any]:
    if not file_path.is_file():
        return {"sessions": {}}
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}


def _write_registry(data: dict[str, Any], file_path: Path = ACTIVE_SESSIONS_FILE) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = file_path.with_suffix(".lock")
    with open(lock_file, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tmp = file_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, file_path)


def extract_ticket_id(text: str) -> str:
    """Extrae un identificador de ticket tipo VIK-123 de un texto si existe."""
    m = re.search(r"\bVIK-\d+\b", text, re.IGNORECASE)
    return m.group(0).upper() if m else ""


def format_session_title(topic: str, ticket_id: str = "", status: str = "Active") -> str:
    """Normaliza el título de la sesión con tags tipo [VIK-##][Status] Tema."""
    clean_topic = topic.strip()
    tid = (ticket_id or extract_ticket_id(clean_topic)).upper()
    st = status.capitalize()

    # Si ya tiene [VIK-..], normalizamos reemplazando o asignando el status solicitado
    m = re.match(r"^\[(VIK-\d+)\](?:\s*\[([^\]]+)\])?\s*(.*)$", clean_topic, re.IGNORECASE)
    if m:
        t_id = m.group(1).upper()
        rest = m.group(3).strip()
        return f"[{t_id}][{st}] {rest}".strip() if rest else f"[{t_id}][{st}]"

    if tid:
        rest = re.sub(rf"^{re.escape(tid)}[:\s-]*", "", clean_topic, flags=re.IGNORECASE).strip()
        return f"[{tid}][{st}] {rest}".strip() if rest else f"[{tid}][{st}]"

    return clean_topic


def start_session(
    topic: str,
    component: str = "",
    provider: str = "human",
    ticket_id: str = "",
    registry_file: Path = ACTIVE_SESSIONS_FILE,
) -> dict[str, Any]:
    """Registra una nueva sesión activa."""
    data = _read_registry(registry_file)
    sessions = data.setdefault("sessions", {})
    now = _iso_now()
    session_id = f"ses_{int(time.time())}_{provider[:10]}"
    tid = (ticket_id or extract_ticket_id(topic)).strip().upper()
    tagged = format_session_title(topic, ticket_id=tid, status="Active")
    entry = {
        "id": session_id,
        "topic": topic.strip(),
        "tagged_title": tagged,
        "component": component.strip(),
        "ticket_id": tid,
        "provider": provider.strip().lower(),
        "status": "active",
        "started_at": now,
        "updated_at": now,
        "notes": "",
    }
    sessions[session_id] = entry
    _write_registry(data, registry_file)
    return entry


def heartbeat_session(
    session_id: str,
    notes: str = "",
    registry_file: Path = ACTIVE_SESSIONS_FILE,
) -> bool:
    """Actualiza la marca de tiempo de una sesión activa."""
    data = _read_registry(registry_file)
    if session_id in data.get("sessions", {}):
        data["sessions"][session_id]["updated_at"] = _iso_now()
        if notes:
            data["sessions"][session_id]["notes"] = notes.strip()
        _write_registry(data, registry_file)
        return True
    return False


def stop_session(
    session_id: str = "",
    outcome: str = "done",
    notes: str = "",
    registry_file: Path = ACTIVE_SESSIONS_FILE,
) -> dict[str, Any] | None:
    """Cierra una sesión activa."""
    data = _read_registry(registry_file)
    sessions = data.setdefault("sessions", {})
    target_id = session_id
    if not target_id:
        active = [s for s in sessions.values() if s.get("status") == "active"]
        if active:
            active.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
            target_id = active[0]["id"]
    if target_id and target_id in sessions:
        entry = sessions[target_id]
        entry["status"] = outcome
        entry["closed_at"] = _iso_now()
        entry["tagged_title"] = format_session_title(entry.get("topic", ""), ticket_id=entry.get("ticket_id", ""), status=outcome)
        if notes:
            entry["notes"] = notes.strip()
        _write_registry(data, registry_file)
        return entry
    return None


def get_active_sessions(
    registry_file: Path = ACTIVE_SESSIONS_FILE,
    max_age_hours: float = MAX_ACTIVE_AGE_HOURS,
) -> list[dict[str, Any]]:
    """Devuelve las sesiones activas no vencidas."""
    data = _read_registry(registry_file)
    sessions = data.get("sessions", {})
    active = []
    now_ts = time.time()
    for s in sessions.values():
        if s.get("status") != "active":
            continue
        try:
            up_dt = datetime.fromisoformat(s.get("updated_at") or s.get("started_at"))
            age_h = (now_ts - up_dt.timestamp()) / 3600.0
            if age_h <= max_age_hours:
                active.append(s)
        except (ValueError, TypeError):
            active.append(s)
    return active


def find_recent_session_files(repo_root: Path = REPO_ROOT, max_age_days: int = 7) -> list[dict[str, Any]]:
    """Escanea los archivos de sesión docs/sessions/*.md del repositorio creados recientemente."""
    results = []
    now_ts = time.time()
    cutoff_ts = now_ts - (max_age_days * 86400)
    for p in repo_root.glob("**/docs/sessions/*.md"):
        if ".runtime" in p.parts:
            continue
        try:
            st = p.stat()
            if st.st_mtime < cutoff_ts:
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            # Extraer título primera línea: # YYYY-MM-DD — <Título>
            title = ""
            for line in content.splitlines()[:5]:
                if line.startswith("# "):
                    title = line.removeprefix("# ").strip()
                    break
            # Extraer qué se hizo o contexto breve
            summary = ""
            m_ctx = re.search(r"##\s+(?:Contexto|Qué se hizo)\s*\n+(.*?)(?=\n##|\Z)", content, re.S)
            if m_ctx:
                summary = " ".join(m_ctx.group(1).split())[:250]
            rel_path = p.relative_to(repo_root).as_posix()
            comp_dir = p.parent.parent
            if comp_dir.name == "docs":
                comp_dir = comp_dir.parent
            component = comp_dir.relative_to(repo_root).as_posix()
            results.append({
                "path": rel_path,
                "component": component,
                "title": title or p.stem,
                "summary": summary,
                "mtime": st.st_mtime,
                "tokens": tokenize(title + " " + summary + " " + rel_path),
            })
        except OSError:
            continue
    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


def find_related(
    query_text: str,
    component_hint: str = "",
    repo_root: Path = REPO_ROOT,
    registry_file: Path = ACTIVE_SESSIONS_FILE,
    max_age_days: int = 7,
) -> dict[str, list[dict[str, Any]]]:
    """Busca sesiones activas e históricas que coincidan temáticamente con el query."""
    q_tokens = tokenize(query_text)
    q_ticket = extract_ticket_id(query_text)
    if component_hint:
        q_tokens |= tokenize(component_hint)
    
    # 1. Sesiones activas coincidentes
    active_matches = []
    for s in get_active_sessions(registry_file):
        s_ticket = (s.get("ticket_id") or extract_ticket_id(s.get("topic", ""))).upper()
        ticket_match = bool(q_ticket and s_ticket and q_ticket == s_ticket)
        s_tokens = tokenize(s.get("topic", "") + " " + s.get("component", "") + " " + s.get("ticket_id", ""))
        overlap = q_tokens & s_tokens
        comp_match = bool(component_hint and s.get("component") and component_hint == s.get("component"))
        if ticket_match or len(overlap) >= 2 or comp_match or (len(overlap) >= 1 and len(q_tokens) <= 3):
            active_matches.append({
                **s,
                "matched_tokens": list(overlap),
                "is_component_match": comp_match,
                "is_ticket_match": ticket_match,
            })
            
    # 2. Sesiones históricas recientes
    recent_matches = []
    for doc in find_recent_session_files(repo_root, max_age_days=max_age_days):
        doc_ticket = extract_ticket_id(doc["title"] + " " + doc["path"])
        ticket_match = bool(q_ticket and doc_ticket and q_ticket == doc_ticket)
        overlap = q_tokens & doc["tokens"]
        comp_match = bool(component_hint and doc["component"] and (component_hint in doc["component"] or doc["component"] in component_hint))
        score = (len(overlap) * 2) + (5 if ticket_match else 0) + (3 if comp_match else 0)
        if ticket_match or score >= 3:
            recent_matches.append({
                "path": doc["path"],
                "component": doc["component"],
                "title": doc["title"],
                "summary": doc["summary"],
                "score": score,
                "matched_tokens": list(overlap),
                "is_ticket_match": ticket_match,
            })
    recent_matches.sort(key=lambda x: x["score"], reverse=True)
    
    return {
        "active": active_matches,
        "recent": recent_matches[:5],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    p_start = sub.add_parser("start", help="Iniciar registro de una sesión")
    p_start.add_argument("topic", help="Tema o descripción breve de la sesión (ej. '[VIK-11] Sincronización')")
    p_start.add_argument("--component", default="", help="Componente del repo")
    p_start.add_argument("--provider", default="human", help="Proveedor (claude, antigravity, codex, human)")
    p_start.add_argument("--ticket", default="", help="ID de ticket (ej. VIK-10)")
    
    p_stop = sub.add_parser("stop", help="Cerrar una sesión activa")
    p_stop.add_argument("--id", default="", help="ID de sesión (opcional: última por defecto)")
    p_stop.add_argument("--outcome", default="done", choices=["done", "abandoned"], help="Resultado")
    p_stop.add_argument("--notes", default="", help="Resumen o notas de cierre")
    
    sub.add_parser("list", help="Listar sesiones activas")
    
    p_check = sub.add_parser("check", help="Buscar sesiones relacionadas con un texto")
    p_check.add_argument("text", help="Texto del ticket o tarea a verificar")
    p_check.add_argument("--component", default="", help="Componente opcional")
    p_check.add_argument("--days", type=int, default=7, help="Días de antigüedad máxima")
    
    args = parser.parse_args()
    
    if args.cmd == "start":
        res = start_session(args.topic, component=args.component, provider=args.provider, ticket_id=args.ticket)
        tag_disp = res.get('tagged_title') or res['topic']
        print(f"✅ Sesión iniciada: {res['id']} | {res['provider']} | {tag_disp}")
    elif args.cmd == "stop":
        res = stop_session(args.id, outcome=args.outcome, notes=args.notes)
        if res:
            tag_disp = res.get('tagged_title') or res['topic']
            print(f"🛑 Sesión finalizada: {res['id']} ({res['status']}) - {tag_disp}")
        else:
            print("No se encontró sesión activa para cerrar.")
    elif args.cmd == "list":
        active = get_active_sessions()
        if not active:
            print("No hay sesiones activas registradas.")
        else:
            print(f"Sesiones activas ({len(active)}):")
            for s in active:
                tag_disp = s.get('tagged_title') or s['topic']
                print(f"  - [{s['id']}] {s['provider']}: \"{tag_disp}\" (iniciada: {s['started_at']}, comp: {s.get('component') or '-'})")
    elif args.cmd == "check":
        res = find_related(args.text, component_hint=args.component, max_age_days=args.days)
        print(f"--- Búsqueda para: \"{args.text}\" ---")
        if res["active"]:
            print(f"⚠ Sesiones ACTIVAS encontradas ({len(res['active'])}):")
            for a in res["active"]:
                print(f"  - {a['provider']}: \"{a['topic']}\" (id: {a['id']}, comp: {a.get('component') or '-'})")
        else:
            print("Ninguna sesión activa en conflicto.")
        if res["recent"]:
            print(f"📄 Sesiones RECIENTES relacionadas ({len(res['recent'])}):")
            for r in res["recent"]:
                print(f"  - {r['title']} ({r['path']}) [coincidencias: {', '.join(r['matched_tokens'])}]")
                if r['summary']:
                    print(f"    Resumen: {r['summary'][:120]}...")
        else:
            print("No se encontraron sesiones recientes con superposición temática.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
