# MultiMarket Quant Engine

Motor de trading cuantitativo multimercado: **B3 (Brasil)**, **Forex/CME** y **Cripto**.

Núcleo determinista agnóstico al mercado + adaptadores por proveedor + capa de
explicabilidad con LLM. Un único monorepo; la activación de fuentes es dinámica
vía `config/asset_sources_map.yaml`.

> **Estado:** Fases 0–4 completadas. `core/`, `database/`, `adapters/` y
> `macro_ingestor/` están implementados y verificados por tests. Siguiente: Fase 5
> (`agent/`). Ver [Estado real](#estado-real) y `.agent/PROJECT_STATE.json`.

---

> **Código de referencia (solo lectura):** `C:/Users/fmaur/Desktop/Trading`
> En el resto de los documentos `REF/` significa esa ruta. `REF/` está **fuera** de este repo y no se importa ni se modifica.

## Arquitectura

```
                      ┌───────────────────────────────┐
                      │           api/                │  FastAPI: routers delgados
                      │  app.py = bootstrap + mounts  │  (sin lógica de negocio)
                      └───────────────┬───────────────┘
                                      │
             ┌────────────────────────┼────────────────────────┐
             │                        │                        │
   ┌─────────▼─────────┐   ┌──────────▼──────────┐   ┌─────────▼─────────┐
   │  macro_ingestor/  │   │       core/         │   │     agent/        │
   │  COT, DXY, Focus, │   │  risk · smc ·        │   │ explicabilidad e  │
   │  funding, whales  │   │  orderflow · sim ·   │   │ interpretación     │
   │  (solo I/O)       │   │  lot_calculator      │   │ (nunca calcula    │
   └─────────┬─────────┘   │  (lógica pura)       │   │  entradas/SL/TP)  │
             │             └──────────▲──────────┘   └───────────────────┘
   ┌─────────▼────────────────────────┴────────────────────────┐
   │                        adapters/                          │  MT5 B3 · Nelogica ·
   │  base_adapter.py (interfaz OHLC/Ticks/Depth)             │  Cedro · MT5 Forex ·
   │  b3/ · forex/ · crypto/  (locks y normalización aquí)   │  Databento · Binance
   └──────────────────────────────────────────────────────────┘
```

**Regla de dependencia:** `adapters → normalizan → core consume`. El flujo es
unidireccional y `core/` **jamás** importa `adapters/`, `api/` ni `agent/`.

---

## Estructura

```
.
├── core/                  # Motores deterministas, agnósticos al mercado
│   ├── risk_engine.py     #   Riesgo, sizing, scoring (port desde REF/)
│   ├── smc_engine.py      #   FVG / Order Blocks / Liquidity Sweeps
│   ├── orderflow_engine.py#   Order flow + CVD, feed-agnóstico
│   ├── simulator.py       #   Backtest / simulación
│   ├── lot_calculator.py  #   Normaliza lotes (Forex) / minicontratos (B3) / USDT
│   ├── paths.py           #   Fuente única de verdad de rutas y DB_PATH
│   └── trade_history.py
├── adapters/              # Datos: implementan BaseAdapter
│   ├── base_adapter.py
│   ├── b3/                #   mt5_b3 · nelogica_profit · cedro_technologies
│   ├── forex/             #   mt5_forex · databento_cme
│   └── crypto/            #   binance_ws · bybit_ccxt
├── macro_ingestor/        # Datos macro / soft data (solo I/O)
│   ├── forex/             #   cot_service · dxy_service · calendar_news
│   ├── b3/                #   bcb_focus · foreigner_flow · di_futures_yield · …
│   └── crypto/            #   funding_rate · onchain_whales · liquidation_heatmap · …
├── agent/                 # LLM: prompt_templates · laya_bridge · tools
├── api/                   # FastAPI
│   ├── app.py             #   Bootstrap, middlewares, mounts
│   ├── routes/            #   health · market · trading · agent · journal · db
│   └── websocket_manager.py
├── database/              # store.py · models.py (SQLite, DB_PATH absoluto)
├── static/                # Frontend (css · js · js/components)
├── config/                # asset_sources_map.yaml · trading_hours.json · strategy.yaml
├── tests/                 # unit · integration · scenarios (mocks JSON)
└── .agent/                # PLAN · ROADMAP · CHECKLIST · DECISIONS · PROJECT_STATE
```

---

## Principios (no negociables)

| # | Principio |
|---|---|
| 1 | `core/` es **100% agnóstico** al activo/mercado: sin MT5, Databento, Binance ni feeds. |
| 2 | Núcleo = **lógica pura**. I/O, websockets, requests y APIs viven en `adapters/`, `macro_ingestor/`, `api/`. |
| 3 | **Inversión de dependencias**: los adaptadores implementan el Protocol `base_adapter.MarketDataAdapter` y entregan datos **normalizados** (`{"time","open","high","low","close","volume"}` y `{"ts","price","size","side"}`, con `side` en `A`/`B` = lado agresor). |
| 4 | **Lógica de negocio fuera de la API**: endpoints delgados; `api/app.py` solo hace bootstrap. |
| 5 | **Monorepo modular**: un repo para B3 + Forex/CME + Cripto. |
| 6 | **Tests verdes antes de marcar completado** — ningún módulo pasa a `completed` sin `pytest` en verde. |
| 7 | **Port selectivo, no big-bang**: `REF/` se preserva intacto como referencia de solo lectura. |
| 8 | **Rutas únicas y absolutas**: todo path resuelve desde `core/paths.py`, nunca relativo al CWD. |
| 9 | `agent/` es **explicabilidad**, nunca decisión en tiempo de ejecución. |

Detalle completo en [`.agent/AGENT_GUIDELINES.md`](.agent/AGENT_GUIDELINES.md).

---

## Mercados

| Mercado | Activos | Adaptador de datos | Size | Soft data |
|---|---|---|---|---|
| **B3 Futuros** | `WIN`, `WDO`, `IND`, `MBR`, `DI1` | MT5 B3 | minicontratos | Focus (BCB), fluxo estrangeiro, curva DI1, Copom |
| **B3 Tesouro** | `NTN-F` | MT5 B3 | bonds | Focus, curva DI1 |
| **Forex / CME** | `EURUSD`, cruces principales | MT5 Forex / Databento | lotes estándar | CFTC COT, DXY, calendario NFP/FOMC/ECB |
| **Cripto** | `BTCUSDT`, `ETHUSDT` perps | Binance WS / Bybit (CCXT) | notional USDT | funding rate, on-chain whales, liquidaciones, Fear & Greed |

La selección por símbolo vive en [`config/asset_sources_map.yaml`](config/asset_sources_map.yaml).
Complementa a `strategy.yaml` (no lo reemplaza): el mapa orquesta **fuentes**, la
estrategia define **reglas**.

### Sesiones y liquidez

[`config/trading_hours.json`](config/trading_hours.json) define ventanas de
negociación, solapamientos y perfiles de liquidez por mercado:

- **B3** — `America/Sao_Paulo` (sin DST desde 2019, offset fijo `-03:00`). WIN `09:00–18:25`, WDO `09:00–18:30` (+ after-market y pregão noturno **deshabilitado por defecto**), DI1 `09:00–18:00`, Tesouro Direto `09:30–18:00`.
- **Forex** — sesiones en UTC: Sidney / Tokio / Londres / Nueva York, con el solapamiento **Londres–NY `12:00–16:00 UTC`** como ventana de mayor liquidez y la franja `21:00–22:00 UTC` como zona muerta.
- **Cripto** — 24/7 con ventanas Asia/Europa/US, eventos de funding (`00:00`, `08:00`, `16:00 UTC`) y reset diario.

Cada ventana lleva un nivel de `confidence` (`high` / `medium` / `unverified`).
**No ejecutes en producción contra una ventana `unverified`.**

---

## Quickstart

```bash
# 1. Dependencias
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# 2. Configuración
copy .env.example .env      # Windows
# cp .env.example .env      # Linux/macOS

# 3. Validar configuración
python -c "import json; json.load(open('config/trading_hours.json'))"

# 4. Tests
pytest tests/ -v
```

La API todavía no expone entrypoint (Fase 6 del roadmap). Cuando exista:

```bash
uvicorn api.app:app --reload --port 8000
```

MetaTrader 5 es **solo Windows** y requiere terminal instalado y autenticado.
Ninguna suite unitaria debe depender de MT5: los adaptadores se prueban con mocks.

---

## Estado real

### Hecho

- Estructura de directorios + paquetes Python (`__init__.py`) completa.
- `.agent/` con memoria persistente del proyecto: `PLAN.md`, `ROADMAP.md`, `CHECKLIST.md`, `DECISIONS.md`, `PROJECT_STATE.json`, `AGENT_GUIDELINES.md`.
- `.env.example` y `requirements.txt` (FastAPI, MT5, Databento, CCXT, LiteLLM, pytest…).
- `config/asset_sources_map.yaml` — esqueleto B3 / Forex / Cripto.
- `config/trading_hours.json` — sesiones, solapamientos y liquidez con control de confianza.
- **Fase 1 · `core/`** — `risk_engine`, `smc_engine`, `orderflow_engine` (CVD puro),
  `market_view`, `simulator`, `lot_calculator`, `clock`, `paths`, `trade_history`.
  Agnóstico al mercado, verificado por un guardián AST (`tests/unit/test_core_purity.py`).
- **Fase 2 · `database/`** — `models.py` (fuente de verdad del esquema), `schema.sql`
  generado, `store.py` portada de REF con seam de config inyectable. 14 tablas
  idénticas a las de producción, comparadas contra un snapshot congelado.
- **Fase 3 · `adapters/`** — `base_adapter.py` (contrato y forma normalizada),
  `forex/mt5_forex.py` (**una sola sesión MT5 por proceso**, con un único hilo y
  cierre que drena lo que está en vuelo) y esqueletos para B3 (3), cripto (2) y
  Databento. La suite no necesita terminal: todo se prueba con un doble.
- **Fase 4 · `macro_ingestor/`** — `base_ingestor.py` (contrato `MacroReading`,
  `IngestorError` independiente de `AdapterError`, caché con TTL y reloj inyectable),
  los tres servicios Forex (`cot_service` CFTC, `dxy_service` Yahoo + SMR alineado por
  tiempo, `calendar_news` Forex Factory con gate *fail-open*) y `registry.py`, que
  traduce los nombres de `asset_sources_map.yaml` a módulos reales y **reporta en vez
  de filtrar** lo que no resuelve. Ningún test toca la red.

Suite completa: **556 passed, 2 xfailed** (`pytest tests -q`). Los dos `xfail` son
decisiones de calibración heredadas de la Fase 1, no código roto.

### Pendiente

`agent/`, `api/` y `static/` están aún sin implementar. Los cinco adaptadores de B3 y
cripto están en **esqueleto**: faltan elegir proveedor (decisión de negocio), no código.

Las ocho fuentes macro de B3 y cripto que declara `asset_sources_map.yaml`
(`bcb_focus`, `foreigner_flow`, `di_futures_yield`, `news_blackout`,
`fear_and_greed_index`, `crypto_onchain_whales`, `funding_rate_monitor`,
`liquidation_heatmap`) **no tienen implementación a propósito**: la referencia no
tiene contrato ni endpoint para ninguna, y un esquema inventado es peor que una
ausencia declarada. Están en `macro_ingestor/registry.PENDIENTES` y se pueden listar
con `registry.faltantes()`. Ver D-026.

### Código de referencia (restaurado)

El monolito MT5-centric de origen (`app.py` 5.185 líneas, `store.py` 1.752,
`agent.py` 1.308, `risk_engine.py` 628, `pattern_engine.py` 234, `simulator.py` 507,
módulo `core/` propio, `strategy.yaml`, `trading.db`) está único y verificado en:

```
C:\Users\fmaur\Desktop\Trading
```

Es **solo lectura**: nunca se modifica ni se importa. Toda referencia `REF/` en los
documentos de `.agent/` apunta a esa ruta. Incluye además una suite de **41 tests**
(`conftest.py`, `pytest.ini`, `fixtures/`, `scenarios/`), que es el activo de regresión
principal para las Fases 1–7.

Era un *placeholder* de OneDrive (directorios con atributo `ReparsePoint`) sin objetos
recuperables; ya fue **eliminado**. No recrearlo: la única copia del código de referencia
está en la ruta de arriba.

---

## Roadmap

| Fase | Alcance | Prioridad |
|---|---|---|
| **0** | Scaffolding, `.agent/*`, configs base | Crítica — **completa** |
| **1** | `core/` — risk, smc, orderflow, simulator, lot_calculator | Crítica (bloquea el split de `app.py`) |
| **2** | `database/` — store, models, `DB_PATH` único | Alta |
| **3** | `adapters/` — `base_adapter` + MT5 B3 / Forex / cripto | Alta (habilita multimercado) |
| **4** | `macro_ingestor/` — COT, DXY, Focus, funding, whales | Media-alta |
| **5** | `agent/` — prompt templates, bridge LLM, tools | Media |
| **6** | `api/` + `static/` — routers delgados, WS, frontend | Alta |
| **7** | Tests, scenarios mock, validación E2E | Crítica (validación final) |

Detalle con criterios de aceptación: [`.agent/ROADMAP.md`](.agent/ROADMAP.md).
Tarea a tarea: [`.agent/CHECKLIST.md`](.agent/CHECKLIST.md).
Análisis y mapeo desde el proyecto anterior: [`.agent/PLAN.md`](.agent/PLAN.md).

---

## Desarrollo

```bash
pytest tests/unit -v          # núcleo puro, sin I/O
pytest tests/ -v             # suite completa
ruff check . && black .      # estilo
mypy .                       # tipos
```

Contribución sujeta a `.agent/AGENT_GUIDELINES.md`: sin lógica de negocio en
endpoints, sin I/O en `core/`, sin proveedor filtrado al núcleo, y tests en verde
antes de dar por terminado un módulo.

## Licencia

Por definir.