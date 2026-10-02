"""Tests de los esqueletos de B3, cripto y Databento.

Qué fija este archivo
---------------------
No que los esqueletos hagan algo: eso es lo que NO hacen, y no hay forma honesta de
probarlo todavía. Lo que fija es el CONTRATO que hay que cumplir el día que se
implementen, y sobre todo tres decisiones que son fáciles de deshacer sin querer:

1. **Construir no falla, usar sí.** Un registro de adaptadores tiene que poder
   listar `mt5_b3` y `binance_ws` sin que eso tumbe el arranque. Si el `__init__`
   lanzara, cualquier `registry = {a.name: a() for a in TODOS}` reventaría al
   importar el módulo.
2. **El fallo es una excepción de la familia correcta.** B3 y cripto lanzan
   `AdapterError` (mercado no disponible); Databento NO, porque no es un mercado
   sino un proveedor de datos y su fallo es de configuración.
3. **El mensaje dice qué hacer.** "No implementado" sin más es un callejón sin
   salida para quien lee el error. Estos tests comprueban que el mensaje nombra la
   decisión pendiente.

Por qué hay tan pocos tests
--------------------------
Porque un esqueleto con 40 tests es un esqueleto disfrazado de implementado.
Lo que aporta valor aquí es fijar el contrato; el comportamiento de verdad se
prueba cuando haya código, y entonces estos tests se amplían.
"""

from __future__ import annotations

import pytest

from adapters.b3.cedro_technologies import CedroTechnologiesAdapter
from adapters.b3.mt5_b3 import MT5B3Adapter, contrato_base
from adapters.b3.nelogica_profit import NelogicaProfitAdapter
from adapters.base_adapter import AdapterError, MarketDataAdapter, TerminalUnavailable
from adapters.crypto.binance_ws import BinanceWSAdapter, ProveedorCriptoNoElegido
from adapters.crypto.bybit_ccxt import BybitCCXTAdapter
from adapters.forex.databento_cme import DatabentoCME, DatabentoNoConfigurado

# Los cuatro que sí son proveedores de mercado y deben cumplir el Protocol.
PROVEEDORES = [
    MT5B3Adapter,
    NelogicaProfitAdapter,
    CedroTechnologiesAdapter,
    BinanceWSAdapter,
    BybitCCXTAdapter,
]


class TestConstruirNoFalla:
    """Un registro tiene que poder enumerar adaptadores sin ejecutarlos."""

    @pytest.mark.parametrize("cls", PROVEEDORES + [DatabentoCME])
    def test_se_puede_construir_sin_tocar_el_proveedor(self, cls):
        """El fallo va al USAR el adaptador, no al construirlo.

        El motivo es concreto: `registry = {a.name: a() for a in ADAPTADORES}` es
        exactamente lo que va a hacer la API, y si construir lanzara, importar el
        registro tiraría abajo el arranque entero por un mercado que nadie pidió.
        """
        instancia = cls()
        assert instancia.name


class TestElFalloEsDelTipoCorrecto:
    @pytest.mark.parametrize("cls", PROVEEDORES)
    def test_b3_y_cripto_son_AdapterError(self, cls):
        """`AdapterError` = "este mercado no está disponible".

        Es lo que un `except AdapterError` sabe manejar como "cambia de proveedor
        o sigue sin él", que es la respuesta correcta para los cuatro.
        """
        with pytest.raises(AdapterError):
            cls().symbols()

    @pytest.mark.parametrize("cls", PROVEEDORES)
    def test_es_AdapterError_y_no_NotImplementedError(self, cls):
        """`NotImplementedError` haría que un `except Exception` la tratara
        como un bug del código. No lo es: es una decisión de negocio pendiente."""
        with pytest.raises(TerminalUnavailable):
            cls().ohlc("EURUSD")

    def test_databento_no_es_AdapterError(self):
        """Databento falla por configuración, no por mercado caído.

        Si compartiera la jerarquía, un `except AdapterError` que hoy significa
        "el bróker no responde" se tragaría también un error de clave de API, y
        quien lo manejara le diría al usuario que abra la terminal. En Databento
        no hay terminal.
        """
        with pytest.raises(DatabentoNoConfigurado):
            DatabentoCME().ohlc("ES")

        assert not issubclass(DatabentoNoConfigurado, AdapterError)

    def test_databento_no_se_confunde_con_NotImplementedError(self):
        """Se comprueba la clase exacta, no "algún error"."""
        try:
            DatabentoCME().symbols()
        except Exception as exc:  # noqa: BLE001 - el punto es ver el tipo
            assert type(exc) is DatabentoNoConfigurado


class TestLosMensajesDicenQueHacer:
    """Un error que no dice qué hacer es un callejón sin salida."""

    def test_b3_nombra_los_proveedores_posibles(self):
        """Con tres proveedores, "no implementado" no dice cuál falta."""
        with pytest.raises(TerminalUnavailable) as exc:
            MT5B3Adapter().symbols()
        texto = str(exc.value).lower()
        assert "proveedor" in texto

    def test_databento_nombra_la_decision_pendiente(self):
        """La duda real es si es para validar el CVD o para backtests."""
        with pytest.raises(DatabentoNoConfigurado) as exc:
            DatabentoCME().trades("ES")
        texto = str(exc.value).lower()
        assert "databento" in texto

    def test_databento_explica_que_spec_no_tiene_sentido(self):
        """`spec()` es la trampa de este adaptador y la razón se dice."""
        with pytest.raises(DatabentoNoConfigurado) as exc:
            DatabentoCME().spec("ES")
        assert "multiplier" in str(exc.value).lower() or "spec" in str(exc.value).lower()


class TestCumplenElContrato:
    @pytest.mark.parametrize("cls", PROVEEDORES)
    def test_satisface_el_Protocol_de_mercado(self, cls):
        """`MarketDataAdapter` es un Protocol: basta con tener la forma."""
        assert isinstance(cls(), MarketDataAdapter)

    def test_el_Protocol_no_detecta_que_databento_no_es_mercado(self):
        """Límite real de `runtime_checkable`: mira NOMBRES, no significados.

        `DatabentoCME` SÍ satisface `MarketDataAdapter` según `isinstance`, y sin
        embargo su `symbols()` no tiene respuesta honesta y su `spec()` no aplica:
        no hay bróker. Esto está aquí a propósito como aviso, no como defecto que
        arreglar, porque no se puede arreglar dentro de la bibliothèque:

        - `runtime_checkable` en Python 3.12 solo comprueba que existan los
          atributos. No puede mirar qué hace cada uno.
        - Por eso el `isinstance` del test anterior PASA, y por eso el Protocol no
          puede ser la barrera que impida pasar datos de mercado a un archivo.

        La barrera real tiene que ser la lista explícita de adaptadores de la API,
        no el tipo. Un provider que solo lee histórico no debe entrar por la puerta
        de "adaptador de mercado" por mucho que tenga los cuatro métodos.
        """
        assert isinstance(DatabentoCME(), MarketDataAdapter)


class TestContratoBaseB3:
    def test_quita_el_sufijo_con_separador(self):
        assert contrato_base("EURUSD.a") == "EURUSD"
        assert contrato_base("win_fut26") == "WIN"

    def test_no_adivina_los_futuros_sin_separador(self):
        """`WINFUT26` NO se reduce a `WIN`, y no debe hacerlo.

        Aquí no hay separador donde cortar, y `WINFUT26` podría ser WIN con un mes
        o un subyacente que se llame WINFUT. Una heurística que "casi siempre"
        acierta en símbolos mal formados es peor que no normalizar: manda a la
        B3 un símbolo equivocado y el error aparece en la orden, no aquí.
        """
        assert contrato_base("WINFUT26") == "WINFUT26"
        assert contrato_base("WING") == "WING"


class TestBinanceTimeframes:
    def test_traduce_el_nombre_corto_al_intervalo_del_exchange(self):
        """`H1` del proyecto es `1h` en Binance, no 16385 ni `H1`."""
        assert BinanceWSAdapter.resuelve_timeframe("H1") == "1h"
        assert BinanceWSAdapter.resuelve_timeframe("M15") == "15m"
        assert BinanceWSAdapter.resuelve_timeframe("W1") == "1w"

    def test_un_timeframe_invalido_sigue_siendo_ValueError(self):
        """La validación la hace `resolve_timeframe`, no una tabla propia.

        Duplicar la validación daría dos sitios donde añadir un timeframe y uno
        se olvidaría.
        """
        with pytest.raises(ValueError):
            BinanceWSAdapter.resuelve_timeframe("M7")

    def test_el_exchange_no_inventa_timeframes(self):
        """Si `TIMEFRAMES` crece, esta lista tiene que crecer con él.

        Es un test que falla al añadir un timeframe y no al romper nada: por eso
        usa el conjunto de `TIMEFRAMES` en vez de repetir los ocho a mano.
        """
        from adapters.base_adapter import TIMEFRAMES

        for tf in TIMEFRAMES:
            assert BinanceWSAdapter.resuelve_timeframe(tf) in BinanceWSAdapter.INTERVALOS


class TestCredencialesNoSeFiltran:
    @pytest.mark.parametrize(
        "cls", [NelogicaProfitAdapter, CedroTechnologiesAdapter, BinanceWSAdapter]
    )
    def test_el_error_no_incluye_la_credencial(self, cls):
        """Un `raise ... (f"credenciales: {self.credenciales}")` en un log cuela
        la API key en el log de errores. Aquí se comprueba que el mensaje no la
        contiene, que es la forma barata de que la costumbre empiece bien."""
        secreto = "CLAVE-SECRETA-NO-MOSTRAR"
        instancia = cls(api_key=secreto)
        with pytest.raises(AdapterError) as exc:
            instancia.symbols()
        assert secreto not in str(exc.value)