# PLAN.md - Plan de Reconstrucción Profesional (MultiMarket Quant Engine)

> Documento maestro: contiene el análisis comparativo, mapeo, criterios y plan por fases para rehacer el proyecto de Trading de forma profesional, reutilizando lo valioso sin arrastrar deuda técnica.

> **Código de referencia (solo lectura):** `C:/Users/fmaur/Desktop/Trading`
> En el resto de los documentos `REF/` significa esa ruta. `REF/` está **fuera** de este repo y no se importa ni se modifica.

## 1) Mapeo entre proyecto viejo (REF/) y objetivo propuesto

El proyecto viejo es **monolítico (MT5-centric)**: mucha lógica vive en `app.py` (5.185 líneas). El objetivo propone **núcleo agnóstico + adaptadores por mercado**.

| Concepto (objetivo `contexto.md`) | Estado en `REF/` | Observación |
|---|---|---|
| `core/` (motores reutilizables, agnósticos) | Solo `core/paths.py`, `core/trade_history.py` | Los motores (`risk`, SMC, orderflow) están **en raíz**, no en `core/`. Falta separación núcleo vs I/O. |
| `core/risk_engine.py` | `risk_engine.py` (raíz, 628 líneas) | Existe y es **muy sólido/determinista**. Excelente candidato para migrar a `core/risk_engine.py` (ajustando imports). |
| `core/smc_engine.py` | No existe como módulo puro. Sí `pattern_engine.py` (algoritmos) + `patterns_service.py` (fetch MT5 + wrapper) | `pattern_engine.py` es **puro (OHLC→patrones)**. Ideal para convertirse en `core/smc_engine.py`. |
| `core/orderflow_engine.py` | Parcial. `OrderFlowEngine` en `app.py` + `cvd_service.py` + `orderflow_config.py` | Acoplado a MT5/Databento/app.py. Requiere **extracción y desacoplamiento** (feed-agnóstico). |
| `core/scoring.py` | No existe separado. Scoring en `risk_engine.py` + validación en `strategy.py`/`watcher.py` | Mejor **mantener scoring dentro de `risk_engine.py`** (ya determinista). Evita duplicación. |
| `core/simulator.py` | `simulator.py` (raíz) | Reutilizable directo en `core/simulator.py`. |
| `core/lot_calculator.py` | No existe | Necesario normalizador (Forex/Lotes vs Minis B3 vs USDT Cripto). |
| `adapters/` | Mezclado: `mt5_export.py`, `mt5_mcp_local.py`, `mock_feed.py`, Databento vía `app.py`, `cvd_service.py`, `patterns_service.py` | Requiere **`base_adapter.py` + subcarpetas `b3/`, `forex/`, `crypto/`** (reestructura). |
| `macro_ingestor/` | Parcial: `cot_service.py`, `ff_calendar.py`, `smr_service.py`, `decision_context.py` | Reubicar por mercado. Crear `b3/` y `crypto/` con esqueletos. |
| `core/` extra | **No contemplado en el mapeo original:** `REF/chartism_engine.py` (542 líneas) | Motor de patrones adicional. Decidir en Fase 1 si se porta a `core/` o queda fuera del alcance. |
| Servicios de apoyo | `strategy.py` (779), `symbol_specs.py` (477), `tclock.py` (418), `watcher.py` (883), `execution_quality.py`, `trade_outcome.py`, `market_view.py`, `mock_feed.py` (519) | Auditoría inicial: falta ubicación destino en la arquitectura objetivo. Definir antes de Fase 6. |
| `config/` + `asset_sources_map.yaml` | `strategy.yaml` completo, `orderflow_config.py`, `symbol_specs.py` | Mantener `strategy.yaml` (reglas). Añadir `config/asset_sources_map.yaml` (orquestador por activo/mercado). |
| `api/` (FastAPI) | `app.py` (5.185 líneas) monolítico | Dividir en `api/app.py` (entrypoint), `api/routes/`, `api/websocket_manager.py`. Sacar lógica de negocio a `core/`. |
| `static/` | Raíz: `index.html`, `main.js`, `style.css`, `echarts*.js`, `lightweight-charts*.js`, `tools.js` | Mover a `static/`, `static/css/`, `static/js/components/`. |
| `database/` | `store.py` (raíz) + `trading.db*` | Migrar a `database/store.py` + `database/models.py`. Usar `core.paths.DB_PATH`. |
| `agent/` | `agent.py` (raíz) | Dividir en `agent/laya_bridge.py` + `agent/prompt_templates.py`. Reutilizar tools/router/SSE. |
| `.agent/` | No existe | Crear `PROJECT_STATE.json`, `AGENT_GUIDELINES.md`, `PLAN.md`, `ROADMAP.md`, `DECISIONS.md`, `CHECKLIST.md`. |
| `tests/` | 41 archivos de test + `pytest.ini` + `conftest.py` + `tests/fixtures` + `tests/scenarios` | **Suite de alto valor.** Preservar, portar y ampliar con `tests/scenarios/*.json` por mercado. |

## 2) Qué conservar/reutilizar vs reescribir

### Reutilizar (alto valor, bajo riesgo)
- `risk_engine.py` → `core/risk_engine.py`
- `pattern_engine.py` → base `core/smc_engine.py`
- `simulator.py` → `core/simulator.py`
- `strategy.yaml` (complementar con `asset_sources_map.yaml`)
- `store.py` + esquema DB → `database/` (+ `models.py`)
- `core/paths.py`, `core/trade_history.py`
- `cot_service.py`, `smr_service.py`, `ff_calendar.py` → `macro_ingestor/forex/`
- `symbol_specs.py`, `tclock.py`
- `tests/` + `tests/fixtures` + `tests/scenarios` + `conftest.py` (suite completa de 41 tests)

### Refactorizar (reutilizar lógica interna)
- `patterns_service.py`: I/O → `adapters/forex/mt5_forex.py`, lógica SMC → `core/smc_engine.py`
- `cvd_service.py` + `orderflow_config.py`: cálculos puros → `core/orderflow_engine.py`, I/O → `adapters/`
- `OrderFlowEngine` (app.py): extraer a `core/orderflow_engine.py` (feed-agnóstico, inyección de adaptador)
- `watcher.py`: desacoplar de app monolítica (servicio/worker)
- `agent.py`: dividir en `agent/laya_bridge.py` + `agent/prompt_templates.py`, mejorar persistencia tool results
- `decision_context.py`, `execution_quality.py`, `trade_outcome.py`, `market_view.py`: reubicar por dominio

### Reescribir/reestructurar (acoplamiento)
- `app.py` (4.5K): dividir por capas (routes + WS + servicios). Extracción **gradual** (no big bang).
- Adaptadores: `adapters/base_adapter.py` + `forex/mt5_forex.py`, `b3/*`, `crypto/*`, `forex/databento_cme.py` (opcional)
- Frontend: mover a `static/` (reestructurar css/js/components)
- Estructura por mercados: crear carpetas con `__init__.py`

## 3) Brechas clave vs arquitectura objetivo

| Brecha | Impacto | Solución |
|---|---|---|
| Core engines separados en `core/` | Medio-Alto | Crear `core/smc_engine.py`, `core/orderflow_engine.py`, `core/lot_calculator.py`. Migrar lógica pura. |
| Abstracción adaptadores (`base_adapter.py`) | Alto | Interfaz mínima (OHLC/Ticks/Depth). Inyección de adaptador (DI) para mantener core puro. |
| Orquestación por activo (`asset_sources_map.yaml`) | Alto | Añadir `config/asset_sources_map.yaml` (complementa `strategy.yaml`). |
| Separación API (routes + WS) | Alto | Extraer rutas → `api/routes/*.py`, WS → `api/websocket_manager.py`, `api/app.py` liviano. |
| Persistencia `database/` + `models.py` | Medio | `store.py` → `database/store.py`, crear `database/models.py`. Usar `core.paths.DB_PATH`. |
| Control agente autónomo (`.agent/`) | Bajo-Medio | Crear `PROJECT_STATE.json`, `AGENT_GUIDELINES.md`, `PLAN.md`, `ROADMAP.md`, `DECISIONS.md`, `CHECKLIST.md`. |
| Lot calculator + normalización multimercado | Alto | Crear `core/lot_calculator.py` (separado de risk). |
| Prompts dinámicos por mercado (agent) | Medio | Crear `agent/prompt_templates.py` + ajustar `agent/laya_bridge.py`. |

## 4) Plan de reconstrucción profesional (por fases) – Solo lectura

**Recomendación:** *Greenfield modular + port selectivo (no big bang)*. Mantener `REF/` como **referencia de lectura** (intacto). Crear estructura limpia siguiendo `contexto.md`, portar lógica pura paso a paso, validando con tests.

### Fase 0 – Preparación (scaffolding base)
- [ ] Crear estructura directorios (`config, core, adapters/*, macro_ingestor/*, agent, api/routes, database, static/*, tests/*, .agent`) + `__init__.py`
- [ ] Crear `.agent/PROJECT_STATE.json` (fases, completados/en progreso/pendientes)
- [ ] Crear `.agent/AGENT_GUIDELINES.md` (reglas: core agnóstico, tests verdes antes de marcar completado)
- [ ] Crear `.agent/PLAN.md` (este documento)
- [ ] Crear `.agent/ROADMAP.md` (fases + hitos + criterios de aceptación)
- [ ] Crear `.agent/DECISIONS.md` (decisiones + respuestas a clarificaciones)
- [ ] Crear `.agent/CHECKLIST.md` (checklist ejecutable por fases)
- [ ] Crear `README.md` profesional (basado en plantilla `contexto.md`, ajustado a estado real)
- [ ] Crear `.env.example`, `requirements.txt` (revisar deps: fastapi, uvicorn, MetaTrader5, databento, litellm, pydantic, pyyaml, etc.)
- [ ] Crear `config/asset_sources_map.yaml` (esqueleto B3/Forex/Cripto)
- [ ] Crear `config/trading_hours.json`

### Fase 1 – Núcleo (`core/`) – agnóstico (prioritario)
- [ ] Mover `REF/risk_engine.py` → `core/risk_engine.py` (ajustar imports). Validar lógica pura.
- [ ] Crear `core/smc_engine.py`: extraer lógica pura de `REF/pattern_engine.py` (FVG/OB/Sweeps). Aislar I/O.
- [ ] Crear `core/orderflow_engine.py`: extraer `OrderFlowEngine` de `REF/app.py` + cálculos de `REF/cvd_service.py`. Feed-agnóstico (inyectar adaptador/datos normalizados).
- [ ] Mover `REF/simulator.py` → `core/simulator.py` (ajustar imports).
- [ ] Crear `core/lot_calculator.py` (normalizador lotes/minis/USDT). Separar de risk.
- [ ] Mover `REF/core/paths.py` → `core/paths.py` (verificar integridad) y `REF/core/trade_history.py` → `core/trade_history.py`.
- [ ] Tests unitarios core: ejecutar `tests/unit` relacionados tras migración para validar regresión.

### Fase 2 – Persistencia (`database/`)
- [ ] Mover `REF/store.py` → `database/store.py`, crear `database/models.py` (esquema/tablas). Ajustar imports (`core.paths`).
- [ ] Verificar rutas DB absolutas (`DB_PATH` único). Evaluar reutilizar `REF/trading.db` vs DB limpio (preservando histórico si se decide reutilizar).

### Fase 3 – Adaptadores (`adapters/`) + abstracción
- [ ] Crear `adapters/base_adapter.py` (interfaz abstracta: métodos comunes + contrato datos normalizado OHLC/Ticks/Depth).
- [ ] `adapters/forex/mt5_forex.py`: extraer lógica MT5 de `REF/patterns_service.py`, `REF/cvd_service.py`, `REF/mt5_export.py` (encapsular inicialización MT5 con lock único).
- [ ] `adapters/b3/`: esqueletos `mt5_b3.py`, `nelogica_profit.py`, `cedro_technologies.py`.
- [ ] `adapters/crypto/`: esqueletos `binance_ws.py`, `bybit_ccxt.py`.
- [ ] `adapters/forex/databento_cme.py` (opcional): aislar Databento fuera de app.

### Fase 4 – Macro/Ingestores (`macro_ingestor/`)
- [ ] Reorganizar: `REF/cot_service.py` → `macro_ingestor/forex/cot_service.py`, `REF/smr_service.py` → `macro_ingestor/forex/dxy_service.py` (o `smr_dxy_service.py`), `REF/ff_calendar.py` → `macro_ingestor/forex/calendar_news.py` (o mantener nombre claro). Evaluar `REF/decision_context.py` por dominio.
- [ ] Esqueletos `macro_ingestor/b3/`: `bcb_focus_service.py`, `foreigner_flow.py`, `di_futures_yield.py`, `news_calendar.py`, `social_sentiment.py`.
- [ ] Esqueletos `macro_ingestor/crypto/`: `funding_rate_service.py`, `onchain_whales.py`, `liquidation_heatmap.py`, `fear_greed.py`.

### Fase 5 – Agente IA (`agent/`)
- [ ] Crear `agent/prompt_templates.py` (plantillas dinámicas por mercado/contexto).
- [ ] Crear `agent/laya_bridge.py` (bridge reutilizando lógica de `REF/agent.py`).
- [ ] Migrar tools/router/SSE de `REF/agent.py` a nueva estructura. Mejorar persistencia de tool results (deuda menor). Mantener compatibilidad durante transición.

### Fase 6 – API (`api/`) + Frontend (`static/`)
- [ ] Extraer rutas de `REF/app.py` → `api/routes/` (health, market, trading, agent, journal, db, etc.). Mover WebSocket → `api/websocket_manager.py`. Reducir `api/app.py` a bootstrap/middlewares/mounts.
- [ ] Mover estáticos: `REF/index.html, style.css, main.js, tools.js, echarts*.js, lightweight-charts*.js` → `static/` (reestructurar `css/`, `js/`, `js/components/`). Actualizar `StaticFiles` mount.
- [ ] Desacoplar lógica de negocio (OrderFlow/trading) hacia `core/`/servicios (Fase 1). Endpoints delgados.

### Fase 7 – Tests, Configuración y Validación
- [ ] Crear/ajustar `config/asset_sources_map.yaml` + `trading_hours.json`. Mantener `strategy.yaml` vigente.
- [ ] Ampliar `tests/scenarios/` con mocks JSON por mercado (B3/Forex/Cripto) según `contexto.md`.
- [ ] Ejecutar `pytest tests/` tras cada fase (regresión continua). **No marcar módulo completado sin tests verdes** (AGENT_GUIDELINES).
- [ ] Verificar integración end-to-end (watcher + API + core) con datos mock (evitar dependencias MT5 en tests).

## 5) Recomendación crítica
- **No parchear monolito**: construir estructura modular limpia (Fase 0) y **portar lógica pura selectivamente** (core primero). I/O va a adaptadores.
- **Priorizar Fase 1 (`core/`)** antes de dividir `app.py`. Reduce riesgo de romper comportamiento.
- **Conservar `REF/` intacto como referencia** durante toda la reconstrucción (solo lectura para portado).
- **Objetivo**: mismo rigor matemático/determinista, separación profesional de responsabilidades, preparado para escalar a B3/Cripto sin reescribir núcleo.

## 6) Preguntas de clarificación (para acotar alcance inmediato)
1. **Alcance inicial:** ¿Scaffolding + `README.md` + `.agent/*` únicamente (para subir a GitHub) o **empezar a portar `core/` (Fase 1)**?
2. **Prioridad mercado:** ¿**B3 primero** (siguiendo `contexto.md`) o mantener enfoque **Forex actual** y añadir B3 después?
3. **Proveedor B3 inicial:** ¿**MT5 B3** (rápido/gratis, reutiliza bridge) o **Nelogica/Profit** directamente (producción)?
4. **División `app.py`:** ¿**Big-bang** o **extracción incremental por dominios** (recomendado, menor riesgo)?
5. **Base de datos:** ¿**Reutilizar `REF/trading.db`** (preservar histórico) o **empezar limpio** en nueva estructura?
