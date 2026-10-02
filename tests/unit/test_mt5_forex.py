"""Tests del adaptador de MT5, con el paquete `MetaTrader5` sustituido por un doble.

El test más importante del fichero es `TestLaSesionEsLaUnicaPuerta`: es el que
impide que se vuelva a introducir el bug de REF, donde `patterns_service`,
`cvd_service` y `research/data.py` tenían cada uno su executor, su lock y su
propio ciclo de `initialize()`/`shutdown()`.

Los demás tests son menos dramáticos: comprueban que se llame a la función
correcta del bróker con los argumentos correctos, y que lo que devuelve MT5
llegue normalizado a `core/`.
"""

from __future__ import annotations

import threading
import time

import pytest

from adapters.base_adapter import (
    SymbolNotFound,
    SymbolSpec,
    TerminalUnavailable,
    TIMEFRAMES,
)
from adapters.forex import mt5_forex
from adapters.forex.mt5_forex import MT5ForexAdapter, Session, adapter
from tests.unit.mt5_fake import (
    FakeMT5,
    FakeSymbolInfo,
    FakeSymbolInfoTrade,
    FakeTick,
    rates_fixture,
)


@pytest.fixture
def mt5(monkeypatch):
    """Doble de MT5 instalado en `sys.modules`, listo para configurar."""
    fake = FakeMT5.install(monkeypatch)
    fake.symbols_list = [
        FakeSymbolInfo("EURUSD"),
        FakeSymbolInfo("USDJPY"),
        FakeSymbolInfo("OCULTO", visible=False),
    ]
    fake.info_by_symbol["EURUSD"] = FakeSymbolInfo("EURUSD", digits=5, point=0.00001)
    fake.info_by_symbol["USDJPY"] = FakeSymbolInfo("USDJPY", digits=3, point=0.001)
    fake.trade_by_symbol["EURUSD"] = FakeSymbolInfoTrade(
        "EURUSD", trade_tick_size=0.00001, trade_tick_value=1.0
    )
    return fake


@pytest.fixture
def session():
    """Sesión real (con su hilo) que se cierra al terminar."""
    s = Session()
    yield s
    s.shutdown_executor()


@pytest.fixture
def adp(session, mt5):
    """Adaptador con sesión propia y el doble de MT5 ya instalado."""
    return MT5ForexAdapter(session)


# ---------------------------------------------------------------------------
# El bug de las tres puertas
# ---------------------------------------------------------------------------


class TestLaSesionEsLaUnicaPuerta:
    """Lo que REF tenía mal, escrito como tests que no pueden volver a pasar."""

    def test_solo_hay_un_hilo_que_habla_con_mt5(self, session):
        """`max_workers=1` no es una decisión de rendimiento.

        El paquete MT5 habla con UN terminal por proceso y no tolera que dos
        hilos lo toquen. Con dos hilos, un `copy_rates` y un `order_send` a la
        vez se corrompen de formas que el bróker no reporta: devuelve `None` sin
        error y el llamante no tiene forma de saber que es mentira.
        """
        assert session._executor._max_workers == 1

    def test_dos_lecturas_simultaneas_no_se_solapan(self, mt5, session):
        """El test que falla si alguien vuelve a meter dos executors.

        Se lanza una lectura que se queda dentro a propósito y, mientras tanto,
        se pide otra. Si el acceso no estuviera serializado, el doble vería dos
        llamadas a la vez (`max_concurrent_calls > 1`) y `shutdown` colgaría de
        una llamada en vuelo.

        El primer trabajo no es un `time.sleep`: espera un `Event`, así que el
        test falla rápido si el segundo no puede entrar, en vez de colgarse hasta
        el timeout de la suite.
        """
        entra = threading.Event()
        salir = threading.Event()

        def primero(_mt5_mod):
            entra.set()
            salir.wait(timeout=5)
            return "primero"

        def segundo(_mt5_mod):
            return "segundo"

        t = threading.Thread(target=lambda: session.call(primero))
        t.start()
        assert entra.wait(timeout=5), "el primer trabajo no llegó a ejecutarse"

        # Mientras el primero sigue dentro, el segundo NO puede entrar.
        segundo_hilo = threading.Thread(target=lambda: session.call(segundo))
        segundo_hilo.start()
        time.sleep(0.05)
        assert mt5.max_concurrent_calls <= 1, "dos llamadas a MT5 a la vez"

        salir.set()
        t.join(timeout=5)
        segundo_hilo.join(timeout=5)
        assert not t.is_alive() and not segundo_hilo.is_alive(), "un hilo quedó colgado"
        assert mt5.max_concurrent_calls <= 1

    def test_el_shutdown_nunca_ocurre_con_una_llamada_en_vuelo(self, session, mt5):
        """El `shutdown()` de REF estaba en un `finally`. Aquí no hay ninguno.

        En REF, el `finally: mt5.shutdown()` de una puerta desenchufaba la
        terminal mientras otra estaba a mitad de `copy_rates_from_pos`. El doble
        registra ese caso en `shutdown_while_busy`, y este test afirma que nunca
        ocurre. Para que el test signifique algo, el doble NO levanta al detectar
        concurrencia: registra y sondea el test.
        """
        assert mt5.shutdown_while_busy is False

        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        a = MT5ForexAdapter(session)
        for _ in range(20):
            a.ohlc("EURUSD", "M15", 3)
        session.close()

        assert mt5.shutdown_count >= 1
        assert mt5.shutdown_while_busy is False

    def test_una_sesion_reabierta_sigue_funcionando(self, mt5, session):
        """`close()` no es irreversible: la siguiente llamada reabre.

        `shutdown_executor()` sí lo es (mata el hilo). La diferencia importa en
        tests, que abren y cierran sesiones, y en un reinicio del terminal.
        """
        a = MT5ForexAdapter(session)
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        assert len(a.ohlc("EURUSD", "M15", 3)) == 3

        session.close()
        assert session.is_open is False

        assert len(a.ohlc("EURUSD", "M15", 3)) == 3
        assert session.is_open is True
        assert mt5.called("initialize") is True

    def test_close_espera_a_que_no_quede_nada_en_vuelo(self, mt5, session):
        """Cerrar no es solo `mt5.shutdown()`: primero drena."""
        dentro = threading.Event()
        seguir = threading.Event()

        def lento(_mt5_mod):
            dentro.set()
            seguir.wait(timeout=5)
            return 1

        t = threading.Thread(target=lambda: session.call(lento))
        t.start()
        assert dentro.wait(timeout=5)

        # `close()` en otro hilo debe esperar, no cerrar por las espaldas.
        cerrado = threading.Event()

        def cerrar():
            session.close()
            cerrado.set()

        tc = threading.Thread(target=cerrar)
        tc.start()
        time.sleep(0.05)
        assert cerrado.is_set() is False, "close() cerró con trabajo en vuelo"

        seguir.set()
        t.join(timeout=5)
        tc.join(timeout=5)
        assert cerrado.is_set() is True
        assert mt5.shutdown_while_busy is False

    def test_close_es_idempotente(self, session, mt5):
        """Cerrar dos veces no debe hacer dos `shutdown()` ni reventar.

        Es el estado que se alcanza cuando el servidor web se apaga y el tests
        de integración también cierran la sesión.
        """
        a = MT5ForexAdapter(session)
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(2))
        a.ohlc("EURUSD", "M15", 2)
        session.close()
        session.close()
        assert mt5.shutdown_count == 1


# ---------------------------------------------------------------------------
# La sesión se abre una vez, no en cada llamada
# ---------------------------------------------------------------------------


class TestAperturaDeSesion:
    def test_no_reconecta_en_cada_llamada(self, adp, mt5):
        """REF hacía `initialize()` en CADA llamada y `shutdown()` al salir.

        MT5 tarda ~100ms en conectar. Con 40 lecturas al minuto son 40
        reconexiones, y cada una es una ventana donde el bróker puede responder
        con datos a medio cargar.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        for _ in range(10):
            adp.ohlc("EURUSD", "M15", 3)
        assert len(mt5.calls_named("initialize")) == 1
        assert mt5.shutdown_count == 0

    def test_una_terminal_que_no_arraque_da_un_error_usable(self, session, monkeypatch):
        """El error tiene que decir QUÉ hacer, no solo qué falló.

        REF decía "No hay conexión con MetaTrader 5". El mensaje actual incluye
        el `last_error` del bróker, que es la diferencia entre diagnosticar en
        diez segundos y en diez minutos.
        """
        fake = FakeMT5.install(monkeypatch, initialize_ok=False, last_error_value=10049)
        a = MT5ForexAdapter(session)
        with pytest.raises(TerminalUnavailable) as exc:
            a.ohlc("EURUSD", "M15", 10)
        texto = str(exc.value)
        assert "terminal" in texto.lower()
        assert "10049" in texto

    def test_una_terminal_que_no_arraque_no_deja_la_sesion_abierta(self, session, monkeypatch):
        """Tras un fallo de `initialize()` no puede quedar estado a medias.

        Si el `shutdown()` de limpieza fallara, el siguiente `initialize()`
        conectaría con una terminal en estado desconocido. Con la sesión sin
        abrir, el siguiente intento es un intento limpio.
        """
        fake = FakeMT5.install(monkeypatch, initialize_ok=False)
        a = MT5ForexAdapter(session)
        with pytest.raises(TerminalUnavailable):
            a.ohlc("EURUSD", "M15", 10)
        assert session.is_open is False

    def test_importar_el_adaptador_no_abre_la_terminal(self):
        """`import adapters.forex.mt5_forex` no debe inicializar MT5.

        El adaptador se importa desde `api/routes/db.py`, desde los tests y desde
        cualquier chequeo de imports. Si importar abriera la terminal, un `pytest`
        entero dependería de que haya una sesión abierta.
        """
        assert mt5_forex._SESSION is None or mt5_forex._SESSION.is_open is False

    def test_importar_sin_el_paquete_no_falla(self, monkeypatch):
        """En CI o en un portátil sin terminal, `import` tiene que funcionar.

        El paquete `MetaTrader5` es un import nativo y falla en la línea del
        import si la terminal no está instalada. Por eso el import va dentro de
        la sesión y hay un sustituto que lanza al USARLO.
        """
        import sys

        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        assert mt5_forex._import_mt5() is not None

    def test_sin_el_paquete_el_operador_sabe_que_hace_falta(self, monkeypatch):
        """Sin paquete, la excepción debe decir el `pip install` de memoria."""
        import sys

        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        a = MT5ForexAdapter(Session())
        with pytest.raises(TerminalUnavailable) as exc:
            a.ohlc("EURUSD", "M15", 10)
        assert "pip install" in str(exc.value)


# ---------------------------------------------------------------------------
# Velas
# ---------------------------------------------------------------------------


class TestVelas:
    def test_pide_las_velas_al_brroker_con_los_argumentos_correctos(self, adp, mt5):
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(30))
        adp.ohlc("EURUSD", "M15", 30)
        llamada = mt5.calls_named("copy_rates_from_pos")[0]
        assert llamada[1] == "EURUSD"
        assert llamada[2] == TIMEFRAMES["M15"]
        assert llamada[3] == 0
        assert llamada[4] == 30

    def test_devuelve_la_forma_que_necesita_core(self, adp, mt5):
        """`time/open/high/low/close/volume`, con `time` en segundos epoch.

        Es lo que consumen `core.market_view.footprint()` y
        `core.orderflow_engine.build_cvd_series()`. Si esto cambia, el núcleo se
        entera al primer test que falle de esos dos ficheros.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(4))
        velas = adp.ohlc("EURUSD", "M15", 4)
        assert len(velas) == 4
        for vela in velas:
            assert set(vela) == {"time", "open", "high", "low", "close", "volume"}
        assert velas[0]["time"] == 1_700_000_000
        assert velas[0]["open"] == pytest.approx(1.1000)

    def test_las_velas_llegan_en_orden_de_mas_antigua_a_mas_reciente(self, adp, mt5):
        """El orden es lo que hace que el CVD tenga sentido.

        `copy_rates_from_pos` devuelve de más antigua a más reciente, que es lo
        que espera `build_cvd_series()`. Si alguien "optimiza" invirtiendo la
        lista para ir de la más nueva a la más antigua (parece más natural para un
        gráfico), el CVD suma los deltas al revés y una curva que sube cuando el
        mercado baja. Este test es la red.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(6))
        velas = adp.ohlc("EURUSD", "M15", 6)
        tiempos = [v["time"] for v in velas]
        assert tiempos == sorted(tiempos)

    def test_acota_las_velas_al_tope_del_broker(self, adp, mt5):
        """Pedir 5000 no da error: da 1000 y trunca en silencio.

        MT5 ignora lo que le pases de más. Un modelo que cree que pidió 5000 y
        recibe 1000 no sabe por qué su footprint sale corto, así que el tope se
        aplica aquí y el error es visible en el log.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(50))
        adp.ohlc("EURUSD", "M15", 99999)
        llamada = mt5.calls_named("copy_rates_from_pos")[0]
        assert llamada[4] == 1000

    def test_no_pide_menos_de_10_velas(self, adp, mt5):
        """El bróker no sirve menos de 10 y devolvería una lista vacía."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(50))
        adp.ohlc("EURUSD", "M15", 1)
        assert mt5.calls_named("copy_rates_from_pos")[0][4] == 10

    def test_un_timeframe_invalido_falla_antes_de_tocar_el_broker(self, adp, mt5):
        """Un typo en el timeframe no debe abrir una conexión para nada."""
        with pytest.raises(ValueError, match="no válido"):
            adp.ohlc("EURUSD", "M7", 10)
        assert mt5.calls_named("copy_rates_from_pos") == []

    def test_un_simbolo_vacio_falla_antes_de_tocar_el_broker(self, adp, mt5):
        with pytest.raises(ValueError):
            adp.ohlc("", "M15", 10)
        assert mt5.calls_named("copy_rates_from_pos") == []

    def test_un_simbolo_que_el_broker_no_publica_lo_dice_bien(self, adp, mt5):
        """El mensaje tiene que distinguir "no existe" de "problema técnico".

        Si el bróker no publica el símbolo, la respuesta útil es "ese símbolo no
        está en tu bróker, mira el sufijo", no un error genérico que hace pensar
        que se cayó la conexión.
        """
        with pytest.raises(SymbolNotFound) as exc:
            adp.ohlc("NOEXISTE", "M15", 10)
        texto = str(exc.value)
        assert "NOEXISTE" in texto
        assert "Market Watch" in texto

    def test_un_brroker_sin_datos_devuelve_una_lista_vacia(self, adp, mt5):
        """Sin velas no es un error: un mercado cerrado es una lista vacía.

        Distinguir "no hay velas" de "falló la lectura" importa porque un weekend
        es un caso normal y un `None` sería un fallo a la vista del usuario.
        """
        mt5.rates_by_key[("EURUSD", TIMEFRAMES["M15"])] = None
        assert adp.ohlc("EURUSD", "M15", 10) == []

    def test_la_lectura_diaria_viene_en_la_misma_viaje(self, adp, mt5):
        """`(intradía, D1)` en una llamada, porque el patrón SMC necesita el PDH/PDL.

        Pedirlas por separado son dos viajes al terminal para leer el mismo
        estado, y entre uno y otro el día puede cambiar: la vela D1 de la
        segunda llamada puede ser ya la de hoy.
        """
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(4))
        mt5.set_rates("EURUSD", TIMEFRAMES["D1"], rates_fixture(3, step=0.001))
        intraday, diario = adp.ohlc_with_daily("EURUSD", "M15", 4)
        assert len(intraday) == 4
        assert len(diario) == 3
        marcos = {(c[1], c[2]) for c in mt5.calls_named("copy_rates_from_pos")}
        assert marcos == {("EURUSD", TIMEFRAMES["M15"]), ("EURUSD", TIMEFRAMES["D1"])}

    def test_bars_es_alias_de_ohlc(self, adp, mt5):
        """El mismo dato con el nombre que usa el resto del sistema."""
        mt5.set_rates("EURUSD", TIMEFRAMES["M15"], rates_fixture(3))
        assert adp.bars("EURUSD", "M15", 3) == adp.ohlc("EURUSD", "M15", 3)


# ---------------------------------------------------------------------------
# Trades de cinta
# ---------------------------------------------------------------------------


class TestTrades:
    def test_los_trades_llegan_en_orden_ascendente(self, adp, mt5):
        """MT5 los devuelve al revés. Invertir es lo que hace falta.

        `OrderFlowEngine.add_trade()` acumula el CVD en el orden en que llega:
        alimentarlo con la lista de MT5 tal cual invierte la curva, y un CVD que
        baja cuando el mercado sube es inútil como señal.
        """
        mt5.set_ticks(
            "EURUSD",
            [
                FakeTick(1_700_000_300, 1.1001, 1.1002, 1.1002, 5),
                FakeTick(1_700_000_200, 1.1001, 1.1002, 1.1002, 4),
                FakeTick(1_700_000_100, 1.1001, 1.1002, 1.1002, 3),
            ],
        )
        trades = adp.trades("EURUSD")
        assert [t["ts"] for t in trades] == [1_700_000_100, 1_700_000_200, 1_700_000_300]

    def test_los_trades_usan_el_lado_agresor(self, adp, mt5):
        """Una compra agresiva es `A` (Ask), no `B`."""
        mt5.set_ticks("EURUSD", [FakeTick(1_700_000_100, 1.1000, 1.1001, 1.1001, 5)])
        assert adp.trades("EURUSD")[0]["side"] == "A"

    def test_trades_sin_cinta_devuelve_lista_vacia(self, adp, mt5):
        mt5.set_ticks("EURUSD", [])
        assert adp.trades("EURUSD") == []

    def test_el_rango_es_explicito_nunca_desde_el_principio(self, adp, mt5):
        """Un rango explícito se comporta igual en todas las versiones.

        `copy_ticks_from` lee hacia atrás desde una posición que el bróker no
        define igual según la versión. Con `since_ts=0` el rango es "todo", que
        es lo único portable.
        """
        mt5.set_ticks("EURUSD", [FakeTick(1_700_000_100, 1.1, 1.1001, 1.1001, 1)])
        adp.trades("EURUSD")
        llamada = mt5.calls_named("copy_ticks_range")[0]
        assert llamada[1] == "EURUSD"
        assert llamada[2] == 0
        assert llamada[3] > 1_700_000_000


# ---------------------------------------------------------------------------
# Precio y símbolos
# ---------------------------------------------------------------------------


class TestPrecioYSimbolos:
    def test_el_precio_trae_bid_ask_y_last(self, adp, mt5):
        mt5.tick_by_symbol["EURUSD"] = FakeTick(1_700_000_000, 1.1000, 1.1001, 1.10005)
        precio = adp.price("EURUSD")
        assert precio["bid"] == pytest.approx(1.1000)
        assert precio["ask"] == pytest.approx(1.1001)
        assert precio["last"] == pytest.approx(1.10005)
        assert precio["time"] == 1_700_000_000

    def test_un_precio_que_el_broker_no_publica_es_none(self, adp):
        """None, no un 0.0.

        Un precio 0 se lee como "el mercado está en cero", y un cálculo de
        distancia a partir de ahí da 0 SL, que es peor que no tener ninguno.
        """
        assert adp.price("NOEXISTE") is None

    def test_solo_ofrece_los_simbolos_visibles(self, adp, mt5):
        """Para un símbolo con `visible=False`, `symbol_info` devuelve None.

        Ofrecerlo sería ofrecer algo que no funciona: el usuario lo elige y
        recibe "símbolo no encontrado" sin entender por qué estaba en la lista.
        """
        simbolos = adp.symbols()
        assert "EURUSD" in simbolos
        assert "OCULTO" not in simbolos
        assert simbolos == sorted(simbolos)

    def test_sin_simbolos_devuelve_lista_vacia(self, adp, mt5):
        mt5.symbols_list = []
        assert adp.symbols() == []


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


class TestSpecs:
    def test_arma_el_spec_desde_los_dos_lugares_del_paquete(self, adp, mt5):
        """`SYMBOL_TRADE_*` vive en `symbol_info_trade` en los paquetes nuevos.

        Y `digits`/`point` en `symbol_info` en todos. Un spec montado solo de
        `symbol_info` da `tick_value=0` y, con eso, un cálculo de riesgo que
        parece cero. Por eso se leen los dos y se rellena cada campo del sitio que
        lo tiene.
        """
        spec = adp.spec("EURUSD")
        assert spec.symbol == "EURUSD"
        assert spec.digits == 5
        assert spec.point == pytest.approx(0.00001)
        assert spec.tick_value == pytest.approx(1.0)
        assert spec.tick_size == pytest.approx(0.00001)

    def test_calcula_el_pip_correcto_por_simbolo(self, adp, mt5):
        """El caso del yen y el caso del euro salen distintos. Y el oro, si lo hay."""
        assert adp.spec("EURUSD").pip == pytest.approx(0.0001)
        assert adp.spec("USDJPY").pip == pytest.approx(0.01)

    def test_un_simbolo_inexistente_da_none_y_no_un_spec_inventado(self, adp):
        """Esta es la garantía que importa más que ninguna otra del fichero.

        Un spec inventado (los valores por defecto de otro símbolo) produce un
        cálculo de riesgo plausible y equivocado. El mejor resultado posible
        cuando no se sabe la spec es no saberla.
        """
        assert adp.spec("NOEXISTE") is None

    def test_un_paquete_viejo_sin_symbol_info_trade_aun_da_spec(self, adp, mt5):
        """En un paquete antiguo la función no existe, y eso no es un error.

        Los campos de contrato se leerán de `symbol_info`, que es lo que
        funcionaba antes. Perder las specs por una diferencia de versión sería
        tirar el símbolo entero.
        """
        # Se pone a None en la INSTANCIA, que es como se comporta un paquete
        # viejo: la función no está. `_safe_trade` lo comprueba con `is None`,
        # no con `hasattr`, así que esto reproduce el caso real.
        mt5.symbol_info_trade = None
        mt5.trade_by_symbol.clear()
        spec = adp.spec("EURUSD")
        assert spec.digits == 5
        assert spec.tick_value == 0.0, "sin el paquete nuevo, el valor es 0 = no lo sé"
        assert spec.tick_value_for_volume(1.0) is None

    def test_un_spec_sin_contrato_no_permite_calcular_riesgo(self, adp, mt5):
        mt5.trade_by_symbol.clear()
        spec = adp.spec("EURUSD")
        assert spec.tick_value_for_volume(1.0) is None, (
            "sin tick_value no se puede calcular riesgo, y 0.0 parecería "
            "que no hay riesgo"
        )


# ---------------------------------------------------------------------------
# Inyección de sesión
# ---------------------------------------------------------------------------


class TestInyeccionDeSesion:
    def test_la_fabrica_acepta_una_sesion(self, session):
        assert adapter(session).session is session

    def test_set_session_devuelve_la_anterior(self):
        primera = Session()
        segunda = Session()
        try:
            # La sesión global se guarda y se restaura: dejarla cambiada rompería
            # cualquier test posterior que asuma la de por defecto.
            original = mt5_forex.get_session()
            try:
                anterior = mt5_forex.set_session(primera)
                assert anterior is original
                assert mt5_forex.get_session() is primera

                anterior = mt5_forex.set_session(segunda)
                assert anterior is primera
                assert mt5_forex.get_session() is segunda
            finally:
                mt5_forex.set_session(original)
                if original is not primera and original is not segunda:
                    original.shutdown_executor()
        finally:
            primera.shutdown_executor()
            segunda.shutdown_executor()

    def test_get_session_es_la_misma_para_todos(self):
        """Dos adaptadores sin sesión propia comparten la del proceso.

        Es el requisito del paquete MT5: UN terminal por proceso. Si cada
        adaptador fabricara su sesión, estaríamos de vuelta en las tres puertas
        de REF.
        """
        a = MT5ForexAdapter()
        b = MT5ForexAdapter()
        assert a.session is b.session

    def test_el_adaptador_tiene_nombre(self, adp):
        assert adp.name == "mt5"