# ROADMAP.md - Fases, Hitos y Criterios de Aceptación

## Visión

Rehacer el proyecto `REF/` de forma **profesional y modular** (núcleo agnóstico + adaptadores por mercado), reutilizando lo valioso sin arrastrar deuda técnica. Objetivo: mismo rigor matemático/determinista, preparado para escalar a **B3, Forex/CME y Cripto**.

## ✅ Bloqueo transversal: RESUELTO (2026-10-01)

El código de referencia está en `C:\Users\fmaur\Desktop\Trading`. Las Fases 1–7 pueden ejecutarse.
Ver `DECISIONS.md` (D-005) y `PROJECT_STATE.json` (`reference_code`).

## Fase 0 – Preparación (Scaffolding base)
**Estado:** Completa | **Prioridad:** Crítica

**Hitos:**
- [x] Estructura de directorios creada con `__init__.py` en todos los paquetes
- [x] Archivos de memoria persistente (.agent/*) creados: PLAN.md, PROJECT_STATE.json, AGENT_GUIDELINES.md, ROADMAP.md, DECISIONS.md, CHECKLIST.md
- [x] `README.md` profesional creado
- [x] `.env.example`, `requirements.txt` creados
- [x] `config/asset_sources_map.yaml` (esqueleto B3/Forex/Cripto)
- [x] `config/trading_hours.json`

**Criterios de Aceptación:**
- [x] Árbol de carpetas coincide con arquitectura propuesta
- [x] `.agent/PROJECT_STATE.json` válido (JSON parseable)
- [x] Todos los archivos base creados y legibles
- [x] Configs parseables (`asset_sources_map.yaml` via PyYAML, `trading_hours.json` via json)
- [x] **Tests:** N/A (scaffolding). No requiere tests verdes.

## Fase 1 – Núcleo (`core/`) – Agnóstico al Mercado
**Estado:** Listo para iniciar | **Prioridad:** Crítica (Bloquea división app.py)

**Hitos:**
- [ ] `core/risk_engine.py` (port desde `REF/risk_engine.py`, ajustar imports)
- [ ] `core/smc_engine.py` (extraer lógica pura de `REF/pattern_engine.py`, aislar I/O)
- [ ] `core/orderflow_engine.py` (extraer `OrderFlowEngine` de `REF/app.py` + cálculos `REF/cvd_service.py`, feed-agnóstico)
- [ ] `core/simulator.py` (mover desde `REF/simulator.py`)
- [ ] `core/lot_calculator.py` (crear nuevo: normalizador lotes/minis/USDT)
- [ ] `core/paths.py`, `core/trade_history.py` (verificar integridad/migración)

**Criterios de Aceptación:**
- [ ] `core/*` **sin imports** de `adapters/`, `api/`, `agent/` (verificación)
- [ ] Lógica 100% agnóstica al mercado (revisado)
- [ ] Imports actualizados correctamente (sin rutas relativas rotas)
- [ ] **Tests unitarios core pasan en verde** (`pytest tests/unit -v`)
- [ ] Regresión validada vs comportamiento esperado (smoke tests)

## Fase 2 – Persistencia (`database/`)
**Estado:** Pendiente | **Prioridad:** Alta

**Hitos:**
- [ ] `database/store.py` (mover desde `REF/store.py`, ajustar imports `core.paths`)
- [ ] `database/models.py` (crear esquema/tablas separado)

**Criterios de Aceptación:**
- [ ] Rutas DB absolutas (`DB_PATH` único, via `core.paths`)
- [ ] Decisión registrada en `DECISIONS.md`: reutilizar `REF/trading.db` vs DB limpio (preservar histórico)
- [ ] Compatibilidad verificada con esquema existente (si reutiliza DB)
- [ ] **Tests:** tests relacionados con store/persistencia pasan (si existen)

## Fase 3 – Adaptadores (`adapters/`) + Abstracción
**Estado:** Pendiente | **Prioridad:** Alta (Habilita multimercado)

**Hitos:**
- [ ] `adapters/base_adapter.py` (interfaz abstracta: OHLC/Ticks/Depth + contrato datos normalizado)
- [ ] `adapters/forex/mt5_forex.py` (extraer MT5 de `REF/patterns_service.py`, `cvd_service.py`, `mt5_export.py`, encapsular lock único)
- [ ] `adapters/b3/mt5_b3.py`, `nelogica_profit.py`, `cedro_technologies.py` (esqueletos)
- [ ] `adapters/crypto/binance_ws.py`, `bybit_ccxt.py` (esqueletos)
- [ ] `adapters/forex/databento_cme.py` (opcional)

**Criterios de Aceptación:**
- [ ] `base_adapter.py` define interfaz clara (abstractmethods)
- [ ] Adaptadores implementan interfaz correctamente
- [ ] Concurrencia/locks encapsulados dentro del adaptador (no fugan a core)
- [ ] Core recibe **datos normalizados** (no objetos proveedor)
- [ ] **Tests:** unit tests adaptadores/base (mocks) pasan en verde

## Fase 4 – Macro/Ingestores (`macro_ingestor/`)
**Estado:** Completa | **Prioridad:** Media-Alta

**Hitos:**
- [x] `macro_ingestor/base_ingestor.py` (contrato, errores, caché con TTL, seam HTTP)
- [x] `macro_ingestor/forex/cot_service.py` (desde `REF/cot_service.py`)
- [x] `macro_ingestor/forex/dxy_service.py` (desde `REF/smr_service.py`)
- [x] `macro_ingestor/forex/calendar_news.py` (desde `REF/ff_calendar.py`)
- [x] `macro_ingestor/registry.py` (nombre del YAML → módulo real)
- [x] Evaluar `REF/decision_context.py` → ubicación por dominio (registrado en DECISIONS.md, D-028)
- [~] `macro_ingestor/b3/*` y `macro_ingestor/crypto/*` — **descartados**, no pendientes.
      Las 8 fuentes no tienen contrato en REF; un esqueleto con esquema inventado es peor
      que una ausencia declarada. Están en `registry.PENDIENTES` (D-026).

**Criterios de Aceptación:**
- [x] Reubicación sin romper imports existentes (donde aplique)
- [x] Interfaces uniformes entre mercados (`MacroReading` único para los tres servicios)
- [x] **Tests:** unit tests ingestors (mocks HTTP) pasan en verde — 89 en `test_macro_ingestor.py`, ninguno toca la red

## Fase 5 – Agente IA (`agent/`)
**Estado:** Completa | **Prioridad:** Media

**Hitos:**
- [x] `agent/ports.py` (los puertos que el agente necesita, y `AgentDeps`)
- [x] `agent/tools.py` (18 herramientas, esquemas y execution con puertos)
- [x] `agent/prompt_templates.py` (plantillas dinámicas por mercado/contexto)
- [x] `agent/laya_bridge.py` (router + bucle de herramientas + stream SSE)
- [x] Migrar tools/router/SSE de `REF/agent.py` → nueva estructura (1.308 líneas → 4 módulos)
- [x] Mejorar persistencia de `tool results`: digest con argumentos a la traza, payload
      completo al modelo (D-034)
- [x] Endpoints `agent` de REF `app.py` — **no portados aquí**: pertenecen a la Fase 6

**Criterios de Aceptación:**
- [x] Flujo SSE/tool-calling preservado: framing `data: {json}\n\n` y el tipo dentro del JSON
- [x] `done` es un invariante del stream, no una etiqueta por rama (D-033)
- [x] Prompts dinámicos por contexto/mercado funcionales
- [x] Persistencia de tool results mejorada y verificada con test
- [x] `import agent` no abre MT5 ni carga LiteLLM (seam `completion`, D-032)
- [x] **Tests:** 60 de herramientas, 23 de puertos, 71 de prompts, 56 del bridge —
      suite completa **766 passed, 2 xfailed**, ninguno toca la red

## Fase 6 – API (`api/`) + Frontend (`static/`)
**Estado:** En curso | **Prioridad:** Alta

**Hitos:**
- [x] Extraer rutas `REF/app.py` → `api/routes/` (health, market, trading, agent, journal, orderflow, macro, db, setup)
- [x] Mover WebSocket → `api/websocket_manager.py`
- [x] Reducir `api/app.py` a bootstrap/middlewares/mounts
- [x] Mover estáticos → `static/` (ficheros de REF copiados sin reescribir). Actualizar `StaticFiles` mount
- [x] Desacoplar lógica negocio (OrderFlow/trading/setup) → `core/`/servicios
- [x] Contrato con `static/main.js` escrito y verificado en las dos direcciones (46 servidas, 17 pendientes inventariadas)
- [ ] `api/routes/watcher.py` (status/scan/auto-execute) — el gate del setup ya está en `core/setup_gate.py`; falta el bucle y el envío de órdenes
- [ ] `/api/orderflow/feed`, `/api/orderflow/fixtures`, `/api/stream/{symbol}` — dependen del proveedor de ticks (6E)
- [ ] `execution_quality` (warnings de spread y distancia a la invalidez)

**Criterios de Aceptación:**
- [x] `api/app.py` reducido (liviano). Lógica negocio **fuera de endpoints** (endpoints delgados)
- [x] Rutas migradas con funcionalidad equivalente
- [x] Frontend sirve correctamente desde `/static` (FastAPI StaticFiles)
- [x] **Tests:** integration tests API en verde (1144 passed, 2 xfailed)
- [x] Smoke test: import de `api.app:app` OK

## Fase 7 – Tests, Configuración y Validación
**Estado:** En curso (hito de scenarios B3/cripto cerrado) | **Prioridad:** Crítica (Validación Final)

**Hitos:**
- [ ] Completar/ajustar `config/asset_sources_map.yaml` + `trading_hours.json`. Mantener `strategy.yaml` vigente — falta validar `b3_fixed_income` contra la grade oficial de la B3 (`confidence: unverified`)
- [x] Ampliar `tests/scenarios/` con mocks JSON por mercado (B3/Forex/Cripto) → 9 scenarios: 6 forex + `b3_win_mini`, `b3_wdo_mini`, `crypto_btc_perp`
- [x] Ejecutar `pytest tests/` tras cada subfase (regresión continua) → **1212 passed, 2 xfailed**
- [ ] Integración end-to-end (watcher + API + core) con datos mock — bloqueada: `api/routes/watcher.py` sin portar y necesita el puerto de ejecución
- [x] Verificación core agnóstico + sin dependencias MT5 en tests unitarios → `test_markets_scenarios.py` (68 tests): los scenarios de B3/cripto son clones estructurales del forex y exigen **score idéntico**
- [ ] Conectar los adaptadores de B3/cripto al doble de mercado: hoy construyen pero fallan al usar

**Criterios de Aceptación:**
- [x] **Suite completa `pytest tests/` pasa en VERDE** (0 failures)
- [x] Configs válidas (YAML/JSON parseables) — y ahora los scenarios se validan contra ellas, no contra el criterio del test
- [ ] E2E smoke tests con mocks OK (sin abrir MT5) — la parte API + core ya está cubierta por los tests de integración
- [ ] Proyecto estructurado listo para desarrollo/producción según arquitectura objetivo
- [x] `PROJECT_STATE.json` actualizado a fase estable/completada según alcance decidido → `phase_7_scenarios_b3_cripto_done`

## Hito transversal – Calibración Order Flow con cinta real de Databento
**Estado:** Completa (2026-10-06) | **Prioridad:** Alta (baseline de order flow)

**Hitos:**
- [x] Bloque 1 – `core/paths.py`: `DATA_ROOT` fuera del repo y fuera de OneDrive, con `data_root_guard_error()`
- [x] Bloque 2 – Umbral del Z-score como fracción del techo (`OF_ZSCORE_THRESHOLD_FRAC = 0.91`)
- [x] Bloque 3 – Unidades honestas ("lotes" → "contratos")
- [x] Bloque 4 – `research/fetch_databento.py`: descarga real de GLBX con sidecar sha256
- [x] Bloque 5 – Fixture real `tests/fixtures/orderflow_6e_real.jsonl` (21926 trades, 8 h NY) + 12 tests de reality-check
- [x] Bloque 6 – Memoria: `MEJORAS_DATABENTO.md`, D-064..D-067, checklist, roadmap, `PROJECT_STATE.json`

**Criterios de Aceptación:**
- [x] Los datos de cinta viven en `C:\Users\fmaur\Desktop\trinity_data`, nunca en el repo ni en OneDrive
- [x] Umbral derivado del techo matemático: cambiar `ema_window` no puede apagar el detector en silencio
- [x] Calibración declarada contra un fixture con sha256 trazable y regenerable (`research/build_real_fixture.py`)
- [x] **Tests:** 1477 passed, 2 xfailed (baseline 1465 + 12 de reality-check)

**Pendientes explícitos (fuera del alcance de este hito):**
- [ ] Rotar la clave de Databento (se empezó en texto plano antes de pasarla a `.env`)
- [ ] `data_sources.orderflow: true` y cablear `adapters/forex/databento_cme.py` como feed en vivo
- [ ] `GET|POST /api/orderflow/feed` y `GET /api/orderflow/fixtures` — el fixture ya existe, el endpoint no
- [ ] Redes neuronales: postergadas; el motor determinista (`smc_engine.py` + `orderflow_engine.py`) sigue siendo el baseline

## Hito transversal – Convalidación de la estrategia sobre cinta real 6E (backtest IS/OOS)
**Estado:** Completa (2026-10-06) | **Prioridad:** Alta (veredicto empírico del gate sobre 90 días de cinta real)

**Hitos:**
- [x] Descarga real 90 días (2,49 USD): `6EU6` + `6EZ6`, roll detectado en la cinta (2026-09-11, 5.657 vs 30.639) sin offsets artificiales
- [x] `research/build_6e_candles.py`: velas M15 continuas + `delta` + PDH/PDL por día anterior CON DATOS → parquet + sidecar sha256 (5.888 barras)
- [x] `research/backtest_6e.py`: replay con el core real (`smc_engine` → `setup_score` → `evaluate_gate`), split 60/30 + embargo 24 h, `zone_ttl` (canónico) y `next_open` (cota), MFE/MAE, `reason_tally`, JSON de resultados
- [x] Veredicto: **NO convalida** — OOS sin edge positivo bajo ningún fill (`zone_ttl` +0.087 pips n=23; `next_open` −4.196 pips n=51); sesgo de dirección cambia de signo IS→OOS (tendencia, no sistema)
- [x] Memoria: D-068..D-070, `MEJORAS_DATABENTO.md` §7, checklist, roadmap, `PROJECT_STATE.json`

**Criterios de Aceptación:**
- [x] Backtest con el mismo pipeline que el runtime (no `sim.py` de REF), pesos/min_score del YAML efectivos
- [x] Split IS/OOS sin recalibrar umbrales; única calibración `sl_distance_by_symbol["6E"]` en IS
- [x] Datos y resultados fuera del repo/OneDrive (`trinity_data`), regenerables sin pagar de nuevo
- [x] Failure modes del operador cubiertos por `reason_tally` de `evaluate_gate`

**Pendientes explícitos (fuera del alcance de este hito):**
- [ ] Rotar la clave de Databento (sigue pendiente desde el hito de calibración)
- [ ] Revalidación futura si cambian el gate o la mecánica de fills (pipeline reproducible)

## Hoja de ruta multi-estrategia (D-071) — CTA Swing + Mean Reversion + Copilot + ILOF
**Estado:** Plan aprobado (2026-10-06) | **Prioridad:** Media-alta (después de cerrar 6E)

Principios (no negociables): convalidación antes que conexión (nada en vivo sin veredicto OOS);
`ExecutionService` = única puerta de salida; `regime()` = árbitro de desactivación mutua; los
módulos nuevos nacen en `research/` y se despliegan primero en alerta (`auto_execute=false`);
suite en verde como criterio de aceptación.

- [ ] **F0 – Cierre limpio de 6E** (solo administrativo, sin deploy demo): commit + documentación
  de `research/`. Rotar `DATABENTO_API_KEY` queda PENDIENTE por decisión del usuario.
- [ ] **F1 – Infraestructura multi-perfil**: `strategy_map` (magic→perfil), `strategy_<perfil>.yaml`
  vía `STRATEGY_PATH`, `sl_distance_by_symbol` → `by_profile_by_symbol`; `daily_risk_state` expone
  DD/topes por magic y global. ILOF = "módulo presente en investigación". Sin cambios de ejecución.
- [ ] **F2 – Convalidación CTA en `research/`**: OHLCV D1 de MT5 demo (EURUSD, XAUUSD, US500,
  GBPUSD, AUDUSD, 2 años), simulador breakout próximo-open + trailing ATR + vol-targeting en
  `lot_calculator`; réplica IS/OOS + embargo + fricción. **Gate: solo pasa si OOS supera la fricción.**
- [ ] **F3 – Convalidación Mean Reversion (VWAP)**: mismo proceso; `regime()` como interruptor
  (expansión→ILOF+CTA, compresión→VWAP, noticias→solo Copilot).
- [ ] **F4 – Despliegue en alerta**: módulos validados corren simulados con su magic y perfil.
  Cero órdenes reales.
- [ ] **F5 – Ejecución multi-estrategia**: activación vía `ExecutionService` con plan y `exit_policy`
  por perfil (nuevo `core/exit_policy.py` para trailing D1); Copilot manual siempre abierto.

**Pendientes explícitos:** rotar la clave de Databento (heredado), decide el usuario.
