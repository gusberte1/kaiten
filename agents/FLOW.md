# Flujo autónomo multi-proveedor (Vikunja → agentes → merge)

Estado al 2026-09-26: **construido y probado con proveedores falsos y con JEV real; sin habilitar** y
**sin prueba con agentes reales todavía**. Para probar y habilitar (los corre una persona, no Claude):

```
bash /home/admin/Documents/sushi-inspector/agents/scripts/probar_proveedores.sh     # prueba real, informe en .runtime/smoke/
bash /home/admin/Documents/sushi-inspector/agents/scripts/instalar_flujo.sh         # instala y arranca el servicio
bash /home/admin/Documents/sushi-inspector/agents/scripts/permitir_flujo.sh         # (opcional) reglas de permiso de sólo lectura para Claude Code
```

## Cómo funciona

```
Vikunja (ticket asignado a un bot + contrato) ─► intake
  ready ─► IMPLEMENTA proveedor A (worktree aislado agent/<tarea>, autonomía plena)
        ─► controlador: commit, tests propios, guardas (alcance, secretos, tamaño, paths protegidos, hooks)
        ─► REVISA proveedor B ≠ A (solo lectura; se verifica que no tocó nada) → approve | changes | escalate
             changes → vuelve al implementador (máx. 3 rondas) · high → 2 revisores distintos
        ─► merge --no-ff a **dev** (riesgo low/medium) ─► ticket cerrado ─► muestra de auditoría
dev ─► main sólo con `flow.py release --approve` (decisión tuya)
```

**Ramas:** cada ticket trabaja en `agent/<TAREA>` (worktree propio); se integra en `dev` desde un worktree de
integración, así que **nunca toca tu checkout** aunque esté sucio. Antes de integrar se trae `main` a `dev`.
`flow.py release` muestra qué tickets hay sin publicar; `--approve` mergea `dev` en `main` y etiqueta
`release-<fecha>`. Falta (cuando quieras): PR por ticket en GitHub (no hay `gh` instalado).

**Te suman sólo si:** riesgo `high`, acción protegida (`deploy hardware systemd cron secret destructive`) o
toca paths protegidos, un revisor escala o marca `critical`, 3 rondas sin acuerdo, 3 ejecuciones
fallidas, posible secreto en el diff, revisor que modificó archivos, un agente escribe `ESCALAR: motivo`,
o un hook devuelve 2. Aviso por Telegram + comentario en el ticket. Respondés en el ticket con
`/aprobar`, `/rechazar motivo`, `/reintentar guía` (sólo cuentan los comentarios de `human_usernames`) o con
`python3 agents/flow.py decide TAREA approve|reject|retry "nota"`.

## Contrato del ticket (descripción en Vikunja)

```
agente: listo
tipo: code            # opcional: si falta lo propone JEV (code|data|test|research|security|release)
riesgo: low           # opcional: si falta lo propone JEV; JEV sólo puede subirlo (low|medium|high)
componente: tickets/2026.09.03_xxx
test: python3 -m unittest discover -s tickets/2026.09.03_xxx    # prefijos permitidos en la política
acciones: systemd     # opcional: fuerza decisión humana
alcance: datos/x/**   # opcional: paths extra permitidos
proveedor: gemini     # opcional: preferido para implementar
timeout: 60           # opcional, minutos
sin-merge: si         # opcional: no mergear solo
```
Se toma un ticket **no terminado, asignado a un bot** (`bot_usernames` en la sección `tracker` de `flow_config.json`) que tenga
`agente: listo`. Los demás se ignoran (los tickets viejos VIK-POC no se tocan).
Borrar un ticket en Vikunja no cancela lo ya tomado: usá `flow.py decide TAREA reject`.

## Criterios de diseño: motor, adaptadores y proyecto

- **Tracker intercambiable.** El flujo no habla con Vikunja sino con la interfaz `Tracker` (`tracker.py`: `tasks`, `comments`, `comment`, `link`, `close`, `columns`, `setup_columns`, `move`, más `humans`/`bots`).
  Vikunja es una implementación (`VikunjaTracker` en `vikunja_adapter.py`); se elige con `"tracker": {"kind": ...}` en `flow_config.json`.
  Objetivo: poder adaptarlo a otros gestores de tickets (Jira, Trello, etc.) escribiendo un adaptador nuevo y registrándolo en `tracker.KINDS`, sin tocar `flow.py`.
  Por ahora solo existe Vikunja, pero el flujo corre independiente del tracker (sin `tracker` opera con tareas locales).
- **Lógica del proyecto separada del motor.** Todo lo que es del proyecto vive en `flow-project.json` (raíz del repo): ramas, guardas (`protected_paths`, prefijos de test permitidos), `hooks_dir`, email de git, mapa de componentes, comando de notificación.
  `orchestration_policy.json` conserva solo la política del motor (riesgos, cuotas, límites). Mismo host, código separado: el motor puede mejorar en un proyecto y llevarse a otro copiando el motor y escribiendo su propio `flow-project.json`.

## Proveedores
`providers.json`: una entrada por proveedor (argv de implementar y de revisar). Claude, Codex y Gemini
habilitados; DeepSeek (aider) deshabilitado hasta instalar el harness. Sumar otro = copiar una entrada.
Cuota agotada → cooldown de 5 h y se usa otro. Si no hay revisor independiente disponible la tarea
**espera**: nunca se autorrevisa.

Antigravity es un proveedor secundario experimental de Sashimi. Usa la CLI
oficial `agy -p`, que ejecuta un prompt y termina; el proveedor pide
`--output-format json` para conservar el resultado estructurado en stdout y
el driver genérico guarda diagnósticos en stderr por separado. Permanece
`enabled: false` hasta un smoke real. No lee ni hereda API keys: requiere una
sesión de cuenta Google ya autenticada por `agy`. `--mode=plan` no es un límite
de sólo lectura, por lo que el controlador también verifica el árbol del
revisor.

Para ejecutar el smoke después de autenticar `agy` interactivamente, usar
`bash /home/admin/Documents/sushi-inspector/agents/scripts/probar_antigravity.sh`.

## Cuotas de los proveedores (`quota.py`) — se consultan antes de asignar
Un timer de usuario (`sushi-quota.timer`, cada 15 min; instalar con `bash agents/scripts/instalar_cuotas.sh`)
corre `quota.py collect`: lee `claude -p "/usage"`, `agy -p "/usage"` y el último `rate_limits` de los rollouts
de Codex, y escribe `.runtime/quota.json` (última lectura por ventana; si una fuente falla se conserva la anterior
y queda marcada) y `.runtime/quota-history.jsonl` (una línea por corrida, para medir la conversión 5 h → semanal).
El % disponible y la hora de reinicio se guardan crudos; tiempos restantes, ritmo y razón se recalculan al leer.

Por ventana: `ritmo = usado / tiempo transcurrido`, `tope = disponible / tiempo restante`, `razón = ritmo / tope`.
Razón > 1.15 se agota antes del reinicio; < 0.6 se pierde cuota. La semanal manda sobre la de 5 h.
Veredicto por grupo: `AGOTADA` (≤ 3 % libre: bloqueo duro), `FRENAR` (semanal se agota), `ESPERAR` (5 h al límite),
`MANTENER`, `USAR` (sobra cuota), `SIN_DATO` (lectura más vieja que 1 h; Codex 24 h, porque sólo se actualiza cuando corre).

En el flujo: cada proveedor declara en `providers.json` qué grupos lo alimentan (`"quota": [...]`; DeepSeek no tiene
ventana y queda `SIN_DATO`). `Providers.usable` descarta al `AGOTADA` con la hora real de reinicio, y `Team.pick`
suma al puntaje de confianza `+0.15 USAR / -0.15 ESPERAR / -0.30 FRENAR` (doble en riesgo `high`). Sin archivo de cuota
o con error, el flujo se comporta como antes. Cada corrida guarda el veredicto usado en su `meta.quota`.
Ver: `python3 agents/flow.py quota`, `python3 agents/quota.py show` o `http://100.75.61.75:8099/cuotas`.

## Límites (autonomía plena, límites por política — no por sandbox)
- `orchestration_policy.json` → `flow`: riesgos, revisiones, rondas, timeouts, límite diario de ejecuciones,
  paths protegidos, patrones de secretos, prefijos de test permitidos, tamaño máximo del diff.
- **Hooks**: ejecutables en `agents/hooks/{pre_run,post_run,pre_merge}.d/`. Reciben JSON por stdin;
  exit 0 = seguir, 2 = escalar a persona, otro = bloquear/esperar (ej.: ventana de congelamiento, "no
  mergear con la cámara en uso").
- Por tarea: `alcance`, `acciones`, `timeout`, `sin-merge`, `proveedor`.
- Freno de emergencia: `python3 agents/flow.py pause` (y `resume`).
- Riesgo residual: los proveedores implementadores corren sin confirmaciones y un worktree no es un
  sandbox; las guardas actúan **después** de la ejecución y **antes** del merge. Nada llega a `main` sin
  pasar tests, guardas y revisión de otro proveedor.

## Auditoría
`.runtime/agent-runs/<tarea>/<run>/`: `prompt.md`, `output.txt` y `stderr.txt` (redactados), `diff.patch`, `tests.log`,
`review.json`, `meta.json` (+ `jev_screen`/`verdict_via_jev` si intervino JEV). Cada bundle se registra en la cadena de hashes de la tarea.
```
python3 agents/audit.py pending | sample -n 3 | show TAREA | verify | reviewed TAREA ok|bad
python3 agents/flow.py status
```
Se marca muestra a las primeras 3 tareas de cada agente y luego al 20 %; llega aviso por Telegram.

## Habilitar
Ver los tres scripts al comienzo. Prueba local sin Vikunja:
`python3 agents/flow.py add T1 --title ... --component ... --type code --test "..."` y `python3 agents/flow.py tick`.

Tests: `cd agents && python3 -m unittest test_flow test_orchestrator` (19 tests; proveedores falsos y JEV simulado).

## El equipo (`team.json`)
Perfiles con nombre, roles, proveedores en orden de preferencia (con fallback), especialidad y **nivel de confianza**:

| Agente | Rol | Proveedor | Nivel inicial |
|---|---|---|---|
| **Itamae** (chef / tech lead) | implementa lo complejo, release, investigación | claude | 3 |
| **Nori** (generalista) | features y bugs claros | codex → claude | 2 |
| **Sashimi** (datos y visión) | pipelines, datasets, métricas, reportes | gemini | 2 |
| **Gari** (QA y volumen) | tests, docs, cambios mecánicos | deepseek (dsh) | 1 |
| **Wasabi** (revisor filoso) | revisión de corrección | codex → gemini | 2 |
| **Shoyu** (seguridad y riesgo) | revisión de seguridad/riesgo | claude → gemini | 2 |
| **Jev** | *no trabaja: clasifica* (ver abajo) | OpenRouter | — |

El rol depende del tipo de tarea (code/data→implementer, test/security→evaluator, research→researcher,
release→release_guard); el nivel decide el riesgo máximo (1=low, 2=medium, 3=high). Revisor y autor nunca
comparten proveedor. Ver el equipo: `python3 agents/flow.py team`.

## JEV (Typesafe JEV-1.13) — el clasificador del flujo (`jev.py`)
~0.6 s y ~USD 0.00003 por llamada, devuelve probabilidades y confianza. Probado en vivo con las cinco
consultas. Dónde entra:
1. **Triage del ticket**: propone `tipo` y `riesgo` si faltan, detecta acciones protegidas y pedidos ambiguos
   (no toma el ticket y pide precisión), y sugiere el agente por especialidad. **Sólo puede subir el riesgo.**
2. **Pantalla del diff** (antes del revisor): operaciones peligrosas → te llama; tests debilitados → vuelve al autor.
3. **Rescate de veredictos**: si un revisor no devuelve JSON, JEV clasifica su texto; sólo se acepta con confianza ≥ 0.8.
4. **Categorización de hallazgos** para la retrospectiva (taxonomía fija: bug, missing_tests, scope_creep, ...).
Si JEV no responde, el flujo cae a reglas deterministas. Cada llamada queda en `.runtime/jev/calls.jsonl`.
Tope diario configurable (`jev_daily_cap`). Nunca aprueba ni baja riesgo por sí solo.

## Aprendizaje (`learn.py`)
Todo se deriva del estado del orquestador (sin segunda fuente de verdad):
- **Confianza**: nivel = inicial + (tareas limpias ÷ 5) − auditorías malas − rechazos tuyos. Sube/baja solo.
- **Enrutado**: la asignación pondera la tasa de éxito por agente y tipo de tarea (+ 5 % de exploración).
- **Lecciones**: lo que revisores y vos corrigieron antes en ese componente vuelve al prompt del próximo agente.
- **Tu veredicto de auditoría**: `python3 agents/audit.py reviewed TAREA ok|bad --note "..."` es la señal más fuerte.
- **Retrospectiva**: cada 5 tareas cerradas (o `flow.py retro`) genera `agents/learning/retros/<fecha>.md` con
  métricas por agente, qué se corrige más y **propuestas para el flujo/equipo que sólo vos aplicás**
  (ej.: "Wasabi aprobó 8/8", "se repite scope_creep → acotar alcance"). Te llega un aviso por Telegram.

## Ejecución autónoma de proveedores
La ejecuta el servicio `sushi-agent-flow` (systemd), no una sesión de Claude: no requiere permisos
interactivos. `providers.json` define cómo se lanza cada CLI sin confirmaciones (Claude `acceptEdits`, Codex
`workspace-write`, Gemini `yolo`, DeepSeek `dsh --profile headless` con la key sólo en el entorno del proceso).
Los revisores van en modo solo lectura cuando el CLI lo permite y, en todos los casos, el controlador verifica
que no hayan tocado nada. `probar_proveedores.sh` comprueba los flags reales de cada uno.
