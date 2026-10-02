"""Interfaz neutral con el sistema de tickets (Vikunja, Jira, Trello, ...).

El flujo sólo habla con este contrato; cada sistema se implementa en su propio
módulo y se elige con `"tracker": {"kind": ...}` en flow_config.json.
Formas normalizadas (el adaptador traduce desde/hacia su API):
  ticket     {"id", "title", "description", "done", "priority", "assignees": [usuario, ...]}
  comentario {"id", "author": usuario, "text"}   (ids crecientes por ticket)
"""
from __future__ import annotations

import importlib
from typing import Any, Protocol

KINDS = {"vikunja": ("vikunja_adapter", "VikunjaTracker")}


class Tracker(Protocol):
    humans: set[str]   # usuarios (minúsculas) cuyos comentarios cuentan como órdenes
    bots: set[str]     # usuarios (minúsculas) a los que se asignan tickets para el flujo

    def tasks(self) -> list[dict]: ...
    def comments(self, tid: int) -> list[dict]: ...
    def comment(self, tid: int, text: str) -> dict: ...                 # devuelve {"id": ...}
    def link(self, tid: int, comment_id: int | None = None) -> str: ...
    def close(self, tid: int) -> None: ...
    def columns(self) -> list[str]: ...                                 # columnas del tablero, por título
    def setup_columns(self, wanted: list[str], rename: dict[str, str]) -> None: ...
    def move(self, tid: int, column: str) -> None: ...


def load_tracker(cfg: dict[str, Any] | None) -> Tracker | None:
    """None si el tracker está ausente o deshabilitado: el flujo corre igual con tareas locales."""
    if not cfg or not cfg.get("enabled"):
        return None
    try:
        module, cls = KINDS[cfg["kind"]]
    except KeyError as exc:
        raise ValueError(f"tracker desconocido: {cfg.get('kind')!r} (disponibles: {', '.join(KINDS)})") from exc
    return getattr(importlib.import_module(module), cls)(cfg)
