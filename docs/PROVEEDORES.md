# Configuración y Replicación Autónoma de Proveedores en Kaiten

Este documento describe la **arquitectura, configuración y estado final requerido** de cada proveedor de modelos para que el sistema Kaiten ejecute flujos agénticos de forma **100% autónoma, segura y administrable remotamente vía web**, sin requerir intervención humana en operaciones seguras ni autorrevisarse.

---

## 1. Principios de Operación Autónoma

1. **Aislamiento de Entorno (Worktrees):** El motor nunca ejecuta código directamente sobre ramas productivas (`main`). Cada tarea corre en un Git worktree independiente (`agent/<tarea>`).
2. **Barrera de Autorrevisión:** El proveedor que implementa nunca puede revisar su propio código; el revisor pertenece obligatoriamente a una familia de modelos diferente.
3. **Protección de Costos y Facturación (`strip_env`):** Los agentes nunca heredan variables de entorno con API keys de pago por token (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). Toda ejecución utiliza suscripciones fijas / cuentas autenticadas (OAuth), salvo DeepSeek que aísla su clave en un archivo seguro dedicado.
4. **Permisos y Autonomía ("Always-Proceed con Lista Negra"):** El agente ejecuta comandos, lecturas, escrituras y tests sin cuadros de diálogo bloqueantes, pero cualquier comando de destrucción o riesgo crítico queda retenido en una compuerta estricta.

---

## 2. Proveedor: Google Antigravity (`agy`)

Antigravity proporciona acceso a modelos de Gemini, Claude (Sonnet 5.5 High) y GPT-OSS (120B) mediante la CLI oficial de Google `agy`. Fue diseñado para operar tanto en terminal local como bajo administración remota.

### 2.1. Binario y Rutas del Sistema
* **Ubicación:** `~/.local/bin/agy` (debe estar en el `PATH` del usuario, vía `~/.bashrc`).
* **Arquitectura soportada:** `aarch64` (ARM 64-bit) y `x86_64`.
* **Verificación:** `agy --version`.

### 2.2. Autenticación y Cero-Costo por Token
* **Modo de cuenta:** Debe operar exclusivamente bajo sesión de cuenta Google autenticada:
  ```bash
  agy auth login
  ```
* **Comprobación de salud en Kaiten:**
  El motor ejecuta `agy -p /usage --output-format json` y verifica que `status == "SUCCESS"`.
* **Prohibición de API Key:** En `~/.gemini/antigravity-cli/settings.json`, la clave `modelProvider` **no** debe estar fijada a `"gemini"` (eso forzaría cobro por API key). Si existe, debe eliminarse.

### 2.3. Configuración para Autonomía Local (Headless / Flow Engine)
Para que el motor ejecute tareas en lote sin que el proceso se cuelgue pidiendo aprobaciones interactivas:
* **Archivo de configuración:** `~/.gemini/antigravity-cli/settings.json` (permisos `chmod 600`).
* **Contenido requerido:**
  ```json
  {
    "permissions": {
      "ask": [
        "command(sudo)",
        "command(rm -r)",
        "command(rm -rf)",
        "command(git push)",
        "command(git reset --hard)",
        "command(git clean)",
        "command(git checkout --)",
        "command(git restore)",
        "command(git branch -D)",
        "command(docker rm)",
        "command(docker rmi)",
        "command(docker system prune)",
        "command(docker compose down)",
        "command(systemctl stop)",
        "command(systemctl disable)",
        "command(systemctl restart)",
        "command(reboot)",
        "command(shutdown)",
        "command(poweroff)",
        "command(dd)",
        "command(mkfs)",
        "command(chmod -R)",
        "command(chown -R)",
        "command(pkill)",
        "command(kill)"
      ]
    },
    "toolPermission": "always-proceed"
  }
  ```
* **Invocación desde Kaiten (`providers.json`):**
  * **Implementador:**
    ```bash
    agy -p "{prompt}" --output-format json --mode=accept-edits --dangerously-skip-permissions
    ```
  * **Revisor (solo lectura):**
    ```bash
    agy -p "{prompt}" --output-format json --mode=plan
    ```
    *(Nota: El motor además verifica con `git status` que el árbol del revisor no haya sido modificado).*

### 2.4. Configuración para Administración Remota y Web (Jetski / Web IDE)
Para que las sesiones puedan iniciarse o administrarse remotamente desde la web (ej. UI Antigravity en `:8105` o control remoto oficial):
* **Archivo de políticas de sesión remota:** `~/.gemini/config/config.json`.
* **Estructura requerida dentro de `userSettings`:**
  ```json
  {
    "userSettings": {
      "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_EAGER",
      "internetAccessPolicy": "AGENT_SETTING_POLICY_ALLOW",
      "globalPermissionGrants": {
        "permissionGrants": {
          "ask": [
            "command(sudo)",
            "command(rm -r)",
            "command(rm -rf)",
            "command(git push)",
            "command(git reset --hard)",
            "command(git clean)",
            "command(git checkout --)",
            "command(git restore)",
            "command(git branch -D)",
            "command(docker rm)",
            "command(docker rmi)",
            "command(docker system prune)",
            "command(docker compose down)",
            "command(systemctl stop)",
            "command(systemctl disable)",
            "command(systemctl restart)",
            "command(reboot)",
            "command(shutdown)",
            "command(poweroff)",
            "command(dd)",
            "command(mkfs)",
            "command(chmod -R)",
            "command(chown -R)",
            "command(pkill)",
            "command(kill)"
          ]
        }
      }
    }
  }
  ```
  * `CASCADE_COMMANDS_AUTO_EXECUTION_EAGER`: Permite que el agente encadene llamadas a herramientas sin pausar la sesión remota en espera de confirmaciones humanas triviales.

### 2.5. Servicio Daemon de Control Remoto (`systemd --user`)
El daemon remoto de Antigravity debe correr como servicio de usuario persistente:
* **Unidad:** `~/.config/systemd/user/antigravity-cli-daemon.service`
  ```ini
  [Unit]
  Description=antigravity remote-control daemon
  After=network.target
  StartLimitIntervalSec=0

  [Service]
  Type=simple
  ExecStart=/home/admin/.local/bin/agy remote-control serve
  Restart=on-failure
  RestartSec=10s
  RestartPreventExitStatus=3
  TimeoutStopSec=30s
  StandardOutput=journal
  StandardError=journal

  [Install]
  WantedBy=default.target
  ```
* **Comandos de control:**
  ```bash
  systemctl --user daemon-reload
  systemctl --user enable --now antigravity-cli-daemon.service
  ```
* **Script vigilante de auto-recuperación (`scripts/agy_remoto.sh`):**
  Un script que valida no solo que el proceso exista, sino que el journal reporte `Authenticated as <cuenta>`. En caso de token vencido o socket caído, detiene y relanza el daemon de forma segura sin exponer credenciales.

---

## 3. Proveedor: Claude Code (`claude`)

### 3.1. Binario y Entorno
* **Instalación:** Vía npm global o NVM (`node >= 20`).
* **Binario:** `claude` disponible en el `PATH`.

### 3.2. Configuración Autónoma
* **Invocación desde Kaiten:**
  * **Implementador:**
    ```bash
    claude -p "{prompt}" --permission-mode acceptEdits --add-dir "{cwd}"
    ```
  * **Revisor:**
    ```bash
    claude -p "{prompt}" --permission-mode plan --add-dir "{cwd}"
    ```
* **Sanitización de credenciales:**
  Kaiten elimina automáticamente `ANTHROPIC_API_KEY` y `ANTHROPIC_AUTH_TOKEN` del entorno de ejecución del proceso para consumir exclusivamente la sesión OAuth autenticada (`claude auth login`).

### 3.3. Control Remoto (`claude rc`)
* `claude rc` requiere un pseudo-terminal (TTY).
* Para correr como servicio de fondo desatendido en `systemd --user`:
  ```ini
  [Service]
  ExecStart=/usr/bin/script -qfec "/usr/local/bin/claude rc" /dev/null
  ```

---

## 4. Proveedor: OpenAI Codex CLI (`codex`)

### 4.1. Binario y Ejecución Autónoma
* **Binario:** `codex` en el `PATH`.
* **Invocación desde Kaiten:**
  * **Implementador:**
    ```bash
    codex exec -C "{cwd}" -s workspace-write "{prompt}"
    ```
  * **Revisor:**
    ```bash
    codex exec -C "{cwd}" -s read-only "{prompt}"
    ```

### 4.2. Control Remoto y Resiliencia de Sockets
* **Daemon remoto:** Utiliza `$HOME/.codex/app-server-control/app-server-control.sock`.
* **Problema conocido y solución:** Tras reinicios del sistema o caídas de proceso, el symlink del socket en `/tmp` queda huérfano y bloquea reinicios futuros con error `"control socket is already in use"`.
* **Script de saneamiento (`scripts/codex_remoto.sh asegurar`):**
  Verifica si el PID en `daemon.pid` sigue vivo; si no existe, purga el socket huérfano antes de invocar `codex remote-control start`.

---

## 5. Proveedor: DeepSeek Harness (`dsh`)

### 5.1. Binario y Aislamiento de Credenciales
* **Binario:** `dsh` en el `PATH`.
* **Seguridad de la API Key:**
  DeepSeek no utiliza OAuth; se autentica mediante API key. Para evitar fugas hacia otros procesos, la clave reside en un archivo de solo lectura para el usuario:
  * Archivo: `~/.config/deepseek/api_key` (`chmod 600`).
  * Kaiten inyecta `DEEPSEEK_API_KEY` únicamente en el subproceso hijo de `dsh`.

### 5.2. Invocación Autónoma
* Invocación: `dsh --profile headless "{prompt}"`.
* Como `dsh` no cuenta con un flag nativo de solo lectura para revisiones, el controlador de Kaiten valida de forma determinista mediante hash de Git que ningún archivo del repositorio haya cambiado durante la fase de revisión.

---

## 6. Proveedor: Google Gemini CLI (`gemini`)

### 6.1. Ejecución y Prevención de Facturación por Token
* **Invocación autónoma:**
  * Implementador: `gemini -p "{prompt}" --approval-mode yolo --skip-trust`
  * Revisor: `gemini -p "{prompt}" --approval-mode plan --skip-trust`
* **Restricción mandatoria de cuenta:**
  Se impone la variable de entorno `GOOGLE_GENAI_USE_GCA=true` para forzar el uso de la cuenta Google y se purgan:
  `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`.
  Si no hay una sesión activa en `~/.gemini/google_accounts.json`, el proveedor queda deshabilitado preventivamente por la compuerta de salud.

---

## 7. Matriz Resumen de Proveedores

| Proveedor | Binario | Flag Implementar (Autónomo) | Flag Revisar (Solo Lectura) | Mecanismo de Control Remoto | Aislamiento de Costos |
|---|---|---|---|---|---|
| **Antigravity** | `agy` | `--mode=accept-edits --dangerously-skip-permissions` | `--mode=plan` | `agy remote-control serve` (systemd user) | OAuth Cuenta Google (`strip_env` API keys) |
| **Agy-Claude** | `agy` | `--model claude-sonnet-5-5-high ...` | `--model claude-sonnet-5-5-high --mode=plan` | Compartido con Antigravity | Cuota Antigravity ("Claude and GPT models") |
| **Agy-GPT** | `agy` | `--model gpt-oss-120b-medium ...` | `--model gpt-oss-120b-medium --mode=plan` | Compartido con Antigravity | Cuota Antigravity ("Claude and GPT models") |
| **Claude Code** | `claude` | `--permission-mode acceptEdits --add-dir {cwd}` | `--permission-mode plan --add-dir {cwd}` | `claude rc` envuelto en pty virtual | OAuth Anthropic (`strip_env` API keys) |
| **Codex** | `codex` | `exec -C {cwd} -s workspace-write` | `exec -C {cwd} -s read-only` | `codex remote-control` + saneamiento socket | Suscripción CLI |
| **DeepSeek** | `dsh` | `--profile headless` | `--profile headless` (controlador verifica diff) | Web UI loopback :3080 | Archivo aislado `~/.config/deepseek/api_key` |
| **Gemini CLI** | `gemini` | `--approval-mode yolo --skip-trust` | `--approval-mode plan --skip-trust` | N/A | `GOOGLE_GENAI_USE_GCA=true` obligatorio |
