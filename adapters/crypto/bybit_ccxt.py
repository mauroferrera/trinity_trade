"""Bybit vía CCXT.

Estado: ESQUELETO. Ver `adapters/b3/mt5_b3.py`.

Por qué CCXT y no un cliente propio
-----------------------------------
CCXT normaliza ya exchanges que de otro modo serían N clientes distintos. El
coste es una dependencia pesada y una abstracción que a veces se nota en los
tiempos en milisegundos, que es exactamente donde vive la cinta. La razón para
usarla es que su API REST es estable y su WebSocket tiene el mismo modelo que el
de Binance, así que el resto del sistema no distingue entre ambos.

Lo que hay que tener presente al implementarlo
---------------------------------------------
CCXT devuelve las fechas en MILISEGUNDOS epoch, no en segundos como MT5. Es la
conversión que `base_adapter.py` normaliza a segundos, y es la razón más clara
por la que ese módulo existe: si cada adaptor normalizara por su cuenta,
un día uno de ellos se olvidaría y el CVD saldría multiplicado por 1000.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec
from adapters.crypto.binance_ws import ProveedorCriptoNoElegido


class BybitCCXTAdapter:
    """Bybit con la API unificada de CCXT."""

    name = "bybit_ccxt"

    def __init__(self, exchange_id: str = "bybit", **credenciales: Any) -> None:
        self.exchange_id = exchange_id
        self.credenciales = credenciales

    def symbols(self) -> List[str]:
        raise ProveedorCriptoNoElegido("Bybit vía CCXT sin implementar.")

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        raise ProveedorCriptoNoElegido("Bybit vía CCXT sin implementar.")

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        raise ProveedorCriptoNoElegido("Bybit vía CCXT sin implementar.")

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise ProveedorCriptoNoElegido("Bybit vía CCXT sin implementar.")


__all__ = ["BybitCCXTAdapter"]