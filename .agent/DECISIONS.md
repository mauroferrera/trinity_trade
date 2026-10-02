# DECISIONS.md - Decisiones Tomadas y Clarificaciones

> Registro persistente de decisiones para evitar re-preguntar entre sesiones. Se actualiza al responder las clarificaciones del PLAN.md (Sección 6).

## ✅ Bloqueo: Código de Referencia Ausente — RESUELTO (2026-10-01)

**Situación detectada:** el proyecto de referencia se buscó en `Trinity_proyect/Trading/`
y resultó vacío: 3 archivos (968 B), todos bajo `.git/logs/`, sin objetos, sin `HEAD`, sin
`config`. Todos sus directorios tenían el atributo `ReparsePoint` de OneDrive, es decir era
un **placeholder** en la nube, no una copia local. También se verificó
`C:/Users/fmaur/OneDrive/Desktop/Trading/` — **0 archivos**.

**Resolución:** el código **sía existe** en otra ruta:

```
C:\Users\fmaur\Desktop\Trading
```

**Evidencia:** inventario verificado el 2026-10-01 — `app.py` 5.185 líneas,
`store.py` 1.752, `agent.py` 1.308, `watcher.py` 883, `strategy.py` 779,
`risk_engine.py` 628, `chartism_engine.py` 542, `mock_feed.py` 519, `simulator.py` 507,
`symbol_specs.py` 477, `tclock.py` 418, `pattern_engine.py` 234, `strategy.yaml`
(99 líneas), `trading.db` (692 KB), módulo `core/` propio (`paths.py`,
`trade_history.py`), `tests/` con **41 archivos de test** + `conftest.py` + `pytest.ini` +
`fixtures/` + `scenarios/`, `.env`, `.env.example`, `.gitignore`.

**Causa probable:** OneDrive dejó una carpeta espejo vacía; el proyecto real vive fuera
de OneDrive. La copia dentro de `Trinity_proyect/` es un artefacto roto.

**Estado:** Cerrado. Fases 1–7 desbloqueadas.

---

## Decisiones Pendientes (Sin Responder)

### 1. Alcance Inicial
**Pregunta:** ¿Scaffolding + `README.md` + `.agent/*` únicamente (para subir a GitHub) o **empezar a portar `core/` (Fase 1)**?

**Respuesta:** _Pendiente de decisión del usuario_

**Fecha:** _Por definir_

---

### 2. Prioridad de Mercado
**Pregunta:** ¿**B3 primero** (siguiendo `contexto.md`) o mantener enfoque **Forex actual** y añadir B3 después?

**Respuesta:** _Pendiente de decisión del usuario_

**Fecha:** _Por definir_

---

### 3. Proveedor B3 Inicial
**Pregunta:** ¿**MT5 B3** (rápido/gratis, reutiliza bridge) o **Nelogica/Profit** directamente (producción)?

**Respuesta:** _Pendiente de decisión del usuario_

**Fecha:** _Por definir_

---

### 4. División de `app.py`
**Pregunta:** ¿**Big-bang** o **extracción incremental por dominios** (recomendado, menor riesgo)?

**Respuesta:** _Pendiente de decisión del usuario_

**Nota:** AGENT_GUIDELINES Regla 7 ya fija "extracción incremental, nunca big-bang". Confirmar o registrar excepción explícita.

**Fecha:** _Por definir_

---

### 5. Base de Datos
**Pregunta:** ¿**Reutilizar `REF/trading.db`** (preservar histórico) o **empezar limpio** en nueva estructura (manteniendo histórico)?

**Respuesta:** _Pendiente de decisión del usuario_

**Fecha:** _Por definir_

---

## Decisiones Tomadas

### D-001 — Horarios de mercado verificados contra fuente oficial
**Fecha:** 2026-10-01
**Decisión:** `config/trading_hours.json` expresa todas las ventanas en **hora local del exchange con timezone IANA** (ej. `America/Sao_Paulo`), no en UTC hardcodeado. Motivo: el horario de Forex se desplaza con el DST de Londres/NY, mientras B3 no aplica DST desde 2019. Así el desfase se resuelve con `zoneinfo` en cada evaluación.
**Consecuencia:** cualquier consumidor debe usar `zoneinfo`, nunca restar offsets fijos.

### D-002 — Nivel de confianza por ventana de horario
**Fecha:** 2026-10-01
**Decisión:** Cada sesión de `trading_hours.json` declara `confidence` (`high` / `medium` / `unverified`) y las no verificadas llevan `requires_verification: true`.
**Consecuencia:** La sesión `b3_fixed_income` (NTN-F) quedó en `unverified` — la B3 declara que los contratos de renda fija "permanecerán inalterados" pero la grade no aparece en el Ofício Circular consultado. **No usar en ejecución de producción hasta verificar** contra la página oficial de renda fixa.

### D-003 — `README.md` refleja el estado real, no el deseado
**Fecha:** 2026-10-01
**Decisión:** El README documenta explícitamente qué está implementado y qué no, incluyendo que `core/`, `adapters/`, `api/`, etc. aún están vacíos. Se actualizó cuando se restauró el código de referencia (ver D-005).
**Consecuencia:** quien lea el repo no asume capacidades inexistentes.

### D-005 — Ruta canónica del código de referencia
**Fecha:** 2026-10-01
**Decisión:** el código de referencia vive **fuera** de este repo, en `C:\Users\fmaur\Desktop\Trading`, y se trata
como **solo lectura**. En `.agent/*` y `README.md` se referencia como `REF/`. No se crea
enlace ni copia dentro de `Trinity_proyect/`.
**Consecuencia:** los comandos de port (Fases 1–7) leen desde `REF/` y nunca escriben allí.
No se crea enlace ni copia dentro de `Trinity_proyect/`.

### D-006 — La suite de tests del monolito es el activo principal de regresión
**Fecha:** 2026-10-01
**Decisión:** los 41 tests de `REF/tests/` (incl. `test_risk_engine.py`,
`test_simulator.py`, `test_paths.py`, `test_db_isolation.py`, `test_watcher.py`) se portan
como base de `tests/unit/` y `tests/integration/`. La Regla 6 (tests verdes antes de marcar
completado) se valida contra ellos y no contra tests nuevos.
**Consecuencia:** antes de tocar `core/` hay que tener línea base verde de los tests del
monolito, para poder detectar regresiones al portar.

### D-004 — Enlace explícito `asset_sources_map.yaml` → `trading_hours.json`
**Fecha:** 2026-10-01
**Decisión:** Cada activo declara `session_id` que resuelve contra `sessions.<id>` en `trading_hours.json`. También se corrigió `data_adapter: binance_ccxt` → `binance_ws` para que coincida con el nombre de módulo planificado (`adapters/crypto/binance_ws.py`).
**Consecuencia:** el orquestador de fuentes y el calendario de sesiones quedan coherentes y verificables por test.

### D-007 — Se elimina el placeholder `Trinity_proyect/Trading/` (resuelto por el agente)
**Fecha:** 2026-10-01
**Contexto:** el usuario cambió la configuración de OneDrive para que los archivos nuevos ya no
se suban a la nube. Eso explica el incidente: `Trinity_proyect/Trading/` era un *placeholder*
(no una copia local) y quedó sin datos recuperables en cuanto dejó de estar sincronizado.
**Decisión:** eliminar la carpeta fantasma y **no** recrearla ni enlazarla. Verificación previa
obligatoria antes de borrar: se confirmó que la única copia del código vive en `REF/`, que su
árbol está versionado, con 198 objetos git, 5 sin seguimiento (`.opencode/`, `COMO_OPERAR.md`,
3 backups de `trading.db`) y remote `https://github.com/mauroferrera/botTraiding.git`. Nada de lo
eliminado era recuperable ni único.
**Consecuencia:** queda una única fuente de verdad. `Trinity_proyect/Trading/` está en
`.gitignore` y cualquier reaparición se interpreta como incidente de OneDrive, no como código.

### D-008 — Riesgo residual: el proyecto sigue dentro de OneDrive
**Fecha:** 2026-10-01
**Situación:** `Trinity_proyect/` está en `C:\Users\fmaur\OneDrive\Desktop\` y sus 23 directorios
tienen el atributo `ReparsePoint` de OneDrive, aunque su contenido está hidratado (los archivos
son `Archive` normal y se leen/escriben con normalidad). El repo de referencia **no** tiene `.git`
todavía; su único control de versiones está en `C:\Users\fmaur\Desktop\Trading\.git`.
**Pendiente de decisión del usuario:** mover `Trinity_proyect/` a `C:\Users\fmaur\Desktop\` y
inicializar git, para eliminar de raíz la dependencia de OneDrive. Hasta entonces, evitar
`Files On-Demand` / "Liberar espacio" sobre esta carpeta.