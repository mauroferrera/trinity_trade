# MEJORAS_MR_VWAP.md - Convalidación del Mean Reversion VWAP en D1 (F3, multi-estrategia)

Histórico de **por qué** el Mean Reversion VWAP tiene los parámetros que tiene y de
lo que costó la convalidación que (no) los sostiene (D-075/D-076) y su recalibración
del interruptor (D-079). Este archivo es el homólogo de `MEJORAS_CTA_D1.md` para la
pata de compresión del roadmap multi-estrategia (D-071): mismo dataset D1 y mismo
proceso que F2, con `regime()` de `core/risk_engine.py` como interruptor.

Última ejecución: **2026-10-07** (re-ejecución D-079), sobre `trinity_data/research/cta/d1/`.

**Veredicto: PASS** (re-ejecución D-079, 2026-10-07, §5 — `OOS exp_ticks_net = +198,57`
con 32 trades). El run histórico D-076 (2026-10-07, §3) fue **NO CONVALIDADO por
construcción**: el gate viejo nunca abría (0 fills). Un solo run tras congelar
parámetros, sin iterar contra resultados (D-065); matices de la métrica en §5.

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
  `core/risk_engine.regime(candles[:i+1], lookback=250)` en la barra de señal y solo
  opera si devuelve `"rango"` (compresión → VWAP, D-071; lookback 250 desde la
  recalibración D-079, antes era 60). Se ejecuta también la MISMA
  estrategia **sin el gate** como referencia, para medir qué compra el interruptor.

Parámetros fijados ANTES de ver el resultado (convalidación, no optimización, D-065):
VWAP 20, k de bandas 2.0, SL_K 2.0, max_hold 10, ATR 14 (Wilder).

## 3. Split y veredicto (D-076, 2026-10-07) — NO CONVALIDADO (run histórico)

> Run histórico con la heurística VIEJA (`ratio span/cuerpo` 6.0/3.0). Se conserva
> íntegro como diagnóstico: la recalibración D-079 (§4) nace de aquí. El veredicto
> vigente es el de §5.

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

## 4. Recalibración del interruptor (D-079, 2026-10-07)

La deuda de §3 ("validar los umbrales contra datos reales con tests que asserten los
TRES estados; decisión de tocar `core/`") se pagó con el método registrado: medir
antes de congelar, congelar antes de mirar PnL (D-065).

**Regla nueva** (`core/risk_engine.py::regime`, parámetros congelados):

| componente | medida | cortes |
|---|---|---|
| Eje de compresión | percentil del ATR% (TR14 media / close) en ventana **250** | ≤20 → rango, ≥80 → expansión |
| Guardia de tendencia | `hurst_exponent()` R/S modificado puro, hasta **252** retornos (None con <128) | **≥0.70** → fuerza expansión y excluye de rango; ≤0.45 solo etiqueta |

- **Degradaciones declaradas**: <60 barras → neutro; serie plana → percentil 50 →
  neutro; Hurst sin historia → decide solo el percentil (y el `detail` lo dice).
  Claves legacy (`ratio`, `span`, `avg_body`) siguen saliendo como diagnóstico.
- **Cortes de Hurst contra su nula, no contra PnL** (`regime_scan.py --null`, 300 RW
  semilladas × 252 retornos): p10=0.447 / p50=0.573 / p95=0.727 (el R/S de muestra
  tiene sesgo +0.07) → 0.45/0.70 = 10.3 % y 8.3 % de falsos sobre ruido, potencia
  82 %/92 % sobre AR(1) ρ=−0.8/+0.8. Los cortes 0.55/0.60 del primer borrador
  habrían colapsado la banda neutro (el ruido solo sienta en 0.573).
- **Evidencia del scan** (`regime_scan.py`, 5 símbolos D1 × 1.296 señales MR, ANTES
  de F3): la regla intermedia `percentil≤20 AND H≤0.45` dejó rango en el **2.1 %** de
  las señales — degeneración <5 % bajo el criterio pre-registrado. La descomposición
  mostró que el estrangulador es el Hurst (H≤0.45 cumple el 6.2 % de las ventanas
  reales; el percentil solo deja el 32.3 %) y que el fallback de estado retrasado
  tampoco rescata (2.5 %). Regla final: **compresión sin tendencia persistente**
  (`percentil≤20 AND H<0.70`), que conserva a Hurst en la decisión como pidió el
  operador y excluye de rango las compresiones en tendencia fuerte (fadar una
  tendencia no es mean reversion).
- **Criterios de no-degeneración cumplidos** (registrados en el docstring del scan
  antes de correrlo): panel rango **29.4 %** / neutro 49.9 % / expansión 20.8 %
  (tres estados con masa); señales rango **30.9 %** (≥10 %). Ningún criterio es PnL.
- **Tests (deuda D-076)**: fixture real `tests/fixtures/regime_d1_samples.json`
  (3 ventanas D1 de 253 barras, una por estado, exportadas y verificadas por el
  scan) + Hurst semillado contra la nula + degradaciones: `TestRegime` 3→19 tests.

Registro: `research/regime_scan.py`, scan JSONs en
`trinity_data/research/results/regime_scan_*.json`, D-079 en `.agent/DECISIONS.md`.

## 5. Re-ejecución F3 (D-079, 2026-10-07) — PASS

`python research/backtest_mr.py` con TODO idéntico a §2/§3 (mismo dataset, split,
fricción 1 tick/lado, sizing vol-target, `verdict` congelado de F2) salvo el
interruptor recalibrado de §4 (regla nueva + lookback 250). **Un solo run después de
congelar; ninguna iteración contra este resultado** (D-065).

Resultado: 1.296 señales → 820 saltadas por régimen, 40 por embargo, resto de skips
en línea (in_position etc.) → **109 fills** (77 IS + 32 OOS). Reparto de señales que
llegan al gate: expansión 219, neutro 601, **rango 109** (todos los rango entran).

| periodo | n | win % | exp (USD) | exp (ticks) | pnl total | maxDD |
|---|---|---|---|---|---|---|
| IS | 77 | 53,2 % | +92,98 | −287,25 | +7.159,82 | −5.138 |
| **OOS** | 32 | 46,9 % | **−59,43** | **+198,57** | −1.901,73 | −6.813 |
| OOS sin gate (ref.) | 120 | 47,5 % | −16,83 | −406,07 | −2.019,97 | −15.051 |

**Veredicto (criterio congelado de F2/D-075): PASS** — `OOS exp_ticks_net = +198.57 > 0`
con n=32 ≥ `MIN_OOS_N=10`, fricción ya descontada. La referencia sin gate sigue en
−406 ticks: el salto de −406 → +198 lo compra el interruptor, no la estrategia
(la tesis del árbitro compresión→VWAP de D-071, §1).

**Matices, para que el PASS no se lea mejor de lo que es:**

- `exp_ticks_net` (media de ticks netos por trade) y `exp_usd_net` (media USD por
  trade) **se contradicen en ambos segmentos**: IS es +USD/−ticks, OOS es −USD/+ticks
  — el promedio simple de ticks pondera distinto por símbolo que el tamaño
  vol-target. El criterio congelado es por ticks (mismo `verdict` que F2); en USD el
  OOS queda −59,43/trade (−1.901 total), esencialmente en break-even.
- IS en ticks es **negativo** (−287): la consistencia intra-muestra no existe; el
  veredicto, como en F2, solo mira OOS.
- 32 trades OOS es suficiente para el umbral congelado (≥10), pero sigue siendo una
  muestra chica: el PASS habilita la siguiente etapa del roadmap, no un sizing agresivo.

Suite completa tras la recalibración: **1764 passed, 2 xfailed** (antes: 1750).
Resultado completo: `trinity_data/research/results/backtest_mr_20261008T023119Z.json`.

## 6. Pendiente

- ~~Deuda `regime()`~~ **PAGADA (D-079)**: umbrales calibrados contra la nula, tests
  de los TRES estados con datos reales (fixture congelado), `core/` tocado con el
  operador al frente.
- La clave de Databento (heredado, decide el usuario).
- El MR VWAP queda fuera de F4/F5 por diseño (ambas son CTA); un futuro F6 evaluaría
  su puesta en marcha a partir de este PASS (pipeline reproducible,
  `python research/backtest_mr.py`).
