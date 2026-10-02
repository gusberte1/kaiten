# kaiten

Motor de flujo agéntico: ticket → un agente implementa en un worktree → revisión cruzada por otro proveedor → merge a `dev` → `main` solo con aprobación humana.

## Principios

- **Motor / adaptadores / proyecto.** El motor (este repo) es portable. Los adaptadores (`providers.json`, tracker) se reconfiguran. Lo del proyecto vive en el proyecto.
- **Tracker intercambiable.** El flujo habla con la interfaz `Tracker` (`tracker.py`), no con un gestor concreto. Hoy hay `VikunjaTracker`; Jira, Trello, etc. se agregan como adaptadores en `tracker.KINDS`. El flujo corre independiente del tracker.
- **Mismo host, código separado.** El motor evoluciona aquí y cada proyecto lo consume; no es un servicio remoto.

## Uso en un proyecto

El motor se incluye en `<proyecto>/agents/` (recomendado: `git subtree add --prefix=agents <url-de-kaiten> main`). El proyecto aporta:

- `flow-project.json` en su raíz: ramas, guardas (`protected_paths`, prefijos de test), `hooks_dir`, componentes, notificación.
- `flow/` en su raíz: `team.json`, `providers.json`, `orchestration_policy.json`, `orchestration_roles.json`, `flow_config.json`, `hooks/`.

`examples/` trae plantillas de ambos. La raíz del proyecto se resuelve con `KAITEN_PROJECT` (por defecto, el padre de la carpeta del motor).

## Estado

Extraído de sushi-inspector. Pendiente: `queue.json` y `current_agent` aún se guardan junto al motor, y `budget.py` invoca `scripts/notificar.py` directo.

Ver `FLOW.md`.
