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
**Estado:** Pendiente | **Prioridad:** Media-Alta

**Hitos:**
- [ ] `macro_ingestor/forex/cot_service.py` (desde `REF/cot_service.py`)
- [ ] `macro_ingestor/forex/dxy_service.py` (desde `REF/smr_service.py`)
- [ ] `macro_ingestor/forex/calendar_news.py` (desde `REF/ff_calendar.py`)
- [ ] `macro_ingestor/b3/*` (5 esqueletos: bcb_focus, foreigner_flow, di_futures_yield, news_calendar, social_sentiment)
- [ ] `macro_ingestor/crypto/*` (4 esqueletos: funding_rate, onchain_whales, liquidation_heatmap, fear_greed)
- [ ] Evaluar `REF/decision_context.py` → ubicación por dominio (registrar en DECISIONS.md)

**Criterios de Aceptación:**
- [ ] Reubicación sin romper imports existentes (donde aplique)
- [ ] Interfaces uniformes entre mercados (mínimo común)
- [ ] **Tests:** unit tests ingestors (mocks HTTP) pasan en verde

## Fase 5 – Agente IA (`agent/`)
**Estado:** Pendiente | **Prioridad:** Media

**Hitos:**
- [ ] `agent/prompt_templates.py` (plantillas dinámicas por mercado/contexto)
- [ ] `agent/laya_bridge.py` (bridge reutilizando lógica `REF/agent.py`)
- [ ] Migrar tools/router/SSE de `REF/agent.py` → nueva estructura
- [ ] Mejorar persistencia de `tool results` (deuda menor)

**Criterios de Aceptación:**
- [ ] Flujo SSE/tool-calling preservado (sin romper endpoints agent existentes durante transición)
- [ ] Prompts dinámicos por contexto/mercado funcionales
- [ ] Persistencia tool results mejorada (verificada)
- [ ] **Tests:** tests agent/tools (mocks) pasan en verde

## Fase 6 – API (`api/`) + Frontend (`static/`)
**Estado:** Pendiente | **Prioridad:** Alta

**Hitos:**
- [ ] Extraer rutas `REF/app.py` → `api/routes/` (health, market, trading, agent, journal, db)
- [ ] Mover WebSocket → `api/websocket_manager.py`
- [ ] Reducir `api/app.py` a bootstrap/middlewares/mounts
- [ ] Mover estáticos → `static/` (reestructurar `css/`, `js/`, `js/components/`). Actualizar `StaticFiles` mount
- [ ] Desacoplar lógica negocio (OrderFlow/trading) → `core/`/servicios

**Criterios de Aceptación:**
- [ ] `api/app.py` reducido (liviano). Lógica negocio **fuera de endpoints** (endpoints delgados)
- [ ] Rutas migradas con funcionalidad equivalente
- [ ] Frontend sirve correctamente desde `/static` (FastAPI StaticFiles)
- [ ] **Tests:** integration tests API básicos pasan en verde
- [ ] Smoke test: servidor arranca sin errores (`uvicorn api.app:app --check` o import)

## Fase 7 – Tests, Configuración y Validación
**Estado:** Pendiente | **Prioridad:** Crítica (Validación Final)

**Hitos:**
- [ ] Completar/ajustar `config/asset_sources_map.yaml` + `trading_hours.json`. Mantener `strategy.yaml` vigente
- [ ] Ampliar `tests/scenarios/` con mocks JSON por mercado (B3/Forex/Cripto)
- [ ] Ejecutar `pytest tests/` tras cada subfase (regresión continua)
- [ ] Integración end-to-end (watcher + API + core) con datos mock
- [ ] Verificación core agnóstico + sin dependencias MT5 en tests unitarios

**Criterios de Aceptación:**
- [ ] **Suite completa `pytest tests/` pasa en VERDE** (0 failures)
- [ ] Configs válidas (YAML/JSON parseables)
- [ ] E2E smoke tests con mocks OK (sin abrir MT5)
- [ ] Proyecto estructurado listo para desarrollo/producción según arquitectura objetivo
- [ ] `PROJECT_STATE.json` actualizado a fase estable/completada según alcance decidido
