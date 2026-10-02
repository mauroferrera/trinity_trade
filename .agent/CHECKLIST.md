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

## Fase 2 – Persistencia (`database/`)
- [ ] Mover `REF/store.py` → `database/store.py` (ajustar imports `core.paths`)
- [ ] Crear `database/models.py` (esquema/tablas)
- [ ] Decisión DB: reutilizar `REF/trading.db` vs limpio → registrar en `DECISIONS.md`
- [ ] Verificar `DB_PATH` único (absoluto)
- [ ] Tests persistencia (si existen) → VERDES

## Fase 3 – Adaptadores (`adapters/`)
- [ ] Crear `adapters/base_adapter.py` (interfaz abstracta OHLC/Ticks/Depth + datos normalizados)
- [ ] Crear `adapters/forex/mt5_forex.py` (extraer MT5: patterns_service, cvd_service, mt5_export, lock único)
- [ ] Crear esqueletos `adapters/b3/mt5_b3.py`, `nelogica_profit.py`, `cedro_technologies.py`
- [ ] Crear esqueletos `adapters/crypto/binance_ws.py`, `bybit_ccxt.py`
- [ ] Crear `adapters/forex/databento_cme.py` (opcional)
- [ ] Tests adaptadores (mocks) → VERDES

## Fase 4 – Macro/Ingestores (`macro_ingestor/`)
- [ ] `macro_ingestor/forex/cot_service.py` (desde Trading)
- [ ] `macro_ingestor/forex/dxy_service.py` (desde REF/smr_service)
- [ ] `macro_ingestor/forex/calendar_news.py` (desde REF/ff_calendar)
- [ ] Esqueletos `macro_ingestor/b3/*` (5 archivos)
- [ ] Esqueletos `macro_ingestor/crypto/*` (4 archivos)
- [ ] Evaluar `REF/decision_context.py` → registrar decisión en `DECISIONS.md`
- [ ] Tests ingestors (mocks HTTP) → VERDES

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
