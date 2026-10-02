# Decisiones — carril compartido de agentes

## 2026-09-26 — Prechecks de Antigravity: sesión de cuenta y sin API key

### Contexto

`agy` guarda la sesión de cuenta en el keyring del sistema, por lo que no hay
un archivo de token seguro que el controlador pueda ni deba leer. En cambio,
`modelProvider=gemini` en settings fuerza explícitamente la ruta de API key.

### Decisión

Al habilitar Antigravity, verificar la sesión mediante `agy -p /usage` con
salida JSON y rechazar `modelProvider=gemini` en
`~/.gemini/antigravity-cli/settings.json`. Ambos prechecks usan el mismo
entorno saneado que la ejecución y jamás reciben API keys.

### Alternativas consideradas

- Inspeccionar archivos del keyring o perfiles de credenciales.
- Considerar el binario presente como sesión autenticada.
- Permitir el provider Gemini configurado si el entorno se sanea.

### Por qué esta

El CLI oficial define que las sesiones de cuenta viven en el keyring y que el
modo headless sin sesión falla con autenticación requerida. El comando de
estado prueba la sesión sin exponer su contenido; bloquear el setting evita la
vía explícita de API key. No se inspeccionan `.env` ni keyrings: el smoke real
debe confirmar que la cuenta autenticada es la ruta elegida antes de habilitar.

## 2026-09-26 — Cache acotada del precheck de sesión de Antigravity

### Contexto

`usable()` participa en cada selección del equipo. Un precheck basado en
`agy -p /usage` puede tardar hasta 15 segundos y no debe repetirse por cada
alternativa durante el mismo ciclo del controlador. El comportamiento exacto
de `/usage` contra el binario real aún requiere el smoke humano.

### Decisión

Cachear el resultado del precheck de sesión durante 300 segundos por instancia
de `Providers`; la configuración sigue deshabilitada y el smoke debe confirmar
que ese comando no consume cuota antes de habilitar Antigravity.

### Alternativas consideradas

- Ejecutar el precheck en cada `team.pick`.
- Omitir por completo el precheck hasta habilitar el proveedor.
- Persistir el resultado entre procesos.

### Por qué esta

Reduce latencia y llamadas repetidas sin convertir una sesión antigua en una
autorización persistente. Un proceso nuevo vuelve a comprobarla y no se leen
credenciales ni keyrings.

## 2026-09-26 — Corrección: Antigravity usa `agy -p` como CLI normal

### Contexto

La documentación oficial posterior confirma que la CLI `agy` tiene modo
headless/print (`-p`), JSON final y `stream-json` NDJSON. La evidencia previa
de `agentapi` corresponde al sidecar IDE y no es el contrato del proveedor de
línea de comandos para el flujo.

### Decisión

Reemplazar el adaptador específico `agentapi` por una entrada normal de
`providers.json`: `agy -p` con `--output-format json`. El driver común ahora
puede conservar stderr separado de stdout mediante `separate_stderr`. El
proveedor queda `enabled: false` hasta el smoke real, usa sesión de cuenta
Google de `agy` y no recibe API keys. Para revisión se usa `--mode=plan`, pero
el controlador mantiene su comprobación independiente de que el árbol quede
intacto.

### Alternativas consideradas

- Conservar `agentapi` y su language server como ruta del flujo.
- Activar `agy` sin smoke real.
- Tratar `--mode=plan` como una garantía de sólo lectura.

### Por qué esta

Sigue el contrato oficial de la CLI, evita una integración particular del IDE
y conserva datos estructurados y diagnóstico separados sin acoplar el
controlador a un proveedor.

## 2026-09-26 — Antigravity como proveedor secundario vía agentapi

### Contexto

La evidencia de la Raspberry muestra `agentapi`, no un modo `--print`. Sus
comandos son `new-conversation`, `send-message` y metadatos; exige
`ANTIGRAVITY_LS_ADDRESS` y usa la sesión de cuenta Google del entorno.

### Decisión

Agregar un adaptador local que ejecuta `agentapi new-conversation`, conserva
stdout (JSON o NDJSON) separado de stderr y exige el language server antes de
lanzar. Antigravity queda como fallback de Sashimi y sólo se prefiere cuando
el ticket indica `proveedor: antigravity`; no entra en el orden automático ni
se lo promueve a principal hasta un smoke real.

### Alternativas consideradas

- Inventar flags `--print`/`--stream-json` que el binario no expone.
- Reusar Gemini CLI o una API key de Google.
- Hacerlo el proveedor predeterminado antes de verificar el ciclo completo.

### Por qué esta

Respeta la interfaz realmente instalada y el aislamiento stdout/stderr, no
lee credenciales ni factura por API key, y limita el riesgo operacional a una
selección explícita o fallback controlado.

## 2026-09-26 — Flujo autónomo con revisión cruzada entre proveedores

### Decisión
`flow.py` (dispatcher) + `providers.json` (proveedores como línea de comando) sobre el controlador
`orchestrator.py`. Implementa un proveedor con autonomía plena en un worktree; revisa **otro** proveedor
en solo lectura; el controlador corre tests y guardas propios; merge a main sin persona salvo riesgo
alto, acción protegida o escalado. Los límites viven en política, hooks y contrato por tarea, no en el
sandbox de cada proveedor (decisión de Gustavo: "autonomía full"). Ver `agents/FLOW.md`.

### Por qué
La supervisión mutua sólo vale si el revisor es independiente y el controlador no cree lo que dicen los
agentes: recorre los tests, el diff y los secretos por su cuenta y deja evidencia con hash encadenado para
auditar por muestreo.

## 2026-09-25 — Vikunja POC aislada con SQLite y acceso sólo por Tailscale

### Contexto

La POC de orquestación necesitaba un backlog humano real sin incorporar una
base externa, proxy o servicio persistente al stack de producción existente.
La Raspberry tiene poco espacio disponible y la POC es de una persona.

### Decisión

Ejecutar `vikunja/vikunja:2.6.0` mediante Docker Compose, con SQLite, datos
persistentes bajo `agents/vikunja-poc/`, sin `restart`, sin systemd ni cron. El
contenedor sólo enlaza `100.75.61.75:3457`; CORS queda deshabilitado. El secreto
se genera localmente en `.env`, ignorado por Git.

### Alternativas consideradas

- PostgreSQL y un stack multiusuario desde el inicio.
- Exponer el contenedor en `0.0.0.0` o mediante proxy público.
- Usar la etiqueta Docker `latest`.

### Por qué esta

El piloto es recuperable, acotado a la VPN y reproducible con una imagen fija.
SQLite evita otro contenedor y es suficiente hasta validar que el flujo humano
de backlog aporta valor. La migración a PostgreSQL queda para el MVP.

## 2026-09-26 — Equipo con nombres, JEV como clasificador, aprendizaje y rama dev

Decisiones de Gustavo: (1) DeepSeek entra vía `dsh` headless; (2) JEV es el sensor del flujo (triage,
pantalla de diff, rescate de veredictos, taxonomía) y sólo puede subir riesgo; (3) el aprendizaje se deriva
del estado y se traduce en confianza por agente, enrutado, lecciones en el prompt y retrospectivas con
propuestas que sólo la persona aplica; (4) el equipo tiene perfiles con nombre; (5) los tickets se integran en
`dev` en un worktree aparte y `main` sólo recibe releases aprobados. Detalle en `agents/FLOW.md`.

## 2026-09-25 — POC: controlador neutral separado de Vikunja y de los proveedores

### Contexto

La cola local resuelve leases, pero no modela una ejecución como objeto
auditable ni puede expresar revisión humana, roles o evidencia obligatoria.
Vikunja será el backlog humano, pero entregar su MCP con escritura directa a
cualquier agente permitiría saltar esas reglas.

### Decisión

Crear una POC local de controlador en `agents/orchestrator.py`. Registra
tareas externas (`VIK-123`), ejecuciones con `run_id`, proveedor, rol,
referencia opaca de sesión, rama/worktree, evidencia y una máquina de estados.
Los roles y la política son JSON versionado; el estado de ejecuciones es
runtime. La integración futura consume webhooks de Vikunja y publica eventos
sanitizados, pero sólo el controlador podrá transicionar o mutar el backlog.

### Alternativas consideradas

- Dar acceso MCP de lectura/escritura de Vikunja a todos los agentes.
- Reemplazar la cola existente directamente por Vikunja.
- Mantener la evidencia únicamente en trailers de commit.

### Por qué esta

Separa intención humana, política de ejecución y evidencia técnica. Conserva
compatibilidad con Codex, Claude Code, Gemini/Antigravity y DeepSeek sin
acoplar la política a un proveedor ni exponer sesiones o secretos.

## 2026-09-15 — Cola local con lease cooperativo, no un segundo loop autónomo

### Contexto

Codex y Claude Code pueden leer el mismo repositorio, pero sus tareas y
automatizaciones no se despiertan entre sí. Sin una reserva explícita, dos
agentes podrían editar a la vez el mismo checkout o repetir una medición.

### Decisión

Agregar una cola versionada `agents/queue.json`, manipulada solamente con
`agents/queue.py`. Cada tarea se toma con dueño y lease temporal; al terminar
queda una evidencia y el próximo paso. El scheduler, si existe, sólo encola
tareas neutrales y nunca ejecuta en paralelo a ambos agentes sobre la misma
tarea.

### Alternativas consideradas

- Dos loops autónomos, uno para Codex y otro para Claude Code.
- Un servicio remoto central desde el inicio.
- Mantener el traspaso únicamente en conversaciones.

### Por qué esta

El archivo vive junto al código, es auditable por git, no añade dependencia ni
secreto y sirve en la Raspberry donde ambos agentes trabajan. Un servicio
central sólo será necesario si varias máquinas deben reclamar tareas de forma
concurrente; la notebook Windows actualmente no comparte filesystem con la
Raspberry.

## 2026-09-15 — Triada de agentes (Claude, Codex, Gemini) con persistencia de turnos y fallback

### Contexto

El loop autónomo en cron (`scripts/loop.sh`) estaba atado a Claude Code con un fallback manual a Codex. Se incorporó Gemini CLI (`gemini`) y se requirió poder elegir manualmente qué agente toma el próximo turno, preservando por defecto al que ejecutó en el turno anterior.

### Decisión

1. Soportar indistintamente a `claude`, `codex` y `gemini` en `scripts/loop.sh`.
2. Persistir el agente activo en `agents/current_agent`.
3. Ofrecer los comandos `python3 agents/queue.py set-agent <nombre>` y `python3 agents/queue.py current-agent` para control humano y de agentes.
4. Si el agente activo se bloquea por cuota o créditos, rotar automáticamente en cascada a los restantes y actualizar `agents/current_agent` con el agente que logró ejecutar.

### Alternativas consideradas

- Mantener scripts separados por agente.
- Hardcodear la rotación en round-robin en cada corrida.

### Por qué esta

Permite que el usuario elija quién toma el control o que el sistema continúe con el agente que viene trabajando bien sin interrupciones ni desvíos innecesarios.
