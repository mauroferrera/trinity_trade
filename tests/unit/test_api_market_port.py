"""Tests de `MT5Market`: la implementación de `MarketPort` sobre el adaptador.

Por qué el adaptador se construye en el test
---------------------------------------------
`adapters.forex.mt5_forex.adapter()` devuelve un ADAPTER SINGLETON que lleva su
propia `Session`, y `Session` cachea el módulo MT5 en el primer uso
(`_ensure_open` → `self._mt5`). Si un test usara ese singleton, la sesión se
abriría contra el primer doble de MT5 del proceso y los tests siguientes leerían
los datos del primero: un fallo que se manifiesta como "mis fixtures no se
aplican" y se chasea como un bug del servicio.

Por eso aquí el adaptador se construye con `MT5ForexAdapter(session)` sobre una
sesión propia, que es exactamente lo que hace `tests/unit/test_mt5_forex.py`.
Sigue siendo la clase REAL: lo que se prueba es la ruta de producción.

Lo que estos tests cazan
-----------------------
Este servicio es donde se cuecen los fallos silenciosos, y todos comparten la misma
forma: el snapshot sale, el JSON tiene las llaves esperadas, y el número que importa
va mal. Los que se afirman aquí:

1. **El PDH/PDL excluye la vela de hoy.** Con la D1 en curso, el "máximo del día"
   se mueve con el precio y no es un nivel.
2. **La config se lee por sus RESOLVERS.** `store.get_trading_config()` devuelve
   `core.strategy.build_flat`, y ahí `risk_weights` y `killzones` son JSON **string**,
   no mapas: leerlos como estructura reventaba el score entero. Los resolvers
   (`strategy.risk_weights_for` / `killzones_for`) aceptan las dos formas —plana y
   anidada— y lo que falta se AVISA en vez de dejar que el default parezca decisión.
3. **El `smr` llega al score.** Sin él el componente `smr_dxy` puntúa 0 siempre.
4. **El fallo del motor DECLARA.** REF hacía `except: pass` y el panel pintaba un
   snapshot sin nota de riesgo con la misma pinta que uno calculado.
5. **El límite de operaciones NO es un 503.** `daily_risk_state` devuelve el error
   en el dict porque quien decide tiene que LEERLO.
6. **`enrich_journal_entry` nunca lanza.** Guardar la bitácora no puede fallar
   porque MT5 esté cerrado.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
import yaml

from adapters.base_adapter import TIMEFRAMES, AdapterError, SymbolNotFound
from adapters.forex.mt5_forex import MT5ForexAdapter, Session
from agent.ports import AgentPortError
from api.services.broker_clock import BrokerClock
from api.services.mt5_market import DEFAULT_SYMBOL, MT5Market
from core import clock
from core import paths as core_paths
from core import strategy
from core.strategy import build_flat
from tests.unit.mt5_fake import (
    TIMEFRAME_D1,
    TIMEFRAME_M15,
    FakeMT5,
    FakeSymbolInfo,
    FakeTick,
    rates_fixture,
)


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------

class StoreStub:
    """La parte de `database.store` que usa el mercado."""

    def __init__(
        self,
        cfg: Optional[Dict[str, Any]] = None,
        cot: Optional[List[Any]] = None,
        prop_state: Optional[Dict[str, Any]] = None,
        mapa: Optional[Dict[int, str]] = None,
    ) -> None:
        self._cfg = cfg if cfg is not None else {}
        self._cot = cot or []
        self._prop_state = prop_state or {}
        self._mapa = mapa or {}

    def get_trading_config(self) -> Dict[str, Any]:
        return self._cfg

    def get_strategy_map(self) -> Dict[int, str]:
        return dict(self._mapa)

    def list_cot_reports(self, *args: Any, **kwargs: Any) -> List[Any]:
        return self._cot

    def get_prop_state(self) -> Dict[str, Any]:
        return self._prop_state

    @staticmethod
    def utc_stamp(dt: Any) -> str:  # pragma: no cover - trivial
        return dt.isoformat()


class OrderflowStub:
    """El mínimo de `core.orderflow_engine.OrderFlowEngine` que usa el mercado."""

    def __init__(self, serie: Optional[List[Dict[str, Any]]] = None, alertas: Optional[List[Any]] = None) -> None:
        self._serie = serie if serie is not None else []
        self._alertas = alertas or []
        self.cvd_calls = 0

    def cvd_series(self, since: Optional[float] = None) -> List[Dict[str, Any]]:
        self.cvd_calls += 1
        return list(self._serie)

    def snapshot(self) -> Dict[str, Any]:
        return {"source": "orderflow", "alerts": list(self._alertas), "cvd": list(self._serie)}


class Deal:
    """Un deal de MT5 con los atributos que lee `core.trade_history`.

    `entry` y `type` son los enteros de la terminal: `entry=0` es ENTRADA, `type=0`
    es BUY. La dirección de la operación consolidated sale del deal de entrada, y
    por eso el doble tiene que llevar los dos.
    """

    def __init__(
        self,
        ticket: int,
        position_id: int,
        type_: int,
        price: float,
        profit: float = 0.0,
        time_: int = 1_700_000_000,
        entry: int = 0,
        symbol: str = "EURUSD",
        commission: float = 0.0,
        swap: float = 0.0,
        volume: float = 1.0,
        magic: int = 0,
    ) -> None:
        self.ticket = ticket
        self.position_id = position_id
        self.type = type_
        self.entry = entry
        self.symbol = symbol
        self.price = price
        self.volume = volume
        self.profit = profit
        self.commission = commission
        self.swap = swap
        self.time = time_
        self.reason = 1
        self.magic = magic


@pytest.fixture
def cfg_real() -> Dict[str, Any]:
    """El `strategy.yaml` de verdad: los pesos y las killzones son los de producción."""
    with open(core_paths.STRATEGY_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@pytest.fixture
def mt5(monkeypatch: pytest.MonkeyPatch) -> FakeMT5:
    fake = FakeMT5.install(monkeypatch)
    fake.info_by_symbol["EURUSD"] = FakeSymbolInfo("EURUSD")
    # El tick tiene que ser un `FakeTick`: el adaptador lee `bid`, `ask`, `last` y
    # `time`, y un `FakeSymbolInfo` no tiene `last` (falla con AttributeError).
    fake.tick_by_symbol["EURUSD"] = FakeTick(time=1_700_000_000, bid=1.0850, ask=1.0852, last=1.0851)
    fake.set_rates("EURUSD", TIMEFRAME_M15, rates_fixture(count=60, base=1.1000))
    fake.set_rates(
        "EURUSD",
        TIMEFRAME_D1,
        rates_fixture(count=3, start_ts=1_699_000_000, base=1.0900, step=0.0020),
    )
    fake.account_info = lambda: SimpleNamespace(
        login=1234,
        name="TRINITY",
        server="Helsinki",
        company="Demo",
        currency="EUR",
        leverage=100,
        balance=10_000.0,
        equity=10_010.0,
        profit=10.0,
        margin=500.0,
        margin_free=9_500.0,
        margin_level=2_000.0,
    )
    fake.positions_get = lambda **kw: []
    fake.history_deals_get = lambda frm, to: []
    return fake


@pytest.fixture
def construir(mt5: FakeMT5, cfg_real: Dict[str, Any]):
    """Fabrica mercados sobre sesiones propias y las cierra al terminar.

    El cierre importa: una sesión sin cerrar deja un hilo del executor vivo y su
    `-wal` abierto, que es justo lo que el canario de `tests/conftest.py` vigila.
    """
    sesiones: List[Session] = []

    def _build(
        store: Optional[Any] = None,
        orderflow: Optional[Any] = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> MT5Market:
        sesion = Session()
        sesiones.append(sesion)
        return MT5Market(
            adapter=MT5ForexAdapter(sesion),
            session=sesion,
            store=store if store is not None else StoreStub(cfg if cfg is not None else cfg_real),
            reloj=BrokerClock(cfg=lambda: cfg_real),
            orderflow=orderflow,
        )

    yield _build
    for sesion in sesiones:
        sesion.shutdown_executor()


@pytest.fixture
def mercado(construir: Callable[..., MT5Market]) -> MT5Market:
    return construir()


# ---------------------------------------------------------------------------
# Normalización
# ---------------------------------------------------------------------------

class TestNormalizacion:
    def test_simbolo_en_mayusculas(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """El bróker no publica `eurusd` en minúsculas: se normaliza antes de pedir."""
        mercado.candles("eurusd", "M15", 10)

        pedido = mt5.calls_named("copy_rates_from_pos")[-1]
        assert pedido[1] == "EURUSD"

    def test_simbolo_vacio_es_error_del_llamante(self) -> None:
        """Sin símbolo no hay snapshot: es un 400, y lo dice el tipo de excepción."""
        with pytest.raises(AgentPortError):
            MT5Market._normaliza("")

    def test_timeframe_invalido_no_llega_al_bróker(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """Un timeframe inventado tiene que morir ANTES de la llamada.

        Si llegara al terminal, MT5 recibiría un timeframe que no entiende y el
        `AdapterError` sería un mensaje de bróker en vez de "el timeframe no existe".
        """
        with pytest.raises(ValueError) as exc:
            mercado.candles("EURUSD", "M7", 10)

        assert "M7" in str(exc.value)
        assert not mt5.calls_named("copy_rates_from_pos")

    def test_timeframe_por_defecto_es_m15(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        mercado.candles("EURUSD", bars=10)

        pedido = mt5.calls_named("copy_rates_from_pos")[-1]
        assert pedido[2] == TIMEFRAMES["M15"]

    def test_el_default_de_símbolo_es_eurusd(self) -> None:
        assert MT5Market._normaliza(" gbpusd ") == "GBPUSD"
        assert DEFAULT_SYMBOL == "EURUSD"


# ---------------------------------------------------------------------------
# Patrones y PDH/PDL
# ---------------------------------------------------------------------------

class TestPatternData:
    def test_pdh_y_pdl_excluyen_la_vela_de_hoy(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """El máximo del día en curso no es un nivel: se mueve con el precio.

        Con las 3 D1 que pide el puerto (hoy y dos días previos), el PDH sale del
        máximo de las ANTERIORES. Si se incluyera la de hoy, cada tick movería la
        "resistencia" y las alertas de distancia.
        """
        mt5.set_rates(
            "EURUSD",
            TIMEFRAME_D1,
            [
                (1_699_000_000, 1.1000, 1.1050, 1.0900, 1.1020, 10, 5, 10),
                (1_699_086_400, 1.1000, 1.1100, 1.0950, 1.1080, 10, 5, 10),
                (1_699_172_800, 1.1000, 1.1300, 1.0980, 1.1250, 10, 5, 10),
            ],
        )

        data = mercado.pattern_data("EURUSD", "M15", 60)

        assert data["pdh"] == pytest.approx(1.1100), "el 1.1300 de hoy no puede ser el PDH"
        assert data["pdl"] == pytest.approx(1.0900)

    def test_sin_velas_devuelve_none(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """`None` y no `{}`: quien llama decide si es 404 o 503."""
        mt5.set_rates("EURUSD", TIMEFRAME_M15, [])

        assert mercado.pattern_data("EURUSD", "M15", 60) is None

    def test_sin_diarias_no_hay_pdh_ni_pdl(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """Sin D1 el snapshot sale igual, sin niveles, en vez de no salir."""
        mt5.set_rates("EURUSD", TIMEFRAME_D1, [])

        data = mercado.pattern_data("EURUSD", "M15", 60)

        assert data is not None
        assert data["pdh"] is None and data["pdl"] is None

    def test_la_cache_evita_el_segundo_viaje(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """Cada snapshot sin caché son 2 viajes a la terminal por petición.

        El TTL es de 10 s, el mismo número y el mismo motivo que en REF: el score de
        una vela M15 no cambia en 10 s.
        """
        mercado.pattern_data("EURUSD", "M15", 60)
        n_primero = len(mt5.calls_named("copy_rates_from_pos"))

        mercado.pattern_data("EURUSD", "M15", 60)

        assert len(mt5.calls_named("copy_rates_from_pos")) == n_primero

    def test_clear_cache_fuerza_el_viaje(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        mercado.pattern_data("EURUSD", "M15", 60)
        n_antes = len(mt5.calls_named("copy_rates_from_pos"))

        mercado.clear_cache("EURUSD", "M15")
        mercado.pattern_data("EURUSD", "M15", 60)

        assert len(mt5.calls_named("copy_rates_from_pos")) > n_antes

    def test_clear_cache_por_timeframe_no_toca_otros(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        mercado.pattern_data("EURUSD", "M15", 60)
        mercado.clear_cache("EURUSD", "H1")

        assert mercado.pattern_data("EURUSD", "M15", 60) is not None

    def test_clear_cache_sin_args_lo_vacia_todo(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        mercado.pattern_data("EURUSD", "M15", 60)
        n_antes = len(mt5.calls_named("copy_rates_from_pos"))

        mercado.clear_cache()
        mercado.pattern_data("EURUSD", "M15", 60)

        assert len(mt5.calls_named("copy_rates_from_pos")) > n_antes


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class TestChartSnapshot:
    def test_trae_lo_que_el_panel_necesita(self, mercado: MT5Market) -> None:
        snap = mercado.chart_snapshot("EURUSD", "M15", 60)

        for clave in (
            "symbol",
            "timeframe",
            "current_price",
            "PDH",
            "PDL",
            "analysis",
            "recent_candles",
            "risk_engine",
        ):
            assert clave in snap, clave
        assert snap["symbol"] == "EURUSD"
        assert snap["feed"] == "live"

    def test_sin_velas_es_symbol_not_found(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """404, no 503: el bróker va bien, el símbolo no está."""
        mt5.set_rates("EURUSD", TIMEFRAME_M15, [])

        with pytest.raises(SymbolNotFound):
            mercado.chart_snapshot("EURUSD", "M15", 60)

    def test_built_at_se_toma_al_entrar(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """La edad del snapshot tiene que incluir el tiempo de armarlo.

        Si el sello se pusiera al final, un snapshot cuyo primer dato ya era viejo
        se mediría como fresco, que es justo lo que la auditoría necesita detectar.
        """
        mt5.set_rates("EURUSD", TIMEFRAME_M15, rates_fixture(count=60))

        snap = mercado.chart_snapshot("EURUSD", "M15", 60)

        # `built_at` es un sello ISO con offset (clave de orden en SQLite) y `time`
        # es texto UTC legible: se comparan como instantes, no como cadenas. Lo que
        # importa es que apunte al momento de la llamada, en UTC.
        instante = clock.parse_iso(snap["built_at"])
        assert instante is not None
        assert instante.tzinfo is not None
        assert abs((clock.now_utc() - instante).total_seconds()) < 60

    def test_las_velas_recientes_van_en_utc_hm(self, mercado: MT5Market) -> None:
        """Hora local mezclada con epochs UTC en la misma respuesta era el bug."""
        snap = mercado.chart_snapshot("EURUSD", "M15", 60)

        assert snap["recent_candles"]
        for vela in snap["recent_candles"]:
            assert len(vela["time"]) == 5 and vela["time"][2] == ":"

    def test_spread_en_puntos(self, mercado: MT5Market, mt5: FakeMT5) -> None:
        """El spread en puntos es la diferencia bid/ask escalada por el point."""
        mt5.tick_by_symbol["EURUSD"] = FakeTick(time=1_700_000_000, bid=1.0850, ask=1.0852)

        snap = mercado.chart_snapshot("EURUSD", "M15", 60)

        assert snap["point"] == pytest.approx(0.00001)
        assert snap["spread_points"] == pytest.approx(20.0)

    def test_cvd_sintetico_avisa_que_es_tick_volume(self, mercado: MT5Market) -> None:
        """Sin feed encendido, el CVD sale del tick volume y LO DICE.

        El tick volume no es volumen real: es un proxy relativo. Sin el aviso, el
        modelo lo lee como tamaño de órdenes y el score se apoya en una magnitud
        que no es la que cree.
        """
        snap = mercado.chart_snapshot("EURUSD", "M15", 60)

        assert snap["cvd_source"].startswith("synthetic")
        assert "tick volume" in snap["cvd_warning"]

    def test_sin_cot_no_inventa_el_bloque(self, mercado: MT5Market) -> None:
        """Sin COT descargado no hay bloque `cot_macro_analysis`."""
        assert "cot_macro_analysis" not in mercado.chart_snapshot("EURUSD", "M15", 60)


class TestRiesgoDelMotor:
    def test_el_smr_llega_al_score(self, construir: Callable[..., MT5Market], monkeypatch: pytest.MonkeyPatch) -> None:
        """Sin `smr` en las entradas, el componente `smr_dxy` puntúa 0 siempre.

        El resultado es un score BAJO sin motivo visible, que es como se pierde la
        confianza en un número. Se afirma que el componente recibe el valor: la
        forma es la de `smr_service.evaluate()`, con `bull`/`bear` y `confirmed`.
        """
        smr = {"bull": {"confirmed": True, "detail": "SMR sube y DXY cae"}, "bear": {"confirmed": False}}
        monkeypatch.setattr(MT5Market, "_smr", lambda self, s, tf, v: smr)

        snap = construir().chart_snapshot("EURUSD", "M15", 60)

        assert snap["smr"] == smr
        assert snap["risk_engine"]["bull"]["components"]["smr_dxy"]["value"] == 1.0
        assert snap["risk_engine"]["bear"]["components"]["smr_dxy"]["value"] == 0.0

    def test_el_fallo_del_motor_se_declara(
        self, construir: Callable[..., MT5Market], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REF hacía `except: pass` y el panel pintaba un snapshot sin nota.

        Ahora el error viaja DENTRO de `risk_engine`: quien lo pinte tiene que poder
        distinguir "no hay score" de "el score es cero".
        """

        def revienta(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("pesos corruptos")

        monkeypatch.setattr("api.services.mt5_market.risk_engine.setup_score", revienta)

        snap = construir().chart_snapshot("EURUSD", "M15", 60)

        assert "error" in snap["risk_engine"]
        assert "pesos corruptos" in snap["risk_engine"]["error"]
        assert snap["current_price"] is not None, "el resto del snapshot sigue siendo válido"

    def test_pesos_de_produccion_entran_en_el_score(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any]
    ) -> None:
        """Los pesos del YAML se usan, y no los del default del motor.

        El YAML de producción y `risk_engine.DEFAULT_WEIGHTS` coinciden hoy EXACTAMENTE
        (los dos suman 85), así que este test no puede fiarse solo de la config real:
        los defaults puestos también pasa. Por eso mueve UN peso y mira ese
        componente. Un score con los defaults por dentro parece un score con la
        calibración del usuario, y no hay forma de distinguirlo mirando la suma.
        """
        movido = dict(cfg_real)
        movido["score"] = dict(cfg_real["score"], weights=dict(cfg_real["score"]["weights"], smc=60.0))

        bull = construir(cfg=movido).chart_snapshot("EURUSD", "M15", 60)["risk_engine"]["bull"]

        assert bull["weight_total"] == pytest.approx(95.0)
        assert bull["components"]["smc"]["weight"] == pytest.approx(60.0)

    def test_la_config_plana_del_store_no_tumba_el_score(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any]
    ) -> None:
        """El bug que motivó estas rutas: `build_flat` deja `risk_weights` y
        `killzones` como JSON **string**.

        Pasarlos tal cual a `setup_score` reventaba con
        `AttributeError: 'str' object has no attribute 'get'` dentro de
        `killzone_score`, el `except` del motor devolvía `{"error": ...}` y el panel se
        quedaba sin veredicto sin motivo aparente. Este test falla si alguien vuelve a
        leer esas claves sin pasar por los resolvers.
        """
        plana = build_flat(cfg_real)

        snap = construir(cfg=plana).chart_snapshot("EURUSD", "M15", 60)

        assert "error" not in snap["risk_engine"], snap["risk_engine"].get("error")
        # El componente killzone sale COMPLETO: con el string, `killzone_score` reventaba
        # antes de poder examinar ninguna ventana.
        assert set(snap["risk_engine"]["killzone"]) >= {"in_killzone", "name"}
        assert snap["risk_engine"]["bull"]["score"] > 0
        assert not [a for a in snap["risk_engine"].get("warnings", []) if "ilegible" in a]
        assert not [a for a in snap["risk_engine"].get("warnings", []) if "ausente" in a]

    def test_config_sin_pesos_avisa(self, construir: Callable[..., MT5Market]) -> None:
        """Falta `score.weights` → se dice. El default no puede parecer decisión."""
        snap = construir(cfg={}).chart_snapshot("EURUSD", "M15", 60)

        avisos = snap["risk_engine"]["warnings"]
        assert any("score.weights" in a for a in avisos)
        assert any("killzones" in a for a in avisos)

    def test_killzone_forzada_usa_una_ventana_configurada(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any]
    ) -> None:
        """`force_killzone` pone el reloj dentro de una sesión REAL de la config.

        Escribir horas a mano pondría el score en una ventana que el usuario no tiene
        configurada, y el resultado del modo test sería del test, no del sistema.
        """
        movido = dict(cfg_real)
        movido["killzones"] = [{"name": "Tokyo", "start": "23:00", "end": "02:00"}]

        snap = construir(cfg=movido).chart_snapshot("EURUSD", "M15", 60, force_killzone=True)

        assert snap["risk_engine"]["killzone_forced"] is True
        assert snap["risk_engine"]["killzone"]["name"] == "Tokyo"
        assert snap["risk_engine"]["killzone"]["in_killzone"] is True

    def test_config_que_falla_avisa_y_no_tumba(self, construir: Callable[..., MT5Market]) -> None:
        class StoreRoto(StoreStub):
            def get_trading_config(self) -> Dict[str, Any]:
                raise RuntimeError("base no disponible")

        snap = construir(store=StoreRoto({})).chart_snapshot("EURUSD", "M15", 60)

        assert any("config no disponible" in a for a in snap["risk_engine"]["warnings"])


# ---------------------------------------------------------------------------
# Estado de riesgo del día
# ---------------------------------------------------------------------------

class TestDailyRiskState:
    def test_sin_cuenta_devuelve_el_error_en_el_dict(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """El gate de entrada LEE `error`; que se propague sería un 500 en la ruta.

        Y si devolviera `{}`, quien decide vería un estado de riesgo sin riesgo.
        """
        mt5.account_info = lambda: None

        estado = construir().daily_risk_state()

        assert "error" in estado

    def test_lee_los_topes_de_la_config(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`max_trades_day` es un tope COMPARTIDO con el EA: sale de la config."""
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])

        estado = construir().daily_risk_state()

        assert estado["max_trades_day"] == cfg_real["risk"]["max_trades_day"]
        assert estado["risk_pct"] == cfg_real["risk"]["risk_pct"]

    def test_lee_los_topes_tambien_de_la_config_plana(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El store NO devuelve la forma anidada, y esa es la que se ve en producción.

        `get_trading_config()` devuelve lo que produce `build_flat`, con los topes
        en la raíz (`{"risk_pct": 0.5, ...}`). Leyendo solo `cfg["risk"]` los cinco
        salían en `null`: un panel que aparenta no tener topes puestos mientras el
        gate de entrada sí los aplicaba. El test de arriba cubría la anidada, que
        es la forma del YAML, así que la plana se escapaba entera.
        """
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])
        plana = strategy.build_flat(cfg_real)

        estado = construir(cfg=plana).daily_risk_state()

        assert estado["max_trades_day"] == plana["max_trades_day"]
        assert estado["risk_pct"] == plana["risk_pct"]
        assert estado["reduced_risk_pct"] == plana["reduced_risk_pct"]
        assert estado["loss_limit_fixed"] == plana["loss_limit_fixed"]
        assert estado["loss_limit_pct"] == plana["loss_limit_pct"]

    def test_la_config_anidada_manda_si_estan_las_dos(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con las dos rutas presentes, gana la anidada: es la fuente declarada."""
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])
        mixto = dict(strategy.build_flat(cfg_real))
        mixto["risk"] = dict(cfg_real["risk"], max_trades_day=7)

        estado = construir(cfg=mixto).daily_risk_state()

        assert estado["max_trades_day"] == 7

    def test_marca_el_tope_alcanzado(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [{}] * int(cfg_real["risk"]["max_trades_day"]))

        assert construir().daily_risk_state()["max_trades_reached"] is True

    def test_sin_operaciones_no_hay_tope_alcanzado(
        self, construir: Callable[..., MT5Market], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])

        assert construir().daily_risk_state().get("max_trades_reached") is False

    def test_pnl_del_dia_desde_el_balance_guardado(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con HWM guardado, el PnL del día es real; si no, se marca estimado.

        Un DD calculado contra el balance del momento cambia según el balance, que
        es la forma más fácil de no tener un DD.
        """
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])
        hoy = BrokerClock(cfg=lambda: cfg_real).trading_day()
        store = StoreStub(cfg_real, prop_state={"trading_day": hoy, "day_start_balance": 10000.0})

        estado = construir(store=store).daily_risk_state()

        assert estado["pnl_day"] == pytest.approx(0.0)
        assert estado["balance_source"] == "guardado"

    def test_pnl_sin_guardado_se_marca_estimado(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Un HWM de OTRO día no sirve: el día de trading resetea."""
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: [])
        store = StoreStub(cfg_real, prop_state={"trading_day": "1999-01-01", "day_start_balance": 1.0})

        assert construir(store=store).daily_risk_state()["balance_source"] == "estimado"

    def test_reporta_la_exposicion_por_magic_y_por_perfil(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1: el primer paso del multi-perfil es PODER MEDIR la exposición por
        estrategia. Los conteos salen del historial; el perfil, del mapa.

        Un magic sin mapa no puede dejar de contarse: `trades_by_magic` son
        hechos y `profiles_by_magic` es la interpretación, y las dos van.
        """
        filas = [
            {"magic": 8882026},
            {"magic": 8882026},
            {"magic": 9999001},
            {"magic": None},
        ]
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: filas)
        store = StoreStub(cfg_real, mapa={8882026: "default", 9999001: "cta"})

        estado = construir(store=store).daily_risk_state()

        assert estado["trades_by_magic"] == {"8882026": 2, "9999001": 1}
        assert estado["profiles_by_magic"] == {"8882026": "default", "9999001": "cta"}

    def test_sin_mapa_un_magic_desconocido_resuelve_a_default(
        self, construir: Callable[..., MT5Market], cfg_real: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sin mapa configurado, todo es el perfil default: el comportamiento de
        un solo YAML no cambia.
        """
        filas = [{"magic": 8882026}, {"magic": 9999001}]
        monkeypatch.setattr(MT5Market, "history", lambda self, days=7: filas)

        estado = construir().daily_risk_state()

        assert estado["profiles_by_magic"] == {"8882026": "default", "9999001": "default"}


# ---------------------------------------------------------------------------
# Order flow
# ---------------------------------------------------------------------------

class TestOrderflow:
    def test_sin_feed_es_neutro_con_motivo(self, mercado: MT5Market) -> None:
        """Un CVD vacío se lee como "no hay presión vendedora" cuando significa
        "nadie está mirando"."""
        snap = mercado.orderflow_snapshot()

        assert snap["payload"] is None
        assert "no está encendido" in snap["reason"]

    def test_sin_feed_no_hay_alertas(self, mercado: MT5Market) -> None:
        assert mercado.orderflow_alerts() == []

    def test_con_feed_devuelve_la_serie(self, construir: Callable[..., MT5Market]) -> None:
        serie = [{"time": 1, "value": 10.0}, {"time": 2, "value": -4.0}]
        mercado = construir(orderflow=OrderflowStub(serie=serie))

        assert mercado.orderflow_snapshot()["cvd"] == serie
        assert mercado.orderflow_alerts() == []

    def test_la_live_manda_si_tiene_puntos(self, construir: Callable[..., MT5Market]) -> None:
        """Con feed encendido y datos, la serie live es la buena."""
        serie = [{"time": i, "value": float(i)} for i in range(40)]

        snap = construir(orderflow=OrderflowStub(serie=serie)).chart_snapshot("EURUSD", "M15", 60)

        assert snap["cvd_source"].startswith("live")
        assert len(snap["cvd"]) == 40

    def test_live_vacia_cae_a_sintetico(self, construir: Callable[..., MT5Market]) -> None:
        """Un CVD que cambia de fuente sin avisar es un CVD con dos contabilidades."""
        snap = construir(orderflow=OrderflowStub(serie=[])).chart_snapshot("EURUSD", "M15", 60)

        assert snap["cvd_source"].startswith("synthetic")

    def test_feed_roto_no_tumba_el_snapshot(self, construir: Callable[..., MT5Market]) -> None:
        """Un motor de cinta que revienta deja el CVD sintético, no el snapshot entero."""

        class FeedRoto:
            def cvd_series(self, since=None):
                raise RuntimeError("lock envenenado")

            def snapshot(self):
                raise RuntimeError("lock envenenado")

        snap = construir(orderflow=FeedRoto()).chart_snapshot("EURUSD", "M15", 60)

        assert snap["cvd_source"].startswith("synthetic")


# ---------------------------------------------------------------------------
# Bitácora
# ---------------------------------------------------------------------------

class TestEnrichJournal:
    def test_no_lanza_sin_bróker(self, construir: Callable[..., MT5Market]) -> None:
        """Guardar la bitácora no puede fallar porque MT5 esté cerrado."""
        mercado = construir()

        class SesionMuerta:
            def call(self, fn):
                raise AdapterError("terminal no disponible")

        mercado._session = SesionMuerta()
        entrada = {"ticket": 42, "symbol": "EURUSD"}

        assert mercado.enrich_journal_entry(entrada) == entrada

    def test_sin_ticket_no_hace_nada(self, mercado: MT5Market) -> None:
        entrada = {"symbol": "EURUSD"}

        assert mercado.enrich_journal_entry(entrada) is entrada

    def test_ya_clasificada_no_se_toca(self, mercado: MT5Market) -> None:
        """Si el usuario ya la corrigió a mano, no se pisa con la del bróker."""
        entrada = {"ticket": 42, "poi_type": "FVG", "liquidity_swept": True}

        assert mercado.enrich_journal_entry(entrada)["poi_type"] == "FVG"

    def test_posicion_inexistente_no_revienta(self, construir: Callable[..., MT5Market]) -> None:
        resultado = construir().enrich_journal_entry({"ticket": 999, "symbol": "EURUSD"})

        assert resultado.get("poi_type") is None


# ---------------------------------------------------------------------------
# Cuenta e historial
# ---------------------------------------------------------------------------

class TestCuenta:
    def test_sin_cuenta_lanza_adapter_error(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """Un `{}` parece "no hay cuenta" y quien necesita oírlo, no lo oye."""
        mt5.account_info = lambda: None

        with pytest.raises(AdapterError):
            construir().account_info()

    def test_posiciones_vacias_es_respuesta_legitima(self, construir: Callable[..., MT5Market]) -> None:
        assert construir().positions() == []


class TestHistorial:
    def test_consolida_por_posicion(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """Abrir y cerrar son dos deals y UNA operación.

        Sin consolidar, el post-mortem cuenta cada lado como una operación
        distinta y todas las estadísticas por componente salen mal.
        """
        entrada = Deal(1, 100, type_=0, price=1.1000, time_=1_700_000_000, entry=0)
        cierre = Deal(2, 100, type_=1, price=1.1050, profit=5.0, time_=1_700_003_600, entry=1)
        mt5.history_deals_get = lambda frm, to: [entrada, cierre]

        filas = construir().history(7)

        assert len(filas) == 1
        assert filas[0]["type"] == "BUY"
        assert filas[0]["profit"] == pytest.approx(5.0)

    def test_deal_sin_posicion_se_descarta(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """`position_id == 0` agrupa fondos y comisiones sueltas bajo la misma
        operación, mezclando operaciones distintas."""
        suelto = Deal(3, 0, type_=0, price=1.1000, profit=-1.0)
        mt5.history_deals_get = lambda frm, to: [suelto]

        assert construir().history(7) == []

    def test_historial_vacio(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        mt5.history_deals_get = lambda frm, to: None

        assert construir().history(7) == []

    def test_la_ventana_la_manda_el_reloj_del_broker(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """El corte es la medianoche del BROKER, no `now - N días`.

        Un corte en UTC trunca el día en curso justo cuando se opera, y con él la
        operación de la tarde.
        """
        vistos: Dict[str, Any] = {}

        def history_deals_get(frm, to):
            vistos["rango"] = (frm, to)
            return []

        mt5.history_deals_get = history_deals_get

        construir().history(7)

        frm, to = vistos["rango"]
        assert frm.tzinfo is not None and to.tzinfo is not None
        assert timedelta(days=7) < (to - frm) <= timedelta(days=8)

    def test_mas_operaciones_vienen_mas_nuevas_primero(self, construir: Callable[..., MT5Market], mt5: FakeMT5) -> None:
        """El historial se ordena por fecha descendente: lo reciente arriba."""
        vieja = Deal(1, 100, type_=0, price=1.1000, time_=1_700_000_000, entry=0)
        nueva = Deal(3, 200, type_=0, price=1.1000, time_=1_700_086_400, entry=0)
        mt5.history_deals_get = lambda frm, to: [vieja, nueva]

        filas = construir().history(7)

        assert [f["position_id"] for f in filas] == [200, 100]

    def test_cada_fila_lleva_el_magic_de_la_operacion(
        self, construir: Callable[..., MT5Market], mt5: FakeMT5
    ) -> None:
        """El magic es el único hecho del deal que distingue una estrategia de otra."""
        entrada = Deal(1, 100, type_=0, price=1.1000, time_=1_700_000_000, entry=0, magic=8882026)
        cierre = Deal(2, 100, type_=1, price=1.1050, profit=5.0, time_=1_700_003_600, entry=1, magic=8882026)
        mt5.history_deals_get = lambda frm, to: [entrada, cierre]

        filas = construir().history(7)

        assert filas[0]["magic"] == 8882026