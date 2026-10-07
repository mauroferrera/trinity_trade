"""Componentes PUROS del CTA Swing D1 (F2, D-071): señales, ATR, trailing.

El SÍ mismo de la estrategia que se convalida en `backtest_cta.py` vive aquí, en
funciones puras sin pandas ni numpy (listas de floats), para que la suite pueda
fijar su aritmética con datos sintéticos deterministas —es la regla de oro de la
fase: primero tests, después veredicto—. El backtest sobre D1 real solo orquesta
estas piezas y suma la fricción, igual que `backtest_6e.py` orquesta el core con
fill y fricción encima.

Qué hace la estrategia (resumen del modelo que se convalida)
------------------------------------------------------------
- ENTRADA por breakout del canal de Donchian: el cierre de la barra `i` rompe
  el canal de las `n` barras ANTERIORES (sin incluir la propia), y el fill es el
  OPEN de `i+1` (próximo-open, D-071). Superior: cierre > max(high[i-n..i-1]);
  inferior: cierre < min(low[i-n..i-1]).
- SALIDA con trailing chandelier: stop = extremo desde la entrada − k×ATR
  (long: high; short: low). El forecast se actualiza SIEMPRE con datos de la
  barra ANTERIOR a la que comprueba el stop: no hay lookahead en el trailing.
- TAMAÑO por vol-targeting (`core.lot_calculator.vol_target_lots`), no por SL:
  cada posición aporta la misma vol diaria esperada al equity.

Convenciones
------------
Todas las funciones reciben listas alineadas por barra y devuelven listas con
`None` en el warm-up, para que el índice signifique "barra i" en todas partes.
Precios en unidades del símbolo; las decisiones usan SIEMPRE barras cerradas:
una barra nunca decide usando su propio rango.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple


def atr(highs, lows, closes: List[float], n: int = 14) -> List[Optional[float]]:
    """ATR de Wilder alineado por barra; `None` antes de la barra n-1.

    `atr[i]` usa los datos hasta el cierre de `i` (es "conocido" al cerrar la
    barra). El primer valor es la media simple de los `n` primeros rangos y
    después se suaviza con RMA: `atr[i] = (atr[i-1]*(n-1) + tr[i])/n`.
    """
    m = len(closes)
    if m == 0:
        return []
    trs: List[float] = [max(highs[0] - lows[0], 0.0)]
    prev_close = closes[0]
    for i in range(1, m):
        h, l, c = highs[i], lows[i], closes[i]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(max(tr, 0.0))
        prev_close = c
    out: List[Optional[float]] = [None] * m
    if m < n:
        return out
    prev = sum(trs[:n]) / n
    out[n - 1] = prev
    for i in range(n, m):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def donchian(
    highs: List[float], lows: List[float], n: int = 20
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Canal de Donchian: `(upper, lower)` con el máximo/mínimo de las últimas
    `n` barras HASTA la barra `i` inclusive (ambas alineadas por barra).
    """
    m = len(highs)
    upper: List[Optional[float]] = [None] * m
    lower: List[Optional[float]] = [None] * m
    for i in range(n - 1, m):
        upper[i] = max(highs[i - n + 1: i + 1])
        lower[i] = min(lows[i - n + 1: i + 1])
    return upper, lower


def breakout_signals(
    closes: List[float],
    upper: List[Optional[float]],
    lower: List[Optional[float]],
) -> List[Tuple[str, int]]:
    """Señales de entrada `(dirección, barra)` del breakout próximo-open.

    El cierre de la barra `i` se compara contra el canal de las `n` barras
    ANTERIORES, no contra el canal que incluye a `i` (`upper[i-1]`/`lower[i-1]`):
    decidir con el rango de la propia barra sería decidir con el futuro del
    fill, que va al open de `i+1`.
    """
    m = len(closes)
    out: List[Tuple[str, int]] = []
    for i in range(1, m):
        u = upper[i - 1]
        lo = lower[i - 1]
        if u is None:
            continue
        if closes[i] > u:
            out.append(("long", i))
        elif closes[i] < lo:
            out.append(("short", i))
    return out


def chandelier(direction: str, entry: float, atr_dec: float, mult: float) -> float:
    """Stop inicial del trailing chandelier (el primer stop de la posición)."""
    return entry - mult * atr_dec if direction == "long" else entry + mult * atr_dec


def update_trail(
    direction: str,
    trail: Optional[float],
    ref: float,
    atr_value: float,
    mult: float,
) -> float:
    """Ratchea el stop con el extremo `ref` (high en long, low en short).

    El chandelier nunca afloja (long: max; short: min): un stop que se aleja
    cuando el precio vuelve es una posición que regala el recorrido ganado.
    """
    if direction == "long":
        cand = ref - mult * atr_value
        return cand if trail is None or cand > trail else float(trail)
    cand = ref + mult * atr_value
    return cand if trail is None or cand < trail else float(trail)


def simulate_trade(
    direction: str,
    entry_i: int,
    entry: float,
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    atrs: List[Optional[float]],
    mult: float,
    atr_dec: float,
) -> Dict[str, float]:
    """Vida de un trade de entrada a mercado: de `entry_i` en adelante.

    El trail de la barra `k` se ratchea con la barra `k-1` (extremo y ATR
    conocidos al cierre), de modo que el stop que se evalúa contra `low[k]`
    (o `high[k]`) nunca vio el rango de `k`. Fill del exit: el stop si la barra
    lo toca; el open si la barra abre más allá del stop (hueco, peor precio).

    Devuelve el exit en precio RAW (sin fricción, la añade el backtest):
    `exit_i`, `exit_raw`, `closed_by` ("trail" o "END_OF_DATA"), y MFE/MAE en
    unidades de precio del símbolo (raw, como en `backtest_6e.py`).
    """
    n = len(closes)
    trail = chandelier(direction, entry, atr_dec, mult)
    mfe, mae = 0.0, 0.0
    exit_i = n - 1
    exit_raw: Optional[float] = None
    closed_by = ""
    for k in range(entry_i, n):
        o, h, l, c = opens[k], highs[k], lows[k], closes[k]
        if direction == "long":
            mfe = max(mfe, h - entry)
            mae = min(mae, l - entry)
            hit = l <= trail
        else:
            mfe = max(mfe, entry - l)
            mae = min(mae, entry - h)
            hit = h >= trail
        if hit:
            if direction == "long":
                exit_raw = min(o, trail) if o < trail else trail
            else:
                exit_raw = max(o, trail) if o > trail else trail
            closed_by = "trail"
            exit_i = k
            break
        if k + 1 < n:
            if direction == "long":
                trail = update_trail("long", trail, h, float(atrs[k]), mult)
            else:
                trail = update_trail("short", trail, l, float(atrs[k]), mult)
    if exit_raw is None:
        exit_raw = closes[n - 1]
        closed_by = "END_OF_DATA"
        exit_i = n - 1
    return {
        "exit_i": float(exit_i),
        "exit_raw": float(exit_raw),
        "closed_by": closed_by,
        "mfe": float(mfe),
        "mae": float(mae),
        "bars_held": float(exit_i - entry_i),
    }


__all__ = [
    "atr",
    "breakout_signals",
    "chandelier",
    "donchian",
    "simulate_trade",
    "update_trail",
]