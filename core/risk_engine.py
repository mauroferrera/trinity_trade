"""Motor de riesgo determinista (M1-M3).

Modulo PURO: no importa MT5, ni la app, ni los servicios de datos. Recibe toda la
entrada por parametros para poder probarse sin terminal abierta. Produce el Setup
Score (0-100), el regimen de mercado, la killzone y la validacion de entrada con
invalidez estructural, R:R minimo y TTL del setup.

Convenciones de direccion: "BUY" (long) o "SELL" (short).
Los pesos, umbrales, ventanas de killzone y TTL vienen de la config de trading y
se sobreescriben con estos defaults si faltan.
"""

from __future__ import annotations

import math
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional, Tuple

# Pesos por defecto, en puntos. El score es sum(valor*peso)/total*100, así que un
# peso > 0 sobre un componente SIN DATO no es neutro: es una reserva de puntuación
# que ese componente nunca puede devolver. El máximo alcanzable baja y el setup se
# degrada sin que nadie pueda decir por qué.
#
# Estos valores son la calibración de producción, no un suelo de emergencia, y
# por eso son los mismos que strategy.yaml (score.weights). Antes estaban
# separados: aquí 0/25/30/20/0 contra 0/20/50/0/15 en el YAML, y cada ruta que
# no leía la config medía sobre una escala distinta. strategy.py los toma de
# aquí, así que la copia no puede volver a divergir; si cambia la calibración,
# cambia en los dos sitios o este bloque deja de ser el que dice.
#
# `cot` = 0.0 porque el COT solo existe para el EURUSD (contrato 6E, informe
# semanal de la CFTC). Para el resto de mercados configurados —oro, índices,
# cruces de yenes— `cot_component` devuelve 0.0 con detalle "Sin reporte COT", y
# un cuarto del score se iba a un cero cada vez. La regla es: peso > 0 solo para
# lo que tiene feed en todos los mercados, o cuya ausencia depende del despliegue
# y no del mercado.
#
# `killzone` = 0.0 porque NO es un componente de score: es un gate. watcher.py lo
# exige como término AND duro (`score >= min_score and in_killzone and ...`), así
# que dentro de la ventana el setup está y fuera no está, sin zona intermedia. Darle
# puntos además de exigirlo cuenta la misma variable dos veces: compra score sin
# comprar acceso, y desvia el umbral hacia setups que el gate ya va a rechazar.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "cot": 0.0,
    "cvd_of": 20.0,
    "smc": 50.0,
    "killzone": 0.0,
    "smr_dxy": 15.0,
}

DEFAULT_GRADE_THRESHOLDS: Dict[str, float] = {
    "alta": 80.0,
    "media": 60.0,
}

# Ventanas UTC. Deben coincidir con strategy.yaml, que es la fuente de verdad
# (07:00-10:00 / 12:30-15:30). Antes defaultaban a 08:00-11:00 / 13:00-16:00
# mientras el YAML decía otra cosa, de modo que cualquier ruta que usara el
# default (tests, snapshot forzado) medía una ventana distinta de la real.
DEFAULT_KILLZONES: List[Dict[str, str]] = [
    {"name": "Londres", "start": "07:00", "end": "10:00"},
    {"name": "Nueva York", "start": "12:30", "end": "15:30"},
]

VERDICT_ALTA = "ALTA_PROBABILIDAD"
VERDICT_MEDIA = "MEDIA_PROBABILIDAD"
VERDICT_SIN = "SIN_OPERATIVA"

REGIME_EXPANSION = "expansion"
REGIME_RANGO = "rango"
REGIME_NEUTRAL = "neutro"

# --- Clasificador de régimen (D-079) -----------------------------------------
#
# La heurística original (ratio span/cuerpo_medio con cortes fijos 6.0/3.0)
# clasificaba "expansion" el 100% de las barras en D1 y M15: el ratio p50
# observado era ~15, lejos de ambos cortes, así que el gate
# compresión→VWAP de D-071 nunca se abrió (MEJORAS_MR_VWAP.md §3). La deuda
# estaba documentada: los cortes nunca se validaron contra datos reales.
#
# Dos medidas con semántica propia los sustituyen:
#
#   - Percentil del ATR% dentro de su propia historia: la compresión es
#     RELATIVA a lo reciente del mismo activo/marco temporal, sin umbral
#     absoluto que haya que recalibrar por mercado. Reparte la serie por
#     construcción en ~20% compresión / ~60% neutro / ~20% expansión.
#   - Exponente de Hurst sobre los retornos: persistencia (H>0.5, tendencia)
#     vs antipersistencia (H<0.5, reversión). Un "rango" con H alto es una
#     tendencia de baja volatilidad, no la acumulación que busca la
#     reversión a la media; y una expansión con H bajo es ruido caro.
#
# Umbrales fijados ANTES de ver el resultado de la re-convalidación F3
# (D-065): se convalidan contra datos, no se ajustan contra el PnL de MR.
REGIME_PCT_WINDOW = 250     # historia del percentil de ATR% (~1 año D1)
REGIME_P_RANGO = 20.0       # percentil <= éste => compresión
REGIME_P_EXPANSION = 80.0   # percentil >= éste => expansión
REGIME_ATR_N = 14           # ventana del ATR media (misma cadencia que F2)
REGIME_MIN_BARS = 60        # historia mínima para clasificar (antes 5)
REGIME_HURST_WINDOW = 252   # retornos máximos para estimar H (~1 año D1)
REGIME_HURST_MIN = 128      # retornos mínimos; por debajo H = None
#
# Cortes de Hurst calibrados contra la DISTRIBUCIÓN NULA del propio
# estimador, no contra el resultado de ninguna estrategia: 300 series RW
# semilladas de 252 retornos dan para R/S p10=0.447, mediana=0.573,
# p95=0.727 (el R/S de muestra tiene sesgo +0.07 a esta escala). Cortes a
# 0.45/0.70 => 10.3% de falsos "antipersistentes" y 8.3% de falsos
# "persistentes" sobre ruido, con potencia 82%/92% sobre AR(1)
# rho=-0.8/+0.8; con 0.55/0.60 (los del primer borrador) el ruido solo
# habría colapsado la banda "neutro". Medido en
# research/regime_scan.py --null, antes de mirar el PnL de MR (D-065).
REGIME_HURST_LO = 0.45      # H <= éste => etiqueta "antipersistente" (diagnóstico)
REGIME_HURST_HI = 0.70      # H >= éste => tendencia fuerte: fuerza expansion y
                            #             excluye de rango (la única guardia
                            #             de Hurst en la decisión, D-079)


def _h_as_min(t: dtime) -> int:
    return t.hour * 60 + t.minute


def _parse_hhmm(value: str) -> Optional[dtime]:
    try:
        h, m = value.strip().split(":")
        return dtime(hour=int(h), minute=int(m))
    except (ValueError, AttributeError):
        return None


def killzone_score(windows: Optional[List[Dict[str, str]]] = None,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """Valor del componente killzone: 1.0 dentro de una ventana, 0.25 fuera.

    Ventanas UTC como [{"name","start","end"}] con HH:MM. `now` se usa por
    parametro (pureza/testeable); si no se pasa, se toma datetime.now(timezone.utc).
    """
    wins = windows if windows is not None else DEFAULT_KILLZONES
    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    t = dtime(hour=dt.hour, minute=dt.minute, second=dt.second)
    tmin = _h_as_min(t)

    active = None
    for w in wins:
        start = _parse_hhmm(w.get("start", ""))
        end = _parse_hhmm(w.get("end", ""))
        if start is None or end is None:
            continue
        smin, emin = _h_as_min(start), _h_as_min(end)
        inside = (smin <= tmin < emin) if emin >= smin else (tmin >= smin or tmin < emin)
        if inside:
            active = w
            break

    if active is not None:
        return {
            "value": 1.0,
            "in_killzone": True,
            "name": active.get("name", "?") ,
            "detail": f"Dentro de la sesion {active.get('name', '?')} (ventana activa en UTC).",
        }
    return {
        "value": 0.25,
        "in_killzone": False,
        "name": None,
        "detail": "Fuera de ventanas de killzone (peso reducido).",
    }


def _true_ranges(win: List[Dict[str, Any]]) -> List[float]:
    """Rango verdadero barra a barra; el primero usa open como cierre previo."""
    trs: List[float] = []
    prev_close: Optional[float] = None
    for c in win:
        h, l = c["high"], c["low"]
        pc = prev_close if prev_close is not None else c["open"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        prev_close = c["close"]
    return trs


def _atr_pct_series(win: List[Dict[str, Any]],
                    n: int = REGIME_ATR_N) -> List[Optional[float]]:
    """ATR medio de los últimos `n` rangos verdaderos dividido por el close.

    Media simple y NO Wilder: aquí no se convalida el ATR de F2, se mide la
    compresión RELATIVA de la serie. La media simple da el mismo peso a
    cada barra de la ventana; Wilder pondera lo reciente y sesgaría el
    percentil hacia la barra actual, que es justo la que se está juzgando.

    Devuelve None en la posición i si el close no es positivo (no hay
    porcentaje que calcular y no se inventa uno).
    """
    trs = _true_ranges(win)
    out: List[Optional[float]] = []
    for i in range(len(win)):
        seg = trs[max(0, i - n + 1):i + 1]
        close = win[i]["close"]
        out.append((sum(seg) / len(seg) / close) if close and close > 0 else None)
    return out


def _percentile_rank(values: List[float], current: float) -> float:
    """Percentil de `current` dentro de `values` (empates: media de rangos).

    Una serie plana (mismo ATR% siempre) devuelve 50 y cae a "neutro": sin
    dispersión no hay compresión relativa que señalar, y sobre una serie
    constante cualquier "rango" o "expansión" sería un artefacto del corte.
    """
    if not values:
        return 50.0
    below = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return (below + 0.5 * equal) / len(values) * 100.0


def hurst_exponent(closes: Optional[List[float]] = None,
                   window: int = REGIME_HURST_WINDOW,
                   min_returns: int = REGIME_HURST_MIN) -> Optional[float]:
    """Exponente de Hurst por R/S modificado.

    Pendiente de la recta log(R/S) vs log(n) sobre subseries de
    n en {16, 32, 64, 128} retornos: H > 0.5 persistencia (tendencia),
    H < 0.5 antipersistencia (reversión), H ~ 0.5 ruido browniano.

    Puro y determinista (sin numpy, sin aleatoriedad): la misma serie da
    siempre el mismo H, condición para testearlo con aritmética de lápiz y
    para que el panel no cambie de respuesta entre dos consultas iguales.

    Devuelve None con menos de `min_returns` retornos (o con un close no
    positivo, donde el log no existe): un H estimado sobre menos de eso es
    ruido, y clasificar con ruido es peor que no clasificar. El caller cae
    a la regla solo-percentil y lo declara en el detalle.
    """
    cs = list(closes or [])[-(window + 1):]
    if len(cs) < min_returns + 1:
        return None
    rets: List[float] = []
    for a, b in zip(cs, cs[1:]):
        if a <= 0 or b <= 0:
            return None
        rets.append(math.log(b / a))

    points: List[Tuple[float, float]] = []
    for n in (16, 32, 64, 128):
        if len(rets) < 2 * n:
            continue  # hace falta al menos una subserie completa de reserva
        rs_vals: List[float] = []
        for start in range(0, len(rets) - n + 1, n):
            chunk = rets[start:start + n]
            mean = sum(chunk) / n
            acc = 0.0
            mn = mx = 0.0
            for k, r in enumerate(chunk):
                acc += r - mean
                if k == 0:
                    mn = mx = acc
                else:
                    mn = min(mn, acc)
                    mx = max(mx, acc)
            var = sum((r - mean) ** 2 for r in chunk) / n
            if var > 0 and mx > mn:
                rs_vals.append((mx - mn) / math.sqrt(var))
        if rs_vals:
            points.append((math.log(n), math.log(sum(rs_vals) / len(rs_vals))))

    if len(points) < 2:
        return None
    # Mínimos cuadrados de log(R/S) sobre log(n): la pendiente ES el Hurst.
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    return sxy / sxx


def _hurst_class(h: Optional[float]) -> Optional[str]:
    """Etiqueta legible de H con los MISMOS cortes que usa la regla."""
    if h is None:
        return None
    if h <= REGIME_HURST_LO:
        return "antipersistente"
    if h >= REGIME_HURST_HI:
        return "persistente"
    return "no_concluyente"


def regime(candles: Optional[List[Dict[str, Any]]] = None,
           lookback: int = REGIME_PCT_WINDOW,
           hurst_window: int = REGIME_HURST_WINDOW) -> Dict[str, Any]:
    """Clasifica el mercado en expansion / rango / neutro.

    Medida actual (D-079), sustituye al ratio span/cuerpo con cortes fijos
    6.0/3.0 que nunca se validaron contra datos reales:

      1. Percentil del ATR% de la última barra dentro de los últimos
         `lookback` valores: <= REGIME_P_RANGO compresión, >=
         REGIME_P_EXPANSION expansión.
      2. Hurst sobre hasta `hurst_window` cierres como guardia de
         tendencia: >= REGIME_HURST_HI fuerza "expansion" aunque la vol
         esté a la baja, y EXCLUYE de "rango" (una compresión con
         tendencia persistente no es revertible: se fada una tendencia).
         <= REGIME_HURST_LO solo etiqueta el diagnóstico ("antipersistente").

    Por qué la guardia de tendencia y no un segundo filtro de rango: el
    AND "percentil<=20 y H<=0.45" dejó "rango" en el 2.1% de las señales
    MR (regime_scan: el H<=0.45 solo cumple el 6.2% de las ventanas
    reales) y el fallback de estado retrasado tampoco rescata (2.5%) —
    ambos por debajo del criterio de no-degeneración <5% registrado antes
    de mirar resultados. Con la guardia, "rango" es compresión que no
    esté en tendencia fuerte (~31% de las señales, régimen sano).

    Degradación documentada: con menos de REGIME_MIN_BARS, con series no
    positivas o sin dispersión → "neutro" (sin datos no se clasifica: un
    estado inventado gatea en falso). Si Hurst no tiene historia (None),
    decide solo el percentil y el detalle lo dice.

    `ratio`, `span` y `avg_body` se siguen devolviendo como diagnóstico
    (backtest_mr los registra en cada trade), pero ya NO deciden.
    """
    cs = candles or []
    if not cs:
        return {"regime": REGIME_NEUTRAL, "detail": "Sin velas suficientes."}
    win = cs[-lookback:]
    if len(win) < 5:
        return {"regime": REGIME_NEUTRAL, "detail": "Velas insuficientes."}
    if len(win) < REGIME_MIN_BARS:
        return {
            "regime": REGIME_NEUTRAL,
            "detail": (f"Solo {len(win)} barras: el percentil necesita "
                       f"{REGIME_MIN_BARS}."),
        }

    hi = max(c["high"] for c in win)
    lo = min(c["low"] for c in win)
    span = hi - lo
    avg_body = sum(abs(c["close"] - c["open"]) for c in win) / len(win)
    avg_wick = sum(c["high"] - c["low"] for c in win) / len(win)
    last = win[-1]
    close_last = last["close"]

    if span <= 0 or avg_body <= 0:
        return {"regime": REGIME_NEUTRAL, "detail": "Rango o cuerpo nulos (insuficiente)."}

    ratio = span / avg_body

    atr_pct_s = _atr_pct_series(win)
    valid = [v for v in atr_pct_s if v is not None]
    current = atr_pct_s[-1]
    if current is None or len(valid) < REGIME_MIN_BARS:
        return {
            "regime": REGIME_NEUTRAL,
            "detail": "Serie de ATR% incompleta (cierres no positivos).",
            "span": round(span, 6),
            "ratio": round(ratio, 2),
            "avg_body": round(avg_body, 6),
        }

    pct = _percentile_rank(valid, current)
    h = hurst_exponent([c["close"] for c in cs], window=hurst_window)
    h_class = _hurst_class(h)

    if pct <= REGIME_P_RANGO and (h is None or h < REGIME_HURST_HI):
        reg = REGIME_RANGO
    elif pct >= REGIME_P_EXPANSION or (h is not None and h >= REGIME_HURST_HI):
        reg = REGIME_EXPANSION
    else:
        reg = REGIME_NEUTRAL

    if h is None:
        h_detail = "Hurst sin historia suficiente (decide solo el percentil)"
    else:
        h_detail = f"Hurst {h:.3f} ({h_class})"
    detail = (f"ATR% percentil {pct:.1f} (compresión<={REGIME_P_RANGO:g}, "
              f"expansión>={REGIME_P_EXPANSION:g}) · {h_detail} → {reg}; "
              f"ratio legado {ratio:.2f}.")

    return {
        "regime": reg,
        "span": round(span, 6),
        "ratio": round(ratio, 2),
        "avg_body": round(avg_body, 6),
        "atr_pct": round(avg_wick / close_last * 100.0, 3) if close_last else 0.0,
        "vol_percentile": round(pct, 2),
        "hurst": round(h, 4) if h is not None else None,
        "hurst_class": h_class,
        "lookback_used": len(win),
        "detail": detail,
    }


def _trend_coef(values: List[float]) -> float:
    """Pendiente normalizada de una regresion lineal simple (indice vs valor)."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values))
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0
    scale = max((abs(max(values)) + abs(min(values))) / 2.0, 1e-9)
    return (sxy / sxx) / scale


def cot_component(cot: Optional[Dict[str, Any]], direction: str) -> Dict[str, Any]:
    """Coincidencia del macro_bias COT con la direccion del setup.

    Sentinelas sin datos: el componente vale 0 y detalla la ausencia (el maximo
    del score baja de forma honesta).
    """
    if not cot:
        return {"value": 0.0, "detail": "Sin reporte COT (fuera de EURUSD o sin sync)."}
    bias = str(cot.get("macro_bias") or "NEUTRAL").upper()
    index = cot.get("cot_index_26w")
    if bias == "BULLISH":
        value = 1.0 if direction == "BUY" else 0.15
    elif bias == "BEARISH":
        value = 1.0 if direction == "SELL" else 0.15
    else:
        value = 0.6
    idx_txt = f"{index:.1f}%" if isinstance(index, (int, float)) else "n/d"
    return {
        "value": round(value, 2),
        "detail": f"macro_bias={bias} ({idx_txt}): {'a favor' if value >= 0.8 else ('en contra' if value <= 0.2 else 'neutral')}.",
    }


def cvd_component(cvd: Optional[List[Dict[str, Any]]], direction: str) -> Dict[str, Any]:
    """Pendiente del CVD a favor de la direccion del setup."""
    if not cvd or len(cvd) < 5:
        return {"value": 0.0, "detail": "Sin serie CVD suficiente."}
    values = [float(p.get("value", 0.0)) for p in cvd]
    slope = _trend_coef(values)
    want_up = direction == "BUY"
    if abs(slope) < 1e-6:
        value = 0.4
    elif (slope > 0) == want_up:
        value = 0.8 + min(0.2, abs(slope) * 2.0)
    else:
        value = 0.15
    return {
        "value": round(value, 2),
        "slope": round(slope, 6),
        "detail": f"Pendiente CVD {'alcista' if slope > 0 else 'bajista'} ({slope:+.6f}): {'a favor' if value >= 0.8 else ('en contra' if value <= 0.2 else 'neutral')}.",
    }


def _bull_keys(patterns: Dict[str, Any], direction: str) -> Dict[str, Any]:
    bullish_side = direction == "BUY"
    fvg_key = "BULLISH_FVG" if bullish_side else "BEARISH_FVG"
    ob_key = "BULLISH_OB" if bullish_side else "BEARISH_OB"
    sweep_key = "PDL_SWEEP" if bullish_side else "PDH_SWEEP"
    opposed_fvg = "BEARISH_FVG" if bullish_side else "BULLISH_FVG"
    opposed_ob = "BEARISH_OB" if bullish_side else "BULLISH_OB"

    fvgs = [z for z in patterns.get("fvgs", []) if z.get("type") == fvg_key]
    obs = [z for z in patterns.get("order_blocks", []) if z.get("type") == ob_key]
    sweeps = [s for s in patterns.get("sweeps", []) if s.get("type") == sweep_key]
    opposed = (
        len([z for z in patterns.get("fvgs", []) if z.get("type") == opposed_fvg])
        + len([z for z in patterns.get("order_blocks", []) if z.get("type") == opposed_ob])
    )
    return {
        "fvgs": fvgs,
        "obs": obs,
        "sweeps": sweeps,
        "opposed": opposed,
        "sweep_key": sweep_key,
    }


def smc_component(patterns: Optional[Dict[str, Any]], direction: str) -> Dict[str, Any]:
    """Confluencia SMC: FVG + Order Block + Liquidity Sweep a favor de la direccion."""
    pat = patterns or {}
    if not any(k in pat for k in ("fvgs", "order_blocks", "sweeps")):
        return {"value": 0.0, "detail": "Sin estructura SMC."}

    k = _bull_keys(pat, direction)
    has_fvg, has_ob, has_sweep = bool(k["fvgs"]), bool(k["obs"]), bool(k["sweeps"])
    if has_fvg and has_ob and has_sweep:
        value, why = 1.0, "FVG + OB + Sweep a favor"
    elif has_fvg and (has_ob or has_sweep):
        value, why = 0.7, ("FVG + " + ("OB" if has_ob else "Sweep") + " a favor")
    elif has_fvg or has_ob or has_sweep:
        value, why = 0.5, ("OB" if has_ob else ("FVG" if has_fvg else str(k["sweep_key"]))) + " a favor"
    else:
        value, why = 0.15, "Sin confluencia a favor"

    if has_sweep and k["opposed"] > 0:
        value = max(0.0, value - 0.1)

    return {
        "value": round(value, 2),
        "detail": why + f" (contrapartida estructural: {k['opposed']}).",
    }


def grade_score(score: float) -> str:
    if score >= DEFAULT_GRADE_THRESHOLDS["alta"]:
        return VERDICT_ALTA
    if score >= DEFAULT_GRADE_THRESHOLDS["media"]:
        return VERDICT_MEDIA
    return VERDICT_SIN


def invalidation_level(patterns: Optional[Dict[str, Any]], direction: str) -> Optional[float]:
    """Nivel de invalidez estructural: para BUY, el menor bottom de zonas alcistas
    vivas (FVG/OB); para SELL, el mayor top de las bajistas. None sin estructura."""
    pat = patterns or {}
    if direction == "BUY":
        lows = [float(z["bottom"]) for z in pat.get("fvgs", []) if z.get("type") == "BULLISH_FVG"]
        lows += [float(z["bottom"]) for z in pat.get("order_blocks", []) if z.get("type") == "BULLISH_OB"]
        return min(lows) if lows else None
    highs = [float(z["top"]) for z in pat.get("fvgs", []) if z.get("type") == "BEARISH_FVG"]
    highs += [float(z["top"]) for z in pat.get("order_blocks", []) if z.get("type") == "BEARISH_OB"]
    return max(highs) if highs else None


def smr_component(smr: Optional[Dict[str, Any]], direction: str) -> Dict[str, Any]:
    """Confluencia SMA/SMR via divergencia con el DXY.

    `smr` viene de smr_service.evaluate() con claves "bull"/"bear". Solo la
    divergencia confirmada aporta valor (1.0); sin divergencia o sin feed el
    componente es neutro (0.0) — nunca penaliza el score por falta de dato,
    que es el contrato explicito del bono (sumar solo si se confirma).
    """
    side = "bull" if str(direction).upper() == "BUY" else "bear"
    info = (smr or {}).get(side) or {}
    if info.get("confirmed"):
        return {
            "value": 1.0,
            "confirmed": True,
            "detail": f"Divergencia SMR {side} confirmada: {info.get('detail') or 'con DXY'}.",
        }
    if smr:
        return {
            "value": 0.0,
            "confirmed": False,
            "detail": info.get("detail") or "Sin divergencia SMR (neutro).",
        }
    return {
        "value": 0.0,
        "confirmed": False,
        "detail": "Sin lectura SMR/DXY (neutro).",
    }


def _resolve_weights(weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """Pesos efectivos: DEFAULT_WEIGHTS sobrepuestos por los de la config."""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if v is not None})
    return w


def component_share(weights: Optional[Dict[str, float]] = None,
                    key: str = "killzone") -> float:
    """Cuánto del score representa un componente, como fracción 0-1. PURO.

    El score es `sum(valor*peso)/total*100`, así que la cuota real de un
    componente es `peso/total` y cambia en cuanto cambia cualquier otro peso del
    YAML. El texto del agente decía "peso 20% del score" con un literal: en
    cuanto el YAML movió los pesos, el prompt seguía diciendo un número que el
    score ya no usaba, y eso es exactamente el tipo de garantía que el sistema no
    puede cumplir.
    """
    w = _resolve_weights(weights)
    total = sum(max(0.0, v) for v in w.values())
    if not total or key not in w:
        return 0.0
    return max(0.0, float(w[key])) / total


def setup_score(inputs: Dict[str, Any],
                weights: Optional[Dict[str, float]] = None,
                killzones: Optional[List[Dict[str, str]]] = None,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """Setup Score 0-100 con breakdown de los componentes.

    inputs:
      direction  ("BUY"/"SELL")
      cot        reporte COT o None
      cvd        serie CVD o None
      patterns   {"fvgs","order_blocks","sweeps",...} o None
      candles    velas OHLC o None
      smr        resultado de smr_service.evaluate() o None
    weights: {"cot","cvd_of","smc","killzone","smr_dxy"} en puntos; si no suman
    100 se normalizan al total efectivo.
    """
    w = _resolve_weights(weights)
    total_w = sum(max(0.0, v) for v in w.values()) or 1.0

    direction = str(inputs.get("direction") or "BUY").upper()
    kz = killzone_score(killzones, now)
    cot = cot_component(inputs.get("cot"), direction)
    cvd = cvd_component(inputs.get("cvd"), direction)
    smc = smc_component(inputs.get("patterns"), direction)
    smr = smr_component(inputs.get("smr"), direction)

    comps = {
        "cot": {**cot, "weight": w["cot"]},
        "cvd_of": {**cvd, "weight": w["cvd_of"]},
        "smc": {**smc, "weight": w["smc"]},
        "killzone": {"value": kz["value"], "weight": w["killzone"],
                     "detail": kz["detail"], "in_killzone": kz["in_killzone"],
                     "name": kz["name"]},
        "smr_dxy": {**smr, "weight": w["smr_dxy"]},
    }
    score = sum(c["value"] * c["weight"] for c in comps.values()) / total_w * 100.0

    return {
        "score": round(score, 1),
        "verdict": grade_score(score),
        "components": comps,
        "regime": regime(inputs.get("candles")),
        "weight_total": round(total_w, 1),
        "computed_at": (now or datetime.now(timezone.utc)).isoformat(),
    }


def validate_entry(
    entry: float,
    sl: float,
    target: Optional[float],
    direction: str,
    current_price: Optional[float] = None,
    invalidate_level: Optional[float] = None,
    created_at: Optional[float] = None,
    now: Optional[float] = None,
    cfg: Optional[Dict[str, Any]] = None,
    balance: Optional[float] = None,
    risk_pct: Optional[float] = None,
    loss_per_lot: Optional[float] = None,
    lot: Optional[float] = None,
) -> Dict[str, Any]:
    """Valida la entrada antes de ejecutar: R:R minimo, invalidez estructural y TTL.

    - R:R: exige (|target-entry| / |entry-sl|) >= min_rr (default 2.0).
    - Invalidez: si current_price cruzo invalidate_level (BUY <= nivel, SELL >=
      nivel) el setup murio y se rechaza con score 0.
    - TTL: si now - created_at > ttl_minutes el setup expiro.
    - Cabeza de riesgo: si se pasan loss_per_lot y lot, verifica que la perdida
      maxima no exceda balance*risk_pct/100.
    """
    c = dict(cfg or {})
    min_rr = float(c.get("min_rr") or 2.0)
    ttl_min = float(c.get("setup_ttl_minutes") or 45.0)
    now_ts = now if now is not None else datetime.now(timezone.utc).timestamp()

    rejected = False
    reasons: List[str] = []

    if sl is None or entry is None:
        rejected = True
        reasons.append("Faltan entry/SL para validar.")
    else:
        dist_s = abs(float(entry) - float(sl))
        if target is not None and dist_s > 0:
            rr = abs(float(target) - float(entry)) / dist_s
            if rr + 1e-9 < min_rr:
                rejected = True
                reasons.append(f"R:R {rr:.2f} < minimo {min_rr:.2f}.")

    if invalidate_level is not None and current_price is not None:
        if direction == "BUY" and current_price <= float(invalidate_level):
            rejected = True
            reasons.append(
                f"Estructura rota: precio {current_price} cruzo la invalidez BUY {invalidate_level}."
            )
        elif direction == "SELL" and current_price >= float(invalidate_level):
            rejected = True
            reasons.append(
                f"Estructura rota: precio {current_price} cruzo la invalidez SELL {invalidate_level}."
            )

    if created_at is not None:
        age_min = (now_ts - float(created_at)) / 60.0
        if age_min > ttl_min:
            rejected = True
            reasons.append(f"Setup expirado (TTL {ttl_min:.0f} min; edad {age_min:.1f} min).")

    if (loss_per_lot is not None and lot is not None
            and balance is not None and risk_pct is not None and risk_pct > 0):
        max_risk = float(balance) * float(risk_pct) / 100.0
        loss = abs(float(loss_per_lot)) * float(lot)
        if loss > max_risk * (1.0 + 1e-9):
            rejected = True
            reasons.append(
                f"Riesgo ${loss:.2f} excede el presupuesto ${max_risk:.2f} ({risk_pct}%)."
            )

    return {
        "approved": not rejected,
        "rejected": rejected,
        "reasons": reasons,
        "entry": entry,
        "sl": sl,
        "target": target,
        "direction": direction,
    }


def effective_risk_pct(score: Optional[float] = None,
                       verdict: Optional[str] = None,
                       cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Riesgo efectivo (% del balance) con el que se dimensiona el lote.

    `reduced_risk_pct` estaba en la config desde el principio y NO lo leía nadie:
    todo se calculaba a `risk_pct`, y un setup de probabilidad MEDIA se llevaba
    el mismo dinero que uno de probabilidad ALTA. Un parámetro de config que no
    hace nada es peor que no tenerlo: aparenta una mitigación que no existe.

    La regla, explícita:
      - sin score ni verdict (ruta manual) -> riesgo NOMINAL, y se declara que
        es nominal, porque el que pulsa el botón no ha pedido un score.
      - verdict ALTA_PROBABILIDAD -> riesgo nominal.
      - cualquier otro verdict -> riesgo reducido (`reduced_risk_pct`, o la
        mitad del nominal si no está configurado).

    Módulo puro: no lee ficheros ni el reloj. Devuelve el desglose para que la
    UI pueda declararlo en vez de dejar que se suponga.
    """
    c = dict(cfg or {})
    nominal = float(c.get("risk_pct") or 0.0)
    reduced_raw = c.get("reduced_risk_pct")
    reduced = float(reduced_raw) if reduced_raw is not None else nominal / 2.0
    v = str(verdict or "").strip().upper()

    if not v:
        out = nominal
        reason = "sin score (operación manual): riesgo nominal"
    elif v == VERDICT_ALTA:
        out = nominal
        reason = f"verdict {v}: riesgo nominal"
    else:
        out = reduced
        reason = (f"verdict {v} (no es {VERDICT_ALTA}): riesgo reducido "
                  f"de {nominal}% a {reduced}%")
    return {
        "risk_pct": round(out, 4),
        "nominal_pct": round(nominal, 4),
        "reduced_pct": round(reduced, 4),
        "reduced": out != nominal,
        "verdict": v or None,
        "score": score,
        "reason": reason,
        "label": f"riesgo {out:g}% "
                 f"({'reducido' if out != nominal else 'nominal'}"
                 f"{'' if v else ', sin score'})",
    }


def prop_firm_state(equity, day_start_balance, prev_state=None, now=None, cfg=None,
                    day_key=None):
    """Enforcement real del modo prop-firm (HWM diario + total + tope ganancia).

    Modulo PURO: recibe equity, el balance de inicio de dia y el estado previo
    persistido; devuelve las porcentajes consumidos, si queda bloqueado y el
    estado siguiente a persistir. Sin MT5 ni red.

    Reglas:
      - baseline (HWM total) se crea en la primera llamada con la equity actual.
      - HWM diario se resetea con el balance de inicio de dia cuando cambia el dia.
      - Bloquea si DD diario >= prop_max_dd_daily_pct, DD total >=
        prop_max_dd_total_pct, o ganancia del dia >= prop_max_profit_day_pct.
      - Si prop_enabled es False -> estado neutro (nada bloquea, sin persistir).

    `day_key` es la clave del "día de trading" con la que el CALLER midió el
    `day_start_balance` (tclock.trading_day: día natural del servidor del
    broker, el mismo que resetea el EA). Tiene que ser la misma clave en ambos
    lados o el HWM se reinicia con un balance de otro día. Si no se pasa, cae a
    la fecha UTC para que el módulo puro siga siendo testeable standalone.
    """
    c = dict(cfg or {})
    enabled = bool(c.get("prop_enabled"))
    max_dd_daily = float(c.get("prop_max_dd_daily_pct") or 2.0)
    max_dd_total = float(c.get("prop_max_dd_total_pct") or 6.0)
    max_profit_day = float(c.get("prop_max_profit_day_pct") or 1.5)
    consistency_days = int(c.get("prop_consistency_days") or 0)

    if not enabled:
        return {
            "enabled": False,
            "blocked": False,
            "reasons": [],
            "dd_daily_pct": None,
            "dd_total_pct": None,
            "day_profit_pct": None,
            "state": None,
        }

    dt = now if now is not None else datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    today = day_key or dt.date().isoformat()
    prev = prev_state or {}

    baseline_equity = float(prev["baseline_equity"]) if prev.get("baseline_equity") is not None else round(float(equity), 4)
    baseline_day = prev.get("baseline_day") or today
    total_peak = float(prev["total_peak"]) if prev.get("total_peak") is not None else baseline_equity
    total_peak = max(total_peak, float(equity))

    day_start = float(day_start_balance) if day_start_balance is not None else float(equity)
    if prev.get("day_peak") is None or prev.get("day_peak_day") != today:
        day_peak, day_peak_day = day_start, today
    else:
        day_peak, day_peak_day = max(float(prev["day_peak"]), float(equity)), today

    dd_daily = max(0.0, day_peak - float(equity))
    dd_total = max(0.0, total_peak - float(equity))
    dd_daily_pct = dd_daily / day_peak * 100.0 if day_peak > 0 else 0.0
    dd_total_pct = dd_total / total_peak * 100.0 if total_peak > 0 else 0.0
    day_profit = float(equity) - day_start
    day_profit_pct = day_profit / day_start * 100.0 if day_start > 0 else 0.0

    reasons: List[str] = []
    if dd_daily_pct >= max_dd_daily - 1e-9:
        reasons.append(
            f"DD diario prop {dd_daily_pct:.2f}% >= tope {max_dd_daily:.2f}% "
            f"(HWM del día {day_peak:.2f})."
        )
    if dd_total_pct >= max_dd_total - 1e-9:
        reasons.append(
            f"DD total prop {dd_total_pct:.2f}% >= tope {max_dd_total:.2f}% "
            f"(HWM total {total_peak:.2f})."
        )
    if max_profit_day > 0 and day_profit > 0 and day_profit_pct >= max_profit_day - 1e-9:
        reasons.append(
            f"Ganancia del día {day_profit_pct:.2f}% >= tope de consistencia {max_profit_day:.2f}%."
        )

    return {
        "enabled": True,
        "blocked": bool(reasons),
        "reasons": reasons,
        "dd_daily_pct": round(dd_daily_pct, 2),
        "dd_total_pct": round(dd_total_pct, 2),
        "day_profit_pct": round(day_profit_pct, 2),
        "consistency_days": consistency_days,
        "state": {
            "baseline_equity": round(baseline_equity, 4),
            "baseline_day": baseline_day,
            "day_peak": round(day_peak, 4),
            "day_peak_day": day_peak_day,
            "total_peak": round(total_peak, 4),
        },
    }