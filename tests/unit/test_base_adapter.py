"""Tests de la forma normalizada: lo que `core/` recibe de cualquier proveedor.

Estos tests no dependen de MT5 ni de ningún proveedor. Fijan el CONTRATO entre
adaptadores y núcleo, y por eso son los que más valoran: si mañana entra
Databento o ccxt, estos tests son la lista de chequeo que hay que cumplir, y
`test_el_adaptador_cumple_el_contrato` se encarga de que no se olviden.

El caso que más ha costado en este proyecto es el del sufijo del bróker, y hay
varios tests abajo específicamente sobre eso. Está en el centro de este módulo
porque es donde se decide si un símbolo configurado se encuentra o no.
"""

from __future__ import annotations

import pytest

from adapters.base_adapter import (
    AdapterError,
    SymbolNotFound,
    SymbolSpec,
    TerminalUnavailable,
    TIMEFRAMES,
    filas_o_vacias,
    normalize_candle,
    normalize_ohlc,
    normalize_symbol,
    normalize_trade,
    resolve_timeframe,
)


class TestExcepciones:
    def test_las_excepciones_de_adaptador_se_distinguen_entre_si(self):
        """Cada fallo necesita una reacción distinta de quien llama.

        `TerminalUnavailable` = "abre la terminal" (se avisa al usuario).
        `SymbolNotFound` = "cambia el símbolo en el YAML" (no es culpa de la
        terminal). Si se colapsaran en un solo `AdapterError`, el llamador
        tendría que adivinar por el texto para decidir qué decirle al usuario.
        """
        assert issubclass(TerminalUnavailable, AdapterError)
        assert issubclass(SymbolNotFound, AdapterError)
        assert TerminalUnavailable is not SymbolNotFound

    def test_no_se_confunde_un_error_de_argumento_con_uno_de_proveedor(self):
        """`ValueError` es del llamador; `AdapterError` es del mundo exterior.

        Es tentador usar `AdapterError` para todo y evitar pensar. El coste es
        que un typo en el timeframe acaba diciendo "comprueba que la terminal
        esté abierta", que no ayuda a nadie a encontrarlo.
        """
        with pytest.raises(ValueError) as exc:
            resolve_timeframe("M7")
        assert "no válido" in str(exc.value)
        assert not isinstance(exc.value, AdapterError)


class TestTimeframes:
    def test_los_timeframes_de_hora_no_son_las_horas(self):
        """M1 vale 1 y H1 vale 16385. Si H1 fuera 60, el bróker lo rechaza.

        El bit 15 marca los timeframes de horas y días en el protocolo de MT5.
        Este test parece obvio y no lo es: el doble de MT5 en
        `tests/unit/mt5_fake.py` usa los valores reales A PROPÓSITO, y si
        alguien "arregla" `TIMEFRAME_H1 = 60` porque le parece más legible,
        este test falla y dice por qué.
        """
        assert TIMEFRAMES["M1"] == 1
        assert TIMEFRAMES["H1"] == 16385
        assert TIMEFRAMES["D1"] == 16408
        assert TIMEFRAMES["W1"] == 32769

    def test_resolve_timeframe_acepta_cualquier_forma_de_escribirlo(self):
        for variante in ("m15", "M15", " m15 ", "M15\n"):
            assert resolve_timeframe(variante) == TIMEFRAMES["M15"]

    def test_resolve_timeframe_no_traga_silenciosamente_un_desconocido(self):
        """Un timeframe desconocido debe NOMBRAR los válidos, no solo fallar."""
        with pytest.raises(ValueError) as exc:
            resolve_timeframe("M99")
        mensaje = str(exc.value)
        assert "M99" in mensaje
        assert "M15" in mensaje, "el error debe listar los timeframes válidos"


class TestNormalizacionDeSimbolos:
    @pytest.mark.parametrize(
        "entrada,esperado",
        [
            ("EURUSD.a", "EURUSD"),
            ("EURUSD_m", "EURUSD"),
            ("EURUSD#", "EURUSD"),
            ("EURUSD-PRO", "EURUSD"),
            ("eurusd", "EURUSD"),
            ("  eurusd.a  ", "EURUSD"),
            ("XAUUSD.pro", "XAUUSD"),
            ("6E.c.0", "6E"),
        ],
    )
    def test_el_sufijo_del_broker_es_ruido(self, entrada, esperado):
        """`normalize_symbol` existe para COMPARAR, no para enviar.

        El bróker llama EURUSD a "EURUSD.a", "EURUSD_m" o "EURUSD#". Comparar el
        símbolo del YAML contra cualquiera de esos da "símbolo desconocido" y
        hace que un símbolo bien configurado no se encuentre nunca.
        """
        assert normalize_symbol(entrada) == esperado

    def test_solo_quita_el_sufijo_ruta_no_los_digitos(self):
        """`6E.c.0` -> `6E`: el "c.0" es la ruta del contrato, no parte del nombre.

        Un normalizador que se comiera todo lo que hay tras el primer separador
        dejaría "6E" tanto para el futuro como para un CFD. Aquí se acepta que
        se colisionen: para comparar, y el envío va con el nombre real.
        """
        assert normalize_symbol("6E.c.0") == "6E"
        # Todos los sufijos del bróker colapsan al MISMO nombre base. Por eso
        # esto sirve para comparar y no para enviar.
        assert normalize_symbol("EURUSD.a") == normalize_symbol("EURUSD.b") == "EURUSD"

    def test_no_inventa_un_nombre_para_una_entrada_vacia(self):
        assert normalize_symbol("") == ""
        assert normalize_symbol(None) == ""


class TestNormalizacionDeVelas:
    def test_traduce_la_namedtuple_posicional_de_mt5(self):
        """MT5 devuelve POSICIONALES. El orden lo fija el C struct del paquete."""
        fila = (1_700_000_000, 1.1, 1.2, 1.09, 1.15, 100, 10, 100)
        vela = normalize_candle(fila)
        assert vela == {
            "time": 1_700_000_000,
            "open": 1.1,
            "high": 1.2,
            "low": 1.09,
            "close": 1.15,
            "volume": 100,
        }

    def test_traduce_un_dict_de_cripto_con_alias_de_una_letra(self):
        """ccxt y Databento usan claves de una letra. Se aceptan igual.

        La forma de ccxt es {"t","o","h","l","c","v"}; la de Databento usa los
        nombres largos. Aceptar ambas evita un `if proveedor == ...` en cada
        adaptador.
        """
        assert normalize_candle({"t": 1, "o": 2, "h": 3, "l": 1, "c": 2, "v": 9}) == {
            "time": 1,
            "open": 2.0,
            "high": 3.0,
            "low": 1.0,
            "close": 2.0,
            "volume": 9,
        }

    def test_una_vela_vacia_no_inventa_precios(self):
        """Faltan claves -> 0, no una excepción.

        MT5 devuelve `None` para una vela sin ticks, y ccxt devuelve dicts con
        claves ausentes en velas degradadas. Lanzar ahí rompe la serie entera
        por una vela suelta.
        """
        assert normalize_candle({"time": 5}) == {
            "time": 5,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
            "volume": 0,
        }

    def test_normalize_ohlc_soporta_lista_vacia(self):
        assert normalize_ohlc([]) == []
        assert normalize_ohlc(None) == []

    def test_normalize_ohlc_acepta_el_array_numpy_que_devuelve_mt5(self):
        """La capa base también era partícipe del bug, no solo el adaptador.

        `normalize_ohlc()` hacía `for r in rows or []`, y un numpy array de más de
        un elemento no admite la pregunta por su verdad: lanza `ValueError`. Como
        el array se indexa por posición igual que una tupla, la fila se normaliza
        sin tocar nada; lo único que hay que cambiar es la comprobación.
        """
        numpy = pytest.importorskip("numpy")
        filas = numpy.array(
            [
                (1_700_000_000, 1.1000, 1.1010, 1.0990, 1.1005, 42, 0, 0),
                (1_700_000_900, 1.1005, 1.1020, 1.1000, 1.1015, 43, 0, 0),
            ],
            dtype=float,
        )
        velas = normalize_ohlc(filas)
        assert len(velas) == 2
        assert velas[0] == {
            "time": 1_700_000_000,
            "open": 1.1000,
            "high": 1.1010,
            "low": 1.0990,
            "close": 1.1005,
            "volume": 42,
        }

    def test_filas_o_vacias_no_pregunta_por_la_verdad_de_un_array(self):
        """La regla, en una línea: `None` se vacía, un array se devuelve tal cual."""
        numpy = pytest.importorskip("numpy")
        array = numpy.array([1.0, 2.0, 3.0])
        assert filas_o_vacias(None) == []
        assert filas_o_vacias(array) is array
        assert filas_o_vacias([]) == []

    def test_el_tiempo_se_devuelve_como_entero(self):
        """Un float en la clave `time` rompe el eje del gráfico más adelante.

        Lightweight Charts ordena por tiempo y mezcla tipos sin avisar.
        Convertir aquí es lo barato; el síntoma allí sería "la vela 3 aparece
        en la 2".
        """
        vela = normalize_candle((1_700_000_000.7, 1, 1, 1, 1, 1, 1, 1))
        assert isinstance(vela["time"], int)


class TestNormalizacionDeTrades:
    def test_un_tick_de_mt5_sale_con_side_agresor(self):
        """`side` es el LADO AGRESOR, no el lado del que cierra la posición.

        Nombrar "buy"/"sell" habría sido peor: en una orden de compra agresiva el
        agresor está en el Ask, así que "buy" confundía el lado que QUIERE subir
        con el lado que PONE órdenes. "A"/"B" (Ask/Bid) no admite esa lectura.
        """
        tick = (1_700_000_000, 1.1000, 1.1001, 1.1001, 5)
        trade = normalize_trade(tick)
        assert trade["side"] == "A", "compró a mercado en el Ask"
        assert trade["price"] == pytest.approx(1.1001)
        assert trade["size"] == 5

    def test_la_venta_agresiva_es_side_b(self):
        """Un tick que aporta volumen al Bid significa que alguien vendió."""
        tick = (1_700_000_000, 1.1000, 1.1001, 1.0999, 5)
        assert normalize_trade(tick)["side"] == "B"

    def test_acepta_un_trade_ya_normalizado_sin_tocarlo(self):
        trade = {"ts": 1, "price": 1.1, "size": 3, "side": "A"}
        assert normalize_trade(trade) == trade

    def test_el_ts_es_segundos_y_no_microsegundos(self):
        """MT5 devuelve `time_msc` en microsegundos; `time` en segundos.

        Confundirlos multiplica la serie temporal por 1e6. El resultado es un CVD
        que parece plano porque todos los trades caen en el mismo microsegundo
        del eje. Por eso el normalizador lee SIEMPRE el primer campo, que es
        `time` en segundos, y no `time_msc`.
        """
        tick = (1_700_000_000, 1.1, 1.1001, 1.1001, 5)
        assert normalize_trade(tick)["ts"] == 1_700_000_000

    def test_side_desconocido_cae_a_ask_en_vez_de_valor_inventado(self):
        """Un lado raro se degrada a "A", que es el caso conservador.

        Las dos opciones eran inventar un tercer valor (que `OrderFlowEngine`
        no sabría interpretar) o lanzar (que tumba la serie por un tick raro).
        Degradar es lo que sobrevive a producción: el error por un tick dudoso
        no puede costar un stream entero.
        """
        assert normalize_trade({"ts": 1, "price": 1.0, "size": 1, "side": "Z"})["side"] == "A"


class TestSymbolSpec:
    """La regla de pips es donde el proyecto ya se comió un bug real.

    En `REF/symbol_specs.py` está escrito: aplicar la convención de forex a oro
    multiplicaba el SL por diez. Un SL de "12 unidades" en EURUSD son 0.0012
    (12 pips) y en oro son 0.12 (12 puntos). Con `point * 10` en oro salía 1.2.
    """

    CASOS = [
        ("EURUSD", 5, 0.00001, 0.0001, True),
        ("USDJPY", 3, 0.001, 0.01, True),
        ("XAUUSD", 2, 0.01, 0.01, False),
        ("XAGUSD", 3, 0.001, 0.01, True),
        ("NAS100", 1, 0.1, 0.1, False),
        ("US30", 1, 1.0, 1.0, False),
    ]

    @pytest.mark.parametrize("symbol,digits,point,pip_esperado,es_pip", CASOS)
    def test_el_pip_segun_los_decimales(self, symbol, digits, point, pip_esperado, es_pip):
        spec = SymbolSpec(symbol=symbol, digits=digits, point=point)
        assert spec.pip == pytest.approx(pip_esperado)
        assert spec.pip_quote is es_pip
        assert spec.unit_label == ("pips" if es_pip else "puntos")

    def test_el_bug_del_oro(self):
        """El número concreto que salió mal en producción, con la magnitud incluida."""
        oro = SymbolSpec("XAUUSD", 2, 0.01)
        eur = SymbolSpec("EURUSD", 5, 0.00001)
        assert oro.pip == 0.01
        assert eur.pip == 0.0001
        assert oro.to_price(12) == pytest.approx(0.12)  # 12 puntos
        assert eur.to_price(12) == pytest.approx(0.0012)  # 12 pips
        # Lo que hacía la versión vieja:
        assert 0.01 * 10 * 12 == pytest.approx(1.2)
        assert 1.2 == pytest.approx(oro.to_price(12) * 10)

    def test_un_pip_override_manda_sobre_la_regla(self):
        spec = SymbolSpec("XAUUSD", 2, 0.01).with_pip(0.02)
        assert spec.pip == pytest.approx(0.02)
        assert SymbolSpec("XAUUSD", 2, 0.01).with_pip(None).pip == pytest.approx(0.01)

    def test_una_spec_sin_punto_no_revienta(self):
        """Un broker raro puede publicar point=0. Devolver 0, no ZeroDivisionError."""
        spec = SymbolSpec("DESCONOCIDO", 5, 0.0)
        assert spec.pip == 0.0
        assert spec.to_price(12) == 0.0
        assert spec.from_price(0.01) == 0.0
        assert "en precio" in spec.format_units(0.01)

    def test_ida_y_vuelta_unidades_precio(self):
        for symbol, digits, point, _pip, _q in self.CASOS:
            spec = SymbolSpec(symbol, digits, point)
            assert spec.from_price(spec.to_price(7.5)) == pytest.approx(7.5)

    def test_una_distancia_minima_se_etiqueta_en_precio_crudo(self):
        """`0.04 pips` es ruido que esconde que el número es una distancia cruda.

        Formatear por debajo de una centésima de unidad engaña: el panel de
        riesgo muestra "0.0 pips" y parece un bug de redondeo cuando en
        realidad el SL está pegado al precio.
        """
        spec = SymbolSpec("EURUSD", 5, 0.00001)
        assert spec.format_units(0.0012) == "12.0 pips"
        # 0.0000001 es una centésima de pip: formatearlo como "0.0 pips" sería
        # ruido que esconde que el SL está pegado al precio.
        assert "en precio" in spec.format_units(0.0000001)

    def test_normaliza_volume_a_los_minimos_del_broker(self):
        """Un bróker rechaza 0.113 lotes, así que se ajusta ANTES de enviar."""
        spec = SymbolSpec("EURUSD", 5, 0.00001, volume_min=0.1, volume_max=1.0, volume_step=0.1)
        assert spec.normalize_volume(0.113) == pytest.approx(0.1)
        assert spec.normalize_volume(0.55) == pytest.approx(0.6)
        assert spec.normalize_volume(5.0) == pytest.approx(1.0)
        assert spec.normalize_volume(0.01) == pytest.approx(0.1)

    def test_normalize_volume_no_deja_ruido_de_float(self):
        """0.01 * 7 / 0.01 = 0.07000000000000001; el bróker lo rechaza."""
        spec = SymbolSpec("EURUSD", 5, 0.00001, volume_min=0.01, volume_step=0.01)
        assert repr(spec.normalize_volume(0.07)) == "0.07"

    def test_sin_datos_de_contrato_devuelve_none_y_no_mentira(self):
        """El caso peligroso: `tick_value=0` y riesgo calculado a cero.

        Si `tick_value_for_volume()` devolviera 0.0, una posición SIN stop
        parecería no tener riesgo, que es el peor resultado posible: se opera
        sin SL creyéndose protegido. None obliga a decidir.
        """
        spec = SymbolSpec("EURUSD", 5, 0.00001, tick_value=0.0, tick_size=0.00001)
        assert spec.tick_value_for_volume(1.0) is None
        assert spec.distance_ticks(0.005) == pytest.approx(500.0)

    def test_sin_tick_size_no_hay_ticks(self):
        spec = SymbolSpec("EURUSD", 5, 0.00001, tick_size=0.0)
        assert spec.distance_ticks(0.005) is None

    def test_con_datos_de_contrato_el_riesgo_se_calcula(self):
        spec = SymbolSpec("EURUSD", 5, 0.00001, tick_size=0.00001, tick_value=1.0)
        assert spec.tick_value_for_volume(1.0) == pytest.approx(1.0)
        assert spec.tick_value_for_volume(2.0) == pytest.approx(2.0)
        assert spec.distance_ticks(0.005) == pytest.approx(500.0)


class TestContratoDelAdaptador:
    """Lo que cualquier adaptador futuro (Databento, ccxt) tiene que cumplir.

    Los tests de `test_mt5_forex.py` repiten estas mismas comprobaciones sobre
    MT5. Están aquí para que añadir un proveedor sea un ejercicio mecánico: se
    implementa `MarketDataAdapter` y esta clase dice si se hizo bien.
    """

    def test_el_adaptador_de_mt5_cumple_el_protocolo(self):
        from adapters.base_adapter import MarketDataAdapter
        from adapters.forex.mt5_forex import MT5ForexAdapter

        adaptador = MT5ForexAdapter.__new__(MT5ForexAdapter)  # sin abrir sesión
        assert isinstance(adaptador, MarketDataAdapter)
        assert adaptador.name == "mt5"

    def test_todo_adaptador_expone_las_cuatro_operaciones(self):
        from adapters.base_adapter import MarketDataAdapter

        for nombre in ("symbols", "ohlc", "trades", "spec"):
            assert hasattr(MarketDataAdapter, nombre), f"falta {nombre}() en el contrato"

    def test_una_vela_normalizada_tiene_las_seis_claves(self):
        """Las seis que `core.market_view.footprint()` y `build_cvd_series()`."""
        vela = normalize_candle((1_700_000_000, 1.1, 1.2, 1.09, 1.15, 100, 10, 100))
        assert set(vela) == {"time", "open", "high", "low", "close", "volume"}

    def test_un_trade_normalizado_tiene_las_cuatro_claves(self):
        """Las cuatro que `OrderFlowEngine.add_trade()` desempacaba."""
        trade = normalize_trade((1_700_000_000, 1.1, 1.1001, 1.1001, 5))
        assert set(trade) == {"ts", "price", "size", "side"}
        assert trade["side"] in ("A", "B")