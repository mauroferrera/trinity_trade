from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import deps

router = APIRouter(tags=["trading"])


def _upper_symbols_allow(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        partes = [p.strip().upper() for p in v.replace(",", " ").split()]
        return [p for p in partes if p]
    if isinstance(v, (list, tuple, set)):
        out: List[str] = []
        for s in v:
            if isinstance(s, str) and s.strip():
                out.append(s.strip().upper())
        vistos = set()
        unicos: List[str] = []
        for s in out:
            if s not in vistos:
                vistos.add(s)
                unicos.append(s)
        return unicos
    return []


def _coerce_positive_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="sl_distance_by_symbol: valores deben ser numéricos")
    if f <= 0.0:
        raise HTTPException(status_code=422, detail="sl_distance_by_symbol: distancias deben ser > 0")
    return f


def _sanitize_sl_map(m: Any) -> Dict[str, float]:
    if m is None:
        return {}
    if not isinstance(m, dict):
        raise HTTPException(status_code=422, detail="sl_distance_by_symbol debe ser un objeto")
    out: Dict[str, float] = {}
    for k, val in m.items():
        if not isinstance(k, str) or not k.strip():
            raise HTTPException(status_code=422, detail="sl_distance_by_symbol: claves inválidas")
        key = k.strip().upper()
        fv = _coerce_positive_float(val)
        if fv is None:
            continue
        out[key] = fv
    return out


def _sanitize_payload(cfg: Dict[str, Any]) -> Dict[str, Any]:
    limpio: Dict[str, Any] = dict(cfg or {})

    if "symbols_allow" in limpio:
        limpio["symbols_allow"] = _upper_symbols_allow(limpio["symbols_allow"])

    if "sl_distance_by_symbol" in limpio:
        limpio["sl_distance_by_symbol"] = _sanitize_sl_map(limpio["sl_distance_by_symbol"])

    if "risk_pct" in limpio and limpio["risk_pct"] is not None:
        try:
            rp = float(limpio["risk_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="risk_pct debe ser numérico")
        if rp < 0.0:
            raise HTTPException(status_code=422, detail="risk_pct debe ser >= 0")
        limpio["risk_pct"] = rp

    return limpio


class TradeMarketBody(BaseModel):
    """Orden de mercado manual.

    `volume` es opcional a propósito: si no viene, el lote se dimensiona con el
    riesgo efectivo del setup (el mismo cálculo que muestra la pantalla de lote).
    Escribirlo a mano es posible y también una forma de saltarse el dimensionado,
    así que la respuesta declara siempre si el lote vino pedido o se calculó.
    """

    symbol: str
    action: str = "BUY"
    volume: Optional[float] = None
    sl_distance: Optional[float] = None
    tp_distance: Optional[float] = None
    magic: Optional[int] = None
    comment: Optional[str] = None
    deviation: Optional[int] = None


@router.get("/api/trade/config", response_model=None)
def get_trade_config(
    store: Any = Depends(deps.store),
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    cfg = store.get_trading_config() or {}
    try:
        risk = market.daily_risk_state() or {"blocked": False}
    except Exception as exc:  # noqa: BLE001
        risk = {"blocked": False, "error": str(exc)}
    try:
        strategy = store.get_config_summary() or {}
    except Exception:  # noqa: BLE001
        strategy = {}
    return {"config": cfg, "risk": risk, "strategy": strategy}


@router.get("/api/trade/info/{symbol}", response_model=None)
def get_trade_info(
    symbol: str,
    market: Any = Depends(deps.market),
) -> Dict[str, Any]:
    """Lo que hace falta para operar este símbolo SIN enviar nada.

    Existe para que el frontend no tenga que suponer nada: digits, punto, lote
    mínimo/máximo/paso, si hay distancia de SL configurada y cuál. Si un símbolo no
    tiene SL configurado, aquí se ve antes de pulsar el botón, no con un 400
    después.
    """
    simbolo = str(symbol or "").strip().upper()
    if not simbolo:
        raise HTTPException(status_code=422, detail="símbolo requerido")
    spec = market.spec(simbolo)
    if spec is None:
        return {"symbol": simbolo, "available": False,
                "error": "El bróker no publica {0}.".format(simbolo)}
    return {
        "symbol": simbolo,
        "available": True,
        "digits": getattr(spec, "digits", None),
        "point": getattr(spec, "point", None),
        "pip": _pip_de(spec),
        "unit_label": getattr(spec, "unit_label", None),
        "contract_size": getattr(spec, "contract_size", None),
        "volume_min": getattr(spec, "volume_min", None),
        "volume_max": getattr(spec, "volume_max", None),
        "volume_step": getattr(spec, "volume_step", None),
    }


def _pip_de(spec: Any) -> Optional[float]:
    """El pip del spec si lo publica; None si no. No se inventa."""
    valor = getattr(spec, "pip", None)
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


@router.post("/api/trade/market", response_model=None)
def post_trade_market(
    body: TradeMarketBody,
    market: Any = Depends(deps.market),
    store: Any = Depends(deps.store),
    news: Any = Depends(deps.news),
    runtime_rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Any:
    """Manda una orden de mercado.

    El status lo decide `ExecutionService`, no esta función: la ruta solo traduce
    el cuerpo y devuelve lo que el servicio dice. Es lo que hace que el watcher y
    esta ruta tengan las MISMAS puertas — si el servicio fuera el único sitio con
    las validaciones, el auto-arranque sería el camino con menos filtros.
    """
    from ..services.execution import ExecutionService

    servicio = ExecutionService(market=market, store=store, news=news,
                                execution=getattr(runtime_rt, "execution", None))
    cuerpo, status = servicio.execute_market_trade(
        symbol=body.symbol, action=body.action, volume=body.volume,
        sl_distance=body.sl_distance, tp_distance=body.tp_distance,
        magic=body.magic, comment=body.comment, deviation=body.deviation,
        source="manual",
    )
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.post("/api/positions/{ticket}/close", response_model=None)
def post_close_position(
    ticket: int,
    market: Any = Depends(deps.market),
    store: Any = Depends(deps.store),
    runtime_rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Any:
    """Cierra la posición indicada por ticket.

    El ticket va en la ruta y no en un cuerpo porque es un identificador, y un
    identificador en la ruta se puede enlazar y comprobar sin abrir el cuerpo de
    la petición.
    """
    from ..services.execution import ExecutionService

    servicio = ExecutionService(market=market, store=store, news=None,
                                execution=getattr(runtime_rt, "execution", None))
    cuerpo, status = servicio.close_position(ticket=ticket)
    if status >= 400:
        raise HTTPException(status_code=status, detail=cuerpo)
    return cuerpo


@router.post("/api/trade/config", response_model=None)
def post_trade_config(
    body: Dict[str, Any],
    store: Any = Depends(deps.store),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body debe ser un objeto JSON")
    cfg_in = body.get("config")
    if cfg_in is None and body:
        cfg_in = body
    if not isinstance(cfg_in, dict):
        raise HTTPException(status_code=422, detail="config requerido")
    saneado = _sanitize_payload(cfg_in)
    store.set_trading_config(saneado)
    try:
        strategy = store.get_config_summary() or {}
    except Exception:  # noqa: BLE001
        strategy = {}
    return {"ok": True, "config": store.get_trading_config() or saneado, "strategy": strategy}


__all__ = ["router"]