# MEJORAS_MR_VWAP.md - Convalidación del Mean Reversion VWAP en D1 (F3, multi-estrategia)

Histórico de **por qué** el Mean Reversion VWAP tiene los parámetros que tiene y de
lo que costó la convalidación que (no) los sostiene (D-075/D-076). Este archivo es el
homólogo de `MEJORAS_CTA_D1.md` para la pata de compresión del roadmap
multi-estrategia (D-071): mismo dataset D1 y mismo proceso que F2, con `regime()` de
`core/risk_engine.py` como interruptor.

Última ejecución: **2026-10-07**, sobre `trinity_data/research/cta/d1/`.

**Veredicto: NO CONVALIDADO** (dos razones independientes, §3).

---

## 1. Qué se contrastó y dónde vive

| | |
|---|---|
| Datos | REUSA el dataset D1 de F2: `copy_rates` D1 de **MT5 demo** (MetaQuotes-Demo, cuenta 112125897) |
| Símbolos | EURUSD, XAUUSD, US500, GBPUSD, AUDUSD — 1.200 barras D1 cada uno (~4,7 años) |
| Raíz | `TRINITY_DATA_ROOT` → `C:\Users\fmaur\Desktop\trinity_data\research\cta\d1\` |
| Split | MISMO eje que F2 (importado de `backtest_cta.py`): IS hasta 2024-11-06, embargo 30d, OOS hasta 2026-10-07 |
| Fuente pile | `research/mr.py` (motor) + `research/backtest_mr.py` (veredicto + interruptor) |
| Resultado | `trinity_data/research/results/backtest_mr_20261007T131624Z.json` |

Todo lo pesado vive **fuera del repo y fuera de OneDrive** (D-064, `core/paths.py`).
Regenerable sin volver a pagar nada: `python research/backtest_mr.py`.

Regla de oro F3 (D-071): ni `research/mr.py` ni `backtest_mr.py` se consumen en
ejecución; convalidan antes de conectar.

## 2. El motor: VWAP rolling + bandas ±k*ATR + target en la media (D-075)

`research/mr.py` es puro (sin pandas/numpy, listas alineadas por barra con `None` en
el warm-up), testeado con aritmética de lápiz en `tests/unit/test_mr.py` (18 tests):

- **VWAP** rolling de 20 barras sobre precio típico `(H+L+C)/3` ponderado por
  `volume`: `None` en las primeras `n−1` barras (warm-up, como `cta.atr`), y las
  barras de volumen cero se saltan en la ventana (no contaminan la media ponderada).
- **Bandas** `VWAP ± 2×ATR` (ATR Wilder de F2). Donde falta VWAP o ATR, falta la banda.
- **Señal CONTRARIA al cierre de la barra `i`**: cierre > banda superior → SHORT
  (estirado, espera volver a la media); cierre < banda inferior → LONG. La banda de
  `i` se conoce al cerrar `i` (VWAP y ATR de `i` son datos ya cerrados) y el fill va
  al open de `i+1`, mismo convenio que F2 y sin lookahead. Cierre IGUAL a la banda no
  dispara (desigualdad estricta, testeado).
- **Vida del trade** (`simulate_mr`): target fijo = VWAP de la barra de señal
  (conocido al abrir), stop = fill ∓ 2×ATR de la barra de señal. SL y TP se evalúan
  barra a barra; si la misma barra toca ambos, **SL PRIMERO** (conservador, como
  `backtest_6e.py`). Hueco: solo el SL se rellena peor (al open); el TP llena en el
  target exacto — nunca se acredita un hueco a favor. Sin toque en `max_hold` = 10
  barras → cierre al cierre de la barra límite (`MAX_HOLD`); datos agotados →
  `END_OF_DATA`. MFE/MAE en precios raw (sin fricción), mismo convenio que F2/6E.
- **Fricción y tamaño** iguales a F2: 1 TICK adverso por lado con el `tick_size` del
  sidecar de cada símbolo, `vol_target_lots(equity=100k, atr, spec, vol_target=0.10)`,
  una posición a la vez por símbolo.
- **El interruptor** NO vive en el motor: `backtest_mr.py` evalúa
  `core/risk_engine.regime(candles[:i+1], lookback=60)` en la barra de señal y solo
  opera si devuelve `"rango"` (compresión → VWAP, D-071). Se ejecuta también la MISMA
  estrategia **sin el gate** como referencia, para medir qué compra el interruptor.

Parámetros fijados ANTES de ver el resultado (convalidación, no optimización, D-065):
VWAP 20, k de bandas 2.0, SL_K 2.0, max_hold 10, ATR 14 (Wilder).

## 3. Split y veredicto (D-076) — NO CONVALIDADO

Mismo split que F2: IS hasta **2024-11-06**, embargo 30 días hasta **2024-12-06**
(40 señales como contexto), OOS hasta fin. Fricción 1 tick/lado ya DESCONTADA del
neto, así que el gate "OOS supera la fricción" = `OOS exp_ticks_net > 0` con al menos
10 trades (mismo `verdict` que F2).

**(1) Configuración canónica CON el interruptor: 0 fills → INCONCLUSIVE por
construcción.** De 1.296 señales: 1.254 saltadas por `regime() == "expansion"`, 40
por embargo, 2 por no tener barra de fill. El gate nunca abre, no hay nada que
medir.

**(2) Referencia SIN gate: NO supera la fricción.**

| periodo | n | win % | exp (USD) | exp (ticks) | pnl total | PF | maxDD |
|---|---|---|---|---|---|---|---|
| IS | 183 | 43,2 % | −59,23 | −283,5 | −10.839,83 | 0,886 | −16.322 |
| **OOS** | 120 | 47,5 % | **−16,83** | **−406,1** | −2.019,97 | **0,968** | −15.051 |

**(3) Diagnóstico del interruptor: `regime()` no discrimina en datos reales.**
Clasifica por `ratio = span(60 barras)/cuerpo_medio` con umbrales fijos (≥6 →
expansión, ≤3 → rango) y ese ratio no baja nunca:

| fuente | ratio p50 | "rango" observado |
|---|---|---|
| D1, 6.000 barras × 5 símbolos (dataset F2) | ≈ 15 | 1 barra (GBPUSD) |
| M15, 5.000 barras × 5 símbolos (temporalidad de producción, `mt5_market.py:60`) | — | **0 % (100 % expansión)** |

El test de `regime()` (`tests/unit/test_risk_engine.py::TestRegime`) solo asserta
"que corra" — `in ("expansion","rango","neutro")` —, nunca que cada estado dispare:
los umbrales nunca se validaron contra datos reales. Hoy `regime()` solo se **muestra**
en el panel (ningún gate en vivo lo lee), así que el impacto funcional es cero, pero
queda la deuda: el árbitro "compresión→VWAP" de D-071 no puede abrirse con la
heurística actual.

**Veredicto: NO CONVALIDADO.** Decisión del operador tras ver el diagnóstico: cerrar
F3 así **sin tocar `core/risk_engine.py`** (recalibrar umbrales contra datos es mejora
futura, no calibración a posteriori de una estrategia, D-065). Efecto en el roadmap:
F4/F5 (CTA en alerta) no se ven afectados — con `regime()` siempre en "expansión" el
CTA quedaría siempre habilitado y hoy nada gatea en vivo por régimen.

Suite completa tras F3: **1555 passed, 2 xfailed** (antes de F3: 1537).

## 4. Pendiente

- **Deuda `regime()`**: validar/calibrar los umbrales 6.0/3.0 contra datos reales
  (D1 y M15) con tests que asserten los TRES estados; decisión de tocar `core/`.
- La clave de Databento (heredado, decide el usuario).
- El MR VWAP queda FUERA de F4/F5 por no convalidar; se revalidaría si cambian sus
  parámetros o el interruptor (pipeline reproducible, `python research/backtest_mr.py`).
