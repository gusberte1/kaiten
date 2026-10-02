#!/usr/bin/env python3
"""Carga en Vikunja los tickets del plan de mejoras del 2026-09-29 y avisa por Telegram.

Idempotente: si ya existe un ticket con el mismo prefijo `[ID]` en el título, no lo duplica.
Cada ticket se asigna a bot-Orchestrator con contrato `agente: listo`, así el flujo
(`sushi-agent-flow.service`) lo toma con implementación + revisión cruzada.

    python3 agents/scripts/crear_tickets_plan_mejoras.py --dry-run   # valida contratos, no toca Vikunja
    python3 agents/scripts/crear_tickets_plan_mejoras.py             # crea, asigna y avisa por Telegram

Plan completo: https://claude.ai/artifact/Ho3qXEKUr5V46oKjUpgoLB
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agents"))
import flow  # noqa: E402

BOT = "bot-Orchestrator"
EVT = "tickets/2026.09.03_evento_caja_tapada"
REG = "tickets/2026.09.20_registro_eventos"
LLENO = "tickets/2026.09.03_lleno_fase2"
OBB = "tickets/2026.08.25_obb_bust_v2"
STATUS = "system-setup/status-page"

REGLAS = """## Reglas de esta tarea

- Contexto completo: esta descripción se basta sola. Plan de origen: `https://claude.ai/artifact/Ho3qXEKUr5V46oKjUpgoLB` (referencia opcional).
- Autonomía total. **NO escales salvo algo crítico**: pérdida de datos, servicio de producción caído, secreto expuesto o cambio irreversible. Dudas de diseño: elegí la opción más simple y reversible y anotala en el resumen.
- Prioridad del proyecto: **recall sobre precisión** (no perder eventos de cierre).
- No reinicies servicios, no toques systemd, cron, hardware (cámara/Hailo) ni `main`. Si el cambio necesita eso para activarse, dejá el comando exacto en el resumen.
- No uses la cámara ni el Hailo entre 12:55–14:15 y 21:25–22:10 (corridas del monitor).
- Cada número que reportes sale de un script que dejás en el diff o de un `reports/*.json`; nada de cifras narradas.
- Evaluaciones de modelos: split por sesión o leave-one-out y chequeo explícito de overlap train/test.
- Al terminar: tests propios verdes + resumen de ≤10 líneas con evidencia (rutas, números, comandos)."""


def contrato(tipo, riesgo, comp, test, alcance="", proveedor="", complejidad="", modelo="", timeout=60, extra=""):
    lines = ["## Contrato de ejecución", "", "- **agente**: listo", f"- **tipo**: {tipo}", f"- **riesgo**: {riesgo}",
             f"- **componente**: {comp}"]
    if test:
        lines.append(f"- **test**: {test}")
    if alcance:
        lines.append(f"- **alcance**: {alcance}")
    if proveedor:
        lines.append(f"- **proveedor**: {proveedor}")
    lines.append(f"- **timeout**: {timeout}")
    if extra:
        lines.append(extra)
    lines += ["", f"Complejidad: {complejidad} · Modelo sugerido: {modelo}"]
    return "\n".join(lines)


def ut(dirpath, pattern):
    return f'python3 -m unittest discover -s {dirpath} -p "{pattern}"'


# (id, prioridad Vikunja 0-5, título, contrato, cuerpo)
T = [
# ---------------- Fase 0 · Estabilizar ----------------
("OPS-2", 5, "Purga de disco por umbral (la SD se llena antes del domingo)",
 contrato("code", "medium", "scripts", ut("scripts", "test_mantenimiento_umbral.py"),
          "scripts/mantenimiento_datos.py, scripts/test_mantenimiento_umbral.py", "claude", "media", "Sonnet 5"),
 """## Objetivo
La SD pasó de 17 GB libres (27/09) a 11 GB (29/09, 82 %). `scripts/mantenimiento_datos.py` corre sólo los domingos 04:00: a ~3 GB/día se llena antes.

## Hacer
1. Agregar `--umbral PCT` a `mantenimiento_datos.py`: si el uso de `/` está por debajo, sale sin borrar (exit 0, una línea de log); si lo supera, ejecuta las mismas reglas actuales.
2. Medir qué directorios crecen más desde el 27/09 (script de solo lectura, salida a `reports/crecimiento_disco_20260929.json`).
3. Mantener intacta la protección de ground truth (fotos `reviewed`, clips `cierre_real`, `cierre_no_detectado`, `no_es_cierre`).

## Criterios de aceptación
- Test con un filesystem simulado: bajo umbral no borra nada; sobre umbral borra sólo lo purgable; el GT nunca se toca.
- Resumen con la línea de cron sugerida (`--umbral 85` diaria). No instalar el cron."""),

("SEC-2", 5, "Confirmación obligatoria en el botón de purga de :8099",
 contrato("code", "low", STATUS, ut(STATUS, "test_status.py"), "", "codex", "baja", "Haiku 4.5"),
 """## Objetivo
`:8099` tiene "Limpiar ahora", que borra datos de la SD desde cualquier equipo de la tailnet.

## Hacer
1. Revisar si hoy hay confirmación del lado servidor.
2. Exigir un dry-run reciente (≤10 min) y una confirmación en dos pasos con un token de un solo uso emitido por el dry-run. Sin token válido, el POST devuelve 409 con un mensaje claro.
3. `window.confirm()` no sirve como única defensa: la validación va en el servidor.

## Criterios de aceptación
- Tests: POST sin token → 409; con token vencido → 409; con token válido → ejecuta; el token no se reusa."""),

("REV-3", 5, "Manifest con hash del ground truth para atar cada evaluación a una versión",
 contrato("code", "low", "scripts", ut("scripts", "test_manifest_gt.py"),
          "scripts/manifest_gt.py, scripts/test_manifest_gt.py, reports/**", "deepseek", "baja", "Haiku 4.5 / DeepSeek"),
 f"""## Objetivo
`golden_dataset_conteo.json`, `labels_conteo.json` y `poses_revision.json` se editan en el lugar y hoy están modificados sin commit. Ninguna evaluación registra qué versión del GT usó.

## Hacer
1. `scripts/manifest_gt.py`: recorre los archivos de GT conocidos ({LLENO}/golden_dataset_conteo.json, {LLENO}/labels_conteo.json, tickets/2026.09.19_tipos_piezas_v0.1/revision_poses/poses_revision.json, `revision.json` de {EVT}) y escribe `reports/gt_manifest.json` con sha256, tamaño, cantidad de entradas y fecha.
2. Función importable `gt_hash()` que devuelve un hash corto combinado, para que los evaluadores lo incluyan en sus JSON.

## Criterios de aceptación
- Tests con archivos temporales: el hash cambia si cambia una etiqueta y no cambia si sólo cambia el orden de claves JSON."""),

("AGT-C", 5, "Higiene del runtime del flujo: estado fuera de git y ramas integradas podadas",
 contrato("code", "medium", "agents", "python3 -m unittest discover -s agents -p \"test_flow.py\"",
          "agents/flow.py, agents/queue.py, agents/test_flow.py, .gitignore", "claude", "baja", "Sonnet 5"),
 """## Objetivo
`agents/current_agent` aparece modificado en el checkout principal y ensucia `main`. Hay 19 ramas `agent/VIK-*` locales de tickets ya integrados o rechazados.

## Hacer
1. Mover `current_agent` a `.runtime/current_agent` con lectura de compatibilidad del archivo viejo; agregarlo a `.gitignore` y sacarlo del índice (`git rm --cached`).
2. `flow.py prune`: además de worktrees, borrar ramas `agent/VIK-*` cuyo ticket esté `done` o `rejected` y cuya punta ya esté contenida en `dev` (o que estén rechazadas). Nunca borrar `backup/*`, `dev`, `main` ni `codex`.

## Criterios de aceptación
- Tests: una rama integrada se borra; una rama con commits no integrados de un ticket abierto se conserva.
- `queue.py current-agent` sigue funcionando."""),

("AGT-B", 5, "Cuota y confianza: fallback para Sashimi, timeout por tipo y no castigar fallas de infraestructura",
 contrato("code", "medium", "agents", "python3 -m unittest discover -s agents -p \"test_*.py\"",
          "agents/team.json, agents/learn.py, agents/flow.py, agents/orchestration_policy.json, agents/test_flow.py", "claude", "media", "Sonnet 5"),
 """## Objetivo
Retro del 29/09: Itamae (claude) bajó a nivel 1 por 5 ejecuciones fallidas de cuota o timeout, mientras a Claude le sobraría el 32 % de la cuota semanal. Sashimi sólo tiene `antigravity`, cuya cuota semanal de Gemini está agotada. Las propuestas de las retros (scope_creep, missing_tests) se repiten sin generar tickets.

## Hacer
1. `team.json`: Sashimi con fallback `["antigravity", "claude", "codex"]`.
2. `run_timeout_s` por tipo en la política (`research` 3600, `data` 2700, resto 1800).
3. `learn.py`: una ejecución que falló por cuota, timeout o auth (clasificada por `blocked_regex` o timeout) no cuenta como fallo del agente para la confianza.
4. La retro crea un ticket en Vikunja por propuesta nueva (título `[retro] ...`, sin asignar a bot, dedup por texto).

## Criterios de aceptación
- Tests: un timeout no baja la confianza; un fallo de tests sí; Sashimi cae a claude si antigravity está AGOTADA."""),

("GOV-1", 5, "Preparar el release dev → main (23 commits: VIK-24 a VIK-29)",
 contrato("release", "medium", "agents", "python3 -m unittest discover -s agents -p \"test_flow.py\"",
          "docs/releases/**", "", "media", "Sonnet 5 (release_guard)", extra="- **sin-merge**: si"),
 f"""## Objetivo
`dev` está 23 commits adelante de `main`: VIK-24, 25, 26, 27, 28 (multicaja: la segunda caja no cancela ni deduplica, arregla el 5° cierre perdido del 28/09) y 29. Producción corre `main`.

## Hacer
1. En un worktree de `dev`: correr las suites de {EVT}, {REG}, `agents` y `{STATUS}`; guardar los logs.
2. `docs/releases/2026-09-30-release.md`: qué cambia por componente, tests con resultado, riesgos (los cambios en `monitor_vivo.py` afectan la corrida de las 13:00) y rollback exacto (`git revert -m 1 <merge>` o el tag anterior `release-20260928-2237`).
3. **No ejecutar** `flow.py release --approve`: lo aprueba Gustavo con un solo mensaje.

## Criterios de aceptación
- Todas las suites verdes o, si alguna falla, la falla explicada con causa y el ticket hijo creado."""),

("EVT-1", 5, "Comparar HEF v17 contra v13 con replay offline de sesiones grabadas",
 contrato("data", "low", EVT, ut(EVT, "test_replay_hef_compare.py"),
          f"{EVT}/replay_hef_compare.py, {EVT}/test_replay_hef_compare.py, {EVT}/reports/**", "claude", "alta", "Opus 5.5", timeout=120),
 f"""## Objetivo
El HEF v17 (`tickets/2026.08.30_modelo_deteccion_v1/models/hailo/v17/modelo.hef`) tiene R 0,979 contra 0,957 de v13 en el test congelado, pero nunca corrió la lógica de cierre. Antes de desplegarlo hace falta ver qué cierres cambian.

## Hacer
1. `replay_hef_compare.py`: sobre las sesiones con video continuo o clips HQ disponibles, correr v13 y v17 fuera de las ventanas del monitor. Pasar las detecciones por la FSM actual (`reglas_cierre.py`) y listar eventos que aparecen, desaparecen o cambian de hora.
2. Cruzar con los veredictos humanos de :8094 (`revision.json`): cierres reales ganados o perdidos, falsos positivos ganados o perdidos.
3. Reporte `reports/replay_hef_v17_vs_v13.json` y resumen con una recomendación: desplegar, desplegar con sombra o no desplegar.

## Criterios de aceptación
- Test con detecciones sintéticas para la parte de comparación (sin Hailo).
- No tocar `models/latest.hef` ni `models/latest.pt`."""),

# ---------------- Fase 1 · Medir y automatizar ----------------
("OPS-3", 4, "Vigilancia de servicios en :8099 con alertas por Telegram",
 contrato("code", "medium", STATUS, ut(STATUS, "test_*.py"),
          f"{STATUS}/**", "claude", "media", "Sonnet 5"),
 """## Objetivo
`sushi-monitor-review.service` acumuló 15.032 reinicios porque un proceso manual retenía el puerto 8094, y nadie se enteró.

## Hacer
1. En `status.py`, un hilo cada 5 min que revisa: cada servicio `sushi-*` (activo, `NRestarts` creciendo), cada puerto del catálogo responde HTTP, disco > 85 %, la última corrida del monitor dejó `eventos.jsonl` y el Hailo no tuvo timeout.
2. Ante un cambio a falla, avisar por Telegram con `scripts/notificar.py` (dedup: un aviso por problema hasta que se resuelva, y otro al resolverse).
3. Tarjeta "Salud" en la página principal con el estado de cada chequeo.

## Criterios de aceptación
- Tests con `systemctl` y HTTP simulados: puerto tomado por otro proceso → alerta; dos ciclos seguidos con la misma falla → un solo aviso.
- En el resumen, el comando de reinicio de `sushi-status` que activa el cambio (no lo ejecutes)."""),

("OPS-4", 4, "Script de deploy por servicio con smoke HTTP y endpoint /version",
 contrato("code", "medium", "scripts", ut("scripts", "test_deploy.py"),
          f"scripts/deploy.sh, scripts/test_deploy.py, {EVT}/revisar_monitor.py, {STATUS}/status.py, {EVT}/test_revisar_monitor.py", "codex", "baja", "Sonnet 5"),
 """## Objetivo
ESTADO repite "acción pendiente: reiniciar el servicio". Los cambios quedan en disco sin activarse y no hay forma de saber qué versión se está sirviendo.

## Hacer
1. `/version` en :8094 y :8099: devuelve el commit (`git rev-parse --short HEAD` al arrancar) y la hora de inicio.
2. `scripts/deploy.sh <servicio>`: reinicia la unit de usuario, espera que responda y compara `/version` contra `HEAD`. Si no coincide, sale con código ≠ 0 y muestra las últimas 20 líneas del journal.
3. No ejecutar el deploy en esta tarea.

## Criterios de aceptación
- `bash -n scripts/deploy.sh` OK; tests de `/version` en ambos servidores."""),

("EVT-2", 4, "Esquema único de evento de cierre (un solo reloj y un solo ID)",
 contrato("code", "medium", EVT, ut(EVT, "test_esquema_evento.py"),
          f"{EVT}/esquema_evento.py, {EVT}/esquema_evento.json, {EVT}/test_esquema_evento.py, {REG}/exportar_eventos.py, {REG}/test_sync.py, docs/**", "claude", "alta", "Opus 5.5", timeout=90),
 f"""## Objetivo
Cada evento tiene tres relojes: hora de volcado del clip, cierre físico (`cierre_en`) y hora del frame. Eso causó VIK-24, VIK-26, VIK-29, el parche `cierre_hhmmss` y el falso FN de la auditoría periódica.

## Hacer
1. `esquema_evento.json` (JSON Schema) y `esquema_evento.py` con `event_id(sesion, track, cierre_en)` y `validar(meta)`. Campos de hora con nombre y semántica explícitos (`cierre_en` = verdad; `volcado_en` = técnico).
2. Documentar en `docs/ESQUEMA_EVENTO.md` qué archivo produce y qué archivo consume cada campo (clip meta, `eventos.jsonl`, `registro.json`, Firebase).
3. `exportar_eventos.py` valida en modo aviso: loguea los eventos inválidos y no bloquea.
4. Correr el validador sobre `data/` y reportar cuántos de los 699 eventos lo cumplen (`reports/validacion_esquema_eventos.json`). No migrar datos en esta tarea.

## Criterios de aceptación
- Tests: `event_id` es estable y único para dos cajas simultáneas; un meta sin `cierre_en` es inválido."""),

("EVT-3", 4, "Replay del corpus como test de regresión de la lógica de cierre",
 contrato("code", "medium", EVT, ut(EVT, "test_regresion_cierres.py"),
          f"{EVT}/regresion_cierres.py, {EVT}/test_regresion_cierres.py, {EVT}/fixtures_regresion/**, {EVT}/reports/**", "claude", "media", "Sonnet 5", timeout=90),
 """## Objetivo
Cada versión de la lógica (v12, v13...) se valida a mano contra el corpus (53 sesiones, 697 eventos). Nada impide que un cambio a `reglas_cierre.py` pierda cierres reales.

## Hacer
1. `regresion_cierres.py`: reproduce la FSM sobre los `timeline.json` y la telemetría guardada y compara contra los veredictos humanos de :8094. Salida: recall y precisión por sesión y total, más la lista de cierres reales perdidos.
2. Fixture chico y versionado (≤5 sesiones, ≤2 MB) que corre en < 60 s dentro del test. El corpus completo se corre por CLI y escribe `reports/regresion_cierres.json`.
3. El test falla si el recall del fixture baja del valor de referencia guardado.

## Criterios de aceptación
- Test verde con la lógica actual; el test falla si se rompe a propósito una regla (demostralo en el resumen con el diff de prueba, sin commitearlo)."""),

("CNT-2", 4, "Sacar el regresor del conteo publicado y usarlo como señal de revisión",
 contrato("code", "medium", REG, ut(REG, "test_*.py"), "", "codex", "baja", "Sonnet 5"),
 """## Objetivo
Sobre 51 eventos verificados: SAM tiene MAE 1,64 en cajas de menos de 20 piezas y el regresor 6,09. Un ensamble ciego llevaría el MAE global de 3,98 a 8,24.

## Hacer
1. En `analizar_eventos.py`, el conteo publicado es el de SAM (selector v3). El regresor se sigue calculando, pero sólo como `conteo_regresor`.
2. Si `|SAM − regresor| > max(3, 0,3 × SAM)` o la confianza óptica del frame elegido es < 0,78, marcar `revisar_conteo: true` con el motivo. El sitio ya muestra campos de análisis; agregar ese flag al `registro.json`.

## Criterios de aceptación
- Tests: el conteo publicado nunca sale del regresor; los casos de discrepancia quedan marcados."""),

("CNT-3", 4, "Evaluador de conteo único con split por sesión y chequeo de leakage",
 contrato("data", "low", LLENO, ut(LLENO, "test_evaluar_conteo.py"),
          f"{LLENO}/evaluar_conteo.py, {LLENO}/test_evaluar_conteo.py, {LLENO}/reports/**", "", "media", "Codex / Gemini 3.5"),
 """## Objetivo
El leakage banco == golden set pasó al menos dos veces y dio "Exact Match" falsos. El gate del contador (MAE ≤ 1,5, ±1 ≥ 80 %) exige GroupKFold por sesión.

## Hacer
1. `evaluar_conteo.py`: evaluación con GroupKFold por sesión sobre `golden_dataset_conteo.json`. Antes de medir, aborta si un recorte o una sesión aparece en train y test a la vez.
2. Reporte `reports/eval_conteo_<fecha>.json` con MAE, ±1, ±2, exactitud en vacías y resultados por rango de piezas (<20, ≥20).

## Criterios de aceptación
- Test que inyecta un overlap y verifica que el evaluador aborta; test de GroupKFold sin sesiones compartidas."""),

("CNT-6", 4, "Publicar métricas del OBB en un JSON",
 contrato("data", "low", OBB, ut(OBB, "test_eval_obb_json.py"),
          f"{OBB}/eval_obb_json.py, {OBB}/test_eval_obb_json.py, {OBB}/reports/**", "deepseek", "baja", "DeepSeek / Haiku 4.5"),
 """## Objetivo
`/arquitectura` no puede mostrar métricas del OBB v2b_n porque no hay un JSON con MAE y correlación.

## Hacer
1. `eval_obb_json.py`: a partir de los resultados existentes (smart crop vs sin smart crop sobre las 129 multi-caja y los 19 eventos `cierre_real` de `p4.json`), escribir `reports/obb_metricas.json` con OBB válido %, IoU mediano, MAE de conteo con y sin smart crop y la correlación r.
2. Sin reentrenar nada.

## Criterios de aceptación
- Test que valida las claves y tipos del JSON."""),

("AGT-A", 4, "Contratos con criterios de aceptación y modelo por complejidad",
 contrato("code", "medium", "agents", "python3 -m unittest discover -s agents -p \"test_*.py\"",
          "agents/flow.py, agents/providers.py, agents/providers.json, agents/flow_prompts/**, agents/test_flow.py, agents/FLOW.md", "claude", "alta", "Opus 5.5", timeout=90),
 """## Objetivo
Retro del 29/09: 5 de 11 tareas limpias a la primera, revisores que aprueban el 25 % y el 37 %, 56 hallazgos de tipo bug y 24 de missing_tests. Además todo corre con el modelo por defecto de cada CLI.

## Hacer
1. Contrato: nuevas claves opcionales `criterios:` (lista) y `complejidad: baja|media|alta`. Si falta `criterios` y JEV marca ambigüedad ≥ 0,5, se pide en el ticket (no se escala a la persona).
2. `providers.json`: placeholder `{model}` en los argv. La política mapea complejidad → modelo por proveedor (claude: alta → opus, media → sonnet, baja → haiku; codex y antigravity según lo que acepte cada CLI; si el flag no existe, se omite).
3. El prompt del implementador y el del revisor incluyen los criterios, y el revisor los verifica uno por uno en su JSON.
4. Documentar en `FLOW.md`.

## Criterios de aceptación
- Tests: el argv lleva el modelo correcto por complejidad; un contrato sin `modelo` sigue funcionando igual que antes."""),

("AGT-D", 4, "Bandeja única de decisiones humanas y resumen diario por Telegram",
 contrato("code", "medium", STATUS, ut(STATUS, "test_*.py"), f"{STATUS}/**, scripts/resumen_decisiones.py, scripts/test_resumen_decisiones.py", "codex", "media", "Sonnet 5"),
 """## Objetivo
Los pedidos a Gustavo están dispersos: ESTADO ("a la espera de autorización"), ROADMAP D1–D6, `human_review` en Vikunja, LOOP.md ("Necesito del humano") y las retros.

## Hacer
1. `:8099/decisiones`: lista única de tickets en `human_review` (leyendo `.runtime/flow-meta.json` y el estado del orquestador, sólo lectura), tickets de Vikunja con la etiqueta o el prefijo `[decisión]` y el release pendiente (`git rev-list --count main..dev`).
2. `scripts/resumen_decisiones.py`: un solo mensaje de Telegram con lo pendiente y el enlace a cada ticket (vía `scripts/notificar.py`). Se programa después; dejá la línea de cron en el resumen.

## Criterios de aceptación
- Tests con estado simulado: aparecen las tres fuentes; sin pendientes, no se manda mensaje."""),

("AGT-E", 4, "Un solo carril autónomo: loop.sh deja de implementar y sólo crea tickets",
 contrato("code", "medium", "scripts", "bash -n scripts/loop.sh",
          "scripts/loop.sh, agents/session_registry.py, agents/test_session_registry.py, agents/README.md", "claude", "alta", "Opus 5.5"),
 """## Objetivo
Hoy corren tres carriles a la vez: cron `scripts/loop.sh` (4 veces por día), el daemon `sushi-agent-flow` y las sesiones interactivas (148 de 176 commits en 7 días). La decisión del 15/09 (`agents/DECISIONS.md`) era una sola cola. Hubo decisiones stale y colisiones.

## Hacer
1. `loop.sh`: en vez de lanzar un agente que implementa, revisa `ESTADO.md`, `LOOP.md` y el tablero, y crea o actualiza tickets en Vikunja asignados a bot-Orchestrator con contrato, así el daemon los ejecuta con revisión cruzada. Mantener el modo viejo detrás de `LOOP_MODO=legacy`.
2. `session_registry.py start` idempotente y con TTL, para que las sesiones interactivas puedan registrarse desde un hook de arranque (documentar el hook, no instalarlo).

## Criterios de aceptación
- `bash -n` OK; tests de session_registry con TTL vencido."""),

("AGT-F", 4, "CI de tests en GitHub Actions",
 contrato("code", "low", ".github", "python3 -m unittest discover -s agents -p \"test_flow.py\"",
          ".github/workflows/tests.yml, docs/**", "codex", "baja", "Sonnet 5"),
 f"""## Objetivo
Los únicos workflows compilan el HEF. Los 68 tests de agentes, los 43 del evento y los de sync corren sólo en la Pi.

## Hacer
1. `.github/workflows/tests.yml`: en push y PR a `dev` y `main`, con Python 3.13 en ubuntu, correr las suites CPU de `agents`, {EVT}, {REG} y `{STATUS}`. Los tests que necesitan Hailo, cámara o modelos grandes se saltean con skip si falta el recurso.
2. Publicar el resultado resumido en la rama `ci-resultados`, igual que los workflows existentes, para poder leerlo sin entrar a GitHub.

## Criterios de aceptación
- YAML válido; en el resumen, la lista de tests salteados y el motivo."""),

("GOV-2", 4, "ESTADO con plantilla y tope, ROADMAP actualizado y LOOP archivado",
 contrato("code", "low", "scripts", ut("scripts", "test_podar_estado.py"),
          "ESTADO.md, ROADMAP.md, LOOP.md, docs/archive/**, scripts/podar_estado.py, scripts/test_podar_estado.py, agents/hooks/pre_merge.d/**", "", "baja", "Haiku 4.5 / DeepSeek"),
 """## Objetivo
`ESTADO.md` volvió a 339 líneas tras la poda del 28/09, con narrativa y fórmulas LaTeX. `LOOP.md` tiene 3.384 líneas. `ROADMAP.md` es del 19/09 y dice cosas falsas hoy (OBB pausado, tapada v8, BB v13 sin v17).

## Hacer
1. `scripts/podar_estado.py`: deja en ESTADO la cabecera, el cuadro de componentes y las últimas N secciones (≤150 líneas) y mueve el resto a `docs/archive/ESTADO_<mes>.md` sin perder texto. Hook `pre_merge.d` que avisa (exit 1 no, sólo log) si ESTADO supera 150 líneas.
2. `LOOP.md`: archivar las entradas previas a septiembre en `docs/archive/LOOP_2026-08.md`.
3. `ROADMAP.md`: reescribir el tablero de tracks con el estado real (monitor v13 lógica, HEF v13 en prod y v17 candidato, OBB v2b_n + smart crop en producción, SAM selector v3) y enlazar el plan de mejoras.

## Criterios de aceptación
- Test: la poda conserva el 100 % del texto (ESTADO nuevo + archivo = original)."""),

# ---------------- Fase 2 · Calidad de producto ----------------
("AGT-G", 3, "Hook que verifica que las cifras del resumen salen de un reporte",
 contrato("code", "low", "agents/hooks", "python3 -m unittest discover -s agents/hooks -p \"test_*.py\"",
          "agents/hooks/**", "codex", "media", "Codex (diseño Opus 5.5)"),
 """## Objetivo
Hubo leakage de Antigravity dos veces, un "100 % recall" armado con contadores que no se sostuvo y cifras del OBB sin JSON.

## Hacer
1. `agents/hooks/post_run.d/verificar_cifras.py`: toma del JSON de entrada el resumen del implementador y el diff; extrae números con unidad o contexto de métrica (recall, MAE, P, R, %, eventos) y busca cada uno en los `reports/*.json` o salidas de test del diff o del repo.
2. Si hay cifras sin respaldo: exit 1 (vuelve al autor con la lista), nunca 2 (no escala a la persona).
3. Tolerancia de redondeo y lista blanca (fechas, horas, IDs de ticket).

## Criterios de aceptación
- Tests: resumen con cifra respaldada → 0; con cifra inventada → 1; fechas y horas no cuentan."""),

("REV-1", 3, "Portal \"qué revisar hoy\" con prioridad por incertidumbre",
 contrato("code", "medium", STATUS, ut(STATUS, "test_*.py"), f"{STATUS}/**", "claude", "media", "Sonnet 5 (diseño Opus 5.5)", timeout=90),
 f"""## Objetivo
Gustavo revisa en nueve lugares (:8080, 8090, 8091, 8093, 8094, 8096, 8100, 8099 y el sitio en Firebase), y todo el aprendizaje del sistema espera sus veredictos.

## Hacer
1. `:8099/revisar`: una cola por punto de revisión (R1 cajas, R2 cierres, R3 conteo) con la cantidad pendiente y links profundos al ítem exacto (por ejemplo, `:8094/?cierre_hhmmss=...`).
2. Orden por valor de revisión: R2 primero los clips `actividad` sin veredicto con confianza media y los dedups dudosos; R3 los eventos con `revisar_conteo: true` o combos ≥ 20 piezas; R1 las fotos con predicción de confianza entre 0,45 y 0,8.
3. Sólo lectura de los archivos de cada app: no cambiar sus formatos.

## Criterios de aceptación
- Tests con datos simulados del orden y los conteos; el enlace de cada ítem apunta a la app correcta."""),

("CNT-1", 3, "Selector de frames para combos grandes",
 contrato("data", "medium", REG, ut(REG, "test_selector_frames.py"), f"{REG}/reports/**", "claude", "alta", "Opus 5.5 · Gemini 3.5 evalúa", timeout=120),
 """## Objetivo
En combos de 20 piezas o más, SAM tiene MAE 8,28 (ejemplo: `20260927_214449`, GT 30, SAM 5). El error viene de elegir frames oscuros o con la tapa parcialmente puesta.

## Hacer
1. Selector `v3.1`: en cajas grandes (área OBB sobre el percentil 70 o conteo preliminar ≥ 15), descartar frames con tapa detectada que solape la caja (> 15 %) y exigir área estable (±10 % entre frames vecinos). El conteo final es la mediana del top-3.
2. Evaluar v3.0 vs v3.1 sobre los 51 eventos verificados, separando < 20 y ≥ 20 (`reports/selector_v31.json`).
3. v3.1 queda como default sólo si no empeora el grupo < 20.

## Criterios de aceptación
- Tests nuevos del filtro de tapa y de la mediana; tests actuales del selector verdes."""),

("OPS-6", 3, "Presupuesto para correr el monitor durante el turno completo",
 contrato("research", "low", EVT, "", f"{EVT}/reports/**, docs/**", "", "media", "Opus 5.5"),
 """## Objetivo
El monitor corre 13:00 (68 min) y 21:30 (30 min): cubre ~1,6 h por día. El recall de producción sólo mide esas ventanas.

## Hacer
Informe `docs/sessions/2026-10-xx-presupuesto-monitor-turno-completo.md` con números medidos de las corridas existentes:
1. CPU y temperatura por minuto de monitor (logs de hardware en `system-setup` o cron `sushi-inspector-hardware`), fps efectivos y timeouts del Hailo.
2. Disco por hora de monitor (clips, HQ, periódicas) y cómo cambia con la purga por umbral.
3. Cola de análisis SAM que genera una hora de monitor y si alcanza la noche para procesarla.
4. Propuesta de ventanas y cambios necesarios, con riesgos. Sin tocar cron ni systemd."""),

("SEC-1", 3, "Aislamiento de los agentes implementadores (ADR + prueba de concepto)",
 contrato("research", "low", "agents", "", "agents/DECISIONS.md, agents/docs/**, agents/scripts/poc_sandbox_*.sh", "", "alta", "Opus 5.5"),
 """## Objetivo
Los implementadores corren con `acceptEdits`, `yolo` o `--dangerously-skip-permissions` como el usuario `admin` en la Pi de producción. `FLOW.md` lo reconoce: un worktree no es un sandbox y las guardas actúan después.

## Hacer
1. ADR en `agents/DECISIONS.md` que compare un usuario unix dedicado (como `openclaw-agent`, ver `system-setup/2026-08-08-ssh-openclaw-agent.md`), bubblewrap y un contenedor, evaluando: acceso a tokens (`~/.config`), systemd, la cámara, la red y el costo de mantener las sesiones de cuenta de cada CLI.
2. Script de prueba `agents/scripts/poc_sandbox_bwrap.sh` que corre un comando dentro del aislamiento propuesto y demuestra que no puede leer `~/.config/sushi-inspector` ni llamar a `systemctl`. No instalarlo ni cambiar `providers.json`."""),

("OPS-7", 3, "Simulacro de restauración del backup de Drive",
 contrato("code", "low", "scripts", ut("scripts", "test_simulacro_restore.py"),
          "scripts/simulacro_restore.py, scripts/test_simulacro_restore.py", "deepseek", "baja", "Haiku 4.5 / DeepSeek"),
 """## Objetivo
`sushi-backup-drive` respalda eventos cada noche, pero nunca se probó restaurar. La SD tiene la única copia local del GT (4.383 fotos `reviewed`).

## Hacer
1. `scripts/simulacro_restore.py`: baja una muestra aleatoria (≤50 archivos) del último backup a un directorio temporal, compara sha256 contra el original y escribe `reports/simulacro_restore_<fecha>.json`. Reutilizar la herramienta que ya usa el servicio de backup.
2. Sin programarlo; dejar la línea de cron mensual en el resumen.

## Criterios de aceptación
- Tests con un backend simulado: archivo corrupto → falla reportada."""),

("EVT-5", 3, "Importador de ventas para medir recall externo",
 contrato("code", "low", EVT, ut(EVT, "test_recall_externo.py"),
          f"{EVT}/recall_externo.py, {EVT}/test_recall_externo.py, {EVT}/fixtures_ventas/**, docs/**", "", "media", "Gemini 3.5 / Sonnet 5"),
 """## Objetivo
El recall se mide contra el propio sistema (clips y fotos periódicas). Falta una verdad externa: las ventas del turno. La planilla todavía no llegó.

## Hacer
1. Definir en `docs/FORMATO_VENTAS.md` el CSV mínimo (hora, cantidad de cajas, tipo/combo si existe) que Gustavo o el local pueden exportar.
2. `recall_externo.py`: cruza ventas con cierres detectados por ventana horaria (tolerancia configurable) y calcula el recall externo sólo en los minutos cubiertos por el monitor.
3. Probar con un CSV sintético en `fixtures_ventas/`.

## Criterios de aceptación
- Tests: una venta sin cierre dentro de la ventana cuenta como FN; una venta fuera de la cobertura del monitor no cuenta."""),

("CNT-5", 3, "Guion de la sesión física de captura (tipos de piezas y conteos)",
 contrato("research", "low", "tickets/2026.09.19_tipos_piezas_v0.1", "", "tickets/2026.09.19_tipos_piezas_v0.1/**, docs/**", "", "baja", "Gemini 3.5 / Sonnet 5"),
 """## Objetivo
Tipos de piezas y el contador están bloqueados desde el 19/09 por tres acciones humanas: nombrar los clusters, sacar 5–10 fotos por tipo con la C922 y hacer ~80 conteos más. Es una sola sesión física.

## Hacer
1. Guion de una página, que se pueda seguir con el celular en la mano: qué cajas armar, en qué orden, cuántas fotos, qué capturar en :8080 y qué cargar en :8093.
2. Hojas de contacto por cluster usando el clustering por grupo (`clusters_pegada_k4` / `clusters_separable_k4`, no el global) con un campo para escribir el nombre. Marcar los clusters 1 y 7 como posible corte por luz.
3. Lista de los 3 tipos sin referencia (Lango Feel, Spicy Shrimp, Spicy New York Phila).
4. Estimación de tiempo total de la sesión."""),

("GOV-3", 3, "Notas de versión por componente generadas desde los commits",
 contrato("code", "low", "scripts", ut("scripts", "test_changelog_componentes.py"),
          "scripts/changelog_componentes.py, scripts/test_changelog_componentes.py, docs/changelog/**", "deepseek", "baja", "DeepSeek"),
 """## Objetivo
Hay 16 versiones independientes y `/arquitectura` enlaza bitácoras en vez de notas de versión.

## Hacer
1. `scripts/changelog_componentes.py`: agrupa los commits por componente (prefijo `feat(x)`, `fix(x)`, `agent(VIK-n)` y rutas tocadas) y por tag `release-*`, y escribe `docs/changelog/<componente>.md`.
2. Salida estable (misma entrada, mismo archivo) para no generar diffs espurios.

## Criterios de aceptación
- Tests con un repo git temporal."""),

# ---------------- Fase 3 · Expansión ----------------
("EVT-4", 2, "Tests de caracterización y primer corte de revisar_monitor.py",
 contrato("code", "medium", EVT, ut(EVT, "test_*.py"), "", "claude", "alta", "Opus 5.5", timeout=120),
 """## Objetivo
`revisar_monitor.py` tiene 2.284 líneas y `monitor_vivo.py` 1.168. Casi todos los tickets tocan los mismos archivos y eso genera conflictos entre dev y main.

## Hacer
1. Tests de caracterización de la API de :8094 (endpoints JSON actuales con datos fijos) antes de mover código.
2. Extraer la capa de datos (listar clips, veredictos, dedup, video continuo) a `revisor_datos.py`, dejando el HTML y las rutas en `revisar_monitor.py`. Sin cambiar comportamiento.
3. `monitor_vivo.py` no se toca en esta tarea.

## Criterios de aceptación
- Todos los tests existentes y los nuevos verdes; respuestas JSON idénticas antes y después sobre los datos fijos."""),

("CNT-4", 2, "Cola de análisis SAM vs horas de monitor: medir y proponer",
 contrato("research", "low", REG, "", f"{REG}/reports/**, docs/**", "", "alta", "Opus 5.5"),
 """## Objetivo
SAM corre en CPU y compite con el monitor (VIK-11 lo mitiga pausando el análisis mientras la cámara está activa). Con más horas de monitor, la cola va a crecer.

## Hacer
Informe con: tiempo de análisis por evento (selector v3), eventos por hora de monitor, capacidad nocturna disponible, y opciones (batch nocturno, SAM más chico, segmentación en Hailo) con su costo estimado y riesgo. Sin cambiar código de producción."""),

("CNT-7", 2, "Inferencia de combo a partir de tipos y conteo: diseño",
 contrato("research", "low", "tickets/2026.09.19_tipos_piezas_v0.1", "", "tickets/2026.09.19_tipos_piezas_v0.1/**, docs/**", "", "alta", "Opus 5.5 · Gemini 3.5"),
 """## Objetivo
El objetivo del track de tipos es inferir el combo de cada caja. `combos.json` tiene 7 combos y 2 gohan con el total de piezas, pero no el reparto por tipo.

## Hacer
Diseño en `docs/`: qué datos faltan (reparto por tipo, fotos de cajas de composición conocida), cómo se evaluaría con holdout por sesión y qué se puede hacer ya con conteo solo (por ejemplo, descartar combos imposibles por total). Depende de CNT-5; no entrenar nada."""),
]


def body(item) -> str:
    _, _, _, k, b = item
    return f"{k}\n\n{b}\n\n{REGLAS}"


def validate() -> list[str]:
    errs = []
    for item in T:
        tid, prio, title = item[0], item[1], item[2]
        c = flow.parse_contract(body(item))
        if c.get("agente") != "listo":
            errs.append(f"{tid}: sin agente: listo")
        if c.get("tipo") not in {"code", "data", "test", "research", "security", "release"}:
            errs.append(f"{tid}: tipo inválido {c.get('tipo')}")
        if c.get("riesgo") not in {"low", "medium", "high"}:
            errs.append(f"{tid}: riesgo inválido")
        if not c.get("componente"):
            errs.append(f"{tid}: sin componente")
        if c.get("tipo") != "research" and not c.get("test"):
            errs.append(f"{tid}: sin test")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sin-telegram", action="store_true")
    args = ap.parse_args()

    errs = validate()
    if errs:
        print("Contratos inválidos:\n  " + "\n  ".join(errs))
        return 2
    if args.dry_run:
        for item in T:
            c = flow.parse_contract(body(item))
            print(f"[{item[0]}] p{item[1]} {c['tipo']}/{c['riesgo']} {c['componente']} :: {item[2]}")
        print(f"\n{len(T)} contratos válidos (dry-run, no se tocó Vikunja).")
        return 0

    cfg = json.loads((ROOT / "agents/flow_config.json").read_text())["tracker"]
    vik = flow.vikunja_adapter.VikunjaTracker(cfg)
    c = vik.client
    raw = c.request("GET", f"/projects/{cfg['project_id']}/tasks?format=markdown&per_page=200")["items"]   # forma cruda: se necesita el id del usuario
    existing = {t["title"].split("]")[0] + "]": t for t in raw if t["title"].startswith("[")}
    bot_id = None
    for t in raw:
        for a in t.get("assignees") or []:
            if a["username"].lower() == BOT.lower():
                bot_id = a["id"]
    if bot_id is None:
        print(f"No encontré el id de {BOT} en tickets existentes.")
        return 2

    creados, saltados, sin_asignar = [], [], []
    for item in T:
        tid, prio, title = item[0], item[1], item[2]
        key = f"[{tid}]"
        if key in existing:
            saltados.append(f"{key} ya existe (#{existing[key]['id']})")
            continue
        t = c.request("POST", f"/projects/{cfg['project_id']}/tasks?format=markdown",
                      {"title": f"{key} {title}", "description": body(item), "priority": prio})
        vid = t["id"]
        ok, got = False, t
        for method, path, payload, ctype in (
            ("PUT", f"/tasks/{vid}/assignees", {"user_id": bot_id}, "application/json"),
            ("POST", f"/tasks/{vid}/assignees", {"user_id": bot_id}, "application/json"),
            ("PATCH", f"/tasks/{vid}", {"assignees": [{"id": bot_id}]}, "application/merge-patch+json"),
        ):
            try:
                c.request(method, path, payload, content_type=ctype)
            except ValueError:
                continue
            got = c.request("GET", f"/tasks/{vid}?format=markdown")
            if any(a["id"] == bot_id for a in got.get("assignees") or []):
                ok = True
                break
        if got.get("priority") != prio:
            try:
                c.request("PATCH", f"/tasks/{vid}", {"priority": prio}, content_type="application/merge-patch+json")
            except ValueError:
                pass
        (creados if ok else sin_asignar).append(f"{key} → VIK-{vid}")
        print(("OK  " if ok else "SIN ASIGNAR  ") + f"{key} → VIK-{vid} {title}")

    print(f"\ncreados {len(creados)} · ya existían {len(saltados)} · sin asignar {len(sin_asignar)}")
    if not args.sin_telegram:
        msg = (f"🍣 Plan de mejoras cargado en Vikunja: {len(creados)} tickets nuevos"
               + (f", {len(saltados)} ya estaban" if saltados else "")
               + (f", {len(sin_asignar)} SIN ASIGNAR (revisar)" if sin_asignar else "")
               + ". El flujo los toma por prioridad (fase 0 primero) con revisión cruzada; sólo te escribe si algo es crítico.\n"
               "Plan: https://claude.ai/artifact/Ho3qXEKUr5V46oKjUpgoLB")
        subprocess.run([sys.executable, str(ROOT / "scripts/notificar.py"), msg], timeout=90)
    return 0 if not sin_asignar else 1


if __name__ == "__main__":
    raise SystemExit(main())
