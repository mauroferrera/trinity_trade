# MEJORAS_CTA_D1.md - Convalidación del CTA Swing en D1 (F2, multi-estrategia)

Histórico de **por qué** el CTA Swing D1 tiene los parámetros que tiene y de lo
que costó la convalidación que los sostiene (D-073/D-074). Este archivo es el
homólogo de `MEJORAS_DATABENTO.md` para la pata de tendencias del roadmap
multi-estrategia (D-071): datos de MT5 demo, motor puro en `research/cta.py`,
tamaño por vol-targeting en `core/lot_calculator.py`, y un backtest IS/OOS con
fricción idéntico en espíritu al de 6E.

Última convalidación: **2026-10-06**, sobre `trinity_data/research/cta/d1/`.

---

## 1. Qué se contrastó y dónde vive

| | |
|---|---|
| Datos | `copy_rates` D1 de **MT5 demo** (MetaQuotes-Demo, cuenta 112125897) |
| Símbolos | EURUSD, XAUUSD, US500, GBPUSD, AUDUSD — 1.200 barras D1 cada uno (~4,7 años) |
| Ventana real | 2021-12-23 (US500) / 2022-02 → 2026-10-07 |
| Raíz | `TRINITY_DATA_ROOT` → `C:\Users\fmaur\Desktop\trinity_data\research\cta\d1\` |
| Sidecar | `*_D1.parquet.meta.json`: sha256 + specs (tick_size/tick_value/volume_min/step) |
| Fuente pile | `research/build_cta_candles.py` (descarga) + `research/cta.py` (motor) + `research/backtest_cta.py` (veredicto) |
| Resultado | `trinity_data/research/results/backtest_cta_20261007T015728Z.json` |

Todo lo pesado vive **fuera del repo y fuera de OneDrive** (D-064, `core/paths.py`).
Regenerable sin volver a pagar nada: `python research/build_cta_candles.py` +
`python research/backtest_cta.py`.

Regla de oro F2 (D-071): ni `research/cta.py` ni `backtest_cta.py` se consumen en
ejecución; convalidan antes de conectar.

---

## 2. El motor: breakout Donchian + trailing chandelier + vol-targeting (D-073)

`research/cta.py` es puro (sin pandas/numpy, listas alineadas por barra con
`None` en el warm-up), testeado con aritmética de lápiz en
`tests/unit/test_cta.py`:

- **Entrada** por breakout de Donchian: el cierre de la barra `i` contra el canal
  de las `n` barras ANTERIORES (`close[i]` vs `upper[i-1]`/`lower[i-1]`). Decidir
  con el rango de la propia barra sería decidir con el futuro del fill, que va al
  open de `i+1`. Test `test_el_breakout_usa_el_canal_sin_la_barra_actual`.
- **Salida** con trailing chandelier `extremo − k×ATR` (long) que **solo
  ratchea**: el stop de la barra `k` usa el extremo de `k−1`; si la barra de exit
  abre más allá del stop (hueco) se rellena al open, peor precio. El test
  `test_sin_lookahead_en_el_trailing` prohíbe explícitamente el bug del
  ratchet-antes-de-evaluar (salida a 103 en lugar de 99).
- **Tamaño** por volatilidad objetivo (`core/lot_calculator.vol_target_lots`):
  `objetivo_diario = equity×vol_target/sqrt(252)`, `per_lot = tick_value×ATR/tick_size`,
  floor al `lot_step` y clamp al `min_lot`. NO entra en `CALCULATORS`: ese registro
  despacha por riesgo hasta un SL; el vol-target es otra clase de pregunta (D-071).
- **Fricción** de 1 TICK adversa por lado, con el `tick_size` de CADA símbolo
  (del sidecar de MT5), no un pip plano.
- **Una posición a la vez por símbolo** (sin piramidar): las señales que llegan con
  la posición abierta se descartan (`skipped_in_position`), 634 en el run final.

## 3. Split y veredicto (D-074) — PASS

Split derivado del span real del dataset: IS hasta **2024-11-06**, embargo 30 días
hasta **2024-12-06** (contexto, sin trades: 20 señales descartadas), OOS hasta fin.
Sin calibración: ATR 14 (Wilder), Donchian 20, `k=3`, vol_target 0.10, equity 100k,
**fijados antes de ver el resultado** (convalidación, no optimización, D-065).

Resumen canónico (Donchian 20):

| periodo | n | win % | exp (USD) | exp (ticks) | pnl total | PF | maxDD |
|---|---|---|---|---|---|---|---|
| **IS** | 106 | 39,6 % | **+75,74** | +608,4 | +8.028,78 | 1,135 | −12.099 |
| **OOS** | 73 | 38,4 % | **+117,09** | +1.797,4 | +8.547,88 | 1,176 | −11.990 |

Referencia Donchian 55 (solo lectura): OOS 47 trades, **+150,99 USD/trade**, PF 1,212.

**Veredicto: CONVALIDA.** Con la fricción (1 tick/lado) ya descontada del neto,
`OOS exp_ticks_net > 0` ⟺ OOS supera la fricción (gate de la F2). Consistente:
IS y OOS piden la misma cosa (exp setencia positiva, win ~39 %, ganadores ~2× el
perdedor medio) y el resultado se mantiene al cambiar la ventana de Donchian.

Distribución por símbolo (fills): EURUSD 39 · XAUUSD 35 · US500 35 · GBPUSD 35 ·
AUDUSD 35. Salidas: 174 por trailing, 5 censuradas por fin del dataset.

Pieza representativa del edge (verificada contra el parquet): XAUUSD long del
2025-08-28, fill al open 3.417,06, trailing a **4.136,50** (high 4.381,53 menos
3×ATR ≈ 81,7) con 37 días en posición y MAE de solo 209 $: el clásico win grande
de una estrategia de tendencia. MFE/MAE sobre precios raw (sin fricción), mismo
convenio que el backtest 6E.

## 4. Pendiente

- La clave de Databento (heredado, decide el usuario).
- F2 solo convalida; ejecutar el CTA es F4/F5 (`exit_policy.py`, perfil propio,
  modo alerta con `auto_execute=false`). Mientras tanto, `regime()` manda.