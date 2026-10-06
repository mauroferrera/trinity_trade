"""El score del setup y su evaluación, en HTTP.

Dos rutas y un solo cálculo
--------------------------
`/api/risk/setup` publica el **score**:Setup Score por dirección, régimen, killzone e
invalidez. `/api/chart/setup-eval` publica además el **plan**: entrada, SL, TP, si está
aprobado y el overlay para dibujarlo.

Las dos leen el mismo snapshot y el mismo gate (`core.setup_gate`). Que compartan la
aritmética no es una casualidad: REF las tenía separadas y el mismo número medido en
la ruta del panel y en la del botón de entrada acababa divergiendo, porque cada una
reconstruía el plan por su cuenta. Aquí una ruta que dibuja una bandera y una que
declara un veredicto no pueden discrepar sobre qué es un setup válido.

Reglas de este fichero
----------------------
1. **Delgadas.** Ni score, ni zonas, ni R:R: eso es `core/setup_gate.py`. Aquí solo
   leer puertos y traducir errores.
2. **Los errores son de `api.errors`.** `SymbolNotFound` sube y sale 404, `AdapterError`
   sale 503, `ConfigUnavailable` sale 503. No hay `try/except Exception` devolviendo un
   `200` con un dict de error, porque un `200` con `error` es un fallo que la UI pinta
   como un resultado.
3. **`approved` NO autoriza a operar.** Es el veredicto del setup (score, killzone,
   zona alcanzable y R:R). Las puertas que bloquean de verdad —noticias, límites de
   cuenta, contrato del símbolo— viven en la ejecución y en el watcher, que es donde
   se conoce la hora real de la orden. Por eso `news_blackout` se publica pero no
   filtra: se informa y no se finge.

El parámetro `synthetic`
------------------------
El frontend lo envía porque en REF el `MarketSimulator` podía alimentar estas rutas.
Aquí no hay simulador cableado, y fingir que lo hay haría que un flag de test devolviera
un `200` con datos inventados. `synthetic=1` es por tanto un `503` explícito: la
funcionalidad no está, y decirlo es más barato que descubrirlo en un setup pintado.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import deps
from ..services import setup_eval
from ..services.mt5_market import DEFAULT_SYMBOL, DEFAULT_TIMEFRAME, simbolo

router = APIRouter(tags=["setup"])

#: Motivo único para pedir datos sintéticos. Sin proveedor de ticks (6E) no hay
#: `MarketSimulator` al que apuntar, y un `synthetic=1` que devolviera velas reales sería
#: peor que un error: el usuario creería estar probando el flag y estaría mirando el
#: mercado real como si fuera una simulación.
SIN_SINTETICOS = (
    "synthetic=1 no está disponible: no hay proveedor de ticks ni MarketSimulator "
    "cableados en esta fase (6E). El snapshot es siempre de datos reales del bróker."
)


def _rechaza_sintetico(synthetic: Optional[bool]) -> None:
    if synthetic:
        raise HTTPException(status_code=503, detail=SIN_SINTETICOS)


def _snapshot(market: Any, nombre: str, timeframe: str, force_killzone: bool = False) -> Dict[str, Any]:
    """El snapshot, o un 422 con el motivo si no trae scores.

    El `SymbolNotFound` y el `AdapterError` de aquí NO se tocan: los traduce
    `api.errors` a 404 y 503, y es información que el frontend ya sabe pintar.
    """
    snap = market.chart_snapshot(nombre, timeframe, force_killzone=force_killzone)
    if not isinstance(snap, dict) or not snap.get("risk_engine"):
        raise HTTPException(
            status_code=422,
            detail="risk engine no disponible para este snapshot (sin velas o sin scores)",
        )
    return snap


def _spec(market: Any, nombre: str) -> Any:
    """El `SymbolSpec` del símbolo, o `None`. Nunca inventado.

    `None` degrada con honestidad: los niveles se redondean a 6 decimales y los motivos
    se dan en distancia de precio en vez de "pips". Un spec inventado daría un número
    con unidades equivocadas en oro, que es el mercado donde más caro sale.
    """
    try:
        return market.spec(nombre)
    except Exception:  # noqa: BLE001 - el spec es presentación, no veredicto
        return None


def _noticias(news: Any) -> Optional[Dict[str, Any]]:
    """El veredicto de noticias, o `None` si el puerto no está cableado.

    No se propaga el fallo: `news_blackout` es informativo en esta ruta y una excepción
    al leer el calendario haría perder el plan entero, que es la parte que el operador
    sí necesita para decidir.
    """
    if news is None:
        return None
    try:
        return news.gate()
    except Exception as exc:  # noqa: BLE001 - fail-open declarado, no excepción
        return {"ok": True, "fail_open": True, "block": None,
                "detail": "gate de noticias no disponible: {0}".format(exc)}


@router.get("/api/risk/setup", response_model=None)
def risk_setup(
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    synthetic: Optional[bool] = Query(None, description="siempre 0 en esta fase"),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Setup Score por dirección, régimen, killzone e invalidación estructural.

    El `risk_engine` del snapshot se publica ENTERO, con su desglose por componente: es
    la ruta donde el operador audita de dónde sale el número, y un breakdown recortado
    es exactamente la evidencia que faltaría en el momento de preguntar "¿por qué 45?".

    `422` cuando el snapshot no trae score (no hay velas, o el motor no pudo calcular) y
    `404`/`503` cuando el problema es el bróker. Un score no disponible no es un score de
    cero: por eso es un error y no un `200` con ceros.
    """
    _rechaza_sintetico(synthetic)
    snap = _snapshot(market, simbolo(symbol), str(timeframe or DEFAULT_TIMEFRAME).upper())
    try:
        return setup_eval.risk_setup(snap)
    except setup_eval.SinSetup as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/api/chart/setup-eval", response_model=None)
def chart_setup_eval(
    symbol: str = DEFAULT_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    min_score: Optional[float] = Query(None, ge=0.0, le=100.0),
    killzone: Optional[bool] = Query(None, description="fuerza la killzone del score (modo test)"),
    synthetic: Optional[bool] = Query(None, description="siempre 0 en esta fase"),
    market: Any = Depends(deps.market),
    store: Any = Depends(deps.store),
    news: Any = Depends(deps.news),
) -> Dict[str, Any]:
    """Evalúa el setup y devuelve el plan con el overlay de entrada, SL y TP.

    El umbral sale de `strategy.yaml` (`score.min_score`). `min_score` lo sobrescribe
    para una llamada —lo usa el frontend solo en modo test— y `min_score_used` declara
    en la respuesta cuál se aplicó: un umbral distinto del del YAML que solo consta en
    la URL es un umbral que no se puede auditar después.

    `killzone=1` fija el reloj del score dentro de la primera ventana configurada. El
    score resultante NO representa el horario real, y por eso la respuesta lo dice en
    `killzone_force` y `score_note`, y el snapshot queda marcado con
    `risk_engine.killzone_forced`: es lo mismo que va al log de setups y a la auditoría.

    No ejecuta ninguna orden. Un `approved: true` es "el setup cumple el gate", no "puedes
    mandar la orden": el blackout de noticias y los límites de cuenta se comprueban en la
    ejecución.
    """
    _rechaza_sintetico(synthetic)
    nombre = simbolo(symbol)
    snap = _snapshot(market, nombre, str(timeframe or DEFAULT_TIMEFRAME).upper(), force_killzone=bool(killzone))
    cfg = store.get_trading_config() or {}
    try:
        payload = setup_eval.evaluate(snap, cfg, spec=_spec(market, nombre), min_score=min_score)
    except setup_eval.SinSetup as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    veredicto = _noticias(news)
    if veredicto is None:
        veredicto = {"ok": True, "fail_open": True, "block": None,
                     "detail": "puerto de noticias no cableado: el blackout no se ha comprobado"}
    return setup_eval.con_noticias(payload, veredicto)


__all__ = ["SIN_SINTETICOS", "router"]
