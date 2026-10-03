"""COT de la CFTC para el 6E (EURUSD).

Portado de `REF/cot_service.py`. Fuentes y reglas idénticas; lo que cambia es que
este módulo no toca la base de datos ni la red sin que se lo pidan.

Qué es el COT y por qué pesa 0 en el score
-----------------------------------------
La CFTC publica cada viernes las posiciones de los operadores clasificados por tipo: los
Asset Managers (fondos), el Leveraged Money (fondos apalancados y CTAs) y los No
Comerciales. El índice clásico de 26 semanas se calcula sobre los No Comerciales
del reporte "Legacy Futures Only" y va de 0 a 100: por debajo de 25 se considera
posición especulativa extrema *larga*, por encima de 75, extrema *corta*.

`core/risk_engine.py` tiene `DEFAULT_WEIGHTS["cot"] = 0.0`, y no es una errata: el
informe es **semanal y solo del 6E**, así que para oro, índices o yen devuelve 0 con
el detalle "Sin reporte COT" y el setup se degrada sin que nadie pueda explicar por
qué. Está documentado en el propio `risk_engine`. Este módulo devuelve `symbols =
("EURUSD",)` para que quede claro a qué se aplica, y no intenta jugar a ser útil en
los demás mercados.

Las dos fuentes y por qué hacen falta las dos
--------------------------------------------
- **TFF** (`gpe5-46if`): Asset Managers y Leveraged Money. Es el detalle moderno.
- **Legacy Futures Only** (`6dca-aqww`): No Comerciales. Es el que alimenta el
  índice de 26 semanas.

Se descargan las dos y se cruzan por fecha de reporte. Si se sustituyera el índice
clásico por una métrica del TFF, el significado de "extremo" cambiaría y dejaría de
ser comparable con lo que dice cualquier publicación que lo cite.

La precedencia del sesgo
-----------------------
1. Índice de 26 semanas válido (>= 2 semanas): <25 BULLISH, >75 BEARISH.
2. Si no, o si queda neutro: score = 0.6·norm(ΔAM) + 0.4·norm(−ΔLF), ±0.5.

El orden importa y no es arbitrario: el índice es un percentil de 26 semanas y el
score un delta de una semana. Cuando los dos se contradicen (índice en extremo pero
delta del revés) manda el índice, porque es el que tiene historia. Es la decisión
de calibración que REF ya tenía tomada y no se toca aquí.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Dict, List, Optional

from macro_ingestor.base_ingestor import (
    FeedUnreadable,
    FeedUnavailable,
    TTLCache,
    http_text,
    neutro,
    reading,
)

# --- Fuentes (Socrata de la CFTC, sin API key) -------------------------------
TFF_DATASET = "gpe5-46if"
LEGACY_DATASET = "6dca-aqww"
MARKET = "EURO FX - CHICAGO MERCANTILE EXCHANGE"
SYMBOL = "EURUSD"
BASE_URL = "https://publicreporting.cftc.gov/resource"
FETCH_LIMIT = 60
TIMEOUT = 30

# --- Calibración (idéntica a REF) -------------------------------------------
INDEX_WINDOW = 26
MIN_INDEX_WEEKS = 2
IDX_BULL_LOW = 25.0
IDX_BEAR_HIGH = 75.0
SCORE_BULL = 0.5
SCORE_BEAR = -0.5

SOURCE = "CFTC COT (Socrata)"

#: El COT es semanal y solo del 6E. Un `()` aquí haría que el score lo aplicase a
#: oro y a índices, que es justo el bug que motivó poner su peso a 0.
SYMBOLS: tuple = (SYMBOL,)


def socrata_url(dataset: str, cols: str, market: str = MARKET, limit: int = FETCH_LIMIT) -> str:
    """Construye la URL de la consulta SoQL.

Puro y sin red: los tests comprueban la URL, no el JSON de la CFTC. El nombre
    del mercado va dentro de la consulta entre comillas, así que uno con apóstrofo
    rompería la URL; se escapa con `quote` en lugar de escribirla a mano.
    """
    sql = (
        f"select {cols} where market_and_exchange_names = '{market}' "
        f"order by report_date_as_yyyy_mm_dd desc limit {limit}"
    )
    return f"{BASE_URL}/{dataset}.json?$query={urllib.parse.quote(sql)}"


def _norm(values: List[float]) -> List[float]:
    """Normaliza a [-1, 1] por min-max.

    Una serie plana devuelve todo ceros en vez de dividir por cero: sin movimiento
    no hay sesgo, que es distinto de "el sesgo es la mitad".
    """
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo == 0:
        return [0.0] * len(values)
    return [2.0 * (x - lo) / (hi - lo) - 1.0 for x in values]


def fetch_reports(opener: Any = None, limit: int = FETCH_LIMIT) -> List[Dict[str, Any]]:
    """Descarga y cruza TFF + Legacy por fecha de reporte. Serie ASCENDENTE.

    Propaga `FeedUnavailable`: el que no lanza es `poll()`. Aquí la distinción entre
    "no hay datos" y "no se pudo consultar" es lo que permite que la caché sirva su
    respaldo vencido marcado como `stale` en vez de perder la serie.

    El cruce por intersección de fechas es intencionado: si la TFF tiene una fecha
    que la Legacy no, esa fila se descarta en vez de rellenarse con un 0. Un 0 en un
    net position significa "posiciones planas", que es una afirmación sobre el
    mercado, y aquí sería mentira.
    """
    tff = _fetch(
        TFF_DATASET,
        "report_date_as_yyyy_mm_dd, asset_mgr_positions_long, asset_mgr_positions_short, "
        "lev_money_positions_long, lev_money_positions_short",
        opener,
    )
    leg = _fetch(
        LEGACY_DATASET,
        "report_date_as_yyyy_mm_dd, noncomm_positions_long_all, noncomm_positions_short_all",
        opener,
    )

    if not tff or not leg:
        return []

    try:
        tff_by_date = {r["report_date_as_yyyy_mm_dd"][:10]: r for r in tff}
        leg_by_date = {r["report_date_as_yyyy_mm_dd"][:10]: r for r in leg}
    except (KeyError, TypeError) as exc:
        raise FeedUnreadable(
            f"La CFTC devolvió una forma inesperada: {exc}"
        ) from exc

    merged: List[Dict[str, Any]] = []
    for fecha in sorted(set(tff_by_date) & set(leg_by_date), reverse=True)[:limit]:
        a, b = tff_by_date[fecha], leg_by_date[fecha]
        try:
            merged.append(
                {
                    "report_date": fecha,
                    "am_net": int(a["asset_mgr_positions_long"]) - int(a["asset_mgr_positions_short"]),
                    "lf_net": int(a["lev_money_positions_long"]) - int(a["lev_money_positions_short"]),
                    "nc_net": int(b["noncomm_positions_long_all"]) - int(b["noncomm_positions_short_all"]),
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Una fila con una columna que falta o no es número se salta, no tumba
            # la serie entera: 55 semanas de historia valen más que las 56.
            continue
    merged.reverse()
    return merged


def _fetch(dataset: str, cols: str, opener: Any = None) -> List[Dict[str, Any]]:
    """Una consulta Socrata.

    **Propaga `FeedUnavailable` a propósito.** REF devolvía `[]` cuando la fuente
    no respondía, y aquí esa idea se descartó: `TTLCache` distingue "el fetch funcionó
    y el resultado es vacío" de "el fetch falló" justamente porque son cosas
    distintas. Si un fallo de red se convierte en `[]`, la caché toma `[]` por un
    valor válido, **tira una serie buena de 55 semanas** y solo entonces, en el
    siguiente fallo, tendría su respaldo... que ya no existe. Convertir el error en
    dato vacío destruye el dato y el mecanismo que debía protegerlo.

    Lo que sí es un `[]` legítimo es "la consulta funcionó y no hay filas": eso
    significa que no había reporte nuevo, no que la CFTC se cayó.
    """
    url = socrata_url(dataset, cols)
    body = http_text(url, timeout=TIMEOUT, opener=opener)
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FeedUnreadable(f"{dataset}: JSON ilegible: {exc}") from exc
    return rows or []


def build_report(stored: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Serie cruda -> reporte derivado. `None` si no hay datos. NUNCA inventa cifras.

    Pura y sin red: es la parte que hay que testear, y la que decide si el sesgo
    que se acaba pintando se sostiene.
    """
    if not stored:
        return None

    rep = sorted(stored, key=lambda r: r["report_date"])
    latest = rep[-1]

    d_am = [rep[i]["am_net"] - rep[i - 1]["am_net"] for i in range(1, len(rep))]
    d_lf = [rep[i]["lf_net"] - rep[i - 1]["lf_net"] for i in range(1, len(rep))]
    n_am = _norm(d_am)
    # El signo se invierte en el leveraged money: un fondo apalancado que AUMENTA su
    # posición corta es una señal alcista para el euro, igual que un asset manager
    # que aumenta su larga. REF ya lo tenía así y la calibración depende de ello.
    n_lf = _norm([-x for x in d_lf])
    score = round(0.6 * n_am[-1] + 0.4 * n_lf[-1], 3) if n_am else None

    window = rep[-INDEX_WINDOW:]
    nc = [r["nc_net"] for r in window]
    lo_i, hi_i = min(nc), max(nc)
    cot_index = (
        round((latest["nc_net"] - lo_i) / (hi_i - lo_i) * 100.0, 1) if hi_i != lo_i else 50.0
    )
    index_valid = len(window) >= MIN_INDEX_WEEKS

    bias = "NEUTRAL"
    if index_valid and cot_index < IDX_BULL_LOW:
        bias = "BULLISH"
    elif index_valid and cot_index > IDX_BEAR_HIGH:
        bias = "BEARISH"
    elif score is not None and score >= SCORE_BULL:
        bias = "BULLISH"
    elif score is not None and score <= SCORE_BEAR:
        bias = "BEARISH"

    return {
        "report_date": latest["report_date"],
        "asset_managers_net": latest["am_net"],
        "leveraged_funds_net": latest["lf_net"],
        "non_commercial_net": latest["nc_net"],
        "delta_asset_managers": round(d_am[-1], 0) if d_am else None,
        "delta_leveraged_funds": round(d_lf[-1], 0) if d_lf else None,
        "score": score,
        "cot_index_26w": cot_index,
        "index_valid": index_valid,
        "macro_bias": bias,
        "symbol": SYMBOL,
    }


# --- El contrato del ingestor ------------------------------------------------

_cache = None


def _get_cache(opener: Any = None, ttl: float = 3600.0) -> "TTLCache":
    """La caché se construye en la primera llamada, no al importar.

    El COT es **semanal**: un TTL de una hora ya es de sobra, y una hora es
    suficiente para que un watcher que barre cada 30 s no martillee la CFTC. La
    instancia vive en el módulo porque el dato es el mismo para todos los
    consumidores del proceso, igual que la `Session` de MT5 en `adapters/`.

    Se construye aquí y no al importar para que un test pueda pasar su propio
    `opener` sin que quede una caché con otro opener detrás.
    """
    global _cache
    if _cache is None:
        _cache = TTLCache(fetch=lambda: fetch_reports(opener=opener), ttl=ttl)
    return _cache


def poll(opener: Any = None, stored: Optional[List[Dict[str, Any]]] = None,
         ttl: float = 3600.0) -> Dict[str, Any]:
    """Un `MacroReading` con el sesgo del COT. Nunca lanza.

    `stored` permite que el llamador pase la serie que ya tiene en la base de datos
    y se evite la red: `sync()` en REF hacía exactamente eso (descargaba, persistía,
    recalculaba desde la BD), pero aquí la persistencia es de quien llama.

    El contrato es `stored is not None`, no "si `stored` tiene algo": una lista
    VACÍA que el llamador pasa significa "consulté y no hay nada", y responder
    yendo a la red detrás de sus espaldas convertiría esa respuesta en una
    descarga incontrolada.
    """
    stale = False
    reason = ""
    causa = ""

    try:
        if stored is not None:
            reports = list(stored)
        else:
            result = _get_cache(opener=opener, ttl=ttl).get()
            reports = list(result.value or [])
            stale = bool(result.stale)
            if stale:
                reason = "COT servido de caché vencida: " + result.reason()
            causa = result.reason()
    except (FeedUnavailable, FeedUnreadable) as exc:
        return neutro(SOURCE, f"COT no disponible: {exc}", asof_ts=0)

    if not reports:
        # "No devolvió reportes" y "se cayó la red" son el mismo `payload is
        # None`, pero solo uno es un fallo. Sin la causa, quien lea el motivo
        # pensará que la CFTC no tiene nada nuevo cuando en realidad no respondió.
        return neutro(
            SOURCE,
            "CFTC no devolvió reportes: {}".format(causa or "¿fuera de mercado o sin datos nuevos?"),
        )

    try:
        report = build_report(reports)
    except (KeyError, TypeError, ValueError) as exc:
        return neutro(SOURCE, f"COT ilegible: {type(exc).__name__}: {exc}")

    if report is None:
        return neutro(SOURCE, "sin reportes almacenados")

    return reading(
        source=SOURCE,
        payload=report,
        asof_ts=_date_to_epoch(report["report_date"]),
        stale=stale,
        reason=reason,
    )


def _date_to_epoch(report_date: str) -> int:
    """`'YYYY-MM-DD'` -> epoch SEGUNDOS a medianoche UTC.

    El COT se publica a las 15:30 ET del martes para la posición del martes, así
    que poner la medianoche del día del reporte es una simplificación
    conservadora: el dato no parece más reciente de lo que es. Aclarar la hora de
    publicación exacta es un dato que este módulo no tiene y no va a inventar.
    """
    from datetime import datetime, timezone

    try:
        dt = datetime.strptime(str(report_date)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    return int(dt.timestamp())


def clear_cache() -> None:
    """Olvida la serie cacheada y su `opener`.

    Se descarta la instancia, no solo el valor: el `fetch` captura el `opener` del
    primer `poll` que lo construyó, y si la instancia sobrevive, el siguiente
    `poll(opener=...)` seguiría hablando con la fuente del anterior. Los tests
    pasarían probando lo que no es lo que creen.
    """
    global _cache
    if _cache is not None:
        _cache.invalidate()
    _cache = None


__all__ = [
    "SOURCE",
    "SYMBOLS",
    "build_report",
    "clear_cache",
    "fetch_reports",
    "poll",
    "socrata_url",
]