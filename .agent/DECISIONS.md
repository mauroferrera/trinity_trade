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
### D-017 - Una sola sesion de MT5 por proceso, con un unico hilo

**Contexto:** REF tenia TRES puertas independientes al terminal MT5, cada una con su
propio ciclo de vida: `patterns_service.py` (executor `mt5p`, lock, `initialize()` y
`shutdown()`), `cvd_service.py` (executor `mt5c`, lock y ciclo propios) y
`research/data.py` (un `RLock` y ciclo propios). `app.run_mt5` es alias de
`patterns_service._mt5_run`, asi que ademas el nombre sugeria una puerta unica que no
existia.

El terminal MT5 es un recurso de PROCESO, no de hilo. Con tres ciclos de vida
independientes, uno puede cerrar el terminal mientras otro lo esta usando, y el
sintoma (None en todas las velas, o un None en medio de una serie) aparece lejos de la
causa.

**Decision:** `adapters/forex/mt5_forex.py` expone una `Session` por proceso con un
`ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")`. `get_session()` /
`set_session()` permiten sustituirla en tests. La conexion se cachea y se reutiliza (no
se llama `initialize()` en cada lectura), y `close()` drena lo que esta en vuelo antes de
cerrar: si no, un cierre concurrente dejaria un `Future` corriendo contra un terminal
apagado.

**Por que un hilo y no un lock:** el lock resuelve el acceso concurrente pero no el
`initialize()`/`shutdown()` a lo largo de la vida de la sesion. Un executor de un solo hilo
da las dos cosas con un mecanismo, y hace que el orden de las llamadas sea FIFO y visible.

**Consecuencia asumida:** las llamadas se serializan, asi que un stream lento bloquea a
los demas. Es aceptable: MT5 no gana nada de ser llamado en paralelo, y el
diagnosticar de "va lento pero va" es mejor que el de "a veces None sin explicación".

### D-018 - La forma normalizada se hereda de lo que `core/` ya consume

**Contexto:** el plan de la Fase 3 pedia "interfaz abstracta OHLC/Ticks/Depth + datos
normalizados". Definir un formato nuevo habria obligado a tocar `core/`, que esta
terminado y verificado, para adaptinglo a una forma arbitraria.

**Decision:** la forma normalizada es exactamente la que `core/` ya consume. Vela
`{"time", "open", "high", "low", "close", "volume"}` con `time` en SEGUNDOS epoch; trade
`{"ts", "price", "size", "side"}` con `side` en `"A"`/`"B"`.

`A`/`B` y no `buy`/`sell` porque el campo es el lado AGRESOR, no el lado que cierra la
posicion: en una orden de compra agresiva el agresor esta en el Ask. Llamarlo "buy"
confunde el delta con la posicion.

**Reglas que el modulo hace cumplir por construccion:**

- `time` en segundos, siempre. MT5 mezcla segundos y microsegundos; ccxt devuelve
  MILISEGUNDOS. Es la razon mas clara de por que la conversion vive en el adaptador y
  no en cada uno por su cuenta.
- Fallo explicito en vez de numero inventado: `tick_value=0` significa "el broker no lo
  publica", y las funciones de dinero devuelven `None` en vez de `0.0`. Un `0.0` se lee
  como "no hay riesgo" cuando significa "no lo se".
- `normalize_symbol()` quita sufijos para COMPARAR y NUNCA para ENVIAR: mandar `EURUSD`
  cuando el broker publica `EURUSD.a` da `SymbolNotFound`.

**Consecuencia asumida:** el CVD no se calcula en el adaptador.
`core/orderflow_engine.build_cvd_series()` ya es puro y es el dueno de ese calculo;
duplicarlo en el adaptador es la forma de que los dos divergan.

### D-019 - `WINFUT26` no se normaliza a `WIN`, y por eso no se normaliza

**Contexto:** los futuros de B3 se escriben sin separador (`WINFUT26`, `WINQ26`,
`INDZ26`). Una normalizacion que quitasse los ultimos caracteres daria `WIN`, que es lo
que un usuario esperaria, pero no se puede distinguir de un subyacente que se llamase
literalmente `WINFUT`.

**Decision:** `normalize_symbol()` corta SOLO en `.`, `_`, `-` y `#`. Un simbolo sin
separador se devuelve tal cual. `contrato_base()` en `adapters/b3/mt5_b3.py` lo delega y
documenta el limite, en vez de anadir una heuristica que "casi siempre" acierta.

**Por que no la heuristica:** una heuristica con falsos positivos envia a la B3 un simbolo
equivocado, y el error aparece en la orden, no en la normalizacion. Un fallo local es
mucho mas barato que uno que llega al mercado. Si de verdad hace falta, la respuesta
correcta es un mapa declarado por simbolo en `config/asset_sources_map.yaml`, no adivinar.

**Consecuencia asumida:** `WINFUT26` y `WIN` NO coinciden como claves de cache ni al
comparar con la config. Es lo correcto: son el mismo subyacente pero contratos
distintos, con expiraciones distintas, y mezclarlos produce una serie continua que en la
realidad tiene un salto de meses.

### D-020 - Los esqueletos fallan al USAR, no al construir, y cada familia lanza su tipo

**Contexto:** los cinco esqueletos de B3, cripto y Databento no pueden implementarse sin
elegir proveedor, y esa es una decision de negocio. Un `__init__` que lanzara haria que
un registro de adaptadores (`{a.name: a() for a in TODOS}`) reventara al importarse, por
un mercado que nadie pidio.

**Decision:**

- Construir no falla; el error llega al invocar `symbols()`/`ohlc()`, que es donde el
  mensaje puede decir que hacer.
- B3 y cripto lanzan `AdapterError` (vía `TerminalUnavailable`): "este mercado no esta
  disponible" es exactamente lo que son.
- Databento lanza `DatabentoNoConfigurado`, que NO hereda de `AdapterError`. Es un
  proveedor de datos historicos, no un mercado: no hay terminal que abrir. Si
  compartiera la jerarquia, un `except AdapterError` que hoy significa "el broker no
  responde" se tragaría también un error de clave de API, y quien lo manejara le diria al
  usuario que abra la terminal que no existe.

**Limite conocido de `runtime_checkable`:** en Python 3.12 solo comprueba que existan
los atributos, asi que `isinstance(DatabentoCME(), MarketDataAdapter)` es `True` aunque su
`symbols()` no tenga respuesta honesta. No se puede arreglar desde la libreria. Se
documenta en `tests/unit/test_adapter_skeletons.py` y la barrera real tiene que ser la
lista explicita de adaptadores de la API, no el tipo.

### D-021 - El doble de MT5 no lleva cuenta de concurrencia

**Contexto:** al probar que la `Session` serializa el acceso, la forma evidentemente
correcta era contar llamadas concurrentes en `FakeMT5` y afirmar `max <= 1`.

**Decision:** el doble NO lleva contadores. El contador se ajustaria en el propio punto
que hay que demostrar que no se solapa, asi que la asercion pasaria siempre, incluso con
un executor de ocho hilos. La serializacion se demuestra con `threading.Event`: el
primer trabajo marca `entra` y se bloquea, y el test afirma que un segundo trabajo NO
puede entrar antes de que se le abra la puerta.

**Refuerzo extra:** el test de concurrencia se ejecuta 20 veces seguidas. Un fallo de
serializacion es intermitente por naturaleza y una sola pasada en verde no dice nada; el
resultado de la repeticion se puede comprobar sin pytest-repeat.

---

# FASE 4 - MACRO / INGESTORES (2026-10-02)

### D-022 - `IngestorError` es una jerarquia aparte, y una lectura degradada siempre dice si es vieja

**Contexto:** `macro_ingestor/` se parece a `adapters/` porque los dos salen a la red, pero
un fallo ahi no significa lo mismo. Si el broker no responde no se puede operar; si el proxy
de Forex Factory esta caido se sigue operando con una parte del score en cero. Con una
jerarquia unica, el `except AdapterError` que significa "el bróker no responde" se traga
tambien "no se que dice el COT", y quien lo maneje le dira al usuario que abra una terminal
que si funciona.

**Decision:** `IngestorError` (con `FeedUnavailable` y `FeedUnreadable`) NO hereda de
`AdapterError`. La lectura normalizada es `{"source", "asof_ts", "payload", "stale",
"reason"}` y las dos ultimas no son opcionales en la practica:

- `asof_ts` es la fecha del DATO, no la del fetch. Con un feed semanal del COT, la
  diferencia es de hasta siete dias y confundirlos hace que un dato de la semana pasada
  parezca fresco.
- `stale` distingue el dato fresco del respaldo. REF servia la copia vencida de la cache
  sin marcarlo, en los tres servicios: era la forma de que un `payload` viejo pareciese
  actual.
- `reason` es obligatorio tambien cuando no hay dato. `payload: None` sin motivo es
  indistinguible de "no habia nada", y esa es justo la confusion que hay que evitar.

`FeedUnreadable` existe separada de `FeedUnavailable` porque el fallo que mas se va a dar
aqui no es de red: es de formato. Forex Factory no tiene API y se parsea markdown, asi que
un cambio de maquetacion rompe el parseo, no la conexion.

**Consecuencia asumida:** `asof_ts = 0` cuando no hay dato. Un COT caido vale 0.0 en
`core/risk_engine.py` (asi esta ya, `DEFAULT_WEIGHTS["cot"] = 0.0`), no un numero inventado.

### D-023 - El indice del COT sale de "Legacy Futures Only"; el TFF es solo detalle

**Contexto:** la CFTC publica el mismo compromiso en tres informes. El clasico "COT index" de
26 semanas se define sobre los No Comerciales del informe Legacy, y ese es el numero que
todo el mundo cita. El TFF Moderno separa Asset Managers y Leveraged Money, que es mas
detallado pero no es la misma serie.

**Decision:**

- El indice 0-100 se calcula sobre **No Comerciales de `Legacy Futures Only` (`6dca-aqww`)**.
- TFF (`gpe5-46if`) se descarga para el delta de Asset Managers y de Leveraged Money, que
  alimenta el score cuando no hay senal direccional clara.
- Las dos fuentes se cruzan **por fecha de reporte**. Una fila que solo tiene una de las dos
  se descarta; rellenar el hueco con `0` fabricaria un delta que no existe y el 0 en un delta
  significa "sin cambio", que es informacion.
- Las tres funciones puras (`_norm`, `build_report`, el cruce) no tocan la red: se testean
  con series grabadas.

**Por que el cruce descarta y no rellena:** un delta de 0 y un delta desconocido se parecen
en el signo y son opuestos en el significado. El primero dice "el comercial no ha cambiado";
el segundo dice "no lo sabemos". La serie resultante tiene huecos declarados en vez de ceros
falsos.

### D-024 - El SMR alinea por TIEMPO y rechaza los desfases constantes

**Contexto:** REF comparaba `dxy[-1]` con `eurusd[-1]` por posicion. El DXY cotiza en la
NYSE y el EURUSD no: sesiones, festivos y horas de cierre distintas. Por posicion, el
"minimo nuevo" del DXY puede ser el maximo de hace tres dias, y la comparacion sale con
signo invertido sin que nada falle.

**Decision:**

1. Se alinean por timestamp, no por indice, y se comparan solo las horas comunes.
2. Las velas en curso se descartan antes de comparar: una vela que aun no ha cerrado puede
   tener un minimo que luego no existe.
3. Se busca el desfase constante (en velas) que mas solape da entre las dos series. Si el
   mejor no es el desfase 0, el SMR **no se emite**: un proveedor etiqueta por apertura y el
   otro por cierre, y realinear en silencio deja que el signo del desfase decida el resultado.
4. Sin horas comunes no hay SMR. Se devuelve `confirmed=False` con el motivo escrito.
5. La cache de velas es por timeframe. Una cache unica comparaba H1 con M15 y devolvia un
   SMR calculado sobre dos ventanas distintas.

**Por que no se "corrige" el desfase:** alinear un DXY desplazado contra el EURUSD para que
las señales coincidan es ajustar el dato hasta que confirme lo que uno quiere. Ante la duda,
`confirmed=False` y el sesgo de DXY pesa cero: es la unica conclusion honesta cuando las dos
series no hablan de la misma hora.

### D-025 - El gate de noticias es fail-open, y declara si esta suponiendo la hora

**Contexto:** REF cerraba el trading cuando el calendario no se podia leer. Convertir una
caida de un proxy de terceros en "no se opera nunca mas" es la peor de las dos lecturas
posibles: el bot se queda parado sin que nadie entienda por que.

**Decision:**

- Si no hay dato de noticias, el gate devuelve `ok=True` con `fail_open=True` explicito. El
  valor viaja en la lectura para que sea visible, no se deduce de la ausencia de datos.
- El motivo va escrito. `fail_open` sin motivo es indistinguible de "no hay evento alto".
- Si no se pudo leer el reloj del calendario, `timezone_assumed=True`. Un gate que crea
  estar en la ventana correcta sin saber la hora es peor que uno que no sabe.
- El buffer es simetrico (±15 min). Un buffer solo hacia adelante deja entrar la mitad del
  riesgo, y el peor caso es operar DESPUES del dato, cuando el mercado ya lo ha descontado.
- Impacto y titulo se leen por separado: un icono desconocido degrada el evento a `yel` en
  vez de perder el evento entero.

**Consecuencia asumida:** se puede operar a ciegas respecto a las noticias si el proxy lleva
caido. Es una apuesta explicita y monitorizable (`fail_open=True` en el log), no un
comportamiento oculto.

### D-026 - Los ocho esqueletos de B3 y cripto NO se crean

**Contexto:** `config/asset_sources_map.yaml` declara ocho fuentes mas: `bcb_focus`,
`foreigner_flow`, `di_futures_yield`, `news_blackout`, `fear_and_greed_index`,
`crypto_onchain_whales`, `funding_rate_monitor` y `liquidation_heatmap`. REF no tiene
implementacion ni contrato para ninguna de las ocho.

**Decision:** no se escriben esqueletos. Se declaran en `macro_ingestor/registry.PENDIENTES`
y el bot no las consulta.

**Por que un esqueleto aqui es peor que nada:** un esqueleto de adaptador devuelve
`AdapterError`, asi que el fallo es visible. Un esqueleto de ingestor que devuelve
`neutro(...)` parece una fuente que falla todos los dias, y uno que devuelve un esquema
inventado parece una fuente que funciona con datos que no son los de la fuente. Un test
con un fixture ficticio no distingue las tres cosas, porque las tres pasan el test.

**Consecuencia asumida:** los activos B3 y cripto operan sin soft data hasta que haya
contrato verificado. Es una ausencia declarada (`PENDIENTES`) y consultable
(`registry.faltantes()`), no un silencio.

### D-027 - `registry.py`: el YAML nombra conceptos y el codigo nombra servicios

**Contexto:** `asset_sources_map.yaml` declara `cot_report`, `dxy_correlation` y
`ecb_fed_calendar`. Los modulos se llaman `cot_service.py`, `dxy_service.py` y
`calendar_news.py`. Sin una tabla que una las dos cosas, el YAML no ejecuta nada y no hay
forma de notarlo: la fuente simplemente no aparece en el score.

**Decision:** `macro_ingestor/registry.py` traduce nombre-de-YAML a modulo, con tres reglas:

- Un nombre sin implementacion devuelve lectura **neutra con el motivo**, no se filtra. El
  filtro en silencio es el fallo que hay que evitar.
- Se distingue "pendiente" (en `PENDIENTES`, decidido) de "olvidado" (no esta en ninguna
  parte): son errores distintos y el segundo se arregla, el primero no.
- Una entrada del registro que no importa se reporta como **"registro roto"**, porque es un
  bug de este repo y no una caida de la fuente.

Hay un test que cruza `asset_sources_map.yaml` con el registro: anadir un
`soft_data_sources` nuevo al mapa sin registrarlo en ninguna parte rompe la suite.

**Alternativa descartada:** renombrar los modulos para que coincidan con el YAML. Acopla el
nombre de un modulo a una config que puede cambiar, y `from macro_ingestor.forex import
cot_report` sigue sin decir de donde sale el dato.

### D-028 - `REF/decision_context.py` no es de esta fase

**Contexto:** el plan pedia evaluar `decision_context.py` dentro de la Fase 4.

**Evaluacion:** es logica pura (edad del snapshot, procedencia, spread como fraccion del
stop, y las claves que lee la auditoria). No hace I/O y no depende de ningun proveedor: no
es un ingestor.

**Decision:** no entra en `macro_ingestor/`. Va a `core/decision_context.py` cuando exista el
endpoint de auditoria que lo consume (Fase 6), donde sus claves exportadas tienen un lector.
Portarlo ahora seria codigo sin consumidor y una decision sobre `STALE_SNAPSHOT_S` que
depende de `scan_interval_sec` de `strategy.yaml`.

**Lo que si se conserva de esa evaluacion:** el criterio de "caducidad" ya existe en la Fase
4 con otro nombre. `TTLCache` marca el respaldo con `stale=True` y `MacroReading` lleva
`asof_ts`; es la misma idea de "no me digas que esto es actual si no lo es", resuelta en la
capa de I/O en vez de en la de contexto.

### D-029 - Los puertos del agente son `Protocol` más `AgentDeps`, e `import agent` no tiene efectos secundarios

**Contexto:** cada handler de `REF/agent.py` hacía `import app` dentro de la función para llamar
al monolito. Tres consecuencias con nombre: una herramienta que solo necesita la base de datos
(`trade_query`, `journal_append`) no se podía probar sin levantar el API entero; la capa de
explicabilidad quedó soldada a MT5-Forex, así que añadir B3 exigía tocar el agente; y cualquier
capa que importara el agente entraba por la puerta de MT5, que es el mismo problema que D-017
ya había cerrado para los adaptadores.

**Decisión:** `agent/ports.py` declara `MarketPort`, `StorePort`, `NewsPort` y `ExportPort` como
`Protocol`, más `AnalysisAnchors` (el estado que se comparte entre llamadas de una conversación) y
`AgentDeps`, el objeto que los junta y que lleva el reloj y el `sleep` inyectables.
`database/store.py` satisface `StorePort` tal cual, sin adaptadores ni herencia: la comparación
es estructural, y que un doble de test cumpla el contrato se verifica con un test, no con una
clase base que obligaría a `store` a heredar de algo.

**Alternativa descartada:** una `BasePort` común. Habría que hacer que `store` heredase de ella
para poder afirmar que cumple el contrato, y un módulo de funciones sueltas no hereda bien.
Además `runtime_checkable` en Python 3.12 mira NOMBRES, no significados (límite ya documentado
en D-021): el contrato de verdad es la lista explícita de cables, no un `isinstance`.

### D-030 - Tres estados de herramienta: `ok`, `failed` y `unavailable`

**Contexto:** REF tenía `ok` / `failed` / `offline`, y `offline` significaba dos cosas distintas
según quién mirase. Para `price` era "el símbolo no está en Market Watch"; para `set_chart_alert`
era "MT5 no respondió". Con un solo nombre, el modelo recibía el mismo rótulo para un problema de
configuración y para una caída del bróker, y en la traza no había forma de distinguirlos.

**Decisión:** `ok` (hay dato), `failed` (el puerto existe y reventó al producir la respuesta) y
`unavailable` (el puerto NO está cableado, o el proveedor está apagado: no hay nada que reintentar,
hay algo que terminar de construir). El motivo viaja siempre en `error`, para que el log diga
"falta MarketPort" y no "error desconocido".

**Alternativa descartada:** mantener `offline` y matizar el texto del error. El estado es lo que
el modelo lee para decidir si reintenta; el matiz en el texto es justo lo que se pierde.

### D-031 - El agente transporta el score; no lo calcula

**Contexto:** la regla 19 de `AGENT_GUIDELINES.md` dice que el agente explica y no decide. Con las
herramientas pegadas al monolito, "decidir" se colaba por la puerta de atrás: una herramienta que
devuelve un número calculándolo en el handler es una decisión sin auditar.

**Decisión:** `agent/` no calcula entradas, SL, TP ni tamaños. El score lo calcula
`core/risk_engine.py`, la política la redacta `prompt_templates.risk_policy_lines`, y el bucle solo
transporta. La comprobación es de lectura: si el bucle puede "ajustar" un número, ese número deja
de ser auditable.

### D-032 - `completion` es un seam: LiteLLM se importa dentro de la llamada

**Contexto:** importar `litellm` arriba del todo el módulo carga el cliente de LLM al hacer
`import agent`, que es exactamente el efecto secundario que D-029 cierra. Y el bucle con fallback,
rotación de claves y backoff es la parte con más caminos de error de todo el agente: probarlo
contra un proveedor real sería probar la red, no la lógica.

**Decisión:** el puente llama a un `completion` inyectado con la misma firma que
`litellm.acompletion`; el default se resuelve en la primera llamada, con `from litellm import
acompletion` DENTRO de la función. Hay un test que parsea el AST del módulo y falla si ese import
sube al nivel superior. Toda la suite del agente corre sin red y sin credenciales.

### D-033 - `done` es un invariante del stream, y no hay failover después del primer `delta`

**Contexto:** REF emitía `done` en cinco ramas del bucle, así que añadir un camino nuevo y
olvidarlo dejaba al cliente con un stream abierto para siempre. Y REF encadenaba la respuesta del
proveedor siguiente cuando el stream se cortaba DESPUÉS de haber emitido texto: el usuario veía
media respuesta del modelo A y luego la del modelo B, una encima de otra.

**Decisión:** dos reglas, y son la misma idea (el stream es un contrato con el cliente):

- El generador interno NUNCA emite `done`; lo emite el envoltorio `stream()`, una vez, sea cual
  sea el camino. Si el cliente desconecta (`GeneratorExit`) no se emite, porque no hay nadie que
  lo lea: un `yield` dentro de un `finally` revienta con "async generator ignored GeneratorExit".
- La vuelta a otro proveedor solo existe ANTES del primer `delta`. Después se dice que se cortó y
  se termina. Media respuesta es mejor que dos respuestas superpuestas.

**Alternativa descartada:** `finally: yield sse(done)`. Es la forma idiomática y aquí es
incorrecta: en una desconexión lanza. La excepción se propaga y el cierre no se emite, que es lo
correcto.

### D-034 - A la traza el digest; al modelo, el payload completo

**Contexto:** REF guardaba en `messages(role='tool')` el JSON entero del resultado, que en
`chart_snapshot` son cientos de KB por llamada, y no guardaba los argumentos de la llamada. Una
traza sin argumentos no permite reproducir la consulta que la generó.

**Decisión:** dos representaciones, dos consumidores. A la base va el sobre de
`prompt_templates.tool_digest` (tool, args, round, status, elapsed_ms, bytes y recorte
declarado); al modelo, en el mensaje `role='tool'` de la ronda, el resultado completo. Recortar
lo que lee el modelo es inventarse un dato, y guardar el payload entero en cada llamada hace la
tabla `messages` ilegible.

**Lo que NO cambia en esta fase:** `role='tool'` se sigue escribiendo, porque es lo que la tabla
admite y lo que REF hacía, y `store.get_messages()` la sigue filtrando. El contrato de
`get_messages()` no se toca aquí: `tool_call_id` puede quedar huérfano en la traza y el
emparejamiento lo hace el historial de la ronda, no la base.

### D-035 - La degradación de un modelo local se DICE, no se nota

**Contexto:** el function-calling de los modelos locales no es fiable: uno pequeño a veces emite el
tool call como texto plano en lugar de en `tool_calls`. Si eso se emite como `delta`, el usuario ve
un JSON crudo en medio de su respuesta.

**Decisión:** dos comportamientos distintos según cuándo se sepa lo que es el texto. Si el primer
modelo al que se le pregunta es local y la petición es compleja, degrada a "ruta simple" (los datos
en vivo van inyectados en el prompt y el modelo redacta sin llamar a nada) y **se avisa con un
`status`**, porque el usuario pidió herramientas y va a recibir una respuesta redactada sin ellas.
Si aun así llega un texto que es exactamente un JSON con un nombre permitido, se interpreta como
tool call, se ejecuta y **se vacía el búfer**: en ese momento ya se sabe que el texto no era
respuesta.
### D-036 - `Runtime` en `app.state`, y `create_app(runtime)` para los tests

**Contexto:** REF tenia el estado del proceso en variables de modulo (`_engine`, `manager`, `DB_STATE`,
`_main_loop`). Importar `app` encendia un hilo (`_cot_background_sync`), dos apps en el mismo proceso
compartian estado, y no habia forma de saber que estaba cableado: `import app` funcionaba igual con la
base caida, sin MT5 y sin feeds, y el primer sintoma era un 500 en una ruta concreta.

**Decision:** `Runtime` (unico contenedor) creado en `create_app()` y guardado en `app.state.runtime`;
los endpoints lo leen con `deps.runtime(request)`. Que el contenedor sea un ARGUMENTO de `create_app` es
lo que permite `create_app(Runtime(store=Fake(), market=Fake()))` en un test sin monkeypatch global.
`startup()` engancha el loop de asyncio al bus de WebSockets e inicializa la base; NO abre MT5, no
descarga el COT y no arranca ningun feed. Un proceso que enciende todo al arrancar esconde el fallo de
arranque detras de un 502 en la primera peticion.

`Runtime.symbols` cae al `market` si no se pasa: es el proveedor de simbolos, no una pieza mas que haya
que acordarse de cablear.

### D-037 - El mapeo de errores es por clase que SIGNIFICA, no por `RuntimeError`

**Contexto:** REF registro handlers para `RuntimeError` y `TimeoutError`, ambos a 503. `RuntimeError` es
la clase base de casi todo, asi que un `KeyError` disfrazado de `RuntimeError` salia como "no hay
conexion con MetaTrader 5".

**Decision:** un handler por clase con significado: `SymbolNotFound` 404, `AdapterError` 503,
`IngestorError` 502, `AgentPortError` 503, `ConfigUnavailable` 503, `ValueError` de parametros 400. Un
bug de codigo (`TypeError`, `KeyError`, `AttributeError`) NO tiene handler: sube a 500 con traceback en
el log. `ValueError` es el unico ambiguo y se acepta a proposito: es lo que lanzan `core.market_view` y
`core.lot_calculator` con un parametro fuera de rango.

El cuerpo siempre lleva `error` (el JS heredado lo busca por ese nombre).

### D-038 - El token se lee por peticion y con `compare_digest`

REF leia `API_TOKEN` en cada llamada (bien) pero los origenes CORS al importar (mal: cambiarlos
exigia reiniciar y el sintoma era "cambie el origen y el navegador sigue dando CORS"). Aqui los dos se
leen por peticion, y el token se compara con `secrets.compare_digest` en vez de `==`: la comparacion de
cadenas de Python sale en cuanto encuentra una diferencia y filtra el token prefijo a prefijo.

Sin `API_TOKEN` no se exige nada (modo desarrollo local). Con token, la ausencia de cabecera y el valor
equivocado son el MISMO 401, porque esa diferencia solo importa al atacante. En WebSockets el token va
en la query o en `Sec-WebSocket-Protocol`, y se comprueba ANTES del `accept()`: despues el cierre
correcto es 1008, que el cliente distingue de un corte de red.

### D-039 - El contrato con el frontend se escribe, no se supone

`static/main.js` (195 KB, copiado de REF sin reescribir) llama 47 rutas; los routers sirven 43. La
diferencia esta escrita en el docstring de `api/routes/__init__.py`, con lo que falta y que necesita
cada ruta, porque un endpoint que falta se descubre cuando el dashboard deja de pintar un panel.

Los alias que si se anaden son de UNA LINEA y no dependen de nada (`/api/analysis/chartism/{symbol}`,
`/api/analysis/chart-assistant/{symbol}`, `/api/candle/last/{symbol}`, `/api/db/tables/{table}`): el
ultimo no era un alias sino un BUG, `/api/db/tables/{table}` es lo que llama el JS y el router solo
tenia `/api/db/{table}`, asi que el inspector de la pestana de datos devolvia 404.

`store.get_config_summary()` leia claves planas de un YAML ANIDADO (`cfg["min_score"]` en vez de
`score.min_score`) y devolvia `min_score: None`, `risk_weights: {}` y `killzones: []` con la config real
puesta: la UI pintaba "sin reglas" con el sistema entero configurado. Ahora lee por seccion con fallback
plano (`RESUMEN_CLAVES`), porque una config guardada por la UI antigua sigue siendo plana.

### D-040 - Ningun test abre `database/trading.db`, ni para leer

El canario de `tests/conftest.py` mide el hash del fichero real y salta si cambia. Ha saltado DOS veces
esta fase: abrir una base en modo WAL hace que hasta una lectura termine en checkpoint al cerrar, y un
test que escribio a traves del modulo `store` sin redirigir `DB_PATH` dejo 7 filas en la bitacora real.

Los tests que necesitan el store de verdad usan el fixture `db`, que redirige `DB_PATH`, `PROJECT_DIR` y
el seam de config (este ultimo, porque el default es el modulo `strategy`, que aun no existe, y sin
cambiarlo cada test acabaria probando el camino del error de configuracion). Los que solo necesitan leer
usan `FakeStore`: no hay reason para tocar el fichero mas valioso del proyecto para comprobar que un
endpoint devuelve 200.

### D-041 - El orden de declaracion de las rutas es parte del contrato

`/api/journal/{jid}` con `jid: int` se traga `/api/journal/overlay` y devuelve 422 ("no se puede
convertir 'overlay' en entero") en vez de 404: Starlette empareja en ORDEN DE REGISTRO. Por eso
`overlay` se declara antes que la parametrizada, con un test que falla si alguien lo mueve.

La alternativa (tipar el id como `str` y validar dentro) parece mas robusta y es peor: el 422 de
Pydantic sobre el path deja de distinguir "esta ruta no existe" de "mandaste un id malo", y el
frontend heredado trata los dos como error de la peticiion.

El inventario de lo que falta esta en el docstring de `api/routes/__init__.py` y lo hace
CUMPLIR un test (`TestContratoConElFrontend`) en las dos direcciones: si el JS pide una ruta que no
esta inventariada, falla; si la tabla declara como pendiente algo ya servido, tambien. Un inventario
que se queda obsoleto es peor que no tener inventario, porque parece una lista de trabajo.

Cerrado en esta sesion: `/api/journal/overlay`, `/api/chart/drawings` (GET/PUT/DELETE),
`/api/chart/alerts` (GET) y `/api/chart/alerts/{aid}` (DELETE), `POST /api/trades/sync`,
`POST /api/agent/roles`, `GET`/`POST /api/agent/config`, `GET /api/analysis/cvd/{symbol}`,
`GET /api/analysis/smr/{symbol}`, `GET /api/volume-profile/{symbol}`, `GET /api/cot/report` y
`POST /api/cot/refresh`. Quedan 19, y las que dependen de `strategy`/`watcher` o de un puerto de
ejecucion esperan a esos modulos.

### D-042 - El COT se LEE de la tabla y solo se DESCARGA en el refresh

`GET /api/cot/report` construye el sesgo con `store.list_cot_reports()` + `cot_service.build_report`
(puro), sin tocar la red. REF hacia lo mismo por su `sync()`/cache, pero aqui la razon es dura: un
panel que hace una peticion a la CFTC en cada refresco de pagina es un rate-limit esperando a
ocurrir. `POST /api/cot/refresh` es el unico que sale, y por eso es el unico con token.

El refresh persiste lo descargado y escribe `cot_index_26w`/`macro_bias` SOLO en la fila mas
reciente. Escribirlo en todas exigiria un `build_report` por fila con su propia ventana movil, y el
resultado seria un numero que ninguna recomputacion reproduce: mejor la tabla con lo crudo que la
tabla con cifras inventadas por fila.

### D-043 - El Volume Profile reparte el volumen real de las velas, sin inventar la forma

`core.market_view.volume_profile()` reparte el `volume` de cada vela entre los bins que su rango
[low, high] solapa, a prorrata del ancho del solape. Es la unica vista del modulo que no simula
ticks, y la distincion es el punto: un POC con forma triangular (lo que haria `_simulate_ticks`)
se parece a un POC medido en el dibujo y no en el numero, que es lo que el usuario mira.

DENTRO de la vela el reparto es uniforme, y es una limitacion DECLARADA: OHLCV no dice en que
precio se transacto dentro del rango. Lo que no se inventa es el total (hay test: la suma de los
bins es la suma del volumen de las velas, con la tolerancia del redondeo a dos decimales de cada
bin), y el valor (VAH/VAL) se abre desde el POC tomando el vecino mas gordo, no simetricamente.

REF tenia dos fuentes (ticks de MT5 y fallback a barras). Aqui solo hay una, y el `source` lo dice
("bars"): cuando llegue el proveedor de ticks se anade el camino y se cambia el `source`, sin tocar
el shape.

### D-044 - El gate del setup es de `core/`, no del router

En REF el panel de setup y el boton de entrada montaban la aritmetica del plan cada uno por su
cuenta (zona de entrada, niveles, veredicto). Para el mismo setup llegaban numeros distintos
dependiendo de donde mirases, y ese tipo de discrepancia no aparece en un test: aparece en
pantalla, comparando dos pestanas.

Aqui la aritmetica vive en `core/setup_gate.py` (puro, sin `adapters` ni `api`) y las dos rutas
consumen el mismo veredicto. `api/routes/setup.py` compone la respuesta y el overlay de ECharts;
no calcula.

### D-045 - `approved` es el veredicto del gate, NO permiso de ejecucion

El score, la killzone, la zona y el R:R deciden si un setup es OPERABLE. No deciden si se manda la
orden. Confundir las dos cosas convierte un gate en un permiso y abre la puerta a que un 80 en el
panel se lea como "ejecutado".

Por eso `news_blackout` se PUBLICA en la respuesta pero no FILTRA: la puerta que bloquea de verdad
es la que conoce la hora real de la orden, que es la capa de ejecucion.

### D-046 - `synthetic=1` responde 503, no velas falsas

Sin proveedor de ticks (el hito 6E) no hay `MarketSimulator` que cablear. Un 200 con velas reales
bajo un flag de test es peor que un error: el que pide el feed de prueba recibe el mercado real y no
lo sabe. Se responde 503 y se dice por que.

### D-047 - Los resolvers de config aceptan las DOS formas que hay en el repo

La config PLANA (lo que devuelve `store.get_trading_config()` y consume la UI y el agente) tiene
`risk_weights` y `killzones` como JSON strings. La config ANIDADA (la del `strategy.yaml`) tiene
`score.weights` como estructura. Las dos existen y ambas se usan. Aceptar una sola deja el otro
camino con los defaults en silencio, que es la forma mas cara de un bug: el score sale, sale bien
formateado, y no es el que el operador configuro.

### D-048 - Las fabricas de test llevan la unidad del mercado, no la del forex

`make_candles`/`make_trades` redondeaban a 5 decimales y `make_snapshot_from_scenario` tenia
`current_price=1.1045` como default. Con el forex eso es correcto. Con BTCUSDT es una trampa:
`round(x, 5)` deja decimales que ningun feed emite, y un scenario sin precio se colaba valuing en
1.1045 contra un SL de 400 puntos, que es un plan sin sentido en lugar de un fallo visible.

Por eso `digits` es parametro, `tick_size` engancha la serie a la rejilla de cotizacion (el WIN cotiza
de 5 en 5: un `round(x, 0)` produce 129998, un precio que no existe) y `current_price` es
OBLIGATORIO: si no esta, `ValueError`.

### D-049 - Un scenario se valida contra `config/`, no contra el criterio del test

`lot_calculator`, `market_type` y `session_id` tienen que coincidir con `asset_sources_map.yaml`, y
la ventana UTC de la killzone con `sessions` de `trading_hours.json` (corrida al UTC del exchange,
que para la B3 son las 12:00-21:25 UTC de las 09:00-18:25 de Sao Paulo). Si un test fija la
ventana, la ventana pasa a estar en el test y `trading_hours.json` se convierte en decoracion: un
calendario que nadie consulta y que por eso no se mantiene.

### D-050 - Un `pip_override` descarta la convencion forex

La regla de la unidad es `point * 10 si digits >= 3`, y sale de como cotizan los pares de divisas.
Un `pip_override` existe justamente para los simbolos que NO son pares de divisas, asi que cuando
esta puesto la etiqueta "pips" es falsa aunque el simbolo cotice a 3 decimales: el minicontrato de
dolar de la B3 reportaba un SL de 30 puntos como "3,0 pips". Mismo error que el del oro (multiplica
por diez), corregido en el otro sentido.
### D-051 - Un doble de MT5 tiene que devolver el TIPO que devuelve el paquete

`copy_rates_from_pos`, `copy_rates_range` y `copy_ticks_range` devuelven un array **numpy**, no una
lista. El adaptador hacia `rows or []` y `if not rows`, que con un array de mas de un elemento lanza
`ValueError: The truth value of an array with more than one element is ambiguous`: TODA lectura de
velas de EURUSD era imposible, y `/api/risk/setup` respondia 400. El doble devolvia listas, donde
preguntar por la verdad es inocuo, asi que 1220 tests de verde certificaban un codigo que en el
bróker real no funciona nunca.

Un doble mas permisivo que la realidad es peor que no tener doble, porque da la-certificacion-de-lo-que-no.
Por eso `FakeMT5.set_rates(..., as_numpy=True)` existe, y porque el doble de `symbols_get` ahora
**respeta el grupo**: `symbols_get(group="\\*\\*\\*")` devuelve vacio en el bróker real (los escapes
con barra invertida son sintaxis de MQL5, no de la API de Python) y el doble lo ignoraba. Con el
grupo respetado, `/api/symbols` paso de `[]` a los 23 simbolos visibles que publica el bróker.

**Consecuencia:** `filas_o_vacias()` en `adapters/base_adapter.py` es la unica forma de comprobar
"no hay filas". Se compara contra `None` y no se convierte a lista, porque `normalize_candle()`
indexa posicionalmente y un array se indexa igual que una tupla.

### D-052 - Un panel de estado no puede mentir en la direccion de "todo va bien"

Dos Falls de verdad, los dos en `/api/health`:

1. La sesion de MT5 expone `is_open` como `@property`, y el runtime la llamaba como metodo:
   `TypeError` y un **500 en la primera pantalla**, solo con el cableado real, porque el doble de
   mercado lleva `session = None` y la rama no se ejecutaba nunca.
2. La sesion es **perezosa**: `is_open` solo es `True` despues de que una lectura abrio la terminal.
   Una app recien arrancada publicaba `sin_terminal` con el broker conectado y el resto de rutas
   funcionando. El operador veia rojo y no tocaba nada. Ahora `_terminal_alcanzable()` PREGUNTA
   (hace el trabajo minimo de conexion) en vez de suponer. Una terminal cerrada sigue siendo un
   estado legitimo, no una averia: por eso el `try` se traga el fallo.

Y `broker_time.ea_clock` leia `.get("estado")` de un payload que no tiene esa clave y de un
`read_ea_clock()` que puede ser `None`: el campo era **siempre `null`** (o tumbaba el bloque entero
con un `AttributeError`), y un EA parado tres dias pasaba por verificado. Ahora sale `state()["ea_clock"]`
entero, con `age_sec` y `stale`.

**Consecuencia:** un test de health con `FakeMarket()` nunca puede certificar el estado del terminal.
Los dobles de sesion tienen la forma de la real (`_SesionConProperty`, `_SesionConMetodo`,
`_SesionQueExplota`, `_SesionPerezosa`), y hay un test por cada forma.

### D-053 - La config tiene dos formas y las dos existen de verdad

`get_trading_config()` devuelve lo que produce `build_flat`: los topes en la raiz
(`{"risk_pct": 0.5, ...}`). `daily_risk_state()` leia `cfg["risk"]`, asi que los cinco salian en
`null` y el panel de riesgo aparentaba no tener topes puestos mientras el gate de entrada si los
aplicaba. El test existente cubria la forma ANIDADA (la del YAML) y por eso la plana se escapaba
entera. Es el mismo problema que arrastra `api/services/macro_news.py`.

**Consecuencia:** se delega en `core.strategy._primer_valor()` en vez de escribir una tercera
variante del resolver, y con las dos rutas presentes **gana la anidada**, que es la fuente declarada.
Si esto se repite, el resolver pasa de `core/strategy.py` a un modulo de config compartido.

### D-054 - La puerta de ejecucion es un SERVICIO, no un handler

Cada puerta (lista blanca, riesgo del dia, noticias, gates, auditoria) vive en
`api/services/execution.py`, no en las rutas. La razon es concreta: el watcher tiene que ejecutar
por EXACTAMENTE el mismo camino que la API. Si cada handler validara por su cuenta, el
auto-arranque acabaria siendo el camino con menos filtros --porque es el que se escribe el ultimo y
con menos cuidado-- y se pierde dinero sin que nadie lo decida.

`approved=True` de un gate es un VEREDICTO, no un permiso. La orden sale solo si
`validate_entry.approved and execution_quality.approved`, y el motivo de cada uno se ensena por
separado para que se sepa cual fallo.

El status HTTP tamben lo elige el servicio, no la ruta, y no se colapsa todo a 400: un
`sin_terminal` es 503 (reintentable) y un simbolo inexistente es 404. Un 400 para "no hay terminal"
hace que un cliente que reintenta solo ante 5xx no reintente nunca, y la orden se pierde en el
primer corte de conexion.

### D-055 - `log_setup` va ANTES de `order_send`

Una orden que sale y no se puede explicar es indistinguible de una orden que nunca se intento. Si
el proceso muere entre el envio y el registro, con el registro primero la fila sigue ahi con
`validated=1` y el resultado vacio, que es el estado "se intento y no consta". Al reves deja
operaciones sin fila, y una operacion sin fila no sabe ni si ocurrio.

Un rechazo por gate TAMBIEN se escribe, con `validated=0` y sus motivos: un rechazo sin fila no se
puede depurar. Y los intentos de llenado se graban contra ESA MISMA fila, porque con un solo
retcode final el 10030 del FOK desapareceria y el rechazo pareceria de un modo de llenado
cualquiera.

### D-056 - Las noticias fallan ABIERTO, pero el fallo abierto queda escrito

Un calendario que no contesta y una ventana con noticia de alto impacto son hechos distintos.
Bloquear por el primero deja el sistema parado por un fallo de feed. Se opera, pero la fila queda
con `verdict="IGNORED_NEWS"`: la ausencia de veredicto es un dato que hay que poder ver, no un
aprobado silencioso. Y un `block=True` manda sobre `fail_open`: si el calendario ha visto el
evento, su respuesta gana aunque no este seguro del resto.

### D-057 - Cerrar NO pasa por `symbols_allow` ni por el riesgo del dia

Una lista blanca de simbolos para ABRIR no puede impedir cerrar lo que ya esta abierto: hacerlo
dejaria la posicion viva sin ninguna manera de salir, y el unico remedio seria editar la config con
la posicion abierta. El riesgo del dia limita abrir; cerrar con el contador a tope es exactamente
lo que hay que poder hacer, porque es la operacion que REDUCE el riesgo.

### D-058 - `tick_value` de MT5 ya es por UN lote: no se multiplica por `contract_size`

`SYMBOL_TRADE_TICK_VALUE` es el dinero que mueve la cuenta por un tick de UN lote; el tamano del
contrato ya esta dentro. Multiplicarlo otra vez por 100.000 en EURUSD inflaba el riesgo del lote en
cinco ordenes de magnitud, y el sintoma era que toda operacion se rechazaba por "riesgo excede el
presupuesto" con cifras de millones cuando el presupuesto eran decenas.

Por eso la cuenta tiene UN nombre: `lot_calculator.risk_per_unit()`, usado tanto por el
dimensionado (`_lote_por_riesgo`) como por la comprobacion del presupuesto. Si los dos calcularan
por su cuenta, una diferencia entre ambos no daria error: daria un lote dimensionado con una cuenta
y validado contra otra, con los dos numeros redondeados y con aspecto de razonables.

### D-059 - El origen del riesgo se DECLARA, no se deduce

`loss_per_lot_source` se lleva explicito en vez de deducirse de si la cifra existe. Deducirlo
("si hay numero, es del broker") es como se llego a etiquetar de `broker` una estimacion hecha con
el tick value: las dos producen un numero, asi que el numero no dice de donde salio. Un campo que
miente sobre la procedencia del riesgo es peor que un campo ausente, porque alguien lo leera como
una medicion del broker.

### D-060 - Las comparaciones con umbral llevan tolerancia

`abs(1.10050 - 1.10000) / 0.00100` da `0.500000000000167`, no `0.5`. Con un `>` a secas, una entrada
EXACTAMENTE en el umbral --que la regla dice que se admite, porque dice "no puede superar"-- se
rechazaba segun el redondeo del ultimo bit. Un gate que en el borde depende del redondeo no es un
gate: el mismo setup se acepta o se rechaza segun la plataforma y nadie puede reproducir por que.

La tolerancia (`execution_quality.EPSILON` = 1e-9) se aplica solo a las COMPARACIONES con umbral,
que son decisiones. Las medidas se devuelven sin redondear, para que el log ensene el numero de
verdad.

### D-061 - Un cero medido no es un dato ausente

El `spread` de 0.0 (bid == ask: cotizacion bloqueada o sesion de spread cero) es la MEJOR medida
posible, y es el unico valor que la comprobacion de verdad de Python considera falso. Con
`if sp_price` la fraccion caia a `None` y el gate marcaba `unknown` de spread por tener la condicion
perfecta. Las comparaciones usan `is not None`; los denominadores siguen usando verdad a proposito,
porque una distancia de referencia de 0 si es un hueco.

### D-062 - La deduplicacion del watcher es memoria del PROCESO, y se pierde a proposito

El estado de ciclo (`setup_state`) si se persiste, y por eso un setup detectado no se vuelve a
anunciar. Los DESCARTES se deduplican distinto: por firma (score + motivos) en memoria del
proceso. La razon de no persistirlo es que una dedup permanente oculte un rechazo NUEVO que se
parece a uno viejo --el mismo setup, un mes despues, con el mismo score-- y un rechazo que no se
ve es un rechazo que no se corrige. El coste de perder la memoria (reinicio del proceso) es escribir
unas pocas filas de mas en `setup_log`, que es un ruido legible; el de no perderla seria un
historico mudo.

Por lo tanto la memoria de dedup tiene que VIVIR en el servicio y el servicio tiene que vivir mas
que una peticion. Construir un `WatcherService` por `POST /scan` hace que la dedup no sirva de nada
y que un setup por debajo del umbral escriba una fila cada `scan_interval_sec`, para siempre, sin
que nada falle: el fallo se ve en el historico, un mes tarde, cuando ya no queda memoria del ciclo.
De ahi `Runtime.watcher_service()` (una instancia por app) y el descarte de esa instancia cuando
`set_market` cambia el mercado: escanear contra un mercado desconectado es peor que duplicar dos
filas.

### D-063 - El watcher evalua y audita; la ejecucion se declara ausente, no apagada

`GET /api/watcher/status` publica los tres datos que el panel necesita y que no son lo mismo:
`auto_execute_conf` (lo que dice `strategy.yaml`), `auto_execute` (siempre `false`, que es la
lectura del interruptor) y `auto_execute_disponible` (`false`, con `auto_execute_motivo`). Publicar
solo el valor del YAML haria que el panel rotulara AUTO-EJECUCION sobre un sistema que no manda
nada; publicar solo `false` sin el motivo dejaria al operador culpa del interruptor.

`POST /api/watcher/auto-execute` responde `501` en los dos sentidos. Con un `200 {"ok": false}`,
`static/main.js` --que hace `res.ok ? res.json() : reject(...)`-- enseñaria "auto-ejecutar
ACTIVADO" despues de encender un interruptor que no encendio nada. Apagar algo que no se puede
encender tambien es no-op, asi que tampoco hay un 200 para `false`. El token se pide ANTES: sin
token la ruta es `401`, porque decir a cualquiera que la funcion no existe es documentacion publica
innecesaria.

El watcher no tiene puerto de ejecucion. Ni atributo, ni dependencia, ni `order_send`: cuando el
autoarranque entre, entra por `ExecutionService.execute_market_trade` con sus puertas (lista blanca,
riesgo del dia, noticias, `validate_entry` y calidad de ejecucion). En REF el escaneo tenia un
`executor` inyectado y con `auto_execute: true` mandaba la orden en el mismo ciclo que detectaba el
setup, lo que convierte la bandera en una decision de arranque: el sistema empieza a operar solo
desde que el proceso existe, sin nadie mirando cuando opera.

`POST /api/watcher/scan` PIDE TOKEN aunque no mande ordenes: escribe en `setup_log` y
`setup_state`, y quien puede escribir en la auditoria de operaciones es quien puede operar. Que no
opere no lo vuelve lectura. Nota de frontend: `static/main.js` no manda token en ninguna escritura
(incluida `/api/trade/market`), asi que con `API_TOKEN` puesto el boton de escaneo del panel
devuelve `401` igual que el de operar. Es el contrato del frontend heredado, no una excepcion del
watcher.
# CALIBRACIÓN ORDER FLOW / CINTA REAL DATABENTO (2026-10-06)

### D-064 - La raíz de datos es una VARIABLE, y la cinta no entra en el repo

La cinta de Databento pesa más que el código y el repo vive dentro de OneDrive, así que la
raíz no puede ser una constante dentro del árbol. `core/paths.py` expone `DATA_ROOT`
(env `TRINITY_DATA_ROOT`, default `C:\Users\fmaur\Desktop\trinity_data`) y
`data_root_guard_error(path)` devuelve el MOTIVO por el que una raíz no sirve: relativa
(dependería del CWD del proceso), dentro del repo (OneDrive + git), dentro de cualquier
OneDrive (gigabytes sincronizados a diario), dentro de REF (solo lectura). Devolver un motivo
en vez de lanzar es deliberado: quien descarga (`research/fetch_databento.py`) decide, y el
test fija que el default esté limpio y que cada regla dispare por separado. Un comentario
sobre por qué no hay que meter datos aquí depende de que alguien lo lea; una función que
devuelve el motivo no. La clave de API vive en `.env` gitignored (`.gitignore:9`) y
`load_dotenv` lo llama el script de descarga, porque `core/` no puede importar `dotenv`
(`tests/unit/test_core_purity.py` lo prohíbe).

### D-065 - El umbral del Z-score se expresa como FRACCIÓN del techo, no como número

`_update_zscore` actualiza la EMA con el mismo trade ANTES de puntuar, así que la varianza
EWMA de la muestra es siempre la del propio tamaño y el Z queda acotado por
`sqrt((w-1)/2)`: 4.9497 con `w=50`, 3.082 con `w=20`. Un umbral fijo de 4.5 con ventana 20
queda POR ENCIMA de ese techo y el detector se apaga sin ningún error, en silencio: el fallo
más caro posible, porque todo lo demás sigue en verde. `zscore_ceiling(ema_window)` y
`OF_ZSCORE_THRESHOLD_FRAC = 0.91` hacen esa dependencia explícita, y `update_settings`
recalcula el umbral absoluto cuando cambia la ventana (`settings_locked()` publica la
fracción). El 0.91 reproduce el umbral calibrado (4.5 sobre techo 4.9497 = 90.91%) a 4.5043,
diferencia menor que la resolución a la que se reporta el Z.

### D-066 - `6E.c.0` es identidad, no contrato: se descarga y calibra contra `6EZ6`

`OF_SYMBOL = "6E.c.0"` significa "próximo a expirar" y se recalcula solo: hoy resuelve a
`6EV6` (octubre) con 246 trades al día, mientras el mercado rueda en `6EZ6` (diciembre) con
51.118, 208 veces más. Se comprueba contra la simbología de la FECHA, no contra una
constante: `6E.c.0` -> 42001229 -> `6EV6`, `6E.c.1` -> 42823529 -> `6EX6`,
`6E.c.2` -> 5510 -> `6EZ6`. Decidido NO cambiar `OF_SYMBOL` a `6E.c.2`: es el líquido HOY y
el offset numérico es frágil, mientras que `normalize_symbol("6E.c.0") == "6E"` es identidad
y la esperan `test_base_adapter.py` y el frontend. Identidad y contrato negociado se
separan: la identidad vive en `OF_SYMBOL`, la descarga y la calibración van contra `6EZ6` de
forma explícita (`research/build_real_fixture.py::RESOLVED_SYMBOL`). El peligro no era tener
el símbolo equivocado, era tenerlo EN SILENCIO: nada fallaba, simplemente se calibraba
contra 246 trades.

### D-067 - La calibración se declara contra un fixture real trazable; si las cifras históricas no se reproducen, se recalibran y se deja constancia

Las cifras que `orderflow_config.py` citaba (4 h NY, ~20.5k trades, 620 prints a Z>=2.5, 42
a Z>=4.5, ~10 zonas) no se reproducen en la cinta de 2026-10-05, y no se reproducen porque
eran de OTRA sesión: ninguna ventana de 4 h pasa de 14.8k trades (así que "4 h" y "20.5k" ya
chocaban entre sí en el propio original), a día completo hay 1511/116 y escalar a 20.5k da
~616/47, y las ventanas de 8 h con 20-23k trades dan 627-653. La sesión original era de ~8 h
y de otro día. Decisión: el fixture se fija en 8 h de NY (10:00-18:00 UTC del 2026-10-05,
21926 trades de `6EZ6`), las cifras del docstring se REESCRIBEN a lo que ese fixture mide
(631 / 61 / 8), y el historial 620/42/~10 se conserva en `MEJORAS_DATABENTO.md` porque
borrarlo sin más haría que nadie supiera que alguna vez existió. No se cambia NINGÚN valor
de umbral: solo la prosa que lo describe. Y como "documentarlo" no basta,
`tests/unit/test_orderflow_real.py` fija el sha256 del fixture contra su sidecar y vuelve a
medir el motor, de modo que o el archivo es el que dice el sidecar, o el módulo es lo que
dice el test. Regenerar con `research/build_real_fixture.py`, no parchear el número a mano.

# CONVALIDACIÓN DE LA ESTRATEGIA SOBRE CINTA REAL 6E (2026-10-06)

### D-068 - La estrategia se CONVALIDA con un backtest sobre cinta real, no con más calibración de umbrales

La pregunta de fondo era si el gate (SMC + CVD + killzones + SMR) tiene edge operativo sobre 6E.
Se responde con un backtest M15 sobre 90 días de cinta real de Databento (costo real 2,49 USD, no
los 6 presupuestados) y NO con más ajuste de umbrales: convalidación, no optimización. El backtest
reproduce el runtime real de Trinity —`smc_engine.analyze` (300 velas + PDH/PDL del día UTC
anterior CON DATOS) → `risk_engine.setup_score` (CVD 30 velas, `cvd_component` con `_trend_coef`,
weights `cot 0 / cvd_of 20 / smc 50 / killzone 0 / smr_dxy 15`, killzones Londres 07:00-10:00 y
NY 12:30-15:30 UTC) → `setup_gate.evaluate_gate`—, no un `sim.py` de REF. Los cuatro agregados del
operador se cierran así: MFE/MAE se reportan en raw (sin retail simulator, con fricción documentada
solo en la ejecución); el split IS/OOS con embargo se aprueba (D-069) sin recalibrar nada; la
guardia de OneDrive ya estaba resuelta por diseño (raíz de datos real `trinity_data`, fuera de
OneDrive y sin ReparsePoint); y los failure modes salen gratis del `reason_tally` de
`evaluate_gate` (D-070). El veredicto no se filtra por métrica: se entrega con los DOS modelos de
fill y los números completos (n, win, exp en pips y USD).

### D-069 - Split IS/OOS 60/30 con embargo de 24 h y ventanas de roll medidas sobre la propia cinta

90 días de trades: 6EU6 (trimestal de septiembre) + 6EZ6 (diciembre). El roll se detecta en la
cinta, no se impone: el cruce de volumen ocurre el 2026-09-11 (6EU6 5.657 trades vs 6EZ6 30.639
ese día), así que `ROLL_TS = 2026-09-11T00:00:00Z`; antes se usa 6EU6 y desde ahí 6EZ6, y la cola
del contrato viejo y la cabeza del nuevo en transición se DESCARTAN (mezclarlos en la misma vela
no es un precio limpio). Los setups que se INICIAN en `[2026-09-10T00:00Z, 2026-09-12T00:00Z)`
(solo 48 h, roll por exclusión sin offsets artificiales) se cuentan como EXCLUIDOS, no como
rechazos. Día degradado según Databento: `2026-08-29` (en IS, documentado en el sidecar). El split:
IS `[2026-07-08 00:00Z, 2026-09-06 00:00Z)` (60 días, contiene el degradado), embargo
`[2026-09-06, 2026-09-07)` (solo contexto, sin setups) y OOS `[2026-09-07 00:00Z, fin)` (~30 días,
contiene el roll). Única calibración permitida: `sl_distance_by_symbol["6E"] = 0.0012`, ajustado
SOLO con datos IS y con el origen marcado `symbol`; el resto (min_score 59.5, pesos, TTL 40) son
los de `strategy.yaml` sin tocar.

### D-070 - Los failure modes salen del `reason_tally` del gate, y el veredicto NO convalida la estrategia

Los motivos de rechazo del gate (`reason_tally` de `evaluate_gate`: fuera de killzone, score bajo,
RR, etc.) son los failure modes del operador, sin ningún detector extra: 5.404 setups detectados,
693 aprobados; rechazos top: fuera de killzone 3.988 (~74 %) y luego "score < 59.5" (con killzone
en peso 0.0, el score efectivo del gate es ~70/85 = 82 %, fiel al runtime). Resultados finales
(min_score 59.5, SL 12 pips, fricción 1 pip adversa a entrada y salida, 1 contrato 6E = 25
USD/pip): **`zone_ttl`** (canónico) IS 43 trades, win 20.9 %, exp −6.465 pips (−6.950 USD); OOS 23
trades, win 39.1 %, exp +0.087 pips (+50 USD, PF 1.01). **`next_open`** (cota superior simplificada)
IS 105 trades, win 47.6 %, exp −3.714 pips; OOS 51 trades, win 49.0 %, exp −4.196 pips (−5.350
USD). Sin edge positivo en OOS bajo NINGÚN modelo de fill: la esperanza no supera la fricción de 1
pip más la mecánica de fills. El sesgo de dirección cambia de signo entre IS (25 BUY/18 SELL) y
OOS (3 BUY/20 SELL, 29 % BUY): sesgo de tendencia del periodo, no del sistema. Cierres: 48 SL / 18
TP. **La estrategia NO se convalida** con esta calibración mínima; el pipeline queda reproducible
(`research/build_6e_candles.py` + `research/backtest_6e.py`) para revalidar tras cambios en el
gate o en la mecánica de fills.

# ARQUITECTURA MULTI-ESTRATEGIA (2026-10-06) — CTA Swing + Mean Reversion + Copilot + ILOF

### D-071 - Convalidación antes que conexión: el roadmap multi-estrategia se aprueba, y ninguna estrategia nueva se ejecuta en vivo sin veredicto OOS

Veredicto del operador, incorporado íntegro (4 opciones recomendadas) y aceptado: (1) Fase 0 =
cierre limpio de 6E solo administrativo —commit + documentación de `research/`—, sin deploy a
demo; **rotar la API key de Databento queda pendiente por decisión del usuario (se deja como
está)**. (2) ILOF entra en la arquitectura como "módulo presente en investigación": su veredicto
NO convalida no bloquea la infraestructura multi-perfil, y la revalidación se hace aislada en
`research/` más adelante. (3) Config por perfil: `strategy_<perfil>.yaml` + `strategy_map`
(magic→perfil), aprovechando que `STRATEGY_PATH` ya es una variable de entorno
(`core/paths.py:41`) y ampliando `sl_distance_by_symbol` a `by_profile_by_symbol`; un YAML único
haría que los killzones/TTL intradía contaminaran al CTA D1 en silencio. (4) Datos del CTA:
OHLCV D1/H4 de MT5 demo (`copy_rates`) sobre EURUSD/XAUUSD/US500/GBPUSD/AUDUSD, gratuito y
suficiente para tendencias; Databento queda SOLO para microestructura intradía (M1/M15). Los
cinco principios innegociables: `ExecutionService` sigue siendo la única puerta de salida (la
aduana de capital no se bifurca); `regime()` (`core/risk_engine.py:126`) es el árbitro de
desactivación mutua (expansión→ILOF+CTA, compresión→VWAP, noticias→solo Copilot); cada módulo
entra por `research/` con split IS/OOS + embargo + fricción (el pipeline de `backtest_6e.py`);
el despliegue nuevo empieza en modo alerta/simulado (`auto_execute=false`, D-063) y solo después
en vivo; la suite en verde (1477 passed, 2 xfailed) es el criterio primario de aceptación por
commit. Fases: F0 cierre 6E, F1 infraestructura multi-perfil (strategy_map + DD por magic en
`daily_risk_state`), F2 convalidación CTA, F3 convalidación VWAP + régimen como interruptor, F4
despliegue en alerta, F5 ejecución multi-estrategia (nuevo `core/exit_policy.py` para trailing
D1, gates evalúan el plan de cada perfil). Detalle en `ROADMAP.md` y `CHECKLIST.md`.

### D-072 - F1 entregada: `strategy_map` magic→perfil, SL por perfil y exposición por magic en `daily_risk_state`, sin tocar la ejecución

F1 se implementa y queda detrás del mismo seam de config que `STRATEGY_PATH`: nuevo módulo puro
`core/strategy_map.py` (parsea el mapa, resuelve `profile_for_magic` → `default` si desconocido o
sin fichero, deriva `sl_distance_by_profile` con precedencia `by_profile_by_symbol[perfil][SYM]` →
legacy `sl_distance_by_symbol` SOLO para el default → `sl_distance` global → `sl_default_pips` x
spec → `(None, "none")`, todo bajo `StrategyMapError`); `settings/strategy_map_source.py` (lectura
cacheada por firma mtime+tamaño, ausente→`DEFAULT_MAP={8882026:"default"}`, roto→lanza);
`config/strategy_map.yaml` con el mapa real; `STRATEGY_MAP_PATH` en `core/paths.py`; seam
`ConfigSource.strategy_map()` en `database/store.py` (import tardío, `StrategyConfigError`→
`ConfigUnavailable`); en `api/services/mt5_market.py` `daily_risk_state` expone `trades_by_magic`
y `profiles_by_magic` (los conteos son HECHOS del historial, el perfil sale del mapa; los topes
por perfil llegan en F2+, fase de despliegue 2). El perfil "default" es el heredero histórico:
un magic desconocido, un fichero ausente o un fallo del mapa resuelven SIEMPRE a "default"
(comportamiento de un solo YAML intacto). REGLA DE ORO F1 cumplida: nada de esto se consume en
ejecución todavía (`execution.py`, `watcher_service.py`, gates y `strategy.yaml` sin cambios);
los módulos nuevos solo se validan en tests (`tests/unit/test_strategy_map.py`, 29 tests;
`test_api_market_port.py` con `Deal.magic`, 3 tests de exposición; `test_store.py` con el seam).
Lint: ruff no está instalado en el entorno (ni en el PATH ni como módulo), la validación de F1 es
`py_compile` + límite de 170 chars en `core/` (máx. observado 95) + suite completa
`1515 passed, 2 xfailed` (antes de F1: `1477 passed, 2 xfailed`).
