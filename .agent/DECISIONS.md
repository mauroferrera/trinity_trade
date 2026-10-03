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
