# CHECKLIST.md - Checklist Ejecutable (Plan → Práctica)

> Seguimiento paso a paso por fases. Marcar `[x]` al completar. Actualizar `PROJECT_STATE.json` al mover entre fases.

## ✅ BLOQUEO RESUELTO (2026-10-01)

El código de referencia **sía existe** en `C:\Users\fmaur\Desktop\Trading` (el antiguo
`Trinity_proyect/Trading/` era un placeholder de OneDrive ya eliminado). Inventario
verificado: `app.py` 5.185 líneas, `store.py` 1.752, `agent.py` 1.308, `risk_engine.py` 628,
`pattern_engine.py` 234, `simulator.py` 507, módulo `core/` propio, `strategy.yaml`,
`trading.db` y **41 archivos de test** con `conftest.py` + `pytest.ini`. El repo de referencia
está versionado y con remote en GitHub.

**Ruta canónica:** `C:\Users\fmaur\Desktop\Trading` — solo lectura, nunca se modifica ni se importa.
Decisión registrada en `DECISIONS.md` (D-005).

## Fase 0 – Preparación (Scaffolding base) — ✅ COMPLETA
- [x] Crear estructura de directorios (`config, core, adapters/*, macro_ingestor/*, agent, api/routes, database, static/*, tests/*, .agent`) + `__init__.py`
- [x] Crear `.agent/PLAN.md`
- [x] Crear `.agent/PROJECT_STATE.json`
- [x] Crear `.agent/AGENT_GUIDELINES.md`
- [x] Crear `.agent/ROADMAP.md`
- [x] Crear `.agent/DECISIONS.md`
- [x] Crear `.agent/CHECKLIST.md`
- [x] Crear `README.md` profesional (refleja el estado real, no el deseado)
- [x] Crear `.env.example`
- [x] Crear `requirements.txt` (fastapi, uvicorn, MetaTrader5, databento, ccxt, litellm, pydantic, pyyaml, python-dotenv, pytest…)
- [x] Crear `config/asset_sources_map.yaml` (esqueleto B3/Forex/Cripto, 7 activos, `session_id` enlazado a `trading_hours.json`)
- [x] Crear `config/trading_hours.json` (sesiones/liquidez por mercado, timezones IANA, control de `confidence`)
- [x] Validar configs parseables (`yaml.safe_load` + `json.load`) y `python -m compileall`

**Criterios OK Fase 0:** Árbol creado, .agent/* válidos, configs base creados. Tests N/A (scaffolding).

> Nota Fase 7: `config/trading_hours.json` ya tiene contenido verificado contra fuentes
> oficiales, pero la ventana `b3_fixed_income` está marcada `confidence: unverified` y
> `requires_verification: true` (la B3 declara que los contratos de renta fija quedan
> "inalterados" sin publicar la grade en el OC consultado). Verificar antes de producción.

## Fase 1 – Núcleo (`core/`) – Agnóstico al Mercado
- [x] Mover `REF/risk_engine.py` → `core/risk_engine.py` (ajustar imports, validar lógica pura)
- [x] Crear `core/smc_engine.py` (extraer lógica pura de `REF/pattern_engine.py`, aislar I/O)
- [x] Crear `core/orderflow_engine.py` (extraer `OrderFlowEngine` app.py + cálculos `cvd_service.py`, feed-agnóstico + DI)
- [x] Mover `REF/simulator.py` → `core/simulator.py` (ajustar imports)
- [x] Crear `core/lot_calculator.py` (normalizador lotes/minis/USDT)
- [x] Verificar/mover `core/paths.py`, `core/trade_history.py` (integridad)
- [x] Verificar core sin imports de `adapters/api/agent` (regla unidireccional)
- [x] Ejecutar tests unitarios core (`pytest tests/unit -v`) → **VERDES** (258 passed, 2 xfailed)

### Añadido durante la ejecución (no estaba en el plan original)

- [x] `core/orderflow_config.py` (constantes de calibración; era un fichero suelto en la raíz de REF)
- [x] `core/market_view.py` (footprint/heatmap/barras por evento; pura, y `simulator.py` la necesita)
- [x] `adapters/synthetic_feed.py` (desde `REF/mock_feed.py`; sin esto no hay fixtures deterministas)
- [x] `config/strategy.yaml` (byte-idéntico a REF; `tests/conftest.py` lee las killzones de ahí)
- [x] `tests/unit/test_core_purity.py` (guardia AST de la regla unidireccional)
- [x] `tests/unit/test_paths.py` (adaptado: las rutas ahora son `config/` y `database/`, no la raíz)
- [x] `tests/unit/test_lot_calculator.py`

**Decisiones tomadas en esta fase**

- `core/` NO importa `adapters`. `simulator.py` y `orderflow_config.py` quedan en `core/` porque son lógica pura; la fuente sintética (`synthetic_feed.py`) sí es un adaptador, y por eso `core` no la importa: solo la usan los tests.
- `OrderFlowEngine` recibe `symbol` por constructor y `fmt_ts` por inyección. En REF el símbolo venía de una constante global y el timestamp de `tclock.fmt_utc`, que arrastraba `strategy` y MetaTrader5 al núcleo.
- `pick_cvd_source` recibe las etiquetas de fuente por parámetro. En REF iban hardcodeadas (`"live (Databento 6E)"`), lo que mentía en cuanto el feed no era Databento.
- `tests/conftest.py` lee `config/strategy.yaml` directamente en vez de `strategy.get_trading_config()`: mismo resultado, sin el módulo `strategy` (Fase 6). El canario de la DB real se conserva midiendo `core.paths.DB_PATH`.
- **Tests pospuestos, no descartados**: `TestOfFeedGuard`, `TestSetupEvalSynthetic` y `TestRiskSetupSynthetic` (exercitan `app.py`), y todo lo de `store.py` / `strategy.py` / `smr_service.py`. Volverán en las fases de API y persistencia.
- **Dos bugs reales encontrados por los tests** en `core/lot_calculator.py` (nuevo): `risk_value` reportaba `lots * tick_value`, que no es el riesgo de la posición (faltaba la distancia al SL); y un SL más fino que un tick devolvía un tamaño desproporcionado en vez de fallar. Ambos corregidos.

**Pendiente de calibración (2 xfail, ambos de decisión de negocio, no de código roto)**

- `test_missing_cvd`: `smr_dxy` pesa 15 y el fixture no trae SMR, así que el techo cae a 82.4 y 58.8 queda bajo el corte de MEDIA.
- `test_sweep_and_reverse_favors_sell`: el COT pesa 0 desde `64883d3`, así que un COT bajista ya no empuja a ALTA.

## Fase 2 – Persistencia (`database/`) - COMPLETA
- [x] Mover `REF/store.py` → `database/store.py` (ajustar imports `core.paths`)
- [x] Crear `database/models.py` (esquema/tablas) + `database/schema.sql` generado
- [x] Decisión DB: empezar de cero (D-014)
- [x] Verificar `DB_PATH` único (absoluto)
- [x] Tests persistencia → VERDES

**Esquema idéntico al de producción, verificado**

- `tests/fixtures/schema_snapshot.json` congela el esquema de `REF/trading.db` (14 tablas, tipos, defaults, PK e índices). `test_schema.py` falla si el esquema se desvía, así que la Fase 2 no puede "funcionar" contra una forma de tabla distinta a la que espera el resto del sistema.
- `schema.sql` se genera desde `models.py` y un test compara el fichero contra el generador. Sin eso el `.sql` sería una segunda fuente de verdad que nadie actualiza.

**Tres correcciones que no eran cosméticas**

- `REF/store.py:33` recalculaba `DB_PATH` por su cuenta, ignorando `core.paths`. Ahora delega, y hay un test que lo fija: dos rutas a la DB es la forma más fácil de escribir en el fichero equivocado y no enterarse.
- `apply_migrations` reventaba con `no such table: roles` al correr contra una base recién creada. Ahora salta las migraciones de tablas que aún no existen.
- `init_db()` depended de `strategy` (Fase 6) a través del seam y no podía crear el esquema sin el agente. Al revés de lo razonable: ahora siembra el rol `general` con prompt vacío y avisa con `RuntimeWarning`. Solo se traga `ConfigUnavailable`, nunca un error de config real (hay test: un YAML roto sigue exploando).

**Contrato que los tests dejaban por descubrir**

- `update_journal_entry` es un PUT (reemplaza la fila entera), no un PATCH: las claves que no vienen se pierden. Lo documenta el docstring porque la firma no lo delata.
- `get_messages` filtra las filas `role='tool'` a propósito (existen como traza de debug; devolverlas sin el `tool_calls` emparejado rompería la API del agente). El test afirma las dos mitades: persistido sí, expuesto no.

**Verificación**

- Suite completa: `347 passed, 2 xfailed` (los xfail siguen siendo los de calibración heredada de la Fase 1).
- `database/trading.db` creada y vacía (14 tablas, 1 rol, 0 trades). `.gitignore:16` la excluye, confirmado con `git check-ignore`.

## Fase 3 – Adaptadores (`adapters/`) - COMPLETA
- [x] Crear `adapters/base_adapter.py` (interfaz abstracta OHLC/Ticks/Depth + datos normalizados)
- [x] Crear `adapters/forex/mt5_forex.py` (extraer MT5: patterns_service, cvd_service, mt5_export, lock único)
- [x] Crear esqueletos `adapters/b3/mt5_b3.py`, `nelogica_profit.py`, `cedro_technologies.py`
- [x] Crear esqueletos `adapters/crypto/binance_ws.py`, `bybit_ccxt.py`
- [x] Crear `adapters/forex/databento_cme.py` (opcional)
- [x] Tests adaptadores (mocks) → VERDES

**La forma normalizada ya no se inventa: se hereda de lo que `core/` consume**

- Vela: `{"time", "open", "high", "low", "close", "volume"}` con `time` en SEGUNDOS epoch. Es lo que ya esperan `core.market_view` y `core.orderflow_engine`, no un formato nuevo.
- Trade: `{"ts", "price", "size", "side"}` con `side` en `"A"`/`"B"` = lado AGRESOR. `A`/`B` y no `buy`/`sell` porque el agresor no es el lado que cierra: en una compra agresiva el agresor está en el Ask.
- `SymbolSpec` con la regla del pip (`point * 10 si digits >= 3`), que ya distingue pips de puntos en oro e índices.
- El CVD **no** se recalcula aquí: `core.orderflow_engine.build_cvd_series()` ya es puro y es el dueño de ese cálculo. El adaptador solo entrega velas.

**El bug que motivó la fase: REF tenía TRES puertas a MT5**

- `patterns_service.py` con executor `mt5p`, lock y `initialize()`/`shutdown()` propios.
- `cvd_service.py` con executor `mt5c`, lock y ciclo propios.
- `research/data.py` con un `RLock` y ciclo propios. `app.run_mt5` es alias de `patterns_service._mt5_run`.

Tres ciclos de vida para un terminal que es un recurso de proceso es la forma de que dos hilos lleguen a la vez. Ahora hay una `Session` por proceso con un `ThreadPoolExecutor(max_workers=1)`: una sola puerta, y `close()` drena lo que está en vuelo antes de cerrar. Tres tests lo fijan, y uno de ellos corre la prueba 20 veces seguidas porque este tipo de bug es intermitente por naturaleza.

**Dos decisiones que los tests obligaron a cambiar**

- El doble de MT5 (`tests/unit/mt5_fake.py`) NO lleva cuenta de concurrencia: contar ahí metería una lock en el sitio donde se quiere demostrar que no la hay. La serialización se demuestra con `threading.Event` (el segundo trabajo no puede entrar mientras el primero está dentro), no con un contador que el doble ajustaría por su cuenta.
- `runtime_checkable` de Python 3.12 mira NOMBRES, no significados: `isinstance(DatabentoCME(), MarketDataAdapter)` es `True` aunque su `symbols()` no tenga respuesta honesta. Se documenta como límite conocido en vez de fingir que el Protocol protege. La barrera real tiene que ser la lista explícita de adaptadores de la API.

**Esqueletos: construir NO falla, usar sí**

- Un registro hace `{a.name: a() for a in ADAPTADORES}`; si el `__init__` lanzara, importar la API tiraría abajo el arranque por un mercado que nadie pidió. El fallo llega al usar el adaptador, que es donde el mensaje puede ser accionable.
- B3 y cripto lanzan `AdapterError` (mercado no disponible). Databento **no**: es un proveedor de datos, no un mercado, y su fallo es de configuración. Mezclarlos haría que un `except AdapterError` que significa "el bróker no responde" se tragara un error de clave de API.
- `WINFUT26` **no** se normaliza a `WIN`. `normalize_symbol()` corta en `.`, `_`, `-`, `#` y un futuro sin separador no tiene dónde cortar; adivinar dónde acaba el subyacente (`WIN` vs `WING`) es un parser de exchange. Queda documentado como límite en vez de escrito como heurística.

**Verificación**

- Suite completa: `467 passed, 2 xfailed` (los xfail siguen siendo los de calibración heredados de la Fase 1).
- `test_adapter_skeletons.py` fija el contrato de los cinco: 35 tests.
- Import de los seis módulos de adaptadores sin abrir terminal, y sin que ninguno tire al importarse.

## Fase 4 – Macro/Ingestores (`macro_ingestor/`) - COMPLETA
- [x] `macro_ingestor/base_ingestor.py` (contrato, jerarquía de errores, caché con TTL y seam HTTP)
- [x] `macro_ingestor/forex/cot_service.py` (desde REF/cot_service)
- [x] `macro_ingestor/forex/dxy_service.py` (desde REF/smr_service)
- [x] `macro_ingestor/forex/calendar_news.py` (desde REF/ff_calendar)
- [x] `macro_ingestor/registry.py` (nombre del YAML → módulo real)
- [x] Evaluar `REF/decision_context.py` → decisión registrada (D-028: es puro, va a `core/` en la Fase 6)
- [x] Tests ingestores (mocks HTTP) → VERDES
- [ ] Esqueletos `macro_ingestor/b3/*` — **descartados, no pendientes** (D-026)
- [ ] Esqueletos `macro_ingestor/crypto/*` — **descartados, no pendientes** (D-026)

**El error que la fase tenía que evitar: el respaldo viejo sin marcar**

REF tenía tres versiones de la misma caché y las tres servían la copia vencida **sin decir
que era vencida**. Un `payload` de la semana pasada con `reason: ""` es indistinguible de uno
de hace un minuto, y quien lo lee no tiene forma de notarlo. Ahora `stale` y `reason` viajan
siempre en la lectura, incluso cuando no hay dato.

`IngestorError` no hereda de `AdapterError` (D-022): "no sé qué dice el COT" y "el bróker no
responde" llevan a acciones distintas, y un solo `except` las Mezclaría.

**COT: el índice sale de Legacy Futures Only, no del TFF**

El índice clásico de 26 semanas se define sobre los No Comerciales de `Legacy Futures Only`;
el TFF Moderno da el detalle (Asset Managers, Leveraged Money) que alimenta el score cuando no
hay señal direccional clara. Las dos se cruzan por fecha de reporte y una fila incompleta **se
descarta**: rellenar con `0` fabricaría un delta, y en un delta el `0` significa "sin cambio",
no "no lo sé".

Bug de REF corregido: `_fetch()` devolvía `[]` cuando la CFTC caía, y `[]` es una lista válida
de cero reportes. El sistema leía "el COT no dice nada" donde la verdad era "no lo supe leer".

**DXY: alinear por tiempo, y callar antes que desviar**

Comparar `dxy[-1]` con `eurusd[-1]` **por posición** compara el DXY de hoy con el EURUSD de hace
tres días cuando hay un festivo en Nueva York, y la divergencia sale con el signo invertido sin
que nada falle. Ahora se alinean por timestamp y, si las dos fuentes están desplazadas una vela
de forma constante (apertura contra cierre), **no se emite SMR**. Un desfase constante significa
que las dos series no describen la misma ventana; realinearlo en silencio deja que el signo
decida el resultado. Ante la duda: `confirmed=False`.

La caché de velas es por timeframe. Una única caché comparaba H1 con M15 y devolvía un SMR
"correcto" calculado sobre dos ventanas que no se corresponden.

**Calendario: fail-open, con la hora declarada**

El fallo que motiva la decisión es de REF: cerraba el trading cuando el calendario no se podía
leer. Una caída de un proxy de terceros convertida en "no se opera nunca más" deja el bot
parado sin explicación. Ahora devuelve `ok=True` con `fail_open=True` **explícito**, y si no se
pudo leer el reloj del calendario marca `timezone_assumed=True`. Un gate que cree estar en la
ventana correcta sin saber la hora es peor que uno que no sabe.

El buffer es simétrico (±15 min): uno solo hacia adelante deja entrar la mitad del riesgo.

**El puente con la configuración (D-027)**

`asset_sources_map.yaml` nombra conceptos (`cot_report`, `dxy_correlation`,
`ecb_fed_calendar`) y el código se llama servicios (`cot_service`, `dxy_service`,
`calendar_news`). Sin una tabla que una ambas cosas, el YAML no ejecuta nada y no hay forma de
notarlo. `registry.py` la hace, y **no filtra en silencio**: un nombre sin implementación vuelve
como neutro con el motivo, y se distingue "pendiente" (decidido) de "olvidado" (bug).

Hay un test que cruza el YAML con el registro: añadir un `soft_data_sources` nuevo sin
registrarlo rompe la suite.

**Por qué no hay ocho esqueletos B3/cripto (D-026)**

REF no tiene contrato ni endpoint para ninguna de las ocho fuentes que declara el YAML. Un
esqueleto de adaptador falla visiblemente; un esqueleto de ingestor que devuelve `neutro(...)`
parece una fuente rota a diario, y uno que devuelve un esquema inventado parece una fuente que
funciona. Un test con un fixture ficticio pasa en los tres casos, así que el test no protege de
nada. Se declaran en `registry.PENDIENTES` y son consultables con `registry.faltantes()`.

**Verificación**

- `test_macro_ingestor.py`: 89 tests (77 de los tres servicios + 12 del registro).
- Suite completa: `556 passed, 2 xfailed` (los xfail siguen siendo los de calibración heredados
  de la Fase 1).
- Ningún test toca la red: el `opener` se inyecta siempre, y donde hace falta un `poll` se
  inyecta uno falso.

## Fase 5 – Agente IA (`agent/`) - COMPLETA
- [x] `agent/ports.py` (los puertos que el agente necesita del exterior, y `AgentDeps`)
- [x] `agent/tools.py` (18 herramientas, esquemas y execution con puertos)
- [x] `agent/prompt_templates.py`
- [x] `agent/laya_bridge.py` (router, bucle de herramientas y stream SSE)
- [x] Migrar tools/router/SSE de `REF/agent.py` → nueva estructura
- [x] Mejorar persistencia tool results (digest con argumentos a la traza, payload completo al modelo)
- [x] Tests agent/tools (mocks) → VERDES

**El error que la fase tenía que evitar: el agente decidiendo lo que no debe decidir**

`REF/agent.py` mezclaba en 1.308 líneas el router de modelos, el bucle de tool-calling y el
generador SSE, y sus herramientas hacían `import app` dentro de cada handler. Eso producía tres
fallos con nombre: el agente no se podía probar sin el API, la capa de explicabilidad quedó
soldada a MT5-Forex, y el import era un efecto secundario. Ahora los puertos son `Protocol`
(`database/store.py` los satisface sin adaptadores) y `import agent` no abre terminal ni carga
un cliente de LLM.

El bucle **no calcula nada**: el score es de `core/risk_engine.py` y la política la redactan las
plantillas. Si el bucle puede "ajustar" un número, ese número deja de ser auditable.

**Tres estados de herramienta, no dos**

REF tenía `ok` / `failed` / `offline`, y `offline` significaba dos cosas distintas según quién
mirase: para `price` era "el símbolo no está en Market Watch" y para `set_chart_alert` era "MT5
no respondió". Ahora son `ok`, `failed` (el puerto existe y reventó) y `unavailable` (el puerto
no está cableado, y no hay nada que reintentar: hay algo que terminar de construir).

**`done` es un invariante del stream (D-033)**

REF lo emitía en cinco sitios: añadir un camino nuevo y se olvidaba, y el cliente se queda con
un stream abierto para siempre. Aquí el generador interno nunca lo emite; lo emite el
envoltorio, una vez, sea cual sea el camino — y si el cliente desconecta no se emite, porque no
hay nadie que lo lea.

Por la misma razón **no hay failover después del primer `delta`**: REF encadenaba la respuesta
del modelo B sobre la del modelo A, una encima de otra. Media respuesta es mejor que dos
respuestas superpuestas. La vuelta atrás solo existe antes del primer texto.

**La traza y el modelo no ven lo mismo, a propósito (D-034)**

A la base va el sobre de `prompt_templates.tool_digest`, con argumentos, estado, tamaño y
recorte declarado; al modelo, el payload completo. REF guardaba el JSON entero del resultado
—cientos de KB en `chart_snapshot` por llamada— y no guardaba los argumentos: una traza sin
ellos no permite reproducir la consulta.

**El reloj se inyecta también en modo herramientas, y el historial se lee antes de persistir**

La política de riesgo que lee el modelo en modo herramientas habla de killzones en UTC, así que
sin la hora no se puede aplicar. Y el historial se lee ANTES de guardar el turno del usuario:
REF persistía y luego releía, con lo que la pregunta llegaba dos veces al modelo.

**Verificación**

- `test_agent_ports.py` 23, `test_agent_tools.py` 60, `test_agent_prompts.py` 71,
  `test_laya_bridge.py` 56.
- Suite completa: `766 passed, 2 xfailed` (los xfail siguen siendo los de calibración heredados
  de la Fase 1).
- Ningún test toca la red: LiteLLM entra por el seam `completion` y el `sleep` del backoff por
  `deps.sleep`, así que el test del 429 con reintentos no tarda ni un milisegundo.

## Fase 6 – API (`api/`) + Frontend (`static/`)
- [x] Extraer rutas `REF/app.py` → `api/routes/*.py` (health, market, agent, journal, orderflow, macro, trading, setup)
- [x] Mover WebSocket → `api/websocket_manager.py`
- [x] Reducir `api/app.py` (bootstrap/middlewares/mounts)
- [x] Mover estáticos → `static/` + actualizar `StaticFiles`
- [x] Desacoplar lógica negocio → core/servicios (endpoints delgados)
- [x] Import check + smoke test servidor (`uvicorn api.app:app` import OK)
- [x] Integration tests API → VERDES (1144 passed, 2 xfailed)
- [x] Contrato con `static/main.js` escrito y verificado (46 rutas servidas; 17 pendientes, inventariadas)
- [x] `core/strategy.py` (seam de configuración: `build_flat`, resolvers de pesos y killzones)
- [x] `core/setup_gate.py` (gate puro: zona de entrada, niveles, killzone forzada, veredicto)
- [x] `api/services/setup_eval.py` (composición de la respuesta y overlay de ECharts)
- [x] `api/routes/setup.py` (`/api/risk/setup` y `/api/chart/setup-eval`)
- [x] `api/routes/watcher.py` (status/scan/auto-execute) — el watcher evalúa y audita; la auto-ejecución se DECLARA ausente (`501`), no apagada (D-062, D-063)
- [x] `api/routes/trading.py` — las 4 rutas `/api/trade/*` existen, más `GET /api/trade/info/{symbol}`, `POST /api/trade/market` y `POST /api/positions/{ticket}/close`
- [ ] `GET|POST /api/orderflow/feed`, `/api/orderflow/fixtures`, `/api/stream/{symbol}` — dependen del proveedor de ticks (6E)
- [x] `core/execution_quality.py` — módulo puro con `measure`/`evaluate`/`spread_points`/`reason_code`; mide deriva de entrada, spread sobre SL e invalidación alcanzable

### Hito "Puerto de ejecución MT5" (cerrado)

**Qué se añadió**
- `core/execution_quality.py` — gate puro. `approved=True` es un VEREDICTO, no permiso: quien ejecuta tiene que mirar los dos gates (`validate_entry` y este) por separado.
- `adapters/forex/mt5_execution.py` — `MT5ExecutionAdapter` con la misma `Session` de lectura. `order_send` devuelve objeto `MqlTradeResult` o `None`, nunca dict. `SYMBOL_FILLING_MODE` se lee como MÁSCARA de bits (FOK=1, IOC=2), que es lo que publica el bróker real para EURUSD: solo FOK.
- `api/services/execution.py` — `ExecutionService`, la puerta ÚNICA de salida. Vive en un servicio y no en los handlers para que el watcher y la API tengan exactamente las mismas puertas.
- `api/services/mt5_market.py` — `daily_risk_state()` devuelve `blocked`/`reasons`/`trades_counted` y usa `max_trades_day` del estado, no el riesgo crudo.
- `api/runtime.py` — `Runtime.execution` cableado al puerto, compartiendo sesión.
- `tests/unit/test_mt5_execution.py` (30) y `tests/unit/test_execution_service.py` (58) — cada puerta tiene su test de rechazo.

**El orden de las puertas, y por qué no es arbitrario**
1. Símbolo y acción: una petición mal formada no toca el bróker.
2. `symbols_allow`: antes de leer la cuenta.
3. Riesgo del día (503 si no se puede leer: un fallo de lectura no es un permiso).
4. Noticias: `block=True` bloquea aunque venga `fail_open`; sin veredicto se opera avisando y la fila queda `IGNORED_NEWS`.
5. Spec/precio → SL/TP → lote → `validate_entry` + `execution_quality`.
6. `store.log_setup` **antes** de `order_send`: una orden viva sin fila no sabe ni si ocurrió.
7. `order_send`, y `update_trade_result` con los intentos de llenado contra esa misma fila.

**Cierre sin lista blanca**: `close_position` no pasa por `symbols_allow` ni por el riesgo del día. Una lista de símbolos para ABRIR no puede impedir cerrar lo que ya está abierto, y cerrar es la operación que REDUCE el riesgo.


## Fase 7 – Tests, Configuración y Validación Final
- [ ] Completar `config/asset_sources_map.yaml` + `trading_hours.json` (mantener `strategy.yaml`)
- [x] Ampliar `tests/scenarios/` con mocks JSON B3/Forex/Cripto → 9 scenarios (6 forex + 3 B3/cripto)
- [x] Ejecutar `pytest tests/` completo → **0 FAILURES, VERDE** (1442 passed, 2 xfailed)
- [ ] E2E con datos mock (sin MT5) → OK — la parte API + core ya está cubierta por los tests de integración; el bucle de `watcher` sigue sin portar
- [x] Validar core agnóstico + sin dependencias MT5 en unit tests → `tests/unit/test_markets_scenarios.py` (68): los 3 scenarios de B3/cripto son clones estructurales del forex y deben dar el **mismo score** (82,4)
- [x] Actualizar `PROJECT_STATE.json` (fase/estado final) → `phase_7_scenarios_b3_cripto_done`
- [ ] Revisar CHECKLIST completo

### Hito "Scenarios B3/cripto" (cerrado)

**Qué se añadió**
- `tests/scenarios/b3_win_mini.json` — minicontrato de índice de la B3: 130.000 con **0 decimales**, punto de 5, tick de R$1,00/contrato, sesión `b3_mini_index` (12:00–21:25 UTC).
- `tests/scenarios/b3_wdo_mini.json` — minicontrato de dólar: 5,850 con **3 decimales** y `pip_override`, y **R:R 1,5 < 2,0** para que el gate lo rechace. Sesión `b3_mini_usd` (12:00–21:05 UTC).
- `tests/scenarios/crypto_btc_perp.json` — perpetuo BTCUSDT: 65.000 con **1 decimal**, macro por *fear & greed* (sin índice COT que reportar) y **nocional en USDT**. Sesión `crypto_24x7` (13:00–20:00 UTC).
- `tests/unit/test_markets_scenarios.py` — 68 tests en 8 bloques.

**Por qué el score tiene que ser *idéntico***
Los tres son clones estructurales de `killzone_edge_1min`: mismo FVG + order block + barrido del mínimo, mismo CVD punto por punto, misma forma de velas. Solo cambian escala, decimales, sesión y unidad de dinero. Por eso la aserción central es `score(BTCUSDT) == score(EURUSD)` y no un `score >= N`: dos scores parecidos y distintos del forex seguirían significando que hay un supuesto de forex metido dentro.

**Lo que sí cambia por mercado (y ahora está comprobado)**
| | WIN | WDO | BTCUSDT |
|---|---|---|---|
| Unidad | 100,0 puntos | 30,0 puntos | 4.000,0 puntos |
| Etiqueta | puntos | puntos (con override) | puntos |
| Tamaño | 1 contrato | 1 contrato | 0,25 BTC = 16.250 USDT |
| Riesgo al SL | R$100 | US$300 | US$100 |

**Bugs encontrados al hacerlo**
- `SymbolSpec.pip_quote` ignoraba el `pip_override`: con 3 decimales + override (WDO) la unidad era el punto de 0,001 pero la etiqueta decía **"pips"**. Un SL de 30 puntos se reportaba como "3,0 pips". Es el mismo error del oro, en el otro sentido.
- `core.setup_gate.entry_zone` solo devolvía `span` en el camino `ZONE_TOO_FAR`: en un setup válido `zone_span` era siempre `None`, así que quien recibía el plan veía el límite pero no cuánto margen le sobraba.
- Incoherencia en el primer borrador del WDO: `digits: 3` con `tick_size: 0,0005` (un símbolo con 3 decimales no cotiza en 4). Corregido a `tick_size: 0,001` / `tick_value: 10,00`.

**Lo que sigue abierto**
- Adaptadores B3/cripto: construyen pero fallan al usar. Los scenarios no los necesitan (trabajan con la forma normalizada); conectarlos es otro hito.
- `b3_fixed_income` sigue `confidence: unverified`: por eso no se añadió un scenario de NTN-F que fijara esa ventana.
- `execution_quality` sin portar (ver nota de la Fase 6).

### Hito "EURUSD leyendo del bróker de verdad" (cerrado)

**Por qué este hito**
La suite estaba verde (1212) y aun así la app no funcionaba con un solo par de divisas. Los tests
usaban dobles que no se parecían a la librería real en tres puntos decisivos. Este hito arranca
`create_app(Runtime.build())` contra MetaTrader 5 de verdad (MetaQuotes-Demo, EA `ILOF Exec`
publicando `ILOF_clock.json`) y llama cada ruta por HTTP.

**Seis bugs, todos de "funciona en el doble, imposible en el bróker"**
- [x] `/api/health` **500**: `session.is_open` es `@property` y el runtime la llamaba como método.
- [x] `/api/health` **decía `sin_terminal` con el bróker vivo**: la sesión es perezosa. Ahora
      `_terminal_alcanzable()` hace el trabajo mínimo de conexión en vez de suponer.
- [x] `broker_time.ea_clock` **siempre `null`**: leía `.get("estado")` de un payload sin esa clave.
      Un EA parado tres días pasaba por verificado.
- [x] `/api/symbols` **devolvía `[]`**: `symbols_get(group="\*")` es sintaxis de MQL5 y no coincide
      con nada en la API de Python. Ahora 23 símbolos.
- [x] **Toda lectura de velas era imposible**: `copy_rates_from_pos` devuelve un array numpy y se
      hacía `rows or []` / `if not rows` → `ValueError`. También en `normalize_ohlc()`. Añadido
      `filas_o_vacias()`, que compara contra `None` y no convierte.
- [x] `/api/risk/daily` con los **cinco topes en `null`**: leía `cfg["risk"]` y el store devuelve la
      forma plana de `build_flat`.

**Verificado por HTTP contra el bróker**
`health` 200 `con_terminal` · `symbols` 23 con EURUSD · `spec`/`price`/`candles` con datos reales ·
`setup-eval` SELL 77.9 con entry/sl/tp en pips · `risk/daily` con los topes puestos · `account` demo.

**Tests**: 14 nuevos, todos sobre la FORMA real (property vs método, sesión perezosa, array numpy,
grupo de `symbols_get`, config plana y anidada) → **1228 passed, 2 xfailed**.

**Lo que sigue pendiente para operar de verdad**
- [ ] Nada del watcher: scan y status están, y la auto-ejecución entra por `ExecutionService`
      cuando se escriba el bucle (D-063). Sigue pendiente `/api/orderflow/*` y `/api/stream/{symbol}`,
      que dependen del proveedor de ticks.

### Hito "Watcher scan/status sin auto-ejecución" (cerrado)

**Qué se añadió**
- `core/setup_lifecycle.py` — puro: máquina de estados del setup, TTL de dedup (2700 s por defecto)
  y firma de rechazo. Sin I/O para que la dedup se pueda probar sin base de datos.
- `api/services/watcher_service.py` — `WatcherService.estado()` y `.escanear()`. Sin puerto de
  ejecución: ni atributo, ni dependencia, ni `order_send`.
- `api/routes/watcher.py` — `GET /status` (lectura, sin token), `POST /scan` (escribe
  `setup_log` + `setup_state`, **pide token**), `POST /auto-execute` (**501**, pide token antes).
- `api/runtime.py` — `Runtime.watcher_service()`: una instancia por app, porque la dedup de
  descartes es memoria del proceso y una instancia por petición la volvería inútil sin que nada fallara.
- `tests/unit/test_setup_lifecycle.py` (39), `tests/unit/test_watcher_service.py` (37) y
  `TestWatcher` + `TestWatcherCompartido` (16) en `tests/integration/test_api_routes.py`.

**Lo que el watcher NO hace, y por qué se declara en vez de callarse**
- `auto_execute` sale **siempre** `false` en el estado; `auto_execute_conf` sí refleja el YAML y
  `auto_execute_disponible` dice que no hay código. Los tres juntos son la diferencia entre
  "está apagado" y "no existe".
- `POST /auto-execute` da `501` en los dos sentidos. Un `200 {"ok": false}` haría que
  `static/main.js` enseñara "ACTIVADO" tras encender un interruptor que no encendió nada.
- El fail-open de noticias sale en `avisos` del ciclo: un `IGNORED_NEWS` cuyo motivo fuera
  "feed caído" sería una afirmación falsa sobre el calendario.
- `audit_logged` (aprobados con fila) y `audit_fallidos` (filas perdidas) van separados: lo
  primero responde a "¿se registró lo que encontré?" y lo segundo a "¿se perdió algo de lo que vi?".
- Un escaneo sin `watcher.symbols` devuelve `motivo`: cero eventos se lee como mercado en calma.

**Verificado por HTTP contra el bróker**
Pendiente: el panel solo llega a `/status` sin token, igual que el resto del frontend heredado.



### Hito "Calibración Order Flow con cinta real de Databento" (cerrado)

**Fecha:** 2026-10-06 | **Suite:** 1477 passed, 2 xfailed (baseline 1465 + 12)

- [x] **Bloque 1 - Raíz de datos externa.** `core/paths.py`: `DATA_ROOT` (env
  `TRINITY_DATA_ROOT`, default `C:\Users\fmaur\Desktop\trinity_data`),
  `DATABENTO_RAW_DIR`, `RESEARCH_DATA_DIR`, `RESEARCH_RESULTS_DIR` fuera del repo y fuera
  de OneDrive; `data_root_guard_error(path)` con 4 reglas. `tests/unit/test_paths.py`
  (`TestRutasExternas`, `TestGuardianDeRaizDeDatos`).
- [x] **Bloque 2 - Umbral como fracción del techo.** `zscore_ceiling(ema_window)`,
  `OF_ZSCORE_THRESHOLD_FRAC = 0.91`, `OF_ZSCORE_THRESHOLD` derivado;
  `orderflow_engine.py` recalcula el umbral al cambiar la ventana y publica la fracción en
  `settings_locked()`. +6 tests (`TestZScoreCeiling`).
- [x] **Bloque 3 - Unidades honestas.** `orderflow_engine.py` "lotes" -> "contratos";
  comentarios de `test_simulator.py` actualizados a "0.91 del techo, ~4.5".
- [x] **Bloque 4 - Descarga real.** `.env` (gitignored), `pyarrow==25.0.1` y
  `databento==0.85.0` en requirements, `research/fetch_databento.py` con CLI
  (`--start/--end/--symbol/--jsonl/--limit/--max-cost/--dry-run`), guardia de fechas
  futuras, aviso de contrato poco líquido, sidecar `*.parquet.meta.json` con sha256.
  Descargas verificadas: 6EZ6 día completo (51.118 trades, 0,0640 USD) y 6E.c.0 de control
  (246 trades, 0,0003 USD).
- [x] **Bloque 5 - Fixture real + reality-check.** `tests/fixtures/orderflow_6e_real.jsonl`
  (21926 trades, 8 h NY, 1462 KiB) + `orderflow_6e_real.meta.json` (procedencia, sha256_lf,
  estadísticos), regenerables con `research/build_real_fixture.py`;
  `tests/unit/test_orderflow_real.py` (12) valida procedencia, estadísticos de cinta y
  umbrales sobre datos que no son suyos.
- [x] **Bloque 6 - Memoria.** `MEJORAS_DATABENTO.md` (histórico: roll, reconciliación de
  cifras, parámetros vigentes, rarezas de la API), `DECISIONS.md` D-064..D-067,
  `PROJECT_STATE.json`, este checklist y `ROADMAP.md`.

**Lo que se decidió y no se vuelve a preguntar** (D-064..D-067): raíz de datos como
variable con guardia que devuelve el motivo; umbral del Z-score como fracción del techo
matemático y no como número fijo; `6E.c.0` como identidad y `6EZ6` como contrato negociado;
calibración declarada contra un fixture real con sha256, recalibrando la prosa cuando las
cifras históricas no se reproducen.

**Pendiente fuera de alcance:** rotar la clave de Databento (se pegó en texto plano antes de
pasarla a `.env`), `data_sources.orderflow: true`, cablear `databento_cme.py` como feed en
vivo y las redes neuronales (postergadas; el motor determinista sigue siendo el baseline).

### Hito "Convalidación de la estrategia sobre cinta real 6E" (cerrado)

**Fecha:** 2026-10-06 | **Veredicto:** NO convalida (D-068..D-070)

**Qué se añadió**
- `research/build_6e_candles.py` — une la cinta real `6EU6` + `6EZ6` en velas M15 continuas con
  `delta` y PDH/PDL del día UTC anterior CON DATOS; roll `2026-09-11T00:00Z` por cruce de volumen
  (5.657 vs 30.639 ese día), cola/cabeza del roll descartadas; sidecar sha256. Salida:
  `trinity_data/databento/candles/6E_M15_2026-07-08_2026-10-06.parquet` (5.888 barras).
- `research/backtest_6e.py` — replay vela a vela con el core real del runtime
  (`smc_engine.analyze` → `risk_engine.setup_score` → `setup_gate.evaluate_gate`), no `sim.py`
  de REF. Split 60/30 + embargo 24 h (IS `[2026-07-08, 2026-09-06)`, embargo `[2026-09-06,
  2026-09-07)`, OOS `[2026-09-07, fin)`). Dos modelos de fill: `zone_ttl` (canónico, LIMIT + TTL
  40 min, fiel al runtime) y `next_open` (cota superior simplificada). Fricción 1 pip adversa,
  1 contrato 6E = 25 USD/pip, `max_trades_day` 3 por fill, MFE/MAE en raw. Resultado:
  `trinity_data/research/results/backtest_6e_20261006T231756Z.json`.

**Resultados** (0.0012 SL, min_score 59.5, pesos del YAML sin recalibrar —convalidación, no
optimización):
- `zone_ttl` IS: 43 trades, win 20.9 %, exp **−6.465 pips** (−6.950 USD); OOS: 23 trades, win
  39.1 %, exp +0.087 pips (+50 USD, PF 1.01).
- `next_open` IS: 105 trades, win 47.6 %, exp −3.714 pips; OOS: 51 trades, win 49.0 %, exp
  **−4.196 pips** (−5.350 USD).

Sin edge positivo en OOS bajo ningún modelo de fill. Sesgo de dirección: IS 25 BUY/18 SELL vs
OOS 3 BUY/20 SELL (29 % BUY) → tendencia del periodo, no del sistema. Cierres: 48 SL / 18 TP.
Rechazos: 5.404 detectados, 693 aprobados; top fuera de killzone 3.988 (~74 %), luego score < 59.5.

**Lo que se decidió y no se vuelve a preguntar** (D-068..D-070): convalidación empírica con
backtest sobre cinta real en vez de más calibración; roll por exclusión medido en la cinta;
failure modes gratis del `reason_tally` del gate; el veredicto se entrega con los dos fills, no
se filtra por métrica.

**Pendiente fuera de alcance:** revalidación futura si cambian el gate o la mecánica de fills
(el pipeline se regenera sin pagar de nuevo).

### Hito "Arquitectura multi-estrategia" (plan aprobado, D-071)

**Fecha:** 2026-10-06 | **Estado:** plan fijado y documentado; ejecución a partir de la F1.

**Qué se decidió** (incorporado del operador, 4 opciones aprobadas)
- F0 = cierre administrativo de 6E (commit + docs de `research/`), sin deploy a demo. **Rotar la
  API key de Databento: dejada como está por decisión del usuario.**
- ILOF entra como "módulo presente en investigación": el veredicto NO convalida no bloquea la
  infraestructura multi-perfil; se revalidará aislado en `research/`.
- Config por perfil: `strategy_<perfil>.yaml` + `strategy_map` (magic→perfil), reutilizando
  `STRATEGY_PATH`; `sl_distance_by_symbol` → `by_profile_by_symbol`.
- Datos del CTA: MT5 demo `copy_rates` (D1/H4, EURUSD/XAUUSD/US500/GBPUSD/AUDUSD); Databento
  solo para microestructura intradía.

**Principios no negociables:** convalidación antes que conexión (nada en vivo sin veredicto OOS);
`ExecutionService` única puerta; `regime()` como árbitro; despliegue nuevo empieza en alerta
(`auto_execute=false`, D-063); suite en verde por commit.

**Fases (detalle en `ROADMAP.md`):** F0 cierre 6E → F1 multi-perfil (strategy_map + DD por magic)
→ F2 convalidación CTA → F3 convalidación VWAP + régimen como interruptor → F4 despliegue en
alerta → F5 ejecución multi-estrategia (`core/exit_policy.py`, gates por perfil).

**Pendiencias abiertas:** rotar la API key de Databento (usuario decide); nº de estrategias que
superen la convalidación OOS (nada más correcto que el backtest para decidirlo).
