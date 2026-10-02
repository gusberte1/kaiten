# kaiten

Motor de flujo agéntico: ticket → un agente implementa en un worktree → revisión cruzada por otro proveedor → merge a `dev` → `main` solo con aprobación humana.

## Principios

- **Motor / adaptadores / proyecto.** El motor (`agents/flow.py`) es portable. Los adaptadores (`providers.json`, tracker) se reconfiguran. Lo del proyecto vive en `flow-project.json`.
- **Tracker intercambiable.** El flujo habla con la interfaz `Tracker` (`agents/tracker.py`), no con un gestor concreto. Hoy hay `VikunjaTracker`; Jira, Trello, etc. se agregan como adaptadores en `tracker.KINDS`. El flujo corre independiente del tracker.
- **Mismo host, código separado.** El motor evoluciona aquí y cada proyecto lo consume; no es un servicio remoto.

## Estado

Extracción inicial (snapshot, sin historial) desde sushi-inspector. La raíz del proyecto se resuelve con la variable `KAITEN_PROJECT` (por defecto, el padre de `agents/`). Pendiente: quitar rutas fijas restantes (`scripts/notificar.py` en budget.py, `queue.json`/`current_agent` junto al motor).

Ver `agents/FLOW.md`, y `flow-project.example.json` / `agents/flow_config.example.json` como plantillas.
