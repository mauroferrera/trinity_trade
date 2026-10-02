"""Databento para futuros de CME (ES, NQ, CL): histórico y microestructura.

Estado: ESQUELETO, y el más dudoso de los cinco. Ver más abajo.

Por qué no es "otro exchange"
----------------------------
Databento no es un bróker: es un proveedor de DATOS HISTÓRICOS de nivel de
cinta, para futuros de CME. Eso tiene tres consecuencias que lo separan del resto
de adaptadores:

1. **No hay sesión que cerrar ni órdenes.** Solo histórico. Por eso este esqueleto
   NO implementa el contrato entero: `symbols()` y `spec()` no tienen una respuesta
   honesta para un archivo que ya se descargó, y `trades()` es una lectura de
   archivo, no una conexión.

   Aun así, `isinstance(DatabentoCME(), MarketDataAdapter)` devuelve True, porque
   un `Protocol` con `runtime_checkable` solo mira que existan los métodos. Es un
   límite conocido de la biblioteca, no un descuido: no se puede arreglar desde
   aquí, así que la barrera real tiene que ser la lista explícita de adaptadores
   de la API y no el tipo. Está documentado en `tests/unit/test_adapter_skeletons.py`.
2. **El universo es pequeño y fijo.** ES, NQ, CL, GC... No hay "símbolo
   desconocido": o está en el dataset o no existe. El problema real es el
   CONTRATO (el mes), que es parte de la identidad: ESU6 y ESZ6 son el mismo
   subyacente y contratos distintos, y mezclarlos produce un histórico continuo
   que en la realidad tiene un salto de months entre expiraciones.
3. **Aporta algo que los otros no: MBO y Depth-of-book reales.** El CVD que
   `core.orderflow_engine.build_cvd_series()` calcula es SINTÉTICO en Forex y en
   cripto (se infiere de ticks sin libro de órdenes). Con Databento se puede
   medir. Eso lo hace el candidato más interesante para validar el motor, no para
   operar.

Lo que hay que decidir antes de escribir código
-----------------------------------------------
- ¿Para qué se quiere: validar el CVD del motor, o alimentar backtests de ES/NQ?
  La respuesta cambia si hace falta streaming (`databento::live`) o solo
  histórico en archivo, que es un orden de magnitud más barato.
- ¿Coste? Los datasets MBO se cobran por GB. Es la decisión con implicaciones
  económicas reales, y por eso este esqueleto no descarga nada por su cuenta.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec


class DatabentoNoConfigurado(RuntimeError):
    """Falta lo que Databento necesita para devolver algo: dataset o clave.

    NO hereda de `AdapterError` a propósito, y la razón es concreta: los otros
    esqueletos fallan porque el proveedor no está ELEGIDO, y este falla porque
    este adaptador no es un proveedor de mercado. Se distinguen para que un
    `except AdapterError` que hoy significa "bróker caído" no se lleve por
    delante un error de configuración.
    """


class DatabentoCME:
    """Histórico de futuros CME vía Databento. Solo lectura de datos."""

    name = "databento_cme"

    def __init__(self, apikey: str = "", **opciones: Any) -> None:
        self.apikey = apikey
        self.opciones = opciones

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        """Barras OHLC ya normalizadas a la forma de `core/`.

        Es el único método con sentido aquí. Devuelve SEGUNDOS epoch porque es lo
        que espera el núcleo; Databento publica nanosegundos, y esa conversión es
        la razón de que la normalización viva en el adaptador.
        """
        raise DatabentoNoConfigurado(
            "Databento no está configurado: falta decidir si se usa para validar "
            "el CVD o para backtests, y el dataset que corresponde. "
            "Ver adapters/forex/databento_cme.py."
        )

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        """Trades de cinta con LADO AGRESOR REAL, no inferido.

        Aquí el `side` de `normalize_trade()` viene del dato (Databento marca
        agresor en MBO/MTP), no de comparar el precio contra bid/ask como hace el
        camino de MT5. Es la diferencia que justifica mirar este proveedor.
        """
        raise DatabentoNoConfigurado("Databento no está configurado.")

    def symbols(self) -> List[str]:
        """Sin sentido para un archivo ya descargado: no se implementa."""
        raise DatabentoNoConfigurado(
            "Databento no publica un universo de símbolos consultable como MT5; "
            "el universo es el del dataset que se descargó."
        )

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        """Sin specs de bróker aquí: CME publica multipliers, no lotes de bróker."""
        raise DatabentoNoConfigurado(
            "Databento publica el multiplier del contrato (el valor del punto), "
            "no un spec de bróker. Traducirlo a SymbolSpec requiere decidir qué "
            "hace `tick_value` en un mercado sin bróker."
        )


__all__ = ["DatabentoCME", "DatabentoNoConfigurado"]