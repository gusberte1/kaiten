# Ciclo de Vida, Releases y Auto-Actualización en Terminales

Este documento especifica el ciclo de vida de desarrollo de Kaiten y la arquitectura óptima para que las terminales y servidores donde corre el flujo se mantengan **actualizados automáticamente** con cada versión aprobada.

---

## 1. Estrategia de Ramas: Desarrollo en `dev` y Releases en `main`

El repositorio de Kaiten sigue una política estricta de estabilidad:

```
feature / ticket (agent/<id>) 
           │
           ▼  (Pull Request / Integración validada)
         dev  ◄─── Entorno de pruebas continuas (Tests 100% OK)
           │
           ▼  (Aprobación Humana / Release)
        main  ◄─── Producción inmutable (Tags: release-YYYYMMDD-HHMM)
           │
           ▼  (Auto-Update Periódico)
   Terminales / Hosts (Raspberry Pi, servidores, estaciones de trabajo)
```

1. **Rama `dev`:**
   * Es la rama activa de desarrollo, validación y nuevas capacidades.
   * Ningún cambio va directo a `main`.
   * Los tests unitarios (`python3 -m unittest discover -s . -p "test_*.py"`) deben pasar íntegramente al 100%.

2. **Rama `main` (Releases):**
   * Solo contiene versiones estables y formalmente aprobadas.
   * La promoción de `dev` a `main` se realiza mediante el comando de release:
     ```bash
     python3 flow.py release --approve
     ```
     o vía merge `--no-ff` con creación de etiqueta:
     ```bash
     git checkout main
     git merge --no-ff dev -m "release: release-$(date +%Y%m%d-%H%M)"
     git tag -a "release-$(date +%Y%m%d-%H%M)" -m "Release aprobado"
     git push origin main --tags
     ```

---

## 2. Implementación Óptima de Instalación y Auto-Actualización

Para que cualquier máquina que consuma Kaiten no requiera comandos manuales cada vez que evoluciona el motor en GitHub, se implementa una arquitectura desacoplada basada en **instalación canónica de usuario + Systemd Timer con compuerta de tests**.

### 2.1. Estructura en el Sistema Operativo
* **Directorio del motor:** `~/.local/share/kaiten` (clon de `git@github-kaiten:gusberte1/kaiten.git` trackeando `main`).
* **Binario CLI en PATH:** Enlace simbólico en `~/.local/bin/kaiten` apuntando a `~/.local/share/kaiten/flow.py`.
* **Proyectos gestionados:** Cualquier directorio de trabajo (ej. `/home/admin/Documents/sushi-inspector`) que contenga su manifiesto `flow-project.json` o `kaiten.project.json`.

---

### 2.2. El Flujo de Auto-Actualización Segura (`kaiten-autoupdate.sh`)

La actualización automática nunca debe romper un entorno en producción. Por ello, el script sigue un protocolo de **4 compuertas**:

1. **Compuerta de Estado Limpio:** Verifica que el directorio local de Kaiten esté en `main` y sin archivos modificados sin commitear (`git status --porcelain`).
2. **Compuerta de Novedades:** Ejecuta `git fetch origin main --tags`. Si el commit local coincide con el remoto, termina silenciosamente (`rc=0`).
3. **Compuerta de Tests Locales:** Antes de aplicar la actualización sobre el entorno activo, ejecuta la suite de pruebas contra el código entrante:
   ```bash
   python3 -m unittest discover -s . -p "test_*.py"
   ```
4. **Compuerta de Aplicación y Reinicio Suave:**
   * Aplica `git merge --ff-only origin/main`.
   * Si existe un servicio del flujo corriendo en segundo plano (ej. `kaiten-flow.service`), envía una señal de reinicio suave (`systemctl --user reload-or-restart`).
   * Si los tests fallan, **aborta la actualización**, resguarda el log de fallo y envía una notificación sin tocar el código en ejecución.

---

### 2.3. Configuración del Temporizador (`Systemd User Timer`)

El servicio corre bajo el usuario local (sin necesidad de permisos `sudo` ni credenciales de root).

#### A. Servicio: `~/.config/systemd/user/kaiten-autoupdate.service`
```ini
[Unit]
Description=Kaiten Engine Safe Auto-Updater
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=%h/.local/share/kaiten
ExecStart=%h/.local/share/kaiten/scripts/kaiten-autoupdate.sh
TimeoutStartSec=180
StandardOutput=journal
StandardError=journal
```

#### B. Timer: `~/.config/systemd/user/kaiten-autoupdate.timer`
```ini
[Unit]
Description=Revisión periódica de nuevas versiones estables de Kaiten

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

* **Activación en la terminal:**
  ```bash
  systemctl --user daemon-reload
  systemctl --user enable --now kaiten-autoupdate.timer
  ```

Con esta configuración, cada terminal revisa cada 1 hora si Gustavo aprobó un nuevo release en `main`, valida los tests en su propia arquitectura y se actualiza de forma transparente.
