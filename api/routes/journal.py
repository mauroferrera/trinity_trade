"""Bitácora, base de datos y estado del watcher. Lo que no es ni mercado ni agente.

Este router agrupa lo que REF tenía disperso: `/api/journal*`, `/api/db/*`,
`/api/setup-log`, `/api/drawings*` y `/api/risk/*`. Agruparlos es cosmético; lo que
no es cosmético es el orden en el que se aplican las tres reglas de esta capa:

1. **Las escrituras piden token.** Todas las que cambian algo (`POST`, `PUT`,
   `DELETE`). Las lecturas no, porque el token es para que un script no escriba en
   la cuenta de nadie, no para que una página no pueda mirar un gráfico.
2. **`PUT` de bitácora es reemplazo, no parche.** `store.update_journal_entry`
   reescribe la fila entera; por eso el modelo manda todos los campos y un campo
   ausente se borra. Es la semántica que el `store` documenta y que el cliente de
   REF ya respeta; lo contrario sería una letra la A que en la siguiente guardado
   desaparece.
3. **El enriquecido es un extra, no una corrección.** Al crear una entrada se llama
   a `market.enrich_journal_entry()` para completar precio/SL/TP si se pueden leer.
   Si el bróker no responde, la entrada se guarda tal cual. Perder la bitácora
   porque el terminal está apagado sería peor que guardarla con dos campos a `None`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import deps

router = APIRouter(tags=["bitácora y datos"])


class JournalBody(BaseModel):
    """La entrada de bitácora. Los campos son los de `journal` en el esquema."""

    conversation_id: Optional[str] = None
    ticket: Optional[int] = None
    symbol: str = "EURUSD"
    action: Optional[str] = None
    poi_type: Optional[str] = None
    liquidity_swept: Optional[bool] = None
    cme_confirmation: Optional[bool] = None
    setup_json: Dict[str, Any] = Field(default_factory=dict)
    emotion: Optional[str] = None
    plan_compliance: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    notes: Optional[str] = None
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    time_open: Optional[int] = None
    time_close: Optional[int] = None


@router.get("/api/journal", response_model=None)
def list_journal(
    symbol: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=500),
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    return store.list_journal(symbol=symbol, days=days, limit=limit)


@router.post("/api/journal", response_model=None)
def create_journal(
    body: JournalBody,
    store: Any = Depends(deps.store),
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Guarda una entrada, enriquecida con precio/SL/TP si se pueden leer.

    El enriquecido se hace con `rt.market` y no con un `Depends(deps.market)` a
    propósito: si el bróker está apagado, `deps.market` devuelve 503 y la bitácora se
    pierde por un problema de infraestructura que no es suyo. Con el mercado en
    `None` la entrada se guarda tal cual, que es lo que hay que hacer.
    """
    entrada = body.model_dump()
    if rt.market is not None:
        entrada = rt.market.enrich_journal_entry(entrada)
    return store.add_journal_entry(entrada)


@router.get("/api/journal/overlay", response_model=None)
def journal_overlay(
    symbol: str = "",
    days: int = Query(30, ge=1, le=3650),
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    """Entradas del journal listas para dibujar SOBRE el grafico.

    Devuelve una linea por SL y por TP de cada entrada que tenga precio de entrada y
    hora de apertura, con el resultado (`win`/`loss`) deducido de si el TP estaba al
    lado correcto del precio de entrada. No se calcula P&L: se calcula geometria, que
    es lo que el `main.js` de REF dibuja.

    Va declarado ANTES de `/api/journal/{jid}` y no por capricho estetico: Starlette
    empareja en orden de registro, asi que un `{jid}` declarado antes se comia la
    palabra `overlay` y pedia un journal con id "overlay". Es la misma trampa que
    `/api/analysis/chartism/{symbol}` frente a `/{symbol}`.

    Las entradas sin `entry_price` o sin `time_open` se saltan en vez de fallar: no
    hay coordenada que dibujar, y una linea en (0, 0) ensucia el eje de precios.
    """
    entradas = store.list_journal(symbol=symbol or None, days=days or None, limit=200)
    lineas: List[Dict[str, Any]] = []
    for entrada in entradas:
        precio_entrada = entrada.get("entry_price")
        apertura = entrada.get("time_open")
        if precio_entrada is None or apertura is None:
            continue
        sl = entrada.get("sl_price")
        tp = entrada.get("tp_price")
        accion = str(entrada.get("action") or "").upper()
        resultado = None
        if tp is not None:
            if accion == "BUY":
                resultado = "win" if tp > precio_entrada else "loss"
            elif accion == "SELL":
                resultado = "win" if tp < precio_entrada else "loss"
        for precio_linea, tipo in ((sl, "sl"), (tp, "tp")):
            if precio_linea is None:
                continue
            lineas.append(
                {
                    "id": "j{0}".format(entrada["id"]),
                    "symbol": entrada["symbol"],
                    "entry_time": apertura,
                    "entry_price": precio_entrada,
                    "line_price": precio_linea,
                    "line_type": tipo,
                    "result": resultado,
                    "label": entrada.get("poi_type") or str(entrada.get("ticket") or entrada["id"]),
                }
            )
    return lineas


@router.get("/api/journal/{jid}", response_model=None)
def get_journal(jid: int, store: Any = Depends(deps.store)) -> Dict[str, Any]:
    entrada = store.get_journal_entry(jid)
    if entrada is None:
        raise HTTPException(status_code=404, detail="entrada {0} no encontrada".format(jid))
    return entrada


@router.put("/api/journal/{jid}", response_model=None)
def update_journal(
    jid: int,
    body: JournalBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Reemplaza la entrada completa. Lo que no venga, se queda en `NULL`."""
    if store.get_journal_entry(jid) is None:
        raise HTTPException(status_code=404, detail="entrada {0} no encontrada".format(jid))
    store.update_journal_entry(jid, body.model_dump())
    return store.get_journal_entry(jid) or {}


@router.delete("/api/journal/{jid}", response_model=None)
def delete_journal(
    jid: int,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    if store.get_journal_entry(jid) is None:
        raise HTTPException(status_code=404, detail="entrada {0} no encontrada".format(jid))
    store.delete_journal_entry(jid)
    return {"ok": True, "id": jid}


# -- base de datos ---------------------------------------------------------------


@router.get("/api/db/tables", response_model=None)
def db_tables(store: Any = Depends(deps.store)) -> List[Dict[str, Any]]:
    """Tablas y su recuento de filas. Es el inspector que usa la pestaña de datos."""
    return store.list_db_tables()


@router.get("/api/db/tables/{table}", response_model=None)
@router.get("/api/db/{table}", response_model=None)
def db_query(
    table: str,
    limit: int = Query(100, ge=1, le=1000),
    store: Any = Depends(deps.store),
) -> Dict[str, Any]:
    """Filas de una tabla, con `limit` acotado.

    El nombre de la tabla va en la URL y `store.query_db_table` valida contra las
    tablas que existen de verdad: si no existe devuelve `None`, y aquí eso es un 404
    con el nombre, no un `200 null` que el frontend pinta como "tabla vacía".

    La respuesta es el DICCIONARIO que devuelve el store (`table`, `columns`, `rows`),
    no una lista de dicts: el store devuelve las filas como arrays para no pagar el
    coste de `dict()` por celda, y el JS las convierte con las `columns` de al lado.
    Por eso la anotación es `Dict` y no `List`, que es lo que tenía antes y mentía.

    Las dos rutas existen porque el JS de REF llama `/api/db/tables/{table}` y este
    router exponía `/api/db/{table}`: con la segunda sola, el inspector de la
    pestaña de datos devolvía 404 a un endpoint que el frontend usa a diario.
    """
    filas = store.query_db_table(table, limit=limit)
    if filas is None:
        raise HTTPException(status_code=404, detail="no existe la tabla {0!r}".format(table))
    return filas


@router.get("/api/setup-log", response_model=None)
def setup_log(
    limit: int = Query(100, ge=1, le=500),
    verdict: str = "",
    symbol: str = "",
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    """El log de setups evaluados, con su veredicto."""
    return store.list_setup_log(limit=limit, verdict=verdict, symbol=symbol)


@router.get("/api/trades", response_model=None)
def trades(
    symbol: Optional[str] = None,
    action: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=3650),
    limit: int = Query(200, ge=1, le=1000),
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    """Operaciones guardadas en la base (no las del bróker: `/api/history`)."""
    return store.list_trades(symbol=symbol, action=action, days=days, limit=limit)


# -- dibujos y alertas del gráfico ------------------------------------------------


@router.get("/api/drawings/{symbol}", response_model=None)
def get_drawings(
    symbol: str,
    timeframe: str = "M15",
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    return store.get_drawings(symbol.upper(), timeframe)


@router.put("/api/drawings/{symbol}", response_model=None)
def save_drawings(
    symbol: str,
    drawings: List[Dict[str, Any]],
    timeframe: str = "M15",
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    store.save_drawings(symbol.upper(), timeframe, drawings)
    return {"ok": True, "symbol": symbol.upper(), "timeframe": timeframe, "n": len(drawings)}


@router.delete("/api/drawings/{symbol}", response_model=None)
def delete_drawings(
    symbol: str,
    timeframe: str = "M15",
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    store.delete_drawings(symbol.upper(), timeframe)
    return {"ok": True, "symbol": symbol.upper(), "timeframe": timeframe}


@router.get("/api/alerts", response_model=None)
@router.get("/api/chart/alerts", response_model=None)
def list_alerts(
    active_only: bool = False,
    limit: int = Query(100, ge=1, le=500),
    store: Any = Depends(deps.store),
) -> List[Dict[str, Any]]:
    """Alertas de precio que el agente dejo puestas.

    Las dos rutas son la misma: `/api/alerts` la montaba REF para su panel y
    `/api/chart/alerts` es la que llama el `main.js` de REF. Duplicarlas es una
    linea y evita el 404 en una pestana que el usuario usa a diario.
    """
    return store.list_chart_alerts(active_only=active_only, limit=limit)


@router.delete("/api/chart/alerts/{aid}", response_model=None)
def cancel_alert(
    aid: int,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Cancela una alerta por id (no la borra: queda con estado `cancelled`).

    Cancelar y no borrar es lo que hace REF, y tiene motivo: el `alert_log` de la
    base es memoria de que el agente puso ahi una linea. Un `DELETE` que desaparece
    del log no deja rastro de por que el usuario la quito.
    """
    alerta = store.get_chart_alert(aid)
    if alerta is None:
        raise HTTPException(status_code=404, detail="alerta {0} no encontrada".format(aid))
    store.set_chart_alert_status(aid, "cancelled")
    return store.get_chart_alert(aid) or {}


# -- drawings que el grafico pinta (contrato de REF) ----------------------------


class DrawingsBody(BaseModel):
    """El set completo de dibujos de un symbol/timeframe."""

    symbol: str = "EURUSD"
    timeframe: str = "M15"
    drawings: List[Dict[str, Any]] = Field(default_factory=list)


@router.get("/api/chart/drawings", response_model=None)
def chart_drawings_get(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    store: Any = Depends(deps.store),
) -> Dict[str, Any]:
    """Los dibujos manuales del symbol/timeframe, en el sobre que espera el JS.

    El dict (y no la lista a pelo) es lo que pedia REF: el cliente lee
    `resp.symbol` para no pintar los de otro timeframe.
    """
    simbolo = symbol.upper()
    tf = timeframe.upper()
    return {"symbol": simbolo, "timeframe": tf, "drawings": store.get_drawings(simbolo, tf)}


@router.put("/api/chart/drawings", response_model=None)
def chart_drawings_put(
    body: DrawingsBody,
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Reemplaza el set completo. `PUT` es reemplazo, no parche (ver el docstring)."""
    simbolo = body.symbol.upper()
    tf = body.timeframe.upper()
    guardados = store.save_drawings(simbolo, tf, body.drawings or [])
    return {
        "status": "ok",
        "symbol": simbolo,
        "timeframe": tf,
        "drawings": guardados,
    }


@router.delete("/api/chart/drawings", response_model=None)
def chart_drawings_delete(
    symbol: str = "EURUSD",
    timeframe: str = "M15",
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    simbolo = symbol.upper()
    tf = timeframe.upper()
    store.delete_drawings(simbolo, tf)
    return {"status": "ok", "symbol": simbolo, "timeframe": tf}


# -- sincronizacion de operaciones -----------------------------------------------


@router.post("/api/trades/sync", response_model=None)
def trades_sync(
    days: int = Query(365, ge=1, le=3650),
    market: Any = Depends(deps.market),
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Vuelca el historial del broker en la tabla de trades y devuelve cuantos hay.

    Hace `clear_trades()` antes de insertar, como REF: el historico del broker es la
    verdad y la tabla es una copia. Sin el `clear`, una operacion que el broker ya no
    devuelve (por ejemplo tras un cambio de magic number) se queda pegada para
    siempre y el post-mortem cuenta operaciones que no existen.

    LO QUE NO HACE, a proposito: el enlace con `setup_log`. REF cerraba aqui el
    post-mortem (`finalize_trade_outcomes`), pero adivinar a que setup pertenece una
    fila cerrada de hace tres dias es justo el error que `pick_link_target` evita en
    el momento del envio. Aqui solo se sincroniza; el post-mortem es otro trabajo.
    """
    filas = market.history(days=days)
    if filas:
        store.clear_trades()
        store.upsert_trades(filas)
    return {"count": len(store.list_trades(limit=200))}


__all__ = ["DrawingsBody", "JournalBody", "router"]