# Carril compartido de agentes

Esta carpeta permite que **Codex**, **Claude Code**, **Gemini** u otro agente
continúen una tarea sin competir por el mismo checkout. No reemplaza el
protocolo de `AGENTS.md`: `main`, `ESTADO.md`, `LOOP.md`, sesiones y commits
siguen siendo la fuente de verdad.

**Antigravity cuenta como `gemini`** a efectos de esta cola (`normalize_agent`
en `queue.py` acepta `--agent antigravity` igual que `--agent gemini`). Es el
único agente activo en este repo que opera por fuera de este carril salvo que
se le indique explícitamente en su sesión que lo use — no lo hace por
default. Ver "Sesiones concurrentes" en `AGENTS.md` (2026-09-17).

**`deepseek` es un agente válido en la cola** (`agents/queue.py`, 2026-09-17)
pero **todavía no tiene harness instalado en la Raspberry** — a diferencia de
Codex/Claude/Gemini, no hay CLI ni API key configurada. `scripts/loop.sh`
tampoco lo incluye en la cascada de rotación automática todavía. Falta
decidir el harness (candidato natural: `aider`, vía API OpenAI-compatible de
DeepSeek) y cargar la API key antes de que pueda tomar tareas de verdad.

## Control de turnos del loop autónomo

El loop autónomo en cron (`scripts/loop.sh`) consulta quién tiene el turno
activo:

```bash
# Ver quién tomará la próxima iteración
python3 agents/queue.py current-agent

# Fijar quién toma el próximo turno (claude | codex | gemini)
python3 agents/queue.py set-agent gemini
```

**Regla de persistencia:** Por defecto, el loop retiene al agente del turno
anterior. Si el agente activo se queda sin cuota o créditos, el script rota
automáticamente en cascada a los restantes y guarda al nuevo agente activo para
las siguientes ejecuciones.

## Reglas operativas

1. Sólo un agente toma una tarea a la vez. Antes de modificar código, hacer el
   chequeo git indicado en `AGENTS.md`, leer `ESTADO.md`, la entrada más nueva
   de `LOOP.md` y `python3 agents/queue.py list`.
2. Tomar el trabajo con un *lease* explícito. El lease es una reserva
   cooperativa, no permiso para saltear revisiones, autorizaciones ni gates.
3. Mientras dure un trabajo largo, renovar el lease con `heartbeat`. Si vence,
   otro agente puede retomarlo sólo después de leer la evidencia disponible.
4. Antes de liberar, bloquear o completar: commitear lo realizado, actualizar
   `ESTADO.md` y `LOOP.md` cuando corresponda, y registrar evidencia concreta
   (commit, reporte o bloqueo) en la cola.
5. El cron/scheduler sólo debe **crear o señalar** trabajo; no debe asumir un
   proveedor de IA fijo. Nunca programar a dos agentes para mutar la misma
   tarea ni el mismo checkout en paralelo.

## Uso

Desde la raíz del repositorio:

```bash
# El coordinador humano o un scheduler neutral crea la tarea.
python3 agents/queue.py add obb-mobile-sam-benchmark \
  --title 'Medir MobileSAM contra los 76 OBB humanos' \
  --component tickets/2026.08.30_modelo_deteccion_v1 \
  --priority P1 \
  --next-step 'Correr el benchmark reproducible y guardar el reporte.'

# Un agente la toma (desde la Raspberry / copia canónica Linux).
python3 agents/queue.py claim obb-mobile-sam-benchmark \
  --agent gemini --lease-minutes 120

# Si sigue trabajando, renueva su reserva.
python3 agents/queue.py heartbeat obb-mobile-sam-benchmark \
  --agent gemini --next-step 'Benchmark en curso; falta guardar métricas.'

# Cierre trazable.
python3 agents/queue.py complete obb-mobile-sam-benchmark \
  --agent gemini \
  --next-step 'Revisar métricas y decidir si generar nuevas propuestas.' \
  --evidence 'commit abc1234; reports/mobile_sam_obb.json'
```

Para un bloqueo real, usar `block` con el motivo y la acción que debe tomar la
persona. Si el agente necesita soltar la tarea para que la siga otro, usar
`release`; no borrar ni editar a mano las tareas históricas.


## Handoff entre Codex y Claude Code

El siguiente agente no necesita heredar el chat anterior. Sólo necesita:

1. Leer `AGENTS.md`, `ESTADO.md`, `LOOP.md` y la sesión del componente.
2. Ver `agents/queue.json`, tomar o retomar la tarea y leer su `evidence`.
3. Confirmar que `main` no diverge y que no hay cambios ajenos sin commitear.
4. Trabajar dentro del alcance de `next_step`; actualizar la evidencia al
   cerrar.

Esto funciona mientras ambos actúen sobre la copia canónica de la Raspberry.
La notebook Windows no comparte filesystem con ella vía SSH: puede ejecutar
un benchmark remoto, pero la tarea debe actualizarse desde la copia canónica
después de traer el resultado.

## Límites intencionales

- La cola no despierta automáticamente un agente de otro proveedor.
- No incluye secretos, credenciales, comandos destructivos ni cambios de
  producción.
- `flock` protege las operaciones de la cola en Linux. Si se necesita operar
  desde varias máquinas, el siguiente paso es un coordinador/servicio central;
  no dos archivos JSON replicados.
