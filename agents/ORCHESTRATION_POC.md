# POC — orquestación auditable de agentes

Esta POC crea la capa de política local que falta entre el backlog humano y los
proveedores de agentes. No instala Vikunja, no ejecuta proveedores y no opera
producción.

## Qué prueba

- Contrato de tarea: ID externo (`VIK-123`), componente, tipo, riesgo y acciones protegidas.
- Ejecución trazable: `run_id`, proveedor, rol, referencia de sesión opaca, rama y worktree.
- Roles con permisos de tipo de tarea, separados de los proveedores.
- Evidencia mínima por tipo/riesgo y estados con revisión humana explícita.

## Estados

`ready → executing → validated | human_review → approved → done`

Una decisión humana es necesaria para riesgo medio/alto o una acción protegida
(`deploy`, `hardware`, `systemd`, `cron`, `secret`, `destructive`). El agente
no puede aprobarse a sí mismo.

## Uso local

El estado runtime queda fuera de Git por defecto; los argumentos permiten usar
un archivo temporal durante pruebas.

```bash
python3 agents/orchestrator.py add-task VIK-123 \
  --title 'Agregar healthcheck' --component agents --type code --risk medium
python3 agents/orchestrator.py start-run VIK-123 \
  --provider codex --role implementer --session-ref opaque-session-ref \
  --branch codex/dev/VIK-123 --worktree /tmp/vik-123
```

El `run_id` que imprime `start-run` se registra en trailers de commit:

```text
Task: VIK-123
Agent-Provider: codex
Agent-Role: implementer
Agent-Run-Id: run_...
Provider-Session-Ref: opaque-session-ref
```

## Puente a Vikunja (siguiente fase)

Vikunja será la fuente de verdad de intención humana: tarea, prioridad,
contrato y aprobación. Un adaptador recibirá su webhook firmado, traducirá el
ticket a `add-task`, y escribirá al ticket sólo eventos sanitizados (run, rama,
commit, tests, estado). Los agentes tendrán acceso MCP de lectura; las
mutaciones de backlog pasarán por el controlador.
