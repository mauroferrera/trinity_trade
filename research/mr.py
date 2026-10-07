"""Motor de Mean Reversion a VWAP (F3, D-071/D-075).

Módulo PURO (sin pandas/numpy), hermano de `research/cta.py` pero para la pata
de compresión del sistema multi-estrategia: en régimen de RANGO (compresión) el
precio oscila alrededor de su media ponderada por volumen, así que el edge es
contrarian (llegó al extremo, vuelve al centro), no trend-following como el CTA.

Piezas (todas decididas ANTES de ver el resultado, D-065: convalidación, no
optimización):

  - VWAP rolling de N barras sobre precio típico (H+L+C)/3 ponderado por
    volumen (`tick_volume` de MT5). `None` en el warm-up (N-1 primeras barras).
  - Bandas ± k*ATR (ATR Wilder, el mismo de F2) alrededor del VWAP.
  - Señal al cierre de la barra `i`: cierre por encima de la banda superior →
    SHORT (estirado, espera revertir al VWAP); por debajo de la inferior →
    LONG. El fill va al open de `i+1` (mismo convenio que F2; la banda de `i`
    se conoce al cerrar `i`, no hay lookahead).
  - Entrada contraria, target = VWAP de la barra de señal (fijo, conocido al
    abrir), stop = sl_k*ATR, y `max_hold` barras como techo temporal. SL y TP
    se evalúan barra a barra y si la misma barra toca ambos, SL PRIMERO
    (conservador, mismo convenio que `backtest_6e.py`).
  - Hueco: solo el SL se rellena peor (al open si la barra abre ya más allá
    del stop); el TP llena en el target exacto — nunca se acredita un hueco
    a favor, que sería una cota optimista.

El GATE de régimen (el "interruptor" de F3: expansión→ILOF+CTA, compresión→
VWAP, noticias→solo Copilot) NO vive aquí: `regime()` es de `core/risk_engine.py`
y el backtest decide operar o no según su salida. Este módulo solo convalida la
lógica de la estrategia.

La fricción la añade el backtest (1 tick adverso por lado, como F2), no este
módulo: aquí todo es precio RAW.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

__all__ = [
    "typical_price",
    "vwap",
    "bands",
    "mr_signals",
    "simulate_mr",
]


def typical_price(
    highs: List[float],
    lows: List[float],
    closes: List[float],
) -> List[float]:
    """Precio típico (H+L+C)/3 por barra: el que pondera el VWAP clásico."""
    return [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]


def vwap(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    n: int,
) -> List[Optional[float]]:
    """VWAP rolling de `n` barras (precio típico ponderado por volumen).

    `None` en las primeras `n-1` barras (warm-up, como en `cta.atr`/`donchian`):
    una media ponderada con menos de `n` puntos no es comparable con una con
    ventana completa.
    """
    if n <= 0:
        raise ValueError(f"n debe ser > 0, recibido {n}")
    if len(highs) != len(volumes):
        raise ValueError("highs y volumes deben tener la misma longitud")
    tp = typical_price(highs, lows, closes)
    m = len(tp)
    out: List[Optional[float]] = [None] * m
    for i in range(n - 1, m):
        num = 0.0
        den = 0.0
        for k in range(i - n + 1, i + 1):
            v = volumes[k]
            if v <= 0:
                continue
            num += tp[k] * v
            den += v
        out[i] = num / den if den > 0 else None
    return out


def bands(
    vwap_series: List[Optional[float]],
    atrs: List[Optional[float]],
    k: float,
) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """Bandas ± k*ATR alrededor del VWAP: (superior, inferior).

    Donde el VWAP o el ATR no están calientes (`None`), la banda es `None`.
    """
    sup: List[Optional[float]] = []
    inf: List[Optional[float]] = []
    for v, a in zip(vwap_series, atrs):
        if v is None or a is None:
            sup.append(None)
            inf.append(None)
        else:
            sup.append(v + k * a)
            inf.append(v - k * a)
    return sup, inf


def mr_signals(
    closes: List[float],
    sup: List[Optional[float]],
    inf: List[Optional[float]],
) -> List[Tuple[str, int]]:
    """Señales `(dirección, barra_i)` de mean reversion al cierre de cada barra.

    Cierre por encima de la banda superior → SHORT (estirado al alza, espera
    volver al VWAP); por debajo de la inferior → LONG. Dentro de la banda o con
    banda `None` (warm-up) → nada.

    A diferencia del CTA (breakout, compara `close[i]` con el canal de `i-1`),
    aquí la banda de `i` se CONOCE al cerrar `i` (VWAP y ATR de `i` son datos
    de esa misma barra ya cerrada), así que comparar con la banda de `i` no mira
    el futuro: el fill va al open de `i+1`.
    """
    out: List[Tuple[str, int]] = []
    for i in range(len(closes)):
        s, lo = sup[i], inf[i]
        if s is None or lo is None:
            continue
        if closes[i] > s:
            out.append(("short", i))
        elif closes[i] < lo:
            out.append(("long", i))
    return out


def simulate_mr(
    direction: str,
    entry_i: int,
    entry: float,
    target: float,
    sl: float,
    opens: List[float],
    highs: List[float],
    lows: List[float],
    closes: List[float],
    max_hold: int,
) -> Dict[str, float]:
    """Vida de un trade de mean reversion: de `entry_i` (barra de fill) en adelante.

    Target fijo = VWAP de la barra de SEÑAL (conocido al abrir la posición, sin
    lookahead), stop fijo = `sl` (k*ATR de la barra de señal). SL y TP se
    evalúan barra a barra; si la misma barra toca ambos, SL PRIMERO (conservador,
    como `backtest_6e.py`). Hueco: solo el SL se rellena peor (al open si la
    barra abre ya más allá del stop); el TP llena en el target exacto. Si la
    posición vive `max_hold` barras sin tocar ningún nivel, se cierra al cierre
    de esa barra ("MAX_HOLD"); si se acaban los datos, "END_OF_DATA".

    Devuelve RAW (sin fricción; la añade el backtest): `exit_i`, `exit_raw`,
    `closed_by` ("SL" | "TP" | "MAX_HOLD" | "END_OF_DATA"), `mfe`, `mae`,
    `bars_held` — MFE/MAE en unidades de precio del símbolo, como en F2.
    """
    n = len(closes)
    mfe, mae = 0.0, 0.0
    exit_i = n - 1
    exit_raw: Optional[float] = None
    closed_by = ""
    end = min(n - 1, entry_i + max_hold - 1)
    for k in range(entry_i, n):
        o, h, l, c = opens[k], highs[k], lows[k], closes[k]
        if direction == "long":
            mfe = max(mfe, h - entry)
            mae = min(mae, l - entry)
            sl_hit = l <= sl
            tp_hit = h >= target
        else:
            mfe = max(mfe, entry - l)
            mae = min(mae, entry - h)
            sl_hit = h >= sl
            tp_hit = l <= target
        if sl_hit and tp_hit:
            exit_raw, closed_by = sl, "SL"
        elif sl_hit:
            exit_raw, closed_by = sl, "SL"
        elif tp_hit:
            exit_raw, closed_by = target, "TP"
        if exit_raw is not None:
            # Hueco: SOLO el SL se rellena peor (al open si atraviesa el nivel,
            # como F2); el TP llena en el target exacto (nunca se acredita un
            # hueco a favor, que sería una cota optimista).
            if closed_by == "SL":
                if direction == "long" and o < exit_raw:
                    exit_raw = o
                elif direction == "short" and o > exit_raw:
                    exit_raw = o
            exit_i = k
            break
        if k == end:
            exit_raw = c
            closed_by = "MAX_HOLD" if end < n - 1 else "END_OF_DATA"
            exit_i = k
            break
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
