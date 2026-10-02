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
**Mitigación aplicada:** `Trinity_proyect/` se inicializó como repo git y se publicó en
`https://github.com/mauroferrera/trinity_trade` (rama `main`, commit inicial
`5259978 chore: scaffolding inicial del monorepo Trinity (Fase 0)`, 31 archivos). El trabajo ya
no depende de la copia local: si OneDrive vuelve a colocar los archivos, se restauran con
`git clone`. `.gitignore` excluye secretos, DBs, backups y cachés.

**Pendiente de decisión del usuario:** mover `Trinity_proyect/` a `C:\Users\fmaur\Desktop\` para
eliminar de raíz la dependencia de OneDrive. Es reversible y no urgente ahora que hay red de
seguridad en GitHub. Mientras tanto, evitar `Files On-Demand` / "Liberar espacio" sobre esta
carpeta. Nota: el remoto quedó **público**; decidir si pasa a privado.
### D-009 - `core/` es puro por construccion, y se comprueba con un test

**Contexto:** la regla del monorepo es que `core/` no importa `adapters`, `api`,
`agent`, MT5 ni ningun proveedor. En REF esa separacion no existia: `risk_engine.py`,
`pattern_engine.py` y `market_view.py` vivian en la raiz junto a `app.py`.

**Decision:** cada motor puro se mueve a `core/` y la regla se hace ejecutable en
`tests/unit/test_core_purity.py`, que analiza el AST de cada modulo (imports prohibidos,
llamadas a `open()`, referencias a la BD) y ademas comprueba el grafo de imports ya
construido para cazar imports dinamicos que el AST no ve.

**Por que un test y no una nota:** esa dependencia no rompe al compilar ni al ejecutar.
Importar MT5 desde `core/` sigue dando los mismos numeros en la maquina que tiene MT5,
y el nucleo deja de ser testeable fuera de ella sin que nada se entere. Solo se nota
demasiado tarde. El test se ha verificado metiendo un modulo deliberadamente impuro en
una carpeta temporal: salta en los cuatro checks.

**Alternativa descartada:** un `test_paths.py` que solo compruebe que las rutas existen.
No detecta que un modulo las recalcule por su cuenta.

### D-010 - `OrderFlowEngine` recibe `symbol` y `fmt_ts` por inyeccion

**Contexto:** en REF el engine vivia dentro de `app.py` y usaba la constante global
`OF_SYMBOL` mas `tclock.fmt_utc` para las alertas. Eso fijaba el motor a un unico
instrumento (6E) y arrastraba `tclock` -> `strategy` + MetaTrader5 al corazon del
calculo de order flow.

**Decision:** `symbol` es parametro de constructor (con ese 6E como default) y
`fmt_ts` es un callable inyectado, con un formateo UTC local sin dependencias como
default. Igual con `pick_cvd_source`: las etiquetas de fuente (`"live (Databento 6E)"`)
se reciben por parametro en vez de estar escritas.

**Por que importa:** un motor de order flow que dice "sintetico (tick volume MT5)" cuando
la serie viene de B3, o que solo funciona con MT5 instalado, no es un motor: es la
referencia con otro nombre. Los tests comprueban las dos cosas.

### D-011 - Las fixtures JSONL y el feed sintetico van a `adapters/`, no a `core/`

**Contexto:** `REF/mock_feed.py` genera las cintas deterministas de las fixtures y las
consume el `MarketSimulator`.

**Decision:** se porta a `adapters/synthetic_feed.py`. Los generadores son deterministas
y no hablan con nadie, asi que tecnicamente podrian vivir en `core/`; pero son una
FUENTE de datos, y la regla del monorepo manda la direccion de la dependencia
`adapters -> core`, nunca al reves. Colocarlos en `core/` obligaria al nucleo a
importar su propio generador de datos.

**Efecto practico:** los tests si importan `adapters.synthetic_feed`; `core` no. La
guardia de pureza lo verifica.

### D-012 - `core/paths.py`: `DB_PATH` y `STRATEGY_PATH` se mueven a subdirectorios

**Contexto:** en REF el `trading.db` y el `strategy.yaml` vivian sueltos en la raiz.
El layout nuevo tiene `database/` y `config/`.

**Decision:** `DB_PATH` pasa a `<root>/database/trading.db` y `STRATEGY_PATH` a
`<root>/config/strategy.yaml`, componiendolos desde `PROJECT_DIR` en un solo sitio.

**Por que no es un detalle menor:** un test que solo compruebe `startswith(PROJECT_DIR)`
habria pasado con las dos layouts y no habria detectado el salto. `tests/unit/test_paths.py`
comprueba la ruta EXACTA de ambas, y que no exista un `trading.db` suelto en la raiz: dos
ficheros con el mismo nombre, uno con datos y otro vacio, son la forma mas silenciosa de
perder el post-mortem.

**Nota operativa:** `tests/conftest.py` conserva el canario que mide el hash del
`trading.db` real antes y despues de toda la sesion y falla si cambio. Ahora mide
`core.paths.DB_PATH`, asi que el guard sigue activo aunque `database/store.py` todavia
no exista.

### D-013 - `config/strategy.yaml` se copia byte-identico, y los tests leen las killzones de ahi

**Contexto:** `tests/conftest.py` en REF sacaba las ventanas de killzone de
`strategy.get_trading_config()`. El modulo `strategy` es de la Fase 6.

**Decision:** `config/strategy.yaml` se copia tal cual (hash verificado) y el conftest lo
lee con `yaml.safe_load`. Es el mismo dato por el camino corto.

**Por que importa el detalle:** el comentario del REF era explicito en que tener una copia
de las ventanas en los tests era justo lo que dejaba que los tests midieran una ventana
distinta de la real. Leer el YAML directamente mantiene esa garantia sin el modulo
intermedio. `test_risk_engine.py::test_defaults_match_strategy_yaml` sigue fijando que
`risk_engine.DEFAULT_KILLZONES` coincide con el YAML.

### D-014 - La base de datos empieza vacia, no se copia la de REF

**Contexto:** `REF/trading.db` tiene 121 filas en `setup_log`, 19 en `trades` y mas en
`cot_history` y `messages`. Es el post-mortem real de alguien, con decisiones de trading
tomadas ahi dentro. Copiarla al repo arrastra datos personales y hace que cada test de
persistencia dependa de datos viejos que nadie va a recordar cuando fallen.

**Decision:** `database/trading.db` se crea vacia con `init_db()`. REF no se toca.

**Lo que se conserva de REF es el ESQUEMA, no los datos.** `REF/store.py:33` recalculaba
`DB_PATH` por su cuenta e ignoraba `core.paths.DB_PATH`; ahora `database/store.py` lo
delega y `tests/unit/test_store.py` lo fija. Dos rutas a la base es la forma mas facil de
escribir en el fichero equivocado sin enterarse.

Como el esquema tiene que ser el de produccion y la base empieza vacia, la comparacion se
hace contra el esquema congelado: `tests/fixtures/schema_snapshot.json` guarda las 14
tablas de REF con sus tipos, defaults, PK e indices, y `tests/unit/test_schema.py` falla si
el esquema se desvía. Es la unica forma de tener a la vez una base vacia y garantias de
compatibilidad con el resto del sistema, que ya espera esa forma de tabla.

`*.db` sigue en `.gitignore` (confirmado con `git check-ignore`), asi que aunque la base se
regenere localmente no hay forma de que acabe en el repo por accidente.

### D-015 - El esquema es codigo, y `schema.sql` es un artefacto generado

**Contexto:** `.gitignore` dice que las DB "se regeneran desde schema.sql", lo cual hace de
ese fichero una fuente de verdad que depende de que alguien se acuerde de regenerarlo cada
vez que toca una tabla.

**Decision:** `database/models.py` es la fuente de verdad (`SCHEMA`, `INDEXES`,
`MIGRATIONS`). `database/schema.sql` se genera con `python -m database.models` y
`tests/unit/test_schema.py::test_schema_sql_esta_sincronizado` falla si el fichero no
coincide byte a byte con `render_sql()`. Editar el `.sql` a mano no es un cambio: rompe el
test.

**Por que separa DDL de la siembra:** REF metia en `init_db()` los `CREATE TABLE`, las
migraciones y las filas iniciales. Las filas iniciales dependen de la config del agente
(Fase 6), asi que mezcladas ahi el esquema no se podia aplicar en ningun entorno sin agente.
Ahora `models.py` solo sabe de forma de tabla.

**Detalle de Windows:** el `schema.sql` se escribe con `write_sql()`, que controla el
encoding. `>` en PowerShell 5.1 produce UTF-16LE y `Out-File -Encoding utf8` mete BOM: en
ambos casos el fichero generado no es el que espera el test, y el sintoma es un
`UnicodeDecodeError` que apunta al fichero equivocado.

### D-016 - El seam de config: default perezoso a `strategy`, inyectable en tests

**Contexto:** REF hacia `import strategy` arriba del todo en `store.py`. Como `store` es
la capa de persistencia, eso arrastraba `yaml` y `risk_engine` a cualquier test de CRUD,
y mientras `strategy` no exista (Fase 6) `import store` falla. La Fase 2 no se puede probar
sin la Fase 6.

**Decision:** `ConfigSource` (Protocol) + `_LazyStrategyConfig` (default). El import de
`strategy` va DENTRO del metodo, asi que solo ocurre si alguien pide la config. Un test
mete un `FakeConfigSource` y ejercita todo el CRUD sin YAML.

**El fallo es explicito:** `ConfigUnavailable` cuando no hay config. `set_config_source()`
sustituye el seam y devuelve el anterior; `None` restaura el default.

**Por que es un tipo propio y no `RuntimeError`:** hay dos reacciones distintas al mismo
fallo. `default_trading_config()` debe estallar (quien la llama la quiere de verdad) y la
siembra de un rol vacio debe poder seguir. Con un `RuntimeError` generico no se distinguen
sin leer el mensaje.

**Consecuencia asumida:** `init_db()` ya no puede depender de `strategy`. Si no hay config,
siembra el rol `general` con prompt vacio y avisa con `RuntimeWarning`. El esquema tiene que
existir ANTES de que haya estrategia. Solo se traga `ConfigUnavailable`; un YAML corrupto
sigue exploando, y hay test que lo fija, porque tragarse ese error convertiria un bug
visible en un prompt de rol vacio silencioso.