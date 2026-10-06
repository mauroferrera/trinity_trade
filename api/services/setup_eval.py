"""La evaluación de setup que HTTP publica: el plan, su veredicto y el overlay.

Qué hay dentro y qué hay fuera
------------------------------
El CÁLCULO —zona de entrada, SL/TP, R:R, comparación con el umbral— vive en
`core.setup_gate`, que es puro y se prueba sin terminal. Aquí solo se compone la
RESPUESTA: qué campos publica cada ruta y qué se dibuja encima del gráfico.

Por qué los overlays no están en el núcleo
------------------------------------------
Un `markLine` de ECharts no es una regla de trading: es cómo esta versión del
frontend pinta una línea. Meterlo en `core/` ataría la aritmética del gate al
librero de gráficos del cliente, y el próximo cliente (un panel distinto, un bot)
se llevaría un módulo que no usa. La frontera es: `core` dice ENTRADA, SL y TP;
esta capa dice dónde se pinta cada uno.

El gate de noticias es INFORMATIVO aquí
--------------------------------------
`news_blackout` se publica pero NO entra en `approved`. La puerta que bloquea de
verdad está en la ejecución (`send_market_order` en REF) y en el watcher, que es
donde se conoce la hora real de la orden. Filtrar en esta capa rechazaría setups con
un dato viejo —el calendario se lee una vez y este snapshot puede ser de hace diez
segundos— y perdería operaciones por una noticia que ya pasó. Lo que sí hace esta
capa es DEJARLO A LA VISTA, para que quien va a pulsar el botón lo sepa antes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import setup_gate, strategy

#: Cuánto se ensancha la vista para que el plan quepa con margen a cada lado. Son
#: segundos, porque `last_time` del snapshot es una vela en epoch.
VIEW_MARGIN_S = 3600

#: Colores del overlay. Verdugo para el stop, verde-azulado para el objetivo: son los
#: mismos que el frontend ya usa para el resto de líneas del gráfico, para que el plan
#: no parezca un dibujo nuevo de otra familia.
COLOR_ENTRADA_BUY = "#2ee6a8"
COLOR_ENTRADA_SELL = "#ff5d6c"
COLOR_SL = "rgba(255,93,108,0.9)"
COLOR_TP = "rgba(46,230,168,0.9)"


class SinSetup(Exception):
    """El snapshot no trae scores: no hay nada que evaluar.

    Excepción propia y no un `dict` con `error`, porque quien la recibe tiene que
    distinguir "no hay veredicto" de "el veredicto dice que no se opera". La primera
    es un `422` con el motivo; la segunda es un `200` con `approved: false` y sus
    motivos.
    """


def risk_setup(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """El `risk_engine` del snapshot, con la identidad del símbolo encima.

    Se publica **entero**, no un subconjunto elegido aquí: el breakdown por
    componente es lo que permite auditar de dónde sale un score, y filtrarlo en el
    servidor sería descartar la evidencia justo en la ruta donde se mira.
    """
    snap = snapshot or {}
    re_ = snap.get("risk_engine") or {}
    if not re_ or "error" in re_:
        raise SinSetup(
            str((re_ or {}).get("error") or "risk engine no disponible para este snapshot")
        )
    if not (re_.get("bull") or re_.get("bear")):
        raise SinSetup("sin scores de risk engine en el snapshot")
    out: Dict[str, Any] = dict(re_)
    out["symbol"] = snap.get("symbol")
    out["timeframe"] = snap.get("timeframe")
    out["current_price"] = snap.get("current_price")
    out["time"] = snap.get("time")
    return out


def evaluate(snapshot: Dict[str, Any],
             cfg: Optional[Dict[str, Any]] = None,
             spec: Any = None,
             min_score: Optional[float] = None) -> Dict[str, Any]:
    """El plan evaluado, con el overlay listo para dibujar.

    `min_score` sobrescribe el umbral de `strategy.yaml` solo si se pasa. Cuando se
    pasa, `min_score_used` lo declara en la respuesta: un umbral distinto del del
    YAML es un umbral distinto, y que conste solo en la URL lo esconde del log de
    setups y de la fila de auditoría.
    """
    snap = snapshot or {}
    gate = setup_gate.evaluate_gate(snap, cfg, spec=spec, min_score=min_score)
    if gate is None:
        raise SinSetup("sin scores de risk engine en el snapshot")

    direction = gate["direction"]
    entry, sl, tp = gate["entry"], gate["sl"], gate["tp"]
    echarts: Dict[str, List[Dict[str, Any]]] = {"markLine": [], "markPoint": []}
    view: Optional[Dict[str, Any]] = None
    last_time = snap.get("last_time")

    if gate["approved"] and None not in (entry, sl, tp):
        arriba = direction == "BUY"
        color = COLOR_ENTRADA_BUY if arriba else COLOR_ENTRADA_SELL
        echarts["markPoint"].append({
            "coord": [last_time, entry],
            "value": "{0} {1:.5f}".format(direction, entry),
            "symbol": "pin",
            "symbolSize": 26,
            "symbolOffset": [0, -8] if arriba else [0, 8],
            "itemStyle": {"color": color, "borderColor": "#fff"},
            "label": {
                "show": True, "color": "#fff", "fontSize": 10,
                "fontWeight": 700, "formatter": direction,
            },
        })
        for valor, texto, estilo in (
            (entry, "Entry", {"color": color, "width": 1.5, "type": "dashed", "opacity": 0.8}),
            (sl, "SL", {"color": COLOR_SL, "width": 1.5, "type": "dashed"}),
            (tp, "TP", {"color": COLOR_TP, "width": 1.5, "type": "dashed"}),
        ):
            echarts["markLine"].append({
                "yAxis": valor,
                "label": {
                    "show": True, "formatter": texto, "color": color,
                    "fontSize": 10, "position": "insideEndTop",
                },
                "lineStyle": estilo,
            })
        lo = min(sl, tp, entry)
        hi = max(sl, tp, entry)
        view = {
            "start_time": (last_time - VIEW_MARGIN_S) if last_time else None,
            "end_time": last_time,
            "price_min": lo,
            "price_max": hi,
        }

    return {
        "symbol": snap.get("symbol"),
        "timeframe": snap.get("timeframe"),
        "direction": direction,
        "score": gate["score"],
        "verdict": gate["verdict"],
        "current_price": gate["current_price"],
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_distance": gate["sl_distance"],
        "sl_distance_source": gate["sl_distance_source"],
        "unit_label": gate["unit_label"] or "en precio",
        "entry_kind": gate["entry_kind"],
        "entry_zone_reject": gate["entry_zone_reject"],
        "approved": gate["approved"],
        "in_killzone": gate["in_killzone"],
        "invalidate_level": gate["invalidate_level"],
        "reasons": gate["reasons"],
        "echarts": echarts,
        "view": view,
        "killzone": (snap.get("risk_engine") or {}).get("killzone"),
        "killzone_force": bool((snap.get("risk_engine") or {}).get("killzone_forced")),
        "score_note": (
            "killzone forzada (test): el score ignora el horario real."
            if (snap.get("risk_engine") or {}).get("killzone_forced") else None
        ),
        "min_score_used": gate["min_score_used"],
        "news_blackout": False,
        "news_gate": None,
        "news_reason": None,
    }


def con_noticias(payload: Dict[str, Any], veredicto: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Engloba el gate de noticias en el payload, sin cambiar `approved`.

    Un veredicto ausente (el calendario no está cableado, o no se pudo leer) deja
    `news_blackout=False` y pone el motivo en `news_reason`: fail-open declarado, que
    es lo contrario de un fail-open silencioso, indistinguible de "no hay noticias".
    """
    v = veredicto or {}
    payload["news_blackout"] = not bool(v.get("ok", True))
    payload["news_gate"] = v.get("block")
    payload["news_reason"] = v.get("detail") or v.get("reason") or None
    if v.get("disabled"):
        payload["news_reason"] = "noticias desactivadas en data_sources.news"
    return payload


def umbral_de_config(cfg: Optional[Dict[str, Any]]) -> float:
    """El umbral vigente, para el caso de que quien llama quiera declararlo."""
    return strategy.min_score_for(cfg or {})


__all__ = [
    "COLOR_ENTRADA_BUY",
    "COLOR_ENTRADA_SELL",
    "COLOR_SL",
    "COLOR_TP",
    "SinSetup",
    "VIEW_MARGIN_S",
    "con_noticias",
    "evaluate",
    "risk_setup",
    "umbral_de_config",
]
