"""Binance por WebSocket (klines + trades).

Estado: ESQUELETO. Ver `adapters/b3/mt5_b3.py`: los esqueletos de multi-mercado
existen para fijar el contrato antes de elegir proveedor, no para fingir que ya
funcionan.

Dos diferencias con Forex que el esqueleto deja claras
-----------------------------------------------------
1. **El lado agresor viene dado.** Binance marca cada trade en la cinta como
   aggressor buyer o seller. En MT5 hay que inferirlo del precio contra el bid/ask
   (`base_adapter.normalize_trade`). Aquí el dato es explícito y la inferencia es
   un bug si se aplica.
2. **El volumen sí es tamaño.** En MT5, `volume` de una vela es `tick_volume`
   (número de cambios de cotización), no dinero. En Binance es el tamaño real
   negociado. Mezclarlos en el mismo campo obliga a que `core/` sepa de qué
   proveedor viene cada vela, que es justo lo que `base_adapter.py` evita.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec, TerminalUnavailable


class ProveedorCriptoNoElegido(TerminalUnavailable):
    """El proveedor de cripto no está disponible todavía.

    `TerminalUnavailable` y no `ImportError`: la librería puede estar instalada y
    aun así faltar la clave de API. Quien llama lo trata como "este mercado no
    está disponible", que es la respuesta honesta.
    """


class BinanceWSAdapter:
    """Binance por WebSocket: `klines` para velas, `aggTrade` para cinta."""

    name = "binance_ws"

    #: Los intervalos de Binance son segundos con sufijo (`15m`), no los enteros
    #: con prefijo que usa MT5 (`M15`). Se traducen en `resuelve_timeframe`.
    INTERVALOS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")

    def __init__(self, **credenciales: Any) -> None:
        self.credenciales = credenciales

    @classmethod
    def resuelve_timeframe(cls, timeframe: str) -> str:
        """Nombre corto del proyecto ("H1") -> intervalo de Binance ("1h").

        Vive en el adaptador y no en `core/` porque el nombre del intervalo es
        vocabulario del exchange. Reutiliza `TIMEFRAMES` para que un timeframe
        desconocido dé el mismo error que en MT5, y no uno nuevo.
        """
        from adapters.base_adapter import resolve_timeframe

        resolve_timeframe(timeframe)  # valida y lanza ValueError si no existe
        return {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
                "H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w"}[timeframe.upper()]

    def symbols(self) -> List[str]:
        raise ProveedorCriptoNoElegido("Binance WebSocket sin implementar.")

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        raise ProveedorCriptoNoElegido("Binance WebSocket sin implementar.")

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        raise ProveedorCriptoNoElegido("Binance WebSocket sin implementar.")

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        """Los `filters` del exchange (`exchangeInfo`) dan lot step y mínimos.

        Es el equivalente más cercano de `SYMBOL_TRADE_*` en MT5, pero NO es lo
        mismo: los limits de Binance son de cantidad de cripto y los de MT5 son de
        lotes de contrato. Por eso se devuelve `None` en vez de traducir un
        `tick_value` que no existe aquí.
        """
        raise ProveedorCriptoNoElegido("Binance WebSocket sin implementar.")


__all__ = ["BinanceWSAdapter", "ProveedorCriptoNoElegido"]