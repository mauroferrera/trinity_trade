"""Mercado, cuenta y análisis. Todo lo que se lee del bróker.

Reglas de este fichero
----------------------
1. **Delgada de verdad.** Aquí no hay lógica de mercado: ni SMC, ni score, ni
   footprint. Eso vive en `core/` (puro) y en `api/services/`. Una ruta que
   calcula es una ruta que no se puede testear sin FastAPI.
2. **Los errores son de `api.errors`.** Cada ruta deja subir `AdapterError`,
   `SymbolNotFound` o `IngestorError` y los handlers los traducen a 503/404/502
   con el motivo. REF envolvía cada endpoint en `try/except Exception` y devolvía
   `500` con el texto de la excepción, así que un símbolo inexistente (un error
   del usuario) y MT5 apagado (un error de infraestructura) llegaban al frontend
   como el mismo `500`.
3. **Sin `try/except` para "devolver algo".** Cuando no hay dato se devuelve un
   estado con el motivo (`404` con el texto, o un dict con `error`), no un `200`
   con un dict vacío que la UI pinta como "todo en cero".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import deps
from ..runtime import Runtime
from ..services.mt5_market import (
    DEFAULT_BARS,
    DEFAULT_SYMBOL,
    DEFAULT_TIMEFRAME,
    simbolo as _simbolo,
)

router = APIRouter(tags=["mercado"])


# -- cuenta y posiciones --------------------------------------------------------



@router.get("/api/account", response_model=None)
def account(market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """Balance, equity, flotante y margen de la cuenta."""
    return market.account_info()


@router.get("/api/positions", response_model=None)
def positions(market: Any = Depends(deps.market)) -> List[Dict[str, Any]]:
    """Posiciones abiertas. `[]` es una respuesta legítima: no tener ninguna."""
    return market.positions()


@router.get("/api/position/{ticket}", response_model=None)
def position(ticket: int, market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """Una posición por ticket. 404 si ya no existe."""
    datos = market.position(ticket)
    if datos is None:
        raise HTTPException(status_code=404, detail="posición {0} no encontrada".format(ticket))
    return datos


@router.get("/api/history", response_model=None)
def history(
    days: int = Query(7, ge=1, le=365),
    market: Any = Depends(deps.market),
) -> List[Dict[str, Any]]:
    """Operaciones cerradas de los últimos `days` días, consolidadas por posición.

    `days` está acotado a 365 porque el endpoint no pagina y una consulta de diez
    años devuelve la tabla entera en memoria para pintarse en una tabla.
    """
    return market.history(days)


@router.get("/api/risk/daily", response_model=None)
def risk_daily(market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """Estado diario: DD, operaciones de hoy, topes alcanzados y bloqueos."""
    return market.daily_risk_state()


# -- precios y velas ------------------------------------------------------------


@router.get("/api/price/{symbol}", response_model=None)
def price(symbol: str, market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """Bid/ask en vivo. 404 si el bróker no publica el símbolo."""
    datos = market.price(_simbolo(symbol))
    if datos is None:
        raise HTTPException(
            status_code=404,
            detail="sin precio para {0}: ¿está en Market Watch?".format(_simbolo(symbol)),
        )
    return datos


@router.get("/api/candles/{symbol}", response_model=None)
def candles(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(DEFAULT_BARS, ge=10, le=1000),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Velas normalizadas (tiempo en SEGUNDOS epoch) y el rango pedido."""
    simbolo = _simbolo(symbol)
    serie = market.candles(simbolo, timeframe, bars)
    if not serie:
        raise HTTPException(
            status_code=404, detail="sin velas de {0} {1}".format(simbolo, timeframe)
        )
    return {
        "symbol": simbolo,
        "timeframe": timeframe,
        "bars": bars,
        "candles": serie,
        "pdh": max(c["high"] for c in serie),
        "pdl": min(c["low"] for c in serie),
    }


@router.get("/api/candle/last/{symbol}", response_model=None)
@router.get("/api/candle/last", response_model=None)
def candle_last(
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """La última vela cerrada y la forming, para el ticker.

    Se devuelve el resultado de `candles()` recortado: la UI del ticker no
    necesita 300 velas por segundo, y pedirlas todas hacía que el precio de
    pantalla llegara tarde por culpa del gráfico que nadie está mirando.

    El símbolo va en la ruta (`/api/candle/last/EURUSD`) porque es lo que llama el
    JS de REF, y también en query porque es como se lee en el resto de la API. Las
    dos formas dan lo mismo: con la ruta, `_simbolo` la normaliza.
    """
    simbolo = _simbolo(symbol)
    serie = market.candles(simbolo, timeframe, 3)
    if not serie:
        raise HTTPException(
            status_code=404, detail="sin velas de {0} {1}".format(simbolo, timeframe)
        )
    return {
        "symbol": simbolo,
        "timeframe": timeframe,
        "last_closed": serie[-2] if len(serie) > 1 else None,
        "forming": serie[-1],
    }


@router.get("/api/symbols", response_model=None)
def symbols(rt: Runtime = Depends(deps.runtime)) -> List[str]:
    """Los símbolos que publica el bróker. `503` si no hay terminal."""
    if rt.symbols is None:
        raise HTTPException(status_code=503, detail="proveedor de símbolos no cableado")
    return list(rt.symbols.adapter.symbols())


@router.get("/api/spec/{symbol}", response_model=None)
def spec(symbol: str, market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """El `SymbolSpec` del bróker (digits, point, tick value, stops).

    404 y no `null`: el frontend necesita distinguir "este símbolo no existe" de
    "existe y el bróker no publica su spec", porque lo primero se arregla adding el
    símbolo y lo segundo no.

    Acepta las tres formas que puede dar un puerto: un `SymbolSpec` (lo que da el
    adaptador), un dict ya serializado (un doble, o un puerto cacheado) o cualquier
    otra cosa, que se devuelve como texto. La primera es la normal; las otras dos
    están porque la ruta no debe ser la que rompe al cambiar de implementación.
    """
    simbolo = _simbolo(symbol)
    dato = market.spec(simbolo)
    if dato is None:
        raise HTTPException(status_code=404, detail="sin spec para {0}".format(simbolo))
    if isinstance(dato, dict):
        return dato
    as_dict = getattr(dato, "as_dict", None)
    if callable(as_dict):
        return as_dict()
    return {"spec": str(dato)}


# -- patrones y snapshot --------------------------------------------------------


@router.get("/api/analysis/patterns/{symbol}", response_model=None)
def patterns(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(DEFAULT_BARS, ge=10, le=1000),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """FVG, order blocks y sweeps del símbolo. 404 si no hay velas."""
    simbolo = _simbolo(symbol)
    data = market.pattern_data(simbolo, timeframe, bars)
    if data is None:
        raise HTTPException(
            status_code=404, detail="sin datos de {0} {1}".format(simbolo, timeframe)
        )
    return data


@router.get("/api/analysis/chart-assistant/{symbol}", response_model=None)
@router.get("/api/chart-assistant/{symbol}", response_model=None)
def chart_assistant(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Snapshot completo: precio, PDH/PDL, velas, SMC, CVD y score del risk engine.

    Con prefijo `/api/analysis/` y sin él, porque el JS de REF pide la primera y el
    resto de la API usa la segunda. Es una línea de alias, no dos rutas.
    """
    return market.chart_snapshot(_simbolo(symbol), timeframe)


@router.get("/api/analysis/chartism/{symbol}", response_model=None)
@router.get("/api/chartism/{symbol}", response_model=None)
def chartism(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(DEFAULT_BARS, ge=10, le=1000),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Alias en inglés de `/api/analysis/patterns`, que es lo que llama el JS de REF.

    Se mantiene el nombre porque el frontend heredado lo pide así y renombrarlo
    obliga a tocar `main.js` (195 KB) en la misma fase en la que se está sacando el
    monolito. Duplicar una ruta de una línea es más barato que dos cambios de
    contrato a la vez.
    """
    return patterns(symbol, timeframe, bars, market)


# -- vistas derivadas (puras, sobre velas) --------------------------------------


def _velas_o_404(market: Any, simbolo: str, timeframe: str, bars: int) -> List[Dict[str, Any]]:
    serie = market.candles(simbolo, timeframe, bars)
    if not serie:
        raise HTTPException(
            status_code=404, detail="sin velas de {0} {1}".format(simbolo, timeframe)
        )
    return serie


@router.get("/api/analysis/footprint/{symbol}", response_model=None)
def footprint(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(150, ge=10, le=1000),
    bid_ratio: float = 0.6,
    rows: int = Query(12, ge=6, le=24),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Footprint por barra, MODELADO desde OHLCV mientras no haya cinta real.

    El modelado es explícito en el nombre de la función de `core.market_view` y el
    `400` de un `bid_ratio` fuera de rango sale del propio motor. La alternativa
    (devolver el footprint vacío cuando no hay feed) es un gráfico en blanco que no
    dice por qué está en blanco.
    """
    from core import market_view

    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    return market_view.footprint(serie, max_bars=bars, bid_ratio=bid_ratio, rows=rows)


@router.get("/api/analysis/heatmap/{symbol}", response_model=None)
def heatmap(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(150, ge=10, le=1000),
    levels: int = Query(64, ge=32, le=128),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Mapa de calor de liquidez detrás del precio, modelado desde OHLCV."""
    from core import market_view

    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    return market_view.liquidity_heatmap(serie, max_bars=bars, levels=levels)


@router.get("/api/analysis/cvd/{symbol}", response_model=None)
def analysis_cvd(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(300, ge=10, le=2000),
    market: Any = Depends(deps.market),
) -> List[Dict[str, Any]]:
    """CVD del simbolo/timeframe activo, acumulado del tick volume de MT5.

    Es el CVD **sintetico** y por eso se devuelve sin etiqueta ni `source`: el
    frontend lo pinta como una linea mas del grafico, y si el motor de cinta esta
    encendido el CVD que se dibuja es el live (lo elige `pick_cvd_source` dentro del
    snapshot). La serie se construye con `build_cvd_series`, que es pura; la ruta
    solo lee velas y la llama.

    Sin velas es un 404 con el motivo, no una lista vacia: una serie sin puntos es
    indistinguible de "el CVD va plano" en un grafico.
    """
    from core.orderflow_engine import build_cvd_series

    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    puntos = build_cvd_series(serie)
    if not puntos:
        raise HTTPException(
            status_code=404,
            detail="sin velas de {0} en {1} para acumular el CVD".format(simbolo, timeframe),
        )
    return puntos


@router.get("/api/analysis/smr/{symbol}", response_model=None)
def analysis_smr(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(300, ge=10, le=2000),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Divergencia SMR (DXY) para el simbolo/timeframe: reglas alcista y bajista.

    La fuente del DXY es Yahoo Finance (DX-Y.NYB) y la consulta la hace el
    ingestor, no la ruta. Sin feed la respuesta es el veredicto neutro con su
    motivo (`confirmed: False`), NO un error: el SMR es un extra del score y tumbar
    el panel entero porque Yahoo no responde seria untruecar el panel entero porque
    un extra no llega.
    """
    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    return market.smr(simbolo, timeframe, serie)


@router.get("/api/volume-profile/{symbol}", response_model=None)
def volume_profile(
    symbol: str,
    timeframe: str = "H1",
    bins: int = Query(48, ge=8, le=200),
    poc_pct: float = Query(70.0, ge=1.0, le=100.0),
    bars: int = Query(300, ge=50, le=2000),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Volume Profile del symbol/timeframe: {bins, profile, poc, vah, val, source}.

    REF tenia dos fuentes (ticks de MT5 y, si no habia, barras OHLCV). Aqui solo hay
    la segunda: el proveedor de ticks es el feed que sigue sin existir, y fingir que
    hay dos fuentes cuando solo hay una produce un `source` que no significa nada.
    Cuando llegue el feed, es anadir el camino de ticks y cambiar el `source`, sin
    tocar el shape.

    `bars` sale de la ruta y no de un default de REF porque el perfil se calcula
    sobre la serie que se lee: si el grafico esta en D1 y el perfil en H1 con las
    mismas 300 velas, los dos dibujan precios distintos y el usuario cree que el
    POC se ha movido.
    """
    from core import market_view

    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    return market_view.volume_profile(serie, bins=bins, poc_pct=poc_pct)


@router.get("/api/analysis/eventbars/{symbol}", response_model=None)
def event_bars(
    symbol: str,
    timeframe: str = DEFAULT_TIMEFRAME,
    bars: int = Query(200, ge=10, le=1000),
    mode: str = "volbar",
    param: int = Query(500, ge=1, le=100000),
    ticks: int = Query(160, ge=8, le=960),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Barras por evento (volbar/tickbar/renko) desde OHLCV."""
    from core import market_view

    simbolo = _simbolo(symbol)
    serie = _velas_o_404(market, simbolo, timeframe, bars)
    return market_view.event_bars(serie, bar_type=mode, param=param, ticks_per_candle=ticks)


__all__ = ["router"]