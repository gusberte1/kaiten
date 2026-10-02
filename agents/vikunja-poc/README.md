# Vikunja POC aislada

Stack Docker local de una sola instancia Vikunja v2.6.0 con SQLite. Está
aislada de los servicios existentes: no usa systemd, cron, proxy, ni expone
puertos LAN; escucha únicamente en la IP actual de Tailscale por el puerto
`3457`.

## Arranque

```bash
cd agents/vikunja-poc
bash bootstrap.sh
bash healthcheck.sh
```

El bootstrap crea `db/`, `files/` y `.env` con un secreto aleatorio no
versionado. Luego crear manualmente el usuario administrador en la UI:
`http://IP_TAILSCALE:3457/`.

## Token del adaptador

En Vikunja: **Settings → API Tokens → Create token**. Nombrarlo
`orchestrator-poc`, con vencimiento corto durante el piloto, y conceder sólo
**Projects → read_all**, **Tasks → read_all, create, update** y **Task
Comments → read_all, create**. Es un perfil único para que el controlador
opere el backlog sin pedir permisos por cada evento. No habilitar permisos de
administración, usuarios, adjuntos o borrado. El token queda sólo en la
Raspberry; los proveedores de agentes nunca lo reciben. Copiarlo una única vez
y cargarlo sin pegarlo en un chat:

```bash
bash agents/vikunja-poc/guardar-token.sh
bash agents/vikunja-poc/api-smoke.sh
```

El puente se prueba con `agents/vikunja_adapter.py`. Consulta el OpenAPI que
sirve la propia instancia y usa exclusivamente API v2; su primera tarea será
crear un proyecto `Sushi Inspector — Agentes POC` y un ticket de riesgo bajo.

## Límites de la POC

- SQLite es deliberado: sirve para un piloto de una persona, no para el MVP
  multiusuario.
- La imagen está fijada a `2.6.0`; no se usa `latest`.
- La versión estable 2.6.0 no incorpora MCP nativo (requiere 2.7.0); la POC
  usa API v2 y el controlador como frontera de política.
- Para detener sin borrar datos: `docker compose down`. No ejecutar `down -v`.

## Puente previsto

1. El humano crea/prioriza el ticket en Vikunja.
2. Un webhook firmado llega al adaptador local.
3. El adaptador crea/actualiza la tarea en `orchestrator.py`.
4. El controlador registra `run_id`, rama, evidencia y approval.
5. El adaptador publica al ticket sólo eventos sanitizados, nunca secretos ni
   transcripciones de sesión.
