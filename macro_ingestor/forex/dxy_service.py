"""DXY (índice del dólar) y divergencia SMR contra el precio del EURUSD.

Portado de `REF/smr_service.py`. La lógica de detección es idéntica; lo que cambia
es de dónde salen las velas del EURUSD y qué pasa cuando el DXY no está.

Qué es el SMR
-------------
- **Alcista (BUY)**: el EURUSD rompe su mínimo previo (barrido de liquidez) Y el DXY
  NO hace máximo nuevo.
- **Bajista (SELL)**: el EURUSD rompe su máximo previo Y el DXY NO hace mínimo nuevo.

La asimetría es la huella de la acumulación o la distribución: el euro barre
liquidez pero el dólar no confirma la ruptura. Reduce los fakeouts, y esa es toda la
tesis; no hay indicador más detrás.

La regla estructural que hace el trabajo: **la ruptura por sí sola no es nada**. Con
que el EURUSD haga un nuevo mínimo y nada más, hay ruptura. Lo que la convierte en
señal es que el otro lado NO la confirme.

Por qué el DXY no es "otro par más"
-----------------------------------
No es un par negociable: es un índice ponderado por seis divisas, y cuatro de ellas
son la mitad del peso (EUR 57%). Sus velas son un promedio ponderado, no un precio de
mercado. Eso tiene una consecuencia práctica: **la correlación EURUSD/DXY no es -1**
como a veces se asume, y el coeficiente de la regla es "el DXY no rompe", no "el DXY
se mueve en contra". Por eso los umbrales son de ruptura y no de correlación.

La divergencia con el mismo par del otro lado
---------------------------------------------
`detect_smr` es pura: recibe dos series de velas y devuelve un veredicto. No sabe de
dónde salieron, no hace red y no toca MT5. En REF esto ya era así y es lo que
permite testear el detector sin red. Se conserva intacta.

Lo que se cambia respecto a REF, y por qué
------------------------------------------
REF tenía dos ataduras que este módulo ya no tiene:

1. **`smr_service` importaba `patterns_service`, que a su vez abría MT5.** Eso
   arrastraba la terminal al servicio macro: un test del detector DXY necesitaba
   MT5 instalado, y un ingestor que depende del bróker no puede degradar a neutro
   cuando el bróker se cae. Aquí las velas del EURUSD entran por parámetro.

2. **El `evaluate()` de REF re-hacía fetch de MT5 si recibía `candles` vacío, y su
   comentario reconoce el peligro**: con el proveedor sintético devolvía la cinta
   del `MarketSimulator` en su `base_price` fija (1.0985) mientras el EURUSD real
   estaba en 1.139, y el score SMR se calculaba contra una serie inventada. Aquí no
   hay proveedor sintético: quien llame decide qué velas pasa, y si no tiene velas,
   el veredicto es neutro con el motivo escrito.

La caché
--------
`TTLCache` con TTL de 120 s y reloj inyectable, más respaldo de lo viejo marcado
`stale=True`. REF hacía lo mismo pero servía la copia vencida sin marcarlo, así que
un consumidor no podía distinguir "el DXY va en 104" de "este 104 es de ayer".
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from macro_ingestor.base_ingestor import (
    FeedUnreadable,
    TTLCache,
    http_text,
    neutro,
    reading,
)

DXY_TICKER = "DX-Y.NYB"
SOURCE = "Yahoo Finance DX-Y.NYB"

#: TTL corto a propósito: el DXY se mueve durante la sesión y se compara contra un
#: EURUSD que también se mueve. Un valor largo compararía dos series de distinta edad,
#: que es como se fabrica una divergencia que no ocurrió.
CACHE_TTL = 120.0

DEFAULT_TIMEFRAME = "M15"
DEFAULT_LOOKBACK = 8

# Yahoo usa segundos con sufijo; el proyecto usa nombres cortos con prefijo. El
# mapa vive aquí, no en `adapters/base_adapter.py`, porque es vocabulario del
# proveedor y el núcleo no debe saber que existe.
_YAHOO_INTERVAL = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "60m",
    "H4": "4h",
    "D1": "1d",
    "W1": "1wk",
}

# Yahoo limita el histórico intradía: 1m son 7-8 días, 5m/15m/30m unas 60 sesiones,
# 60m unas 730. Pedir más de lo que hay devuelve vacío y el módulo degrada a neutro,
# que es preferible a fingir que hay historia.
_YAHOO_PERIOD = {
    "M1": "8d",
    "M5": "60d",
    "M15": "60d",
    "M30": "60d",
    "H1": "730d",
    "H4": "730d",
    "D1": "1y",
    "W1": "5y",
}

#: Duración de cada timeframe en segundos, para saber si una vela ya ha cerrado.
#: Solo se usa para eso: `align_on_time` necesita saber si la última vela sigue
#: formándose, y eso se deduce del reloj, no del nombre del timeframe.
_PERIOD_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

#: El DXY pesa en el sesgo de los pares con dólar, que para este proyecto es
#: EURUSD. Para B3 o BTC no significa nada, así que se declara acotado. Sin
#: comodín: `"*"` sugeriría que aplica a cualquier símbolo con dólar, y no es
#: cierto, porque el detector compara contra EURUSD y no contra el símbolo recibido.
SYMBOLS: tuple = ("EURUSD",)


def validate_timeframe(timeframe: str) -> str:
    """Valida el timeframe contra el mapa de Yahoo.

    `ValueError` y no `IngestorError`: un timeframe mal escrito es un bug del
    llamador, igual que en `adapters/base_adapter.resolve_timeframe`. Mezclar las dos
    familias haría que un typo acabara diciendo "la fuente no está disponible".
    """
    tf = (timeframe or DEFAULT_TIMEFRAME).upper()
    if tf not in _YAHOO_INTERVAL:
        raise ValueError(
            f"timeframe {timeframe!r} no válido. Usa uno de: {', '.join(_YAHOO_INTERVAL)}."
        )
    return tf


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------


def fetch_dxy(timeframe: str = DEFAULT_TIMEFRAME, opener: Any = None) -> List[Dict[str, Any]]:
    """Velas OHLC del DXY en la forma de `adapters.base_adapter`.

    `DX-Y.NYB` es el ticker de Yahoo para el Dollar Index. Se usa Yahoo y no
    Databento porque Databento no cubre índices al contado y porque esto solo
    necesita unas pocas velas para decidir si hay ruptura.

    Devuelve la MISMA forma que devuelve MT5: `{"time","open","high","low","close",
    "volume"}` con `time` en segundos epoch. Reutilizar la forma de
    `adapters.base_adapter` es lo que permite que `detect_smr` no sepa de dónde
    salieron las velas... salvo por `time`, que sí importa: ver `align_on_time`.
    """
    tf = validate_timeframe(timeframe)
    body = http_text(_chart_url(tf), timeout=30.0, opener=opener)
    return parse_yahoo_chart(body)


def _chart_url(tf: str) -> str:
    """URL del gráfico de Yahoo para el rango pedido.

    Sin `opener`: el seam de red es `http_text`, que ya lo recibe. Un parámetro
    que se ignora en la propia firma es una trampa para quien lea que puede
    inyectarlo aquí.
    """
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{DXY_TICKER}"
        f"?interval={_YAHOO_INTERVAL[tf]}&range={_YAHOO_PERIOD[tf]}"
    )


def parse_yahoo_chart(body: str) -> List[Dict[str, Any]]:
    """Respuesta del chart endpoint -> velas normalizadas.

    Puro: los tests le pasan un JSON grabado y comparan velas, sin red.

    Descarta los huecos (`null`) en lugar de rellenarlos a 0.0. Yahoo omite las
    velas de fin de semana y días festivos, y un 0.0 en la serie de un índice
    destruiría el `min`/`max` que usa `detect_smr` para decidir una ruptura.

    Omitir la vela también es lo correcto para alinear: un hueco es una ausencia
    real de cotización, no un precio, y `align_on_time` simplemente lo descarta.
    """
    import json

    try:
        data = json.loads(body)
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise FeedUnreadable(f"Respuesta de Yahoo ilegible: {type(exc).__name__}: {exc}") from exc

    candles: List[Dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = (quote.get(k, [None] * len(timestamps))[i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            # Hueco de calendario (fin de semana, holiday). Se salta la vela.
            continue
        volume = quote.get("volume", [0] * len(timestamps))[i]
        candles.append(
            {
                "time": int(ts),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(volume or 0.0),
            }
        )
    return candles


# ---------------------------------------------------------------------------
# El detector: puro, sin red
# ---------------------------------------------------------------------------


def swing_break(
    window: List[Dict[str, Any]],
    prev: List[Dict[str, Any]],
    side: str,
) -> Dict[str, Any]:
    """¿La última vela rompe un extremo de las anteriores?

    `side="low"`: ¿hace nuevo mínimo (por debajo del mínimo de `prev`)?
    `side="high"`: ¿hace nuevo máximo?

    `margin` es el salto en unidades de precio. Va en el resultado porque sin él no
    se puede distinguir "rompió por una centésima" de "rompió con fuerza", y el
    post-mortem necesita esa diferencia para calibrar.
    """
    cur = window[-1]
    if side == "low":
        prev_extreme = min(c["low"] for c in prev)
        break_price = cur["low"]
        margin = prev_extreme - break_price
    else:
        prev_extreme = max(c["high"] for c in prev)
        break_price = cur["high"]
        margin = break_price - prev_extreme
    return {
        "broke": bool(margin > 0),
        "margin": round(float(margin), 6),
        "level": round(float(prev_extreme), 6),
        "price": round(float(break_price), 6),
    }


def _best_shift(
    eu_times: Dict[int, Any],
    dxy_times: Dict[int, Any],
    period: Optional[int],
    max_shift: int = 2,
) -> int:
    """Desfase constante, en velas, que maximiza el solape. `0` si no se puede saber.

    Existe por un caso concreto: un proveedor etiqueta las velas por su **apertura** y
    el otro por su **cierre**, y entonces todas las horas están desplazadas una vela.
    Es un desfase *constante*, así que se puede detectar: se cuentan las
    coincidencias para los candidatos -2..+2 y se queda con la mejor.

    Importa porque el fallo es silencioso y con signo. Sin esta comprobación, al
    realinear por intersección el detector se queda con una ventana vieja pero
    completa, y declara una divergencia calculada sobre periodos que no coinciden con
    las velas que se están mirando. El módulo prefiere callar.
    """
    if not period:
        return 0
    best_k, best_n = 0, -1
    for k in range(-max_shift, max_shift + 1):
        offset = k * period
        n = sum(1 for t in eu_times if (t + offset) in dxy_times)
        if n > best_n:
            best_k, best_n = k, n
    return best_k


def align_on_time(
    eurusd: List[Dict[str, Any]],
    dxy: List[Dict[str, Any]],
    period: Optional[int] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Intersecta EURUSD y DXY por timestamp. Puro.

    Sin esto, `detect_smr` comparaba las últimas N velas **por posición**, y esa es
    la forma corta de fabricar una divergencia que no ocurrió. Tres formas de que
    las dos series tengan la misma longitud y no cubran el mismo tiempo:

    1. **Huecos distintos.** El DXY cotiza en la NYSE y el EURUSD no. En un festivo
       de Nueva York el EURUSD tiene velas y el DXY no, así que `dxy[-1]` es un
       cierre viejo y `dxy[-n-1:]` cubre una ventana desplazada respecto a la del
       EURUSD. Los dos `min`/`max` se calculan sobre periodos que no coinciden.
    2. **Última vela formándose.** La vela en curso del bróker y la vela cerrada de
       Yahoo no son comparables. Aquí se descarta la vela que todavía no ha cerrado
       cuando se pasan `period` y `now`; sin ellos no se descarta nada, porque
       adivinar el periodo sería adivinar la respuesta.
    3. **Sesiones distintas.** NYSE y FX no abren ni cierran a la misma hora, así
       que en intradía corto la última vela de una es de hace bastante más que la
       de la otra aunque el reloj coincida.

    Se elige **intersección** y no "la más reciente común": descartar la serie
    menor es lo conservador. Ante la duda, `confirmed=False`; el sesgo de DXY pesa
    15 sobre 100 y perder una señal cuesta menos que operar una falsa.

    `shift` informa del desfase constante detectado (ver `_best_shift`). No se
    corrige en silencio: quien llama decide.
    """
    eu_by = {int(c["time"]): c for c in eurusd if c.get("time") is not None}
    dxy_by = {int(c["time"]): c for c in dxy if c.get("time") is not None}

    shift = _best_shift(eu_by, dxy_by, period)
    shift_works = shift != 0

    common = sorted(set(eu_by) & set(dxy_by))
    forming = 0
    if period and now:
        keep = [t for t in common if t + int(period) <= int(now)]
        forming = len(common) - len(keep)
        common = keep

    return {
        "eurusd": [eu_by[t] for t in common],
        "dxy": [dxy_by[t] for t in common],
        "common": len(common),
        # Lo que se ha quedado fuera por no existir en la otra serie, que es el
        # número que explica por qué una serie larga puede quedarse corta.
        "unmatched": len(eu_by) + len(dxy_by) - 2 * len(common) - forming,
        "dropped_forming": forming,
        "shift": shift,
        "shift_works": shift_works,
        "first": common[0] if common else None,
        "last": common[-1] if common else None,
    }


def detect_smr(
    eurusd_candles: Optional[List[Dict[str, Any]]],
    dxy_candles: Optional[List[Dict[str, Any]]],
    direction: str = "BUY",
    lookback: int = DEFAULT_LOOKBACK,
    period: Optional[int] = None,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Divergencia SMR. Devuelve SIEMPRE un dict con la misma forma; nunca lanza.

    Alinea por tiempo antes de comparar (ver `align_on_time`): comparar por posición
    es el error que hace que esto funcione en un backtest de laboratorio y falle en
    producción un martes de festivo.

    Lanza `ValueError` solo si la dirección no es BUY ni SELL: eso es un bug del
    llamador y hay que verlo. La falta de datos, en cambio, devuelve
    `confirmed=False` con el motivo, porque "no lo sé" y "no es una señal" llevan a
    la misma acción pero a lecturas de post-mortem distintas.
    """
    d = str(direction or "BUY").upper()
    if d not in ("BUY", "SELL"):
        raise ValueError(f"direction debe ser BUY o SELL, no {direction!r}.")

    if not eurusd_candles:
        return _no("Sin velas de EURUSD.")
    if not dxy_candles:
        return _no("Sin datos del DXY (feed degradado a neutro).")

    n = max(2, int(lookback))
    aligned = align_on_time(eurusd_candles, dxy_candles, period=period, now=now)
    eu = aligned["eurusd"]
    dxy = aligned["dxy"]

    if aligned["shift_works"]:
        return _no(
            f"Desfase constante de {aligned['shift']:+.0f} vela(s) entre EURUSD y "
            "DXY: las dos fuentes etiquetan las velas distinto (apertura contra "
            "cierre). No se realinea en silencio porque el signo del desfase "
            "decidiría el resultado. Verificar `time` en ambos feeds."
        )
    if aligned["common"] == 0:
        return _no(
            "EURUSD y DXY no comparten ni un timestamp ("
            f"EUR {aligned['unmatched']} velas sin pareja). Sin hora común no hay "
            "divergencia que se pueda afirmar."
        )
    if len(eu) < n + 1 or len(dxy) < n + 1:
        return _no(
            f"Velas insuficientes tras alinear por hora "
            f"(comunes {aligned['common']}, sin pareja {aligned['unmatched']}, "
            f"formándose {aligned['dropped_forming']}; hacen falta {n + 1})."
        )

    # `eu_win` incluye la vela actual; `eu_prev` son las n anteriores. La ruptura se
    # mide contra un máximo/mínimo calculado EXCLUYENDO la vela actual, o cualquier
    # vela rompería siempre su propio extremo.
    eu_win, eu_prev = eu[-n - 1:], eu[-n - 1:-1]
    dxy_win, dxy_prev = dxy[-n - 1:], dxy[-n - 1:-1]

    if d == "BUY":
        eu_break = swing_break(eu_win, eu_prev, "low")
        dxy_break = swing_break(dxy_win, dxy_prev, "high")
        label = "alcista"
        eu_word, dxy_word = "mínimo", "máximo"
    else:
        eu_break = swing_break(eu_win, eu_prev, "high")
        dxy_break = swing_break(dxy_win, dxy_prev, "low")
        label = "bajista"
        eu_word, dxy_word = "máximo", "mínimo"

    data = {
        "eurusd": eu_break,
        "dxy": dxy_break,
        "lookback": n,
        "direction": d,
        "aligned": aligned,
    }
    confirmed = bool(eu_break["broke"]) and not bool(dxy_break["broke"])

    if confirmed:
        detail = (
            f"Divergencia SMR {label}: EURUSD quiebra su {eu_word} previo "
            f"({eu_break['price']:.5f}) y el DXY NO quiebra su {dxy_word} previo "
            f"({dxy_break['level']:.3f})."
        )
    else:
        detail = (
            f"Sin divergencia SMR: EURUSD {'quiebra' if eu_break['broke'] else 'NO quiebra'} "
            f"su {eu_word} previo y el DXY {'quiebra' if dxy_break['broke'] else 'NO quiebra'} el suyo."
        )
    return {"confirmed": confirmed, "detail": detail, "data": data}


def _no(reason: str) -> Dict[str, Any]:
    return {"confirmed": False, "detail": reason, "data": None}


# ---------------------------------------------------------------------------
# El contrato del ingestor
# ---------------------------------------------------------------------------

#: Una caché por timeframe, no una sola. Se podría compartir porque el TTL es
#: corto, pero `poll(timeframe="H1")` alinearía velas de H1 contra DXY de M15 y el
#: resultado no sería una señal degradada: sería una señal calculada sobre dos
#: escalas de tiempo distintas.
_caches: Dict[str, TTLCache] = {}


def _get_cache(
    timeframe: str = DEFAULT_TIMEFRAME,
    opener: Any = None,
    ttl: float = CACHE_TTL,
) -> TTLCache:
    global _caches
    if timeframe not in _caches:
        _caches[timeframe] = TTLCache(
            fetch=lambda: fetch_dxy(timeframe, opener=opener),
            ttl=ttl,
        )
    return _caches[timeframe]


def poll(
    symbol: str = "EURUSD",
    timeframe: str = DEFAULT_TIMEFRAME,
    candles: Optional[List[Dict[str, Any]]] = None,
    opener: Any = None,
    lookback: int = DEFAULT_LOOKBACK,
    ttl: float = CACHE_TTL,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Un `MacroReading` con el veredicto SMR para ambas direcciones.

    `candles` son las del símbolo analizado. Si vienen vacías o `None`, el veredicto
    es neutro: **no** se re-consulta MT5, porque un ingestor que depende del bróker
    no puede degradar cuando el bróker se cae, y el comentario de REF sobre el
    proveedor sintético documenta justo ese fallo.

    `now` se usa para descartar la vela en curso antes de comparar (ver
    `align_on_time`). Por defecto es la hora actual; los tests pasan una fija para
    que el resultado no dependa del minuto en que se ejecuta.
    """
    sym = (symbol or "EURUSD").upper()

    try:
        tf = validate_timeframe(timeframe)
    except ValueError as exc:
        return neutro(SOURCE, f"timeframe inválido: {exc}")

    result = _get_cache(tf, opener=opener, ttl=ttl).get()
    dxy_candles = result.value or []

    if not dxy_candles:
        return neutro(SOURCE, f"Yahoo no devolvió velas del DXY: {result.reason() or 'respuesta vacía'}")

    period = _PERIOD_SECONDS[tf]
    now_ts = int(time.time()) if now is None else int(now)

    bull = detect_smr(candles or [], dxy_candles, "BUY", lookback, period=period, now=now_ts)
    bear = detect_smr(candles or [], dxy_candles, "SELL", lookback, period=period, now=now_ts)

    payload = {
        "symbol": sym,
        "timeframe": tf,
        "dxy_available": True,
        "dxy_last_close": dxy_candles[-1]["close"],
        "dxy_candles": len(dxy_candles),
        "bull": bull,
        "bear": bear,
        "lookback": lookback,
    }
    reason = "DXY servido de caché vencida: " + result.reason() if result.stale else ""
    return reading(
        source=SOURCE,
        payload=payload,
        asof_ts=int(dxy_candles[-1]["time"]),
        stale=result.stale,
        reason=reason,
    )


def clear_cache() -> None:
    """Olvida el DXY cacheado, en todos los timeframes. Para tests y refresco."""
    for cache in _caches.values():
        cache.invalidate()
    _caches.clear()


__all__ = [
    "SOURCE",
    "SYMBOLS",
    "align_on_time",
    "clear_cache",
    "detect_smr",
    "fetch_dxy",
    "parse_yahoo_chart",
    "poll",
    "swing_break",
    "validate_timeframe",
]