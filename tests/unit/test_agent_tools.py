"""Tests de `agent/tools.py`: las 18 herramientas contra puertos falsos.

Por qué dobles y no MT5
-----------------------
La razón de que exista `agent/ports.py` es exactamente esta: las herramientas se
pueden probar sin el bróker. Un test que necesita MetaTrader5 no se ejecuta en la
máquina de desarrollo y acaba sin ejecutarse nunca, que es como se pierde una suite.

Lo que estos tests Fijan, más allá de "devuelve algo":

- El **vocabulario de estados**: puerto ausente es `unavailable`, puerto que
  revienta es `failed`. Confundirlos es lo que hacía REF, y hace que un humano no
  sepa si reintentar o mirar un log.
- El **timeout por herramienta**: una tool lenta tiene que cortar con SU límite,
  no con el de otra. Un único 4 s global era una apuesta, y se comprueba aquí.
- Los **defaults tolerantes**: el modelo manda `"7"` o `null`; eso no es una
  excepción, es un default.
- La **validación de refs** de `chart_annotate`: una `ref` inventada se descarta,
  y un análisis de OTRA conversación no vale.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import pytest

from agent import tools as T
from agent.ports import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    AgentDeps,
    AnalysisAnchors,
)


# ---------------------------------------------------------------------------
# Dobles de puerto
# ---------------------------------------------------------------------------


class MarketFake:
    """`MarketPort` mínimo y con memoria de lo que se le pidió."""

    #: Sentinel para "este símbolo no tiene precio". Sin él, `price=None` se
    #: confundiría con "no me digas qué precio poner" y el test de `price` probaría
    #: el caso equivocado.
    SIN_PRECIO = object()

    def __init__(
        self,
        price: Any = SIN_PRECIO,
        patterns: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        cot: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._price = {"symbol": "EURUSD", "bid": 1.1650, "ask": 1.1652} if price is self.SIN_PRECIO else price
        self._patterns = patterns
        self._snapshot = snapshot
        self.cot = cot
        self.calls: List[str] = []

    def account_info(self) -> Dict[str, Any]:
        self.calls.append("account_info")
        return {"balance": 10_000.0, "equity": 10_120.5, "margin_free": 8_000.0}

    def positions(self) -> List[Dict[str, Any]]:
        self.calls.append("positions")
        return [{"symbol": "EURUSD", "action": "BUY", "volume": 0.1, "profit": 12.0}]

    def history(self, days: int) -> List[Dict[str, Any]]:
        self.calls.append("history:{0}".format(days))
        return [{"ticket": 1, "days": days}]

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        self.calls.append("price:{0}".format(symbol))
        return self._price

    def pattern_data(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        self.calls.append("patterns:{0} {1}".format(symbol, timeframe))
        return self._patterns

    def chart_snapshot(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        self.calls.append("snapshot:{0} {1}".format(symbol, timeframe))
        if self._snapshot is None:
            return {}
        return self._snapshot

    def orderflow_snapshot(self) -> Dict[str, Any]:
        return {"cvd": 1234.0, "delta": 12.0}

    def orderflow_alerts(self) -> List[Dict[str, Any]]:
        return [{"type": "ABSORPTION", "ts": 1_700_000_000}]

    def daily_risk_state(self) -> Dict[str, Any]:
        return {"trading_day": "2026-10-03", "dd_daily": 0.0, "trades_today": 1}

    def enrich_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append("enrich")
        enriched = dict(entry)
        enriched["entry_price"] = 1.1650
        return enriched

    def broker_time(self) -> str:
        return "2026-10-03 11:30:00 BRT"

    def trading_day(self) -> str:
        return "2026-10-03"


class MarketBoom(MarketFake):
    """Revienta en `price`. Para fijar `failed` frente a `unavailable`."""

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        raise RuntimeError("MT5 no responde")


class MarketLento(MarketFake):
    """Tarda más que su timeout. Para el corte por herramienta."""

    def __init__(self, segundos: float) -> None:
        super().__init__()
        self.segundos = segundos

    def account_info(self) -> Dict[str, Any]:
        time.sleep(self.segundos)
        return {"balance": 1.0}


class StoreFake:
    """`StorePort` en memoria. Guarda lo que se le pasa para poder assertarlo."""

    def __init__(self) -> None:
        self.mensajes: List[Dict[str, Any]] = []
        self.journal: List[Dict[str, Any]] = []
        self.alertas: List[Dict[str, Any]] = []
        self.drawings: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.llamadas: Dict[str, Any] = {}

    def get_role(self, role_id: str) -> Optional[Dict[str, Any]]:
        return None

    def get_settings(self) -> Dict[str, Any]:
        return {}

    def add_message(self, conversation_id, role, content, tool_call_id=None, name=None) -> None:
        self.mensajes.append(
            {"conversation_id": conversation_id, "role": role, "content": content,
             "tool_call_id": tool_call_id, "name": name}
        )

    def get_messages(self, conversation_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return []

    def add_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        fila = dict(entry)
        fila["id"] = len(self.journal) + 1
        self.journal.append(fila)
        return fila

    def list_journal(self, symbol=None, days=None, limit=50) -> List[Dict[str, Any]]:
        self.llamadas["list_journal"] = {"symbol": symbol, "days": days, "limit": limit}
        return self.journal[:limit]

    def list_trades(self, symbol=None, action=None, days=None, limit=200) -> List[Dict[str, Any]]:
        self.llamadas["list_trades"] = {"symbol": symbol, "action": action, "days": days, "limit": limit}
        return self.trades

    def get_drawings(self, symbol: str, timeframe: str = "M15") -> List[Dict[str, Any]]:
        self.llamadas["get_drawings"] = {"symbol": symbol, "timeframe": timeframe}
        return self.drawings

    def add_chart_alert(self, symbol, price, label=None, side=None, conditions=None,
                        timeframe=None, expires_at=None) -> Dict[str, Any]:
        alerta = {"symbol": symbol, "price": price, "label": label, "side": side,
                  "conditions": conditions, "timeframe": timeframe, "expires_at": expires_at}
        self.alertas.append(alerta)
        return alerta

    def get_agent_topics(self) -> Dict[str, List[str]]:
        return {}

    def get_trading_config(self) -> Dict[str, Any]:
        return {}


class NewsFake:
    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.payload = payload if payload is not None else {"last": {"title": "NFP"}, "next": {"title": "IPC"}}
        self.simbolos: Any = "no-called"

    def news(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        self.simbolos = symbols
        return self.payload


class ExportsFake:
    def __init__(self) -> None:
        self.leidos: List[str] = []

    def list_exports(self, symbol=None, mode=None, limit=10):
        return [{"filename": "EURUSD_Analyze.txt"}]

    def read_export(self, filename: str):
        self.leidos.append(filename)
        return {"filename": filename, "content": "ATR 12"}

    def latest_export(self, symbol=None, mode=None):
        return {"filename": "ultimo.txt", "content": "x"}

    def extract_export_path(self, text: str) -> Optional[str]:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def deps() -> AgentDeps:
    return AgentDeps(
        market=MarketFake(),
        store=StoreFake(),
        news=NewsFake(),
        exports=ExportsFake(),
        anchors=AnalysisAnchors(now=lambda: 1_000.0),
    )


def _patrones_data() -> Dict[str, Any]:
    return {
        "candles": [
            {"time": 1_000_000, "open": 1.16, "high": 1.162, "low": 1.159, "close": 1.161, "volume": 10},
            {"time": 1_000_900, "open": 1.161, "high": 1.163, "low": 1.160, "close": 1.162, "volume": 12},
        ],
        "analysis": {
            "patterns": {
                "fvgs": [{"type": "BULLISH_FVG", "start_time": 1_000_000, "top": 1.1620, "bottom": 1.1600}],
                "order_blocks": [{"type": "BEARISH_OB", "start_time": 1_000_900, "top": 1.1630, "bottom": 1.1615}],
                "sweeps": [{"type": "PDH_SWEEP", "time": 1_000_900, "wick_extreme": 1.1640}],
            }
        },
    }


def _snapshot(symbol: str = "EURUSD") -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "current_price": 1.1650,
        "risk_engine": {
            "bull": {"score": 71.4, "verdict": "ALTA", "components": {"killzone": 1.0, "trend": 0.8}},
            "bear": {"score": 33.1, "verdict": "SIN OPERATIVA", "components": {"killzone": 1.0}},
            "regime": {"regime": "expansion"},
            "killzone": {"in_killzone": True, "name": "Londres"},
            "invalidation": {"BUY": 1.1600, "SELL": 1.1680},
        },
    }


# ---------------------------------------------------------------------------
# El registro
# ---------------------------------------------------------------------------


def test_registro_tiene_las_18_herramientas():
    assert len(T.TOOLS) == 18


def test_toda_herramienta_declara_puerto_o_es_opcional():
    """Una herramienta sin puertos tiene que justificarse en el registro.

    `now` es la única: lee `core.clock` y solo mejora con el bróker. Si aparece una
    segunda, es que alguien se olvidó de declarar la dependencia y el fallo sale en
    producción como `unavailable` en vez de en el test.
    """
    sin_puertos = [n for n, s in T.TOOLS.items() if not s.puertos]
    assert sin_puertos == ["now"]


def test_esquemas_son_validos_para_litellm():
    for nombre, spec in T.TOOLS.items():
        f = spec.schema["function"]
        assert f["name"] == nombre
        assert f["description"].strip()
        params = f["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        for req in params["required"]:
            assert req in params["properties"], "{0} exige {1} y no lo declara".format(nombre, req)


def test_tool_schemas_filtran_por_permitidos_y_mantienen_el_orden():
    permitidos = {"setup_score", "account_info"}
    nombres = [s["function"]["name"] for s in T.tool_schemas(permitidos)]
    assert nombres == ["account_info", "setup_score"]
    assert len(T.tool_schemas()) == 18


def test_todos_los_tools_por_tema_existen():
    for tabla in (T.TOOLS_POR_TEMA, T.LIVE_DATA_POR_TEMA):
        fortools = [n for tools in tabla.values() for n in tools]
        for nombre in fortools:
            assert nombre in T.TOOLS, "{0} no está en el registro".format(nombre)


def test_tools_for_topics_devuelve_el_juego_del_tema():
    assert T.tools_for_topics(["account"]) == ["account_info"]
    assert T.tools_for_topics(["account", "trade"]) == [
        "account_info", "price", "patterns", "chart_snapshot", "setup_score",
    ]
    # La ruta simple es un subconjunto: sin tools no se puede pasar lo que el
    # modelo local no sabe pedir con argumentos.
    assert set(T.tools_for_topics(["trade"], con_live=True)) <= set(T.tools_for_topics(["trade"]))
    assert T.tools_for_topics([]) == []


# ---------------------------------------------------------------------------
# Estados: la distinción que REF no tenía
# ---------------------------------------------------------------------------


def test_puerto_ausente_es_unavailable_no_failed(deps):
    deps.store = None
    res = T.run_tool("journal_list", {}, deps)
    assert res["status"] == STATUS_UNAVAILABLE
    assert "store" in res["error"]


def test_herramienta_inexistente_es_failed():
    res = T.run_tool("no_existe", {}, AgentDeps())
    assert res["status"] == STATUS_FAILED
    assert "no_existe" in res["error"]


def test_puerto_que_revienta_es_failed_no_unavailable():
    deps = AgentDeps(market=MarketBoom(), store=StoreFake())
    res = T.run_tool("price", {"symbol": "EURUSD"}, deps)
    assert res["status"] == STATUS_FAILED
    assert "MT5 no responde" in res["error"]


def test_price_sin_simbolo_no_se_disfraza_de_offline():
    """El bug de REF: el 'offline' viajaba DENTRO de un ok."""
    deps = AgentDeps(market=MarketFake(price=None), store=StoreFake())
    res = T.run_tool("price", {"symbol": "EURUSD"}, deps)
    assert res["status"] == STATUS_FAILED
    assert res["data"] is None
    assert "EURUSD" in res["error"]


def test_mt5_export_read_sin_puerto_es_unavailable():
    deps = AgentDeps(store=StoreFake())
    res = T.run_tool("mt5_export_read", {"action": "list"}, deps)
    assert res["status"] == STATUS_UNAVAILABLE
    assert "exports" in res["error"]


# ---------------------------------------------------------------------------
# Defaults tolerantes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crudo,esperado",
    [("7", 7), (7, 7), (7.9, 7), (None, 7), ("siete", 7), (-3, 1), (10_000, 365), (True, 7)],
)
def test_history_tolera_argumentos_no_numericos(deps, crudo, esperado):
    res = T.run_tool("history", {"days": crudo}, deps)
    assert res["status"] == STATUS_OK
    assert "history:{0}".format(esperado) in deps.market.calls


def test_trade_query_sin_days_no_filtra(deps):
    T.run_tool("trade_query", {}, deps)
    assert deps.store.llamadas["list_trades"] == {
        "symbol": None, "action": None, "days": None, "limit": 50
    }


def test_trade_query_normaliza_simbolo_y_direccion(deps):
    T.run_tool("trade_query", {"symbol": "eurusd", "action": "buy", "days": "30", "limit": "10"}, deps)
    assert deps.store.llamadas["list_trades"] == {
        "symbol": "EURUSD", "action": "BUY", "days": 30, "limit": 10
    }


def test_limit_se_recorta_a_los_maximos_del_schema(deps):
    T.run_tool("trade_query", {"limit": 100_000}, deps)
    assert deps.store.llamadas["list_trades"]["limit"] == 500


# ---------------------------------------------------------------------------
# Herramientas concretas
# ---------------------------------------------------------------------------


def test_account_info_pasa_los_datos(deps):
    res = T.run_tool("account_info", {}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["equity"] == 10_120.5


def test_now_da_utc_y_hora_del_broker(deps):
    res = T.run_tool("now", {}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["datetime_utc"].endswith("UTC")
    assert res["data"]["broker_time"].endswith("BRT")
    assert res["data"]["trading_day"] == "2026-10-03"


def test_now_sin_bróker_no_inventa_la_hora_del_broker():
    res = T.run_tool("now", {}, AgentDeps())
    assert res["status"] == STATUS_OK
    assert res["data"]["broker_time"] is None
    assert res["data"]["datetime_utc"].endswith("UTC")


def test_patterns_ancla_el_analisis_de_la_conversacion():
    deps = AgentDeps(
        market=MarketFake(patterns=_patrones_data()),
        anchors=AnalysisAnchors(now=lambda: 1_000.0),
    )
    res = T.run_tool("patterns", {"symbol": "eurusd", "timeframe": "m15"}, deps, conversation_id="c1")
    assert res["status"] == STATUS_OK
    anclaje = deps.anchors.get("c1", "EURUSD", "M15")
    assert anclaje is not None
    assert anclaje["candle_lo"] == 1.159
    assert anclaje["candle_hi"] == 1.163
    # Y no vale para otro chat: es el bug de fuga del `_LAST_ANALYSIS` global.
    assert deps.anchors.get("c2", "EURUSD", "M15") is None


def test_patterns_sin_datos_falla_con_mensaje_util():
    deps = AgentDeps(market=MarketFake(patterns=None))
    res = T.run_tool("patterns", {"symbol": "EURUSD"}, deps)
    assert res["status"] == STATUS_FAILED
    assert "EURUSD" in res["error"]


def test_setup_score_lee_el_score_y_no_lo_recalcula(deps):
    deps.market._snapshot = _snapshot()
    res = T.run_tool("setup_score", {"symbol": "EURUSD", "direction": "buy"}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["score"] == 71.4
    assert res["data"]["verdict"] == "ALTA"
    assert res["data"]["direction"] == "BUY"
    assert res["data"]["invalidation"]["SELL"] == 1.1680


def test_setup_score_sin_risk_engine_falla():
    deps = AgentDeps(market=MarketFake(snapshot={"current_price": 1.0}))
    res = T.run_tool("setup_score", {}, deps)
    assert res["status"] == STATUS_FAILED
    assert "risk_engine" in res["error"]


def test_journal_append_enriquece_cuando_hay_mercado(deps):
    res = T.run_tool("journal_append", {"symbol": "xauusd", "action": "sell", "notes": "n"}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["enriquecida"] is True
    assert res["data"]["entry_price"] == 1.1650
    assert res["data"]["symbol"] == "XAUUSD"
    assert res["data"]["action"] == "SELL"


def test_journal_append_guarda_aun_sin_mercado_y_lo_declara(deps):
    deps.market = None
    res = T.run_tool("journal_append", {"symbol": "EURUSD"}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["enriquecida"] is False
    assert len(deps.store.journal) == 1


def test_journal_append_hereda_el_conversation_id(deps):
    T.run_tool("journal_append", {"symbol": "EURUSD"}, deps, conversation_id="chat-9")
    assert deps.store.journal[0]["conversation_id"] == "chat-9"


def test_get_chart_drawings_resume_en_texto(deps):
    deps.store.drawings = [
        {"tool": "hline", "p0": 1.16500, "origin": "user"},
        {"tool": "line", "t0": 1, "p0": 1.16, "t1": 2, "p1": 1.17, "origin": "agent"},
    ]
    res = T.run_tool("get_chart_drawings", {"symbol": "EURUSD"}, deps)
    assert res["status"] == STATUS_OK
    assert res["data"]["count"] == 2
    assert "nivel 1.16500" in res["data"]["lectura"]
    assert "[agente]" in res["data"]["lectura"]


def test_economic_news_pasa_los_simbolos(deps):
    T.run_tool("economic_news", {"symbols": "eurusd, xauusd"}, deps)
    assert deps.news.simbolos == ["EURUSD", "XAUUSD"]


def test_economic_news_devuelve_stale_si_lo_dice():
    news = NewsFake({"items": [], "stale": True, "reason": "calendario no accesible"})
    deps = AgentDeps(news=news)
    res = T.run_tool("economic_news", {}, deps)
    assert res["data"]["stale"] is True
    assert res["data"]["reason"] == "calendario no accesible"


def test_mt5_export_read_lista_y_lee(deps):
    assert T.run_tool("mt5_export_read", {"action": "list"}, deps)["status"] == STATUS_OK
    res = T.run_tool("mt5_export_read", {"action": "read", "filename": "a.txt"}, deps)
    assert res["data"]["content"] == "ATR 12"


def test_mt5_export_read_sin_filename_falla(deps):
    res = T.run_tool("mt5_export_read", {"action": "read"}, deps)
    assert res["status"] == STATUS_FAILED
    assert "filename" in res["error"]


def test_mt5_export_read_action_inventada_falla(deps):
    res = T.run_tool("mt5_export_read", {"action": "borrar"}, deps)
    assert res["status"] == STATUS_FAILED


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------


def test_set_chart_alert_guarda_con_timeframe_solo_si_hay_condiciones(deps):
    res = T.run_tool("set_chart_alert", {"symbol": "eurusd", "price": 1.165}, deps)
    assert res["status"] == STATUS_OK
    alerta = deps.store.alertas[0]
    assert alerta["timeframe"] is None
    assert alerta["side"] == "touch"

    T.run_tool(
        "set_chart_alert",
        {"symbol": "EURUSD", "price": 1.165, "timeframe": "h1", "conditions": [{"type": "killzone", "name": "Londres"}]},
        deps,
    )
    assert deps.store.alertas[1]["timeframe"] == "H1"


def test_set_chart_alert_ttl_es_utc_aware_y_no_naive(deps):
    res = T.run_tool("set_chart_alert", {"symbol": "EURUSD", "price": 1.165, "expires_in_minutes": "30"}, deps)
    assert res["status"] == STATUS_OK
    expires = deps.store.alertas[0]["expires_at"]
    assert expires.endswith("+00:00"), expires


def test_set_chart_alert_rechaza_precio_no_numerico(deps):
    res = T.run_tool("set_chart_alert", {"symbol": "EURUSD", "price": "muy alto"}, deps)
    assert res["status"] == STATUS_FAILED
    assert not deps.store.alertas


def test_set_chart_alert_rechaza_precio_fuera_de_rango(deps):
    for precio in (0, -1, 1e9):
        res = T.run_tool("set_chart_alert", {"symbol": "EURUSD", "price": precio}, deps)
        assert res["status"] == STATUS_FAILED, precio
    assert not deps.store.alertas


def test_set_chart_alert_side_inventado_cae_a_touch(deps):
    T.run_tool("set_chart_alert", {"symbol": "EURUSD", "price": 1.165, "side": "por_d_encima"}, deps)
    assert deps.store.alertas[0]["side"] == "touch"


# ---------------------------------------------------------------------------
# chart_annotate: la validación que no se puede relajar
# ---------------------------------------------------------------------------


def _anclar(deps, conversation_id="c1"):
    deps.market = MarketFake(patterns=_patrones_data())
    T.run_tool("patterns", {}, deps, conversation_id=conversation_id)


def test_annotate_marca_fvg_con_la_ref_exacta():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "markArea", "pattern": "fvgs", "ref": 1_000_000}]},
        deps,
        conversation_id="c1",
    )
    assert res["status"] == STATUS_OK
    area = res["data"]["echarts"]["markArea"][0]
    assert area[0]["xAxis"] == 1_000_000
    assert area[0]["yAxis"] == 1.1600
    assert res["data"]["applied"] == 1
    assert res["data"]["skipped"] == 0


def test_annotate_descarta_una_ref_inventada():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "markArea", "pattern": "fvgs", "ref": 999_999}]},
        deps,
        conversation_id="c1",
    )
    # Todo se descartó: no hay nada que dibujar y eso es un error, no un ok vacío.
    assert res["status"] == STATUS_FAILED
    assert "válida" in res["error"]


def test_annotate_no_usa_el_anclaje_de_otra_conversacion():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps, conversation_id="c1")
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "markArea", "pattern": "fvgs", "ref": 1_000_000}]},
        deps,
        conversation_id="c2",
    )
    assert res["status"] == STATUS_FAILED
    assert "patterns" in res["error"]


def test_annotate_marca_sweep_con_flecha():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "markPoint", "pattern": "sweeps", "ref": 1_000_900}]},
        deps,
        conversation_id="c1",
    )
    punto = res["data"]["echarts"]["markPoint"][0]
    assert punto["coord"] == [1_000_900, 1.1640]
    assert punto["symbol"] == "arrowDown"
    assert punto["value"] == "PDH"


def test_annotate_recorta_figuras_libres_a_la_ventana():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "horizontal", "price_from": 99.0, "name": "lejos"}]},
        deps,
        conversation_id="c1",
    )
    item = res["data"]["echarts"]["draw"][0]
    assert item["tool"] == "hline"
    # La ventana es 1.159..1.163 (span 0.004) y el margen es del 25%.
    assert item["p0"] == pytest.approx(1.164, abs=1e-9)
    assert res["data"]["applied"] == 1


def test_annotate_ordena_los_extremos_de_una_linea():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {
            "symbol": "EURUSD",
            "actions": [{"kind": "line", "price_from": 1.1610, "price_to": 1.1620,
                         "start_time": 1_000_900, "end_time": 1_000_000}],
        },
        deps,
        conversation_id="c1",
    )
    item = res["data"]["echarts"]["draw"][0]
    assert item["t0"] < item["t1"]


def test_annotate_calcula_la_view_de_lo_aplicado():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "markArea", "pattern": "fvgs", "ref": 1_000_000}]},
        deps,
        conversation_id="c1",
    )
    view = res["data"]["view"]
    assert view["start_time"] == 1_000_000
    assert view["price_min"] == 1.1600
    assert view["end_time"] <= 1_000_900


def test_annotate_tope_de_acciones():
    deps = AgentDeps(store=StoreFake(), anchors=AnalysisAnchors(now=lambda: 1_000.0))
    _anclar(deps)
    muchas = [{"kind": "horizontal", "price_from": 1.161} for _ in range(50)]
    res = T.run_tool("chart_annotate", {"symbol": "EURUSD", "actions": muchas}, deps, conversation_id="c1")
    assert len(res["data"]["echarts"]["draw"]) == T.MAX_ACCIONES_ANNOTATE


def test_annotate_exige_ventana_de_velas():
    """Sin velas ancladas no hay contra qué recortar: se dice, no se inventa."""
    deps = AgentDeps(
        store=StoreFake(),
        market=MarketFake(patterns={"analysis": {"patterns": {}}, "candles": []}),
        anchors=AnalysisAnchors(now=lambda: 1_000.0),
    )
    T.run_tool("patterns", {}, deps, conversation_id="c1")
    res = T.run_tool(
        "chart_annotate",
        {"symbol": "EURUSD", "actions": [{"kind": "horizontal", "price_from": 1.16}]},
        deps,
        conversation_id="c1",
    )
    assert res["status"] == STATUS_FAILED
    assert "ventana" in res["error"]


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_el_timeout_es_por_herramienta():
    assert T.TOOLS["account_info"].timeout_s == T.TIMEOUT_S_POR_DEFECTO
    assert T.TOOLS["chart_snapshot"].timeout_s == T.TIMEOUT_S_CONtexto
    assert T.TOOLS["chart_snapshot"].timeout_s > T.TOOLS["account_info"].timeout_s


def test_una_tool_lenta_corta_con_unavailable_y_dice_cuanto():
    lento = T.TOOLS["account_info"]
    original = lento.timeout_s
    try:
        lento.timeout_s = 0.05
        res = T.run_tool("account_info", {}, AgentDeps(market=MarketLento(0.5)))
    finally:
        lento.timeout_s = original
    assert res["status"] == STATUS_UNAVAILABLE
    assert "tiempo agotado" in res["error"]
    assert "account_info" in res["error"]


def test_execute_tool_es_awaitable_y_respeta_el_timeout():
    lento = T.TOOLS["account_info"]
    original = lento.timeout_s
    try:
        lento.timeout_s = 0.05

        async def _run():
            return await T.execute_tool("account_info", "{}", AgentDeps(market=MarketLento(0.5)))

        res = asyncio.run(_run())
    finally:
        lento.timeout_s = original
    assert res["status"] == STATUS_UNAVAILABLE


def test_run_tool_por_dentro_de_un_loop_no_revienta():
    async def _run():
        return T.run_tool("account_info", {}, AgentDeps(market=MarketFake()))

    res = asyncio.run(_run())
    assert res["status"] == STATUS_FAILED
    assert "execute_tool" in res["error"]


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_distingue_vacio_de_malformado():
    assert T.parse_args("") == {}
    assert T.parse_args(None) == {}
    assert T.parse_args('{"symbol": "EURUSD"}') == {"symbol": "EURUSD"}
    assert T.parse_args({"symbol": "EURUSD"}) == {"symbol": "EURUSD"}
    roto = T.parse_args("{no soy json")
    assert "__args_error__" in roto
    assert "no son JSON" in roto["__args_error__"]


def test_parse_args_no_lista_es_error_de_argumentos():
    assert "__args_error__" in T.parse_args("[1, 2, 3]")


def test_arguments_malformados_llegan_al_modelo_como_error():
    res = T.run_tool("price", "{roto", AgentDeps(market=MarketFake()))
    assert res["status"] == STATUS_FAILED
    assert "no son JSON" in res["error"]


def test_un_arguments_malformado_no_llega_al_store():
    """El handler no se ejecuta: no hay bitácora a medias con un error de sintaxis.

    Es mejor que el `except: pass` de REF: allí los argumentos malformados se
    convertían en `{}` y la bitácora guardaba una entrada vacía que parecía
    válida.
    """
    deps = AgentDeps(store=StoreFake())
    res = T.run_tool("journal_append", "{roto", deps)
    assert res["status"] == STATUS_FAILED
    assert deps.store.journal == []