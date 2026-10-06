"""Tests de `agent/ports.py`: el contrato que hace testeable al agente.

Lo que se fija aquí no es la forma de la clase sino las TRES propiedades de las que
depende `chart_annotate`:

1. El anclaje es por conversación. Un análisis de EURUSD M15 del chat A no valida
   las referencias del chat B; con el `_LAST_ANALYSIS` global de REF sí, y el
   modelo podía "anotar" patrones que el usuario de ese chat no tenía en pantalla.
2. El anclaje CADUCA. Un patrón de hace una hora sigue siendo un patrón, pero sus
   coordenadas ya son de otro gráfico.
3. El anclaje CRECE TOPADO. Un usuario que recorre veinte símbolos no necesita
   veinte anclas vivas.

Y una cuarta, la que hace falta para poder probarlos: el reloj es inyectable.
"""

from __future__ import annotations

import pytest

from agent.ports import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    AgentDeps,
    AgentPortError,
    AnalysisAnchors,
    MarketPort,
    StorePort,
)


# ---------------------------------------------------------------------------
# Un puerto doble que cumple el contrato
# ---------------------------------------------------------------------------


class Tienda:
    """Satisface `StorePort` sin heredar de nada."""

    def get_role(self, role_id):
        return None

    def get_settings(self):
        return {}

    def add_message(self, conversation_id, role, content, tool_call_id=None, name=None):
        return None

    def get_messages(self, conversation_id, limit=20):
        return []

    def add_journal_entry(self, entry):
        return entry

    def list_journal(self, symbol=None, days=None, limit=50):
        return []

    def list_trades(self, symbol=None, action=None, days=None, limit=200):
        return []

    def get_drawings(self, symbol, timeframe="M15"):
        return []

    def add_chart_alert(self, symbol, price, label=None, side=None, conditions=None,
                        timeframe=None, expires_at=None):
        return {}

    def get_agent_topics(self):
        return {}

    def get_trading_config(self):
        return {}


class Mercado:
    """Satisface `MarketPort`."""

    def account_info(self):
        return {}

    def positions(self):
        return []

    def history(self, days):
        return []

    def price(self, symbol):
        return None

    def pattern_data(self, symbol, timeframe):
        return None

    def chart_snapshot(self, symbol, timeframe):
        return {}

    def orderflow_snapshot(self):
        return {}

    def orderflow_alerts(self):
        return []

    def daily_risk_state(self):
        return {}

    def enrich_journal_entry(self, entry):
        return entry

    def broker_time(self):
        return ""

    def trading_day(self):
        return ""


def test_los_dobles_cumplen_el_protocolo_estructuralmente():
    """`Protocol` + `runtime_checkable`: que un doble cumpla se COMPRUEBA.

    Si el contrato se cumple con una clase base, `database.store` tendría que
    heredar de algo para poder inyectarse, y eso convierte "un módulo con funciones
    sueltas" en "una jerarquía". Con `Protocol` no hay herencia y el test verifica
    la conformidad.
    """
    assert isinstance(Tienda(), StorePort)
    assert isinstance(Mercado(), MarketPort)


# ---------------------------------------------------------------------------
# AnalysisAnchors
# ---------------------------------------------------------------------------


@pytest.fixture
def reloj():
    """Reloj manual: `reloj[0]` son los segundos actuales."""
    return [1_000.0]


@pytest.fixture
def anchors(reloj):
    return AnalysisAnchors(now=lambda: reloj[0])


ANALISIS = {"patterns": {"fvgs": [{"start_time": 1}]}}
VELAS = [
    {"time": 1_000_000, "low": 1.159, "high": 1.162},
    {"time": 1_000_900, "low": 1.160, "high": 1.163},
]


def test_put_guarda_la_ventana_de_velas(anchors):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    ventana = anchors.get("c1", "EURUSD", "M15")
    assert ventana["analysis"] == ANALISIS
    assert ventana["candle_start"] == 1_000_000
    assert ventana["last_time"] == 1_000_900
    assert ventana["candle_lo"] == 1.159
    assert ventana["candle_hi"] == 1.163
    assert ventana["stored_at"] == 1_000.0


def test_put_devuelve_el_sello(anchors):
    assert anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS) == 1_000.0


def test_no_cruza_conversaciones(anchors):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    assert anchors.get("c2", "EURUSD", "M15") is None
    assert anchors.get("c1", "EURUSD", "M15") is not None


def test_no_cruza_simbolo_ni_timeframe(anchors):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    assert anchors.get("c1", "XAUUSD", "M15") is None
    assert anchors.get("c1", "EURUSD", "H1") is None


def test_caduca_tras_el_ttl(anchors, reloj):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    reloj[0] += AnalysisAnchors.TTL_S + 0.1
    assert anchors.get("c1", "EURUSD", "M15") is None


def test_no_caduca_justo_antes_del_ttl(anchors, reloj):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    reloj[0] += AnalysisAnchors.TTL_S - 0.1
    assert anchors.get("c1", "EURUSD", "M15") is not None


def test_el_caducado_se_olvida_de_verdad(anchors, reloj):
    """Un anclaje caducado que se queda ocupa sitio y el `len` no miente."""
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    reloj[0] += AnalysisAnchors.TTL_S + 1
    anchors.get("c1", "EURUSD", "M15")
    assert len(anchors) == 0


def test_age_s_refleja_la_antiguedad(anchors, reloj):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    reloj[0] += 42.0
    assert anchors.age_s("c1", "EURUSD", "M15") == pytest.approx(42.0)


def test_age_s_es_none_si_caduco(anchors, reloj):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    reloj[0] += AnalysisAnchors.TTL_S + 1
    assert anchors.age_s("c1", "EURUSD", "M15") is None


def test_tope_por_conversacion(reloj):
    anchors = AnalysisAnchors(now=lambda: reloj[0], max_por_conversacion=3)
    for i in range(5):
        reloj[0] += 1
        anchors.put("c1", "SYM{0}".format(i), "M15", ANALISIS, VELAS)
    assert len(anchors) == 3
    # Se quedan los más recientes: los tres últimos consultados.
    assert anchors.get("c1", "SYM4", "M15") is not None
    assert anchors.get("c1", "SYM0", "M15") is None


def test_reanclar_el_mismo_simbolo_no_crece():
    reloj = [1_000.0]
    anchors = AnalysisAnchors(now=lambda: reloj[0])
    for i in range(4):
        anchors.put("c1", "EURUSD", "M15", {"n": i}, VELAS)
    assert len(anchors) == 1
    assert anchors.get("c1", "EURUSD", "M15")["analysis"] == {"n": 3}


def test_get_devuelve_una_copia(anchors):
    """El llamante recorta coordenadas y no debe matar el anclaje."""
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    ventana = anchors.get("c1", "EURUSD", "M15")
    ventana["candle_lo"] = -1
    ventana["analysis"]["patterns"].clear()
    otra = anchors.get("c1", "EURUSD", "M15")
    assert otra["candle_lo"] == 1.159
    assert otra["analysis"]["patterns"] == ANALISIS["patterns"]


def test_forget_olvida_la_conversacion(anchors):
    anchors.put("c1", "EURUSD", "M15", ANALISIS, VELAS)
    anchors.put("c2", "EURUSD", "M15", ANALISIS, VELAS)
    anchors.forget("c1")
    assert anchors.get("c1", "EURUSD", "M15") is None
    assert anchors.get("c2", "EURUSD", "M15") is not None


def test_put_sin_velas_no_inventa_la_ventana(anchors):
    """Sin velas no hay ventana, yeso se ve en el resultado en vez de inventarse."""
    anchors.put("c1", "EURUSD", "M15", ANALISIS, [])
    ventana = anchors.get("c1", "EURUSD", "M15")
    assert ventana is not None
    assert ventana["candle_start"] is None
    assert ventana["candle_lo"] is None


def test_ttl_y_tope_son_configurables(reloj):
    anchors = AnalysisAnchors(now=lambda: reloj[0], ttl_s=10.0, max_por_conversacion=1)
    anchors.put("c1", "A", "M15", ANALISIS, VELAS)
    reloj[0] += 11
    assert anchors.get("c1", "A", "M15") is None


# ---------------------------------------------------------------------------
# AgentDeps
# ---------------------------------------------------------------------------


def test_agent_deps_cablea_lo_que_hay():
    deps = AgentDeps()
    assert deps.market is None
    assert deps.anchors is not None
    assert deps.now() > 0
    assert "ninguno" in repr(deps)


def test_agent_deps_repr_dice_los_cables():
    deps = AgentDeps(market=Mercado(), store=Tienda())
    assert "market" in repr(deps)
    assert "store" in repr(deps)
    assert "news" not in repr(deps)


def test_agent_deps_puerto_ausente_nombra_el_puerto():
    assert AgentDeps().puerto_ausente("market") == "puerto no cableado: market"


def test_el_reloj_inyectable_llega_a_los_anchors():
    reloj = [5.0]
    deps = AgentDeps(now=lambda: reloj[0])
    assert deps.now() == 5.0
    deps.anchors.put("c", "S", "M15", ANALISIS, VELAS)
    assert deps.anchors.get("c", "S", "M15")["stored_at"] == 5.0


def test_el_sleep_inyectable_se_guarda():
    dormido = []
    deps = AgentDeps(sleep=dormido.append)
    deps.sleep(0.5)
    assert dormido == [0.5]


# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------


def test_los_tres_estados_son_distintos():
    assert len({STATUS_OK, STATUS_FAILED, STATUS_UNAVAILABLE}) == 3


def test_agent_port_error_no_es_un_adapter_error():
    """Cada capa tiene su excepción (D-022): el agente no hereda de `adapters`."""
    from adapters.base_adapter import AdapterError

    assert not issubclass(AgentPortError, AdapterError)
    assert issubclass(AgentPortError, RuntimeError)