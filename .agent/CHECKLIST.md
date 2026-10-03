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

## Fase 5 – Agente IA (`agent/`)
- [ ] Crear `agent/prompt_templates.py`
- [ ] Crear `agent/laya_bridge.py` (bridge desde REF/agent.py)
- [ ] Migrar tools/router/SSE `REF/agent.py` → nueva estructura
- [ ] Mejorar persistencia tool results
- [ ] Tests agent/tools (mocks) → VERDES

## Fase 6 – API (`api/`) + Frontend (`static/`)
- [ ] Extraer rutas `REF/app.py` → `api/routes/*.py` (health, market, trading, agent, journal, db)
- [ ] Mover WebSocket → `api/websocket_manager.py`
- [ ] Reducir `api/app.py` (bootstrap/middlewares/mounts)
- [ ] Mover estáticos → `static/` + reestructurar css/js/components + actualizar StaticFiles
- [ ] Desacoplar lógica negocio → core/servicios (endpoints delgados)
- [ ] Import check + smoke test servidor (`uvicorn api.app:app` import OK)
- [ ] Integration tests API → VERDES

## Fase 7 – Tests, Configuración y Validación Final
- [ ] Completar `config/asset_sources_map.yaml` + `trading_hours.json` (mantener `strategy.yaml`)
- [ ] Ampliar `tests/scenarios/` con mocks JSON B3/Forex/Cripto
- [ ] Ejecutar `pytest tests/` completo → **0 FAILURES, VERDE**
- [ ] E2E con datos mock (sin MT5) → OK
- [ ] Validar core agnóstico + sin dependencias MT5 en unit tests
- [ ] Actualizar `PROJECT_STATE.json` (fase/estado final)
- [ ] Revisar CHECKLIST completo
