"""Las herramientas del agente: 18 declaradas, ejecutadas contra puertos.

Qué cambia respecto a `REF/agent.py`
-----------------------------------
`_handler(name, args)` era una cadena de 18 `if` que empezaba con `import app`
dentro de la función (`agent.py:318`). Tres cosas se arreglan aquí:

1. **Registro en vez de `if`.** Cada herramienta es una `ToolSpec` (schema, puertos
   que necesita, timeout, handler) en un diccionario. Añadir una herramienta es
   añadir una entrada, y "qué herramientas existen" es una pregunta que el router y
   los tests pueden hacer sin ejecución.
2. **Puertos inyectados.** Ningún handler importa `app`, `mt5`, `adapters` ni
   `litellm`. Hablan con `AgentDeps`, así que la misma herramienta funciona contra
   MT5 real, contra un doble de test, o responde `unavailable` porque el cable no
   está puesto.
3. **Timeout POR HERRAMIENTA.** REF envolvía todas las llamadas en
   `asyncio.wait_for(..., timeout=4.0)` (`agent.py:851`). Un único número para
   dieciocho operaciones de naturas distintas: `account_info` tarda milisegundos y
   `chart_snapshot` (velas + SMC + CVD + risk engine) nunca cabe en 4 s, así que
   el número no era un timeout sino una apuesta. Aquí cada `ToolSpec` declara el
   suyo y el corte se lee como lo que es.

Los cuatro fallos de REF que este módulo NO reproduce
------------------------------------------------------
- `price` envolvía el fallo DENTRO del éxito: `_ok(app.get_price(...) or
  {"status": "offline", ...})` (`agent.py:327`) devolvía `{"status": "ok",
  "data": {"status": "offline"}}`. El modelo leía "ok" y el "offline" se quedaba
  enterrado en el `data`. Aquí un símbolo sin precio es `unavailable`, que es lo
  que es.
- `int(args.get("days", 7))` reventaba con `TypeError` si el modelo mandaba
  `"days": "7,5"` o `null`, y el `TypeError` se comía el `except` exterior dejando
  un error genérico. `_int()` recorta, valida y cae al default.
- `journal_append` hacía `except Exception: pass` alrededor del enriquecimiento y
  guardaba la entrada igual, sin decir que había quedado a medias (`agent.py:374`).
  Aquí se guarda y se declara qué parte no se pudo completar.
- `patterns` escribía en `_LAST_ANALYSIS`, un dict de módulo sin TTL ni
  conversación. Eso vive en `ports.AnalysisAnchors` y `patterns` solo lo usa a
  través de la interfaz.

Este módulo NO calcula entradas, SL ni TP. `setup_score` devuelve el score del
`core/risk_engine.py` y su veredicto; redactar la consecuencia es cosa del modelo
bajo la regla dura de `prompt_templates.REGLAS_DURA` (regla 19 de
`AGENT_GUIDELINES.md`).
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from agent import prompt_templates as pt
from agent.ports import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    AgentDeps,
    AgentPortError,
)

# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

#: Timeout por defecto de una herramienta que solo lee estado del bróker. Es el
#: número de REF, y aquí es el de las herramientas que de verdad lo necesitan.
TIMEOUT_S_POR_DEFECTO = 4.0

#: Lecturas que arman el contexto entero (velas + SMC + CVD + risk engine). Van con
#: presupuesto propio porque comparten el número con trabajo que no es I/O de red.
TIMEOUT_S_CONtexto = 12.0


# ---------------------------------------------------------------------------
# Utilidades de entrada (el modelo no garantiza tipos)
# ---------------------------------------------------------------------------


def _int(
    args: Dict[str, Any],
    key: str,
    default: int,
    minimo: int = 0,
    maximo: int = 2 ** 53,
) -> int:
    """Entero con recorte, o el default.

    REF hacía `int(args.get("days", 7))` en cinco sitios. Un `"7"` (string) lo
    aceptaba por casualidad; un `null`, un `"7,5"` o un `{}` lanzaban `TypeError`
    que el `except` exterior convertía en un error sin origen. Aquí un valor
    imposible es el default y no una excepción, porque el default aquí SÍ es una
    respuesta razonable (7 días de historial) y una excepción no lo es.

    El `maximo` por defecto es enorme a propósito: esta función también recorta
    `start_time`/`time` de epoch, que son ~1,7e9. Un tope "razonable" de 1e6
    truncaría en silencio el año 2001 y las anotaciones acabarían en la vela
    equivocada sin ningún error.
    """
    valor = args.get(key, None)
    if valor is None or isinstance(valor, bool):
        return default
    try:
        n = int(float(valor))
    except (TypeError, ValueError):
        return default
    return max(minimo, min(maximo, n))


def _float(args: Dict[str, Any], key: str) -> Optional[float]:
    """Float finito o `None`. `None` NO es 0: 0 es un precio."""
    valor = args.get(key, None)
    if valor is None or isinstance(valor, bool):
        return None
    try:
        x = float(valor)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _str(args: Dict[str, Any], key: str, default: str = "") -> str:
    valor = args.get(key, None)
    if valor is None:
        return default
    return str(valor).strip()


def _simb(args: Dict[str, Any], key: str = "symbol", default: str = "EURUSD") -> str:
    """Símbolo en mayúsculas.

    El default es `EURUSD` como en REF porque es lo que el rol y los tests usan,
    pero se documenta como lo que es: un valor por defecto heredado de un agente
    que solo era forex. Un símbolo vacío se queda vacío, que es un error honesto,
    y se reporta arriba.
    """
    return _str(args, key, default).upper()


def _tf(args: Dict[str, Any], default: str = "M15") -> str:
    return (_str(args, "timeframe", default) or default).upper()


def _dias(args: Dict[str, Any]) -> Optional[int]:
    """El filtro de días, o `None` si el modelo no pidió ninguno.

    `None` y `0` NO son lo mismo para `store.list_trades`: `None` es "sin filtro",
    y por eso un default numérico aquí filtraría de más sin querer. REF pasaba
    `days=args.get("days")` directo, que es justo lo que hay que hacer.
    """
    if args.get("days", None) is None:
        return None
    return _int(args, "days", 0, 1, 3650) or None


# ---------------------------------------------------------------------------
# Contexto de una ejecución
# ---------------------------------------------------------------------------


class ToolCtx:
    """Lo que un handler necesita además de sus argumentos.

    Se pasa explícito en vez de leerse de un global porque `chart_annotate` depende
    de la CONVERSACIÓN (para no validar contra el análisis de otro chat) y porque
    un handler que lee el contexto de un sitio solo se puede probar inyectándoselo.
    """

    __slots__ = ("deps", "conversation_id", "round_no")

    def __init__(
        self,
        deps: AgentDeps,
        conversation_id: str = "",
        round_no: Optional[int] = None,
    ) -> None:
        self.deps = deps
        self.conversation_id = str(conversation_id or "")
        self.round_no = round_no

    def puerto(self, nombre: str) -> Any:
        """El puerto o `None`. Los handlers lo usan para_ports opcionales."""
        return getattr(self.deps, nombre, None)


def _ok(data: Any) -> Dict[str, Any]:
    return {"status": STATUS_OK, "data": data, "error": None}


def _fail(error: Any, status: str = STATUS_FAILED) -> Dict[str, Any]:
    return {"status": status, "data": None, "error": str(error)}


# ---------------------------------------------------------------------------
# Handlers: mercado y cuenta
# ---------------------------------------------------------------------------


def _h_account_info(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.account_info()


def _h_positions_list(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.positions()


def _h_history(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.history(_int(args, "days", 7, 1, 365))


def _h_price(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    symbol = _simb(args)
    if not symbol:
        raise AgentPortError("price necesita 'symbol'")
    data = ctx.deps.market.price(symbol)
    if data is None:
        # REF devolvía esto dentro de un ok: el modelo leía status=ok y el
        # 'offline' enterrado. Aquí el rótulo es el de verdad.
        raise AgentPortError("símbolo no disponible en el proveedor: {0}".format(symbol))
    return data


def _h_orderflow_snapshot(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.orderflow_snapshot()


def _h_orderflow_alerts(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.orderflow_alerts()


def _h_patterns(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    symbol = _simb(args)
    timeframe = _tf(args)
    data = ctx.deps.market.pattern_data(symbol, timeframe)
    if data is None:
        raise AgentPortError("sin datos de {0} {1} (¿símbolo en Market Watch?)".format(symbol, timeframe))
    analysis = data.get("analysis") or {}
    ctx.deps.anchors.put(ctx.conversation_id, symbol, timeframe, analysis, data.get("candles") or [])
    return analysis


def _h_chart_snapshot(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.market.chart_snapshot(_simb(args), _tf(args))


def _h_setup_score(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """El score del risk engine, sin recalcularlo aquí.

    La parte importante es que NO se recalcula: se lee el que el snapshot ya trae.
    Recalcular en el agente significaría dos fuentes de verdad para el mismo
    número, y el prompt dice "cita el valor que devuelve la herramienta".
    """
    symbol = _simb(args)
    timeframe = _tf(args)
    direction = (_str(args, "direction", "BUY") or "BUY").upper()
    snap = ctx.deps.market.chart_snapshot(symbol, timeframe)
    risk = (snap or {}).get("risk_engine")
    if not risk:
        raise AgentPortError("risk_engine no disponible para este snapshot")
    prefix = "bull" if direction == "BUY" else "bear"
    lado = risk.get(prefix)
    if not lado:
        raise AgentPortError("el snapshot no trae la rama {0}".format(prefix))
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "current_price": snap.get("current_price"),
        "score": lado.get("score"),
        "verdict": lado.get("verdict"),
        "components": lado.get("components"),
        "regime": risk.get("regime"),
        "killzone": risk.get("killzone"),
        "invalidation": risk.get("invalidation"),
    }


def _h_now(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """La hora UTC y, si hay bróker, la suya.

    El reloj UTC es de `core.clock` porque las velas, las killzones y los
    timestamps del gráfico están en UTC (D-009). La hora de pared del broker NO se
    puede calcular sin el broker, así que si el puerto de mercado no está cableado
    se dice `None` en vez de inventar la hora del SO, que era el desfase de 3 h que
    hizo que el agente razonara sobre un reloj que no era el suyo.
    """
    from core import clock

    market = ctx.deps.market
    broker = None
    dia = None
    if market is not None:
        try:
            broker = market.broker_time()
        except Exception:  # noqa: BLE001 - la hora del broker es un extra, no el dato
            broker = None
        try:
            dia = market.trading_day()
        except Exception:  # noqa: BLE001 - ídem
            dia = None
    return {"datetime_utc": clock.fmt_utc(), "broker_time": broker, "trading_day": dia}


# ---------------------------------------------------------------------------
# Handlers: persistencia
# ---------------------------------------------------------------------------


def _h_trade_query(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    action = (_str(args, "action") or "").upper() or None
    return ctx.deps.store.list_trades(
        symbol=_simb(args, default="") or None,
        action=action,
        days=_dias(args),
        limit=_int(args, "limit", 50, 1, 500),
    )


def _h_journal_append(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """Guarda la entrada y declara si quedó enriquecida.

    REF hacía `try: enrich except: pass` y devolvía la fila guardada, sin decir que
    faltaba el precio. Aquí el resultado lleva `enriquecida`: false es
    información, no un detalle, porque una entrada sin precio no se puede auditar
    contra el gráfico después.
    """
    entrada: Dict[str, Any] = {
        "conversation_id": args.get("conversation_id") or ctx.conversation_id or None,
        "ticket": args.get("ticket"),
        "symbol": _simb(args),
        "action": (_str(args, "action") or "").upper() or None,
        "poi_type": args.get("poi_type"),
        "liquidity_swept": args.get("liquidity_swept"),
        "cme_confirmation": args.get("cme_confirmation"),
        "emotion": args.get("emotion"),
        "plan_compliance": args.get("plan_compliance"),
        "tags": args.get("tags") or [],
        "notes": args.get("notes"),
    }
    enriquecida = False
    mercado = ctx.deps.market
    if mercado is not None:
        try:
            entrada = mercado.enrich_journal_entry(entrada) or entrada
            enriquecida = True
        except Exception:  # noqa: BLE001 - sin bróker se guarda igual, pero se dice
            enriquecida = False
    fila = ctx.deps.store.add_journal_entry(entrada)
    if isinstance(fila, dict):
        fila = dict(fila)
        fila["enriquecida"] = enriquecida
    return fila


def _h_journal_list(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    return ctx.deps.store.list_journal(
        symbol=_simb(args, default="") or None,
        days=_dias(args),
        limit=_int(args, "limit", 50, 1, 200),
    )


def _h_get_chart_drawings(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    symbol = _simb(args, default="") or "EURUSD"
    timeframe = _tf(args)
    drawings = ctx.deps.store.get_drawings(symbol, timeframe) or []
    return pt.summarize_drawings(drawings, symbol, timeframe)


# ---------------------------------------------------------------------------
# Handlers: noticias y exportaciones
# ---------------------------------------------------------------------------


def _h_economic_news(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    simbolos = args.get("symbols")
    if isinstance(simbolos, str):
        simbolos = [s.strip().upper() for s in simbolos.split(",") if s.strip()]
    if simbolos is not None and not isinstance(simbolos, list):
        simbolos = None
    return ctx.deps.news.news(simbolos)


def _h_mt5_export_read(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """Exportaciones del indicador. Sin `ExportPort` cableado responde `unavailable`.

    El puerto existe declarado (`ports.ExportPort`) y la Fase 6 lo implementa. Hasta
    entonces la respuesta honesta es "no hay nadie que lea ese fichero", no un
    `[]` que el modelo lee como "el indicador no ha exportado nada".
    """
    me = ctx.deps.exports
    action = (_str(args, "action", "list") or "list").lower()
    if action == "list":
        return me.list_exports(
            symbol=_simb(args, default="") or None,
            mode=_str(args, "mode") or None,
            limit=_int(args, "limit", 10, 1, 100),
        )
    if action == "read":
        filename = _str(args, "filename")
        if not filename:
            raise AgentPortError("falta 'filename' para action='read'")
        return me.read_export(filename)
    if action == "latest":
        return me.latest_export(
            symbol=_simb(args, default="") or None,
            mode=_str(args, "mode") or None,
        )
    raise AgentPortError("action debe ser list, read o latest")


# ---------------------------------------------------------------------------
# Handlers: alertas y dibujos
# ---------------------------------------------------------------------------


def _h_set_chart_alert(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """Valida la entrada y guarda la alerta.

    El rango de precio (`0 < p < 1e7`) viene de REF y parece arbitrario, pero
    descarta la respuesta inútil que produce un modelo al que le llega `price` como
    texto, o un nivel en pips cuando el símbolo cotiza en otros. Se mantiene como
    GUARDIA DE ENTRADA, no como medida de mercado.
    """
    symbol = _simb(args, default="")
    price = _float(args, "price")
    if not symbol or price is None or not (0 < price < 1e7):
        raise AgentPortError("symbol inválido o precio fuera de rango")
    side = (_str(args, "side", "touch") or "touch").lower()
    if side not in ("above", "below", "touch"):
        side = "touch"
    conditions = args.get("conditions") or []
    timeframe = _tf(args) if conditions else None
    expires_in = _int(args, "expires_in_minutes", 0, 0, 60 * 24 * 30)
    expires_at = None
    if expires_in > 0:
        from datetime import timedelta

        from core import clock

        # UTC AWARE, no hora local naive: `created_at` se guarda en UTC y la
        # comparación del TTL es contra un reloj aware. Con un naive aquí la
        # alerta expiraba 3 h antes de lo pedido, que es el bug que REF documenta
        # junto al `add_chart_alert`.
        expires_at = clock.now_iso(clock.now_utc() + timedelta(minutes=expires_in))
    return ctx.deps.store.add_chart_alert(
        symbol,
        price,
        _str(args, "label") or None,
        side,
        conditions=conditions,
        timeframe=timeframe,
        expires_at=expires_at,
    )


#: Tope de acciones por llamada. REF lo tenía (`args.get("actions") or [])[:8]`) y es
#: una decisión correcta: sin tope, un modelo que se equivoca de bucle puede pedir
#: 500 marcas y el navegador se come la sesión. Se sube a 16 solo si el motivo cabe
#: en un mensaje, y no cabe.
MAX_ACCIONES_ANNOTATE = 8


def _h_chart_annotate(args: Dict[str, Any], ctx: ToolCtx) -> Any:
    """Convierte referencias de patrones en coordenadas, validándolas contra datos reales.

    Dos modos, y la diferencia no es de estilo:

    - **Patrones** (`markArea`/`markPoint`): la `ref` es el `start_time`/`time`
      EXACTO que devolvió `patterns`. Se busca en el análisis anclado a ESTA
      conversación; si no existe, la acción se descarta. Es lo que impide que el
      modelo dibuje un FVG que nadie vio.
    - **Figuras libres** (`line`/`horizontal`/`rect`/`text`): coordenadas propias
      recortadas contra la ventana real de velas. Recortar en vez de rechazar
      porque un precio fuera de rango es un detalle de diseño, no de exactitud.

    Lo que no hay es la opción de inventar: si no hay ventana real, `unavailable`.
    """
    symbol = _simb(args)
    timeframe = _tf(args)
    entrada = ctx.deps.anchors.get(ctx.conversation_id, symbol, timeframe)
    if not entrada:
        raise AgentPortError(
            "sin análisis de patrones anclado para {0} {1} en esta conversación; "
            "consulta primero 'patterns' del MISMO símbolo/timeframe".format(symbol, timeframe)
        )
    analysis = (entrada.get("analysis") or {}).get("patterns", {})
    ventana = _ventana_de_analisis(entrada)
    if ventana is None:
        raise AgentPortError("sin ventana de velas para validar coordenadas de {0} {1}".format(symbol, timeframe))
    last_time = ventana["last"]

    actions = [a for a in (args.get("actions") or []) if isinstance(a, dict)][:MAX_ACCIONES_ANNOTATE]
    mark_area: List[Any] = []
    mark_point: List[Any] = []
    draw: List[Dict[str, Any]] = []
    applied = 0
    skipped = 0

    for act in actions:
        kind = act.get("kind")
        grupo = act.get("pattern")
        ref = _int({"v": act.get("ref")}, "v", -1)
        etiqueta = act.get("name")

        if kind == "markArea" and grupo in ("fvgs", "order_blocks"):
            obj = next(
                (z for z in (analysis.get(grupo) or []) if _int({"v": z.get("start_time")}, "v", -1) == ref),
                None,
            )
            if not obj:
                skipped += 1
                continue
            if grupo == "fvgs":
                bull = obj.get("type") == "BULLISH_FVG"
                color = "rgba(38,166,154,0.30)" if bull else "rgba(239,83,80,0.30)"
                borde = "#26a69a" if bull else "#ef5350"
                nombre = etiqueta or "{0} FVG {1}".format("Bullish" if bull else "Bearish", timeframe)
            else:
                color = "rgba(41,98,255,0.35)"
                borde = "#2962ff"
                nombre = etiqueta or "Order Block {0}".format(timeframe)
            mark_area.append(
                [
                    {
                        "xAxis": ref,
                        "yAxis": obj.get("bottom"),
                        "itemStyle": {"color": color, "borderColor": borde, "borderType": "dashed"},
                        "label": {"show": True, "color": borde, "fontSize": 10, "position": "insideTop"},
                        "name": nombre,
                    },
                    {"xAxis": last_time, "yAxis": obj.get("top")},
                ]
            )
            applied += 1
        elif kind == "markPoint" and grupo == "sweeps":
            obj = next(
                (s for s in (analysis.get("sweeps") or []) if _int({"v": s.get("time")}, "v", -1) == ref),
                None,
            )
            if not obj:
                skipped += 1
                continue
            pdh = obj.get("type") == "PDH_SWEEP"
            mark_point.append(
                {
                    "coord": [ref, obj.get("wick_extreme")],
                    "value": etiqueta or ("PDH" if pdh else "PDL"),
                    "symbol": "arrowDown" if pdh else "arrowUp",
                    "symbolSize": 14,
                    "symbolOffset": [0, -6] if pdh else [0, 6],
                    "itemStyle": {"color": "#ff5d6c" if pdh else "#2ee6a8", "borderColor": "transparent"},
                    "label": {"show": False},
                }
            )
            applied += 1
        elif kind in ("line", "horizontal", "rect", "text"):
            p0 = _float(act, "price_from")
            p1 = _float(act, "price_to")
            t0 = _float(act, "start_time")
            t1 = _float(act, "end_time")
            item: Dict[str, Any] = {"origin": "agent"}
            if kind in ("line", "rect"):
                if p0 is None or p1 is None:
                    skipped += 1
                    continue
                item["tool"] = "line" if kind == "line" else "rect"
                item["p0"] = _recorta_precio(p0, ventana)
                item["p1"] = _recorta_precio(p1, ventana)
                a = _recorta_t(t0 if t0 is not None else ventana["start"], ventana)
                b = _recorta_t(t1 if t1 is not None else ventana["last"], ventana)
                if b < a:
                    a, b = b, a
                item["t0"], item["t1"] = a, b
            elif kind == "horizontal":
                if p0 is None:
                    skipped += 1
                    continue
                item["tool"] = "hline"
                item["p0"] = _recorta_precio(p0, ventana)
            else:
                if p0 is None:
                    skipped += 1
                    continue
                item["tool"] = "text"
                item["t0"] = _recorta_t(t0 if t0 is not None else ventana["last"], ventana)
                item["p0"] = _recorta_precio(p0, ventana)
                item["text"] = (etiqueta or "").strip()[:60]
            draw.append(item)
            applied += 1
        else:
            skipped += 1

    if not mark_area and not mark_point and not draw:
        raise AgentPortError("ninguna ref de patrón ni coordenada de dibujo válida")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "applied": applied,
        "skipped": skipped,
        "echarts": {"markArea": mark_area, "markPoint": mark_point, "draw": draw},
        "view": _view(mark_area, mark_point, draw, last_time),
    }


def _ventana_de_analisis(entrada: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """La ventana de velas que valida coordenadas, o `None` si no la hay.

    Sale del anclaje (`candle_start`/`last_time`/`lo`/`hi`), no de una segunda
    consulta al bróker, y esa es la decisión que importa: si el anclaje se guardó
    de una ventana y las velas han cambiado desde entonces, las coordenadas
    válidas son las de la ventana que el modelo VIO, que es justo lo que un TTL
    corto garantiza (`ports.AnalysisAnchors`).
    """
    lo = entrada.get("candle_lo")
    hi = entrada.get("candle_hi")
    start = entrada.get("candle_start")
    last = entrada.get("last_time")
    if lo is None or hi is None or start is None or last is None:
        return None
    return {"start": start, "last": last, "lo": lo, "hi": hi}


def _recorta_precio(precio: float, ventana: Dict[str, Any]) -> float:
    """El precio, acotado a la ventana más un margen del 25%.

    El margen existe para que un nivel "un poco por encima del rango" se vea
    pegado al borde en vez de desaparecer. Acotar en vez de rechazar es lo que
    hace que la herramienta sea usable: el modelo redondea, y un rechazo por
    0,0001 sería una respuesta que no le sirve a nadie.
    """
    span = (ventana["hi"] - ventana["lo"]) or precio
    return max(ventana["lo"] - span * 0.25, min(ventana["hi"] + span * 0.25, precio))


def _recorta_t(t: float, ventana: Dict[str, Any]) -> float:
    return max(ventana["start"], min(ventana["last"], t))


def _view(
    mark_area: Sequence[Any],
    mark_point: Sequence[Any],
    draw: Sequence[Dict[str, Any]],
    last_time: Any,
) -> Optional[Dict[str, Any]]:
    """Límites de cámara para que el front enfoque lo dibujado.

    Solo con coordenadas REALES aplicadas (de aquí sale el `applied` del handler).
    El `span` se multiplica por 4 para no encuadrar una zona de 30 s de anchura,
    con un mínimo de 2 h.
    """
    refs: List[Any] = []
    lo = hi = None
    for par in mark_area:
        if not par:
            continue
        refs.append(par[0].get("xAxis"))
        for y in (par[0].get("yAxis"), par[1].get("yAxis")):
            if y is None:
                continue
            lo = y if lo is None else min(lo, y)
            hi = y if hi is None else max(hi, y)
    for pt_ in mark_point:
        coord = pt_.get("coord") or []
        if coord:
            refs.append(coord[0])
            y = coord[1] if len(coord) > 1 else None
            if y is not None:
                lo = y if lo is None else min(lo, y)
                hi = y if hi is None else max(hi, y)
    for d in draw:
        t = d.get("t0") if d.get("t0") is not None else d.get("t1")
        if t is not None:
            refs.append(t)
        for k in ("p0", "p1"):
            y = d.get(k)
            if y is None:
                continue
            lo = y if lo is None else min(lo, y)
            hi = y if hi is None else max(hi, y)
    refs_num = [r for r in refs if isinstance(r, (int, float))]
    if not refs_num:
        return None
    tmin = min(refs_num)
    span = max((max(refs_num) - tmin) * 4, 3600 * 2)
    fin = last_time if not isinstance(last_time, (int, float)) else min(last_time, tmin + span)
    return {"start_time": tmin, "end_time": fin, "price_min": lo, "price_max": hi}


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------


class ToolSpec:
    """Una herramienta: lo que el modelo ve, lo que necesita y cuánto puede tardar.

    `puertos` son los puertos OBLIGATORIOS (sin ellos la respuesta es
    `unavailable` sin ejecutar el handler) y `puertos_opcionales` los que mejoran
    el resultado si están (`now` da la hora del broker solo si hay bróker).
    Separar los dos es lo que permite que `journal_append` guarde la entrada sin
    bróker declarando que no la pudo enrichir, en vez de fallar entero.
    """

    __slots__ = ("nombre", "schema", "puertos", "puertos_opcionales", "timeout_s", "handler")

    def __init__(
        self,
        nombre: str,
        schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any], ToolCtx], Any],
        puertos: Sequence[str] = (),
        puertos_opcionales: Sequence[str] = (),
        timeout_s: float = TIMEOUT_S_POR_DEFECTO,
    ) -> None:
        self.nombre = nombre
        self.schema = schema
        self.handler = handler
        self.puertos = tuple(puertos)
        self.puertos_opcionales = tuple(puertos_opcionales)
        self.timeout_s = float(timeout_s)

    def falta_puerto(self, deps: AgentDeps) -> Optional[str]:
        """Nombre del primer puerto obligatorio que no está cableado."""
        for nombre in self.puertos:
            if getattr(deps, nombre, None) is None:
                return nombre
        return None

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "<ToolSpec {0} puertos={1} t={2}s>".format(
            self.nombre, ",".join(self.puertos) or "-", self.timeout_s
        )


def _schema(
    nombre: str,
    descripcion: str,
    propiedades: Optional[Dict[str, Any]] = None,
    requeridos: Sequence[str] = (),
) -> Dict[str, Any]:
    """Envoltorio de schema en el dialecto que espera `litellm` (OpenAI tools)."""
    return {
        "type": "function",
        "function": {
            "name": nombre,
            "description": descripcion,
            "parameters": {
                "type": "object",
                "properties": dict(propiedades or {}),
                "required": list(requeridos),
            },
        },
    }


_TF_ENUM = ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]


#: El registro. El orden es el que ve el modelo en el prompt de herramientas, así
#: que va de "lo que se pregunta siempre" a "lo que se pide con una frase concreta".
TOOLS: Dict[str, ToolSpec] = {
    "account_info": ToolSpec(
        "account_info",
        _schema(
            "account_info",
            "Métricas en tiempo real de la cuenta: balance, equity, flotante, margen usado, "
            "margen libre y nivel de margen. Úsala antes de afirmar cuánto riesgo queda.",
        ),
        _h_account_info,
        puertos=("market",),
    ),
    "positions_list": ToolSpec(
        "positions_list",
        _schema(
            "positions_list",
            "Posiciones abiertas: símbolo, dirección BUY/SELL, volumen, precio de apertura, SL/TP y "
            "flotante.",
        ),
        _h_positions_list,
        puertos=("market",),
    ),
    "price": ToolSpec(
        "price",
        _schema(
            "price",
            "Cotización Bid/Ask en vivo de un símbolo (EURUSD, XAUUSD, 6E...).",
            {"symbol": {"type": "string", "description": "Símbolo, ej: EURUSD, XAUUSD."}},
            ["symbol"],
        ),
        _h_price,
        puertos=("market",),
    ),
    "now": ToolSpec(
        "now",
        _schema(
            "now",
            "Fecha y hora actual: UTC (la de las velas, las killzones y el gráfico), hora del "
            "servidor del broker y día de trading (el que cuentan DD y topes).",
        ),
        _h_now,
        puertos_opcionales=("market",),
    ),
    "history": ToolSpec(
        "history",
        _schema(
            "history",
            "Operaciones cerradas del bróker de los últimos N días.",
            {"days": {"type": "integer", "description": "Días a mirar atrás (7 por defecto).", "minimum": 1, "maximum": 365}},
            ),
        _h_history,
        puertos=("market",),
    ),
    "patterns": ToolSpec(
        "patterns",
        _schema(
            "patterns",
            "Patrones SMC en vivo de un símbolo: Fair Value Gaps (activos o mitigados), Order Blocks "
            "de break of structure y Liquidity Sweeps de PDH/PDL. Los `start_time`/`time` que "
            "devuelve son las referencias EXACTAS que `chart_annotate` valida.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD, XAUUSD."},
                "timeframe": {"type": "string", "description": "M1, M5, M15, M30, H1, H4 (M15 por defecto)."},
            },
        ),
        _h_patterns,
        puertos=("market",),
        timeout_s=TIMEOUT_S_CONtexto,
    ),
    "chart_snapshot": ToolSpec(
        "chart_snapshot",
        _schema(
            "chart_snapshot",
            "Snapshot técnico completo de un símbolo: precio, PDH/PDL, velas recientes, SMC, CVD y el "
            "score del risk engine. Es lo que hay que leer antes de diagnosticar.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD (EURUSD por defecto)."},
                "timeframe": {"type": "string", "description": "M1, M5, M15, M30, H1, H4 (M15 por defecto)."},
            },
        ),
        _h_chart_snapshot,
        puertos=("market",),
        timeout_s=TIMEOUT_S_CONtexto,
    ),
    "setup_score": ToolSpec(
        "setup_score",
        _schema(
            "setup_score",
            "Setup Score determinista (0-100) del risk engine para una dirección, con el breakdown de "
            "componentes, veredicto (ALTA/MEDIA/SIN OPERATIVA), régimen, killzone y nivel de "
            "invalidez. Úsla SIEMPRE antes de citar un score y cita el valor que devuelve: recalcularlo "
            "o reutilizar uno viejo es exactamente el error que esta herramienta evita.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD (EURUSD por defecto)."},
                "timeframe": {"type": "string", "description": "M1, M5, M15, M30, H1, H4 (M15 por defecto)."},
                "direction": {"type": "string", "enum": ["BUY", "SELL"], "description": "BUY por defecto."},
            },
        ),
        _h_setup_score,
        puertos=("market",),
        timeout_s=TIMEOUT_S_CONtexto,
    ),
    "orderflow_snapshot": ToolSpec(
        "orderflow_snapshot",
        _schema(
            "orderflow_snapshot",
            "Fotografía del order flow: CVD acumulado, delta del último trade, volumen total, volumen "
            "de compra/venta, precio, conteo de picos Z-score y alertas recientes.",
        ),
        _h_orderflow_snapshot,
        puertos=("market",),
    ),
    "orderflow_alerts": ToolSpec(
        "orderflow_alerts",
        _schema(
            "orderflow_alerts",
            "Últimas alertas de absorción y picos institucionales del motor de order flow.",
        ),
        _h_orderflow_alerts,
        puertos=("market",),
    ),
    "economic_news": ToolSpec(
        "economic_news",
        _schema(
            "economic_news",
            "Última y próxima noticia económica relevante, con hora, impacto y título. La lectura "
            "incluye `stale` y `reason`: si el calendario no se pudo leer, DILO en vez de tratar el "
            "silencio como 'no hay noticias'.",
            {"symbols": {"type": "array", "items": {"type": "string"}, "description": "Símbolos a filtrar (opcional)."}},
        ),
        _h_economic_news,
        puertos=("news",),
        timeout_s=8.0,
    ),
    "trade_query": ToolSpec(
        "trade_query",
        _schema(
            "trade_query",
            "Operaciones sincronizadas en la base local, con filtros por símbolo, dirección y días.",
            {
                "symbol": {"type": "string", "description": "Filtro por símbolo (EURUSD, 6E...)."},
                "action": {"type": "string", "description": "BUY o SELL."},
                "days": {"type": "integer", "description": "Últimos N días.", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "description": "Máximo de filas (50 por defecto).", "minimum": 1, "maximum": 500},
            },
        ),
        _h_trade_query,
        puertos=("store",),
    ),
    "journal_append": ToolSpec(
        "journal_append",
        _schema(
            "journal_append",
            "Registra una entrada en la bitácora. Campos: poi_type (FVG, OrderBlock, Breaker), "
            "liquidity_swept (PDH, PDL...), cme_confirmation, emotion, plan_compliance, tags, notes.",
            {
                "symbol": {"type": "string", "description": "Símbolo (EURUSD por defecto)."},
                "action": {"type": "string", "description": "BUY o SELL."},
                "poi_type": {"type": "string", "description": "Tipo de setup: FVG, Order Block, Breaker."},
                "liquidity_swept": {"type": "string", "description": "Liquidez barrida: PDH, PDL, Session High/Low."},
                "cme_confirmation": {"type": "string", "description": "Confirmación: Divergencia CVD, Spike Volume, Absorción."},
                "emotion": {"type": "string", "description": "Estado emocional durante la operación."},
                "plan_compliance": {"type": "boolean", "description": "¿Se cumplió el plan?"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Etiquetas libres."},
                "notes": {"type": "string", "description": "Notas de la operación."},
            },
        ),
        _h_journal_append,
        puertos=("store",),
        puertos_opcionales=("market",),
    ),
    "journal_list": ToolSpec(
        "journal_list",
        _schema(
            "journal_list",
            "Entradas de la bitácora, con filtro opcional por símbolo y días.",
            {
                "symbol": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        ),
        _h_journal_list,
        puertos=("store",),
    ),
    "get_chart_drawings": ToolSpec(
        "get_chart_drawings",
        _schema(
            "get_chart_drawings",
            "Dibujos manuales que el usuario guardó en el gráfico (líneas, niveles, rectángulos, "
            "texto, mediciones), con tiempo en epoch segundos y precios reales. Úsalo antes de "
            "opinar sobre lo que el usuario marcó.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD."},
                "timeframe": {"type": "string", "enum": _TF_ENUM, "description": "Timeframe (M15 por defecto)."},
            },
            ["symbol"],
        ),
        _h_get_chart_drawings,
        puertos=("store",),
    ),
    "set_chart_alert": ToolSpec(
        "set_chart_alert",
        _schema(
            "set_chart_alert",
            "Coloca una alarma VISUAL persistente en el gráfico y la guarda; cuando el precio la "
            "alcanza y se cumplen todas las condiciones, el dashboard avisa. Para 'avísame si el "
            "EURUSD toca 1.1650 dentro de la killzone de Nueva York'.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD, XAUUSD."},
                "price": {"type": "number", "description": "Precio del nivel."},
                "label": {"type": "string", "description": "Etiqueta corta, ej: 'Resistencia'."},
                "side": {"type": "string", "enum": ["above", "below", "touch"], "description": "touch por defecto."},
                "timeframe": {"type": "string", "enum": _TF_ENUM, "description": "Referencia para las condiciones SMC (M15)."},
                "conditions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["killzone", "smc", "ttl"], "description": "killzone, smc o ttl."},
                            "name": {"type": "string", "description": "Para killzone: 'Londres' o 'Nueva York' (UTC)."},
                            "pattern": {"type": "string", "enum": ["fvg", "order_blocks", "sweep"], "description": "Para smc."},
                            "side": {"type": "string", "description": "Para smc: 'bullish'/'bearish'; para sweep: 'PDH_SWEEP'/'PDL_SWEEP'."},
                            "minutes": {"type": "integer", "description": "Para ttl: minutos de vigencia."},
                        },
                    },
                },
                "expires_in_minutes": {"type": "integer", "description": "Autoexpiración en minutos desde la creación."},
            },
            ["symbol", "price"],
        ),
        _h_set_chart_alert,
        puertos=("store",),
    ),
    "chart_annotate": ToolSpec(
        "chart_annotate",
        _schema(
            "chart_annotate",
            "Dibuja en el gráfico. Dos modos: (1) marcar PATRONES REALES: las refs son los "
            "`start_time`/`time` EXACTOS que devolvió `patterns` del mismo símbolo/timeframe, cópialos "
            "tal cual; el servidor valida cada ref y descarta las que no existen. (2) FIGURAS LIBRES: "
            "kind line/horizontal/rect/text con coordenadas propias, recortadas a la ventana real de "
            "velas.",
            {
                "symbol": {"type": "string", "description": "Símbolo, ej: EURUSD."},
                "timeframe": {"type": "string", "enum": _TF_ENUM, "description": "El mismo con el que se consultó 'patterns' (M15)."},
                "actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["markArea", "markPoint", "line", "horizontal", "rect", "text"]},
                            "pattern": {"type": "string", "enum": ["fvgs", "order_blocks", "sweeps"], "description": "Grupo donde buscar la ref."},
                            "ref": {"type": ["integer", "string"], "description": "start_time o time exacto devuelto por 'patterns'."},
                            "start_time": {"type": ["integer", "string"], "description": "Inicio en epoch segundos."},
                            "end_time": {"type": ["integer", "string"], "description": "Fin en epoch segundos (line/rect)."},
                            "price_from": {"type": "number", "description": "Precio inicial."},
                            "price_to": {"type": "number", "description": "Precio final (line/rect)."},
                            "name": {"type": "string", "description": "Etiqueta opcional."},
                        },
                        "required": ["kind"],
                    },
                },
            },
            ["symbol", "actions"],
        ),
        _h_chart_annotate,
        puertos=("store",),
        timeout_s=TIMEOUT_S_CONtexto,
    ),
    "mt5_export_read": ToolSpec(
        "mt5_export_read",
        _schema(
            "mt5_export_read",
            "Exportaciones .txt del indicador 'AI Chart Assistant' (MQL5\\Files). Acciones: 'list' "
            "(listar), 'read' (con filename) o 'latest' (filtrable por symbol/mode).",
            {
                "action": {"type": "string", "enum": ["list", "read", "latest"], "description": "list, read o latest."},
                "filename": {"type": "string", "description": "Fichero a leer (solo con action='read')."},
                "symbol": {"type": "string", "description": "Filtrar por símbolo (vacío = todos)."},
                "mode": {"type": "string", "description": "Filtrar por modo: 'Analyze Chart', 'Full Trade Plan'..."},
                "limit": {"type": "integer", "description": "Máximo de filas en 'list' (10 por defecto)."},
            },
            ["action"],
        ),
        _h_mt5_export_read,
        puertos=("exports",),
    ),
}


def tool_names() -> List[str]:
    """Nombres de todas las herramientas, en el orden del registro."""
    return list(TOOLS.keys())


def tool_schema(nombre: str) -> Optional[Dict[str, Any]]:
    return TOOLS[nombre].schema if nombre in TOOLS else None


def tool_schemas(nombres: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Schemas para el llamador, filtrados por los nombres permitidos del rol.

    Filtra en el ORDEN del registro, no en el de la entrada: el prompt de
    herramientas es estable entre turnos, y un orden que cambia hace que dos
    llamadas idénticas del modelo production distinta.
    """
    permitidos = set(nombres) if nombres else None
    return [
        spec.schema
        for nombre, spec in TOOLS.items()
        if permitidos is None or nombre in permitidos
    ]


#: Qué herramienta merece la pena según el tema detectado. Es el criterio del router
#: para decidir si una frase NECESITA herramientas, y el prefiltrado de la ruta
#: simple.
TOOLS_POR_TEMA: Dict[str, Tuple[str, ...]] = {
    "account": ("account_info",),
    "positions": ("positions_list",),
    "history": ("history",),
    "news": ("economic_news",),
    "trade": ("price", "patterns", "chart_snapshot", "setup_score"),
}

#: Herramientas de la ruta simple: las que se consultan por adelantado para
#: inyectar el dato en el prompt. Deliberadamente un subconjunto pequeño de
#: `TOOLS_POR_TEMA`: son las que el modelo local lee bien como texto y las que no
#: necesitan argumentos complicados para significar algo.
LIVE_DATA_POR_TEMA: Dict[str, Tuple[str, ...]] = {
    "account": ("account_info",),
    "positions": ("positions_list",),
    "history": ("history",),
    "news": ("economic_news",),
}

#: Argumentos por defecto de la ruta simple. `history` sin `days` recibiría el 7 del
#: handler, pero se pone explícito para que la línea del prompt diga "últimos 7
#: días" sin depender de un default que vive en otro módulo.
ARGS_LIVE_DATA: Dict[str, Dict[str, Any]] = {"history": {"days": 7}}


def tools_for_topics(
    temas: Iterable[str],
    base: Optional[Dict[str, Tuple[str, ...]]] = None,
    con_live: bool = False,
) -> List[str]:
    """Herramientas que merecen la pena según los temas que se mencionaron.

    `con_live=True` devuelve el subconjunto de la ruta simple (para inyectar datos
    sin tools); `False` devuelve el juego completo del tema (para el prompt de
    herramientas cuando el mensaje es complejo). La lista sale ordenada por el
    registro, por el mismo motivo que `tool_schemas`.
    """
    tabla = (base if base is not None else (LIVE_DATA_POR_TEMA if con_live else TOOLS_POR_TEMA))
    pedidos = set()
    for tema in temas or ():
        for nombre in tabla.get(tema, ()):  # type: ignore[arg-type]
            pedidos.add(nombre)
    return [n for n in TOOLS if n in pedidos]


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------


def parse_args(args_json: Any) -> Dict[str, Any]:
    """Los argumentos del modelo, como dict.

    `execute_tool` en REF hacía `json.loads(args_json) if args_json else {}` y un
    `except` que devolvía `{}` para cualquier error. Un arguments malformado y un
    arguments vacío son COSAS DISTINTAS: el primero significa que el modelo
    escribió algo que no es JSON y hay que decírselo, el segundo que no pidió nada.
    Aquí se distinguen y el error viaja en `args_error`.
    """
    if args_json is None or args_json == "":
        return {}
    if isinstance(args_json, dict):
        return dict(args_json)
    try:
        parsed = json.loads(args_json)
    except (TypeError, ValueError) as exc:
        return {"__args_error__": "arguments no son JSON: {0}".format(exc)}
    if isinstance(parsed, dict):
        return parsed
    return {"__args_error__": "arguments no son un objeto JSON"}


def _dispatch(nombre: str, args: Dict[str, Any], ctx: ToolCtx) -> Dict[str, Any]:
    """Ejecuta el handler y envuelve el resultado en el sobre común."""
    spec = TOOLS.get(nombre)
    if spec is None:
        return _fail("herramienta no encontrada: {0}".format(nombre))
    args_error = args.get("__args_error__") if isinstance(args, dict) else None
    if args_error:
        return _fail(args_error)
    args = {k: v for k, v in args.items() if k != "__args_error__"}
    try:
        return _ok(spec.handler(args, ctx))
    except AgentPortError as exc:
        return _fail(exc, STATUS_FAILED)
    except Exception as exc:  # noqa: BLE001 - la excepción se le DICE al modelo
        return _fail(exc)


def _prepara(
    nombre: str,
    args: Any,
    deps: Optional[AgentDeps],
) -> Tuple[Optional[ToolSpec], Dict[str, Any]]:
    """Resuelve la spec y comprueba puertos. Devuelve `(spec, respuesta_ya_lista)`.

    El chequeo va ANTES de crear el hilo porque un `unavailable` por puerto
    ausente no debe costar un `to_thread`: es la respuesta más frecuente del
    sistema mientras la Fase 6 no cablee el mercado, y la más barata de producir.
    """
    deps = deps if deps is not None else AgentDeps()
    spec = TOOLS.get(nombre)
    if spec is None:
        return None, _fail("herramienta no encontrada: {0}".format(nombre))
    falta = spec.falta_puerto(deps)
    if falta:
        return spec, _fail(deps.puerto_ausente(falta), STATUS_UNAVAILABLE)
    return spec, {}


async def _await_tool(
    nombre: str,
    args: Any,
    deps: Optional[AgentDeps],
    conversation_id: str,
    round_no: Optional[int],
) -> Dict[str, Any]:
    """El cuerpo común de las dos vías, con el corte de tiempo ya resuelto.

    El `wait_for` va ENCIMA del `to_thread` y no dentro porque un hilo no se puede
    cancelar: si el handler se queda esperando al bróker, un corte dentro del hilo
    solo se ve cuando el handler vuelve, que es justo cuando ya no hace falta. El
    hilo abandonado no se mata (da la basura al recolector), pero el cliente
    recibe su respuesta a tiempo, que es lo que importa en un SSE.

    Y no es una sutileza teórica: el `timeout=4.0` de REF aplicado a
    `chart_snapshot` cortaba la respuesta mientras el bróker seguía trabajando, y
    el modelo recibía "tiempo agotado" para un snapshot que salía un segundo
    después.
    """
    spec, listo = _prepara(nombre, args, deps)
    if listo:
        return listo
    if spec is None:  #pragma: no cover - _prepara nunca devuelve esto sin `listo`
        return _fail("herramienta no encontrada: {0}".format(nombre))
    deps = deps if deps is not None else AgentDeps()
    ctx = ToolCtx(deps, conversation_id, round_no)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_dispatch, nombre, parse_args(args), ctx),
            timeout=spec.timeout_s,
        )
    except asyncio.TimeoutError:
        return _fail(
            "tiempo agotado ({0:.0f}s) en {1}".format(spec.timeout_s, nombre),
            STATUS_UNAVAILABLE,
        )


def run_tool(
    nombre: str,
    args: Optional[Dict[str, Any]] = None,
    deps: Optional[AgentDeps] = None,
    conversation_id: str = "",
    round_no: Optional[int] = None,
) -> Dict[str, Any]:
    """Ejecuta una herramienta de forma síncrona, con el timeout de su `ToolSpec`.

    El corte de tiempo se reporta como `unavailable`, no como `failed`: `failed`
    significa "el puerto reventó", y un puerto que no contesta no ha reventado, no
    está. La diferencia cambia la reacción del humano: un `failed` se investiga, un
    `unavailable` se espera.

    Acepta los argumentos como dict o como JSON, que son los dos formatos que
    llegan del bucle: `parse_args` los normaliza igual en las dos vías.

    Dentro de un loop de asyncio esto NO se puede usar, porque `asyncio.run` lo
    rechaza. Para ese caso está `execute_tool`, y el error lo dice con nombre y
    remedio en vez de soltar el `RuntimeError` interno de Python.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Se comprueba ANTES de crear la corrutina: `asyncio.run` la rechazaría
        # después y quedaría sin awaits (un `RuntimeWarning` en cada llamada).
        return _fail(
            "run_tool() no se puede llamar desde un loop de asyncio; usa execute_tool()"
        )
    try:
        return asyncio.run(_await_tool(nombre, args, deps, conversation_id, round_no))
    except Exception as exc:  # noqa: BLE001 - último recurso
        return _fail(exc)


async def execute_tool(
    nombre: str,
    args_json: Any = None,
    deps: Optional[AgentDeps] = None,
    conversation_id: str = "",
    round_no: Optional[int] = None,
) -> Dict[str, Any]:
    """Igual que `run_tool`, pero awaitable: es la que consume el bucle SSE.

    REF envolvía con `asyncio.wait_for(..., timeout=4.0)` el `_handler` de las
    dieciocho herramientas (`agent.py:851`). Aquí cada una tiene el suyo.
    """
    return await _await_tool(nombre, args_json, deps, conversation_id, round_no)


__all__ = [
    "ARGS_LIVE_DATA",
    "LIVE_DATA_POR_TEMA",
    "MAX_ACCIONES_ANNOTATE",
    "TIMEOUT_S_CONtexto",
    "TIMEOUT_S_POR_DEFECTO",
    "TOOLS",
    "TOOLS_POR_TEMA",
    "ToolCtx",
    "ToolSpec",
    "execute_tool",
    "parse_args",
    "run_tool",
    "tool_names",
    "tool_schema",
    "tool_schemas",
    "tools_for_topics",
]
