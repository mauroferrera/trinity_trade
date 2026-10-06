"""Order flow: snapshot de cinta, CVD y el WebSocket `/ws/orderflow`.

Qué hay aquí y qué no
---------------------
Está la LECTURA: el estado del acumulador, su serie de CVD, sus ajustes y las
alertas. No está el FEED. En REF el feed (Databento live/historical/mock) ocupaba
unas 400 líneas del monolito y es un servicio aparte: se cablea cuando exista, y
mientras tanto `/api/orderflow` responde el `neutro(...)` que construye
`macro_ingestor` cuando no hay nadie mirando. Un endpoint de order flow que dice
"no hay feed encendido" con el motivo es correcto; uno que inventa una serie CVD
plana es una señal de la que se puede tomar una decisión.

Sobre el WebSocket
------------------
- El **ping** va en una tarea aparte. Si el heartbeat se cuelga, el bucle de
  recepción tiene que poder notar la desconexión, así que ninguno de los dos
  espera en el mismo sitio.
- El **bucle de recepción** sale con `WebSocketDisconnect` o con cualquier otro
  `Exception`, y el `finally` da de baja la conexión y cancela el ping. Sin
  `finally` la lista del manager acumulaba clientes fantasma.
- El **`disconnect` es idempotente** porque el `finally` puede correr después de que
  el manager ya lo haya quitado al detectar el fallo de un `send`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .. import deps
from ..services.mt5_market import DEFAULT_SYMBOL

router = APIRouter(tags=["order flow"])

#: Segundos entre pings. 15 es lo que usa REF y está bien: por debajo el navegador
#: no lo nota y por encima un proxy puede cortar la conexión por inactividad antes
#: de que llegue el siguiente.
PING_S = 15.0

#: Tope del tamaño de un mensaje de control. Un cliente que mande medio megabyte por
#: el WebSocket para pedir unos settings no necesita que se lo leamos entero.
MAX_CONTROL_BYTES = 4096


# -- lecturas -------------------------------------------------------------------


@router.get("/api/orderflow", response_model=None)
def orderflow(market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """CVD, delta, volumen, alertas y ajustes del acumulador.

    Sin feed encendido devuelve el `neutro(...)` con el motivo; con feed, el
    `snapshot()` de `core.orderflow_engine`, que es thread-safe.
    """
    return market.orderflow_snapshot()


@router.get("/api/orderflow/cvd", response_model=None)
def cvd_series(
    since: Optional[float] = None,
    rt: Any = Depends(deps.runtime),
) -> Dict[str, Any]:
    """La serie de CVD desde `since` (epoch segundos). Vacía si no hay feed."""
    engine = rt.orderflow
    if engine is None:
        return {"symbol": DEFAULT_SYMBOL, "points": [], "feed": "sin_puerto"}
    return {
        "symbol": engine.symbol,
        "points": engine.cvd_series(since=since),
        "msgs_per_sec": engine.msgs_per_sec(),
    }


class OrderFlowSettings(BaseModel):
    """Ajustes del detector de picos y absorción. Todos opcionales.

    Opcionales porque `update_settings` ignora los `None`: un cuerpo con un solo
    campo no reinicia el resto, que es lo que un formulario de ajustes espera al
    guardar solo una casilla.
    """

    zscore_threshold: Optional[float] = None
    absorb_trades: Optional[int] = None
    zscore_min_size: Optional[int] = None
    ema_window: Optional[int] = None
    absorb_vol_min: Optional[float] = None
    absorb_range_ratio: Optional[float] = None
    absorb_delta_ratio: Optional[float] = None


@router.post("/api/orderflow/settings", response_model=None)
def update_settings(
    body: OrderFlowSettings,
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Cambia umbrales del motor y devuelve los que quedaron.

    La respuesta es el estado FINAL, no lo que se mandó: quien configura tiene que
    ver lo que el motor realmente tiene, y el motor recorta valores a su rango
    (un umbral 0 o un `absorb_trades` de 5000 se ajustan).
    """
    engine = rt.orderflow
    if engine is None:
        raise HTTPException(status_code=503, detail="motor de order flow no cableado")
    actualizado = engine.update_settings(
        zscore_threshold=body.zscore_threshold,
        absorb_trades=body.absorb_trades,
        zscore_min_size=body.zscore_min_size,
        ema_window=body.ema_window,
        absorb_vol_min=body.absorb_vol_min,
        absorb_range_ratio=body.absorb_range_ratio,
        absorb_delta_ratio=body.absorb_delta_ratio,
    )
    rt.ws.emit({"event": "config", "settings": actualizado})
    return {"settings": actualizado}


@router.get("/api/orderflow/alerts", response_model=None)
def orderflow_alerts(market: Any = Depends(deps.market)) -> Dict[str, Any]:
    """Alertas de absorción y picos. Vacía si no hay feed."""
    return {"alerts": market.orderflow_alerts()}


# -- WebSocket -------------------------------------------------------------------


async def _ping(ws: WebSocket) -> None:
    """Heartbeat. Sale solo; el fallo lo cuenta el manager."""
    while True:
        await asyncio.sleep(PING_S)
        try:
            await ws.send_json({"event": "ping"})
        except Exception:  # noqa: BLE001 - el cliente se va; el bucle principal lo verá
            return


def _control(rt: Any, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Interpreta un mensaje de control. `None` si no es ninguno de los conocidos.

    El WebSocket solo controla AJUSTES ahora. Cuando exista el feed se añaden
    `feed_start`/`feed_stop` aquí, y es el único sitio que hay que tocar: el
    broadcast del resultado lo hace el gestor.
    """
    if data.get("action") not in ("update_settings", "settings"):
        return None
    engine = rt.orderflow
    if engine is None:
        return {"event": "error", "error": "motor de order flow no cableado"}
    return {
        "event": "config",
        "settings": engine.update_settings(
            zscore_threshold=data.get("zscore_threshold"),
            absorb_trades=data.get("absorb_trades"),
        ),
    }


def market_snapshot(rt: Any) -> Dict[str, Any]:
    """El snapshot del mercado para el primer mensaje del WS.

    Va sin cachear a propósito: es el mensaje con el que el cliente abre la pantalla,
    y un dato viejo le da un precio de hace diez segundos que se corrige al primer
    tick. El coste es un viaje a la terminal, una vez, por conexión.
    """
    if rt.market is None:
        return {}
    try:
        return rt.market.orderflow_snapshot()
    except Exception:  # noqa: BLE001 - abrir el WS no depende del bróker
        return {"error": "snapshot no disponible"}


@router.websocket("/ws/orderflow")
async def ws_orderflow(websocket: WebSocket) -> None:
    """El stream de cinta: estado al conectar, ajustes por control, ping cada 15 s."""
    rt = getattr(websocket.app.state, "runtime", None)
    if rt is None:
        await websocket.close(code=1011)
        return
    if not deps.ws_token(websocket):
        # 1008 = violated policy. Es el único código que el cliente distingue de
        # "se cortó la conexión", que es lo que necesita saber para reintentar con
        # token en vez de esperar al backoff.
        await websocket.close(code=1008)
        return

    manager = rt.ws
    engine = rt.orderflow
    await manager.connect(websocket)
    await manager.send(
        websocket,
        {
            "event": "status",
            "state": "connected",
            "symbol": getattr(engine, "symbol", DEFAULT_SYMBOL),
            "settings": engine.settings() if engine is not None else None,
            "snapshot": market_snapshot(rt),
        },
    )
    ping_task = asyncio.create_task(_ping(websocket))
    try:
        while True:
            raw = await websocket.receive_text()
            if not raw:
                continue
            if len(raw) > MAX_CONTROL_BYTES:
                await manager.send(
                    websocket, {"event": "error", "error": "mensaje de control demasiado grande"}
                )
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            respuesta = _control(rt, data)
            if respuesta is not None:
                await manager.broadcast(respuesta)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 - un cliente roto no tumba el WS
        manager.emit({"event": "error", "error": "{0}: {1}".format(type(exc).__name__, exc)})
    finally:
        ping_task.cancel()
        manager.disconnect(websocket)


__all__ = ["MAX_CONTROL_BYTES", "PING_S", "OrderFlowSettings", "router"]