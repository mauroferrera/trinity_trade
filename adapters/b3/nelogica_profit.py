"""B3 vía Nelogica Profit Open API.

Estado: ESQUELETO. Ver `adapters/b3/mt5_b3.py` para por qué los tres proveedores
de B3 están sin implementar.

La diferencia con el esqueleto de MT5 está en el TRANSPORTE, no en los datos:
Profit entrega eventos pushed por socket con su propio protocolo binario, así que
este adaptador necesitará un cliente WebSocket y un parser de mensajes propios.
Lo que sí se reutiliza del camino de MT5 es la normalización a la forma de
`core/`. Esa es la razón de que `base_adapter.py` exista: el parsing es lo único
que cambia por proveedor, la normalización no.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec
from adapters.b3.mt5_b3 import ProveedorB3NoElegido


class NelogicaProfitAdapter:
    """B3 por la Open API de Nelogica Profit (REST + WebSocket de eventos)."""

    name = "nelogica_profit"

    def __init__(self, **credenciales: Any) -> None:
        self.credenciales = credenciales

    def symbols(self) -> List[str]:
        raise ProveedorB3NoElegido(
            "Nelogica Profit requiere suscripción y credenciales; ver "
            "adapters/b3/nelogica_profit.py."
        )

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("Nelogica Profit sin implementar.")

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("Nelogica Profit sin implementar.")

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise ProveedorB3NoElegido("Nelogica Profit sin implementar.")


__all__ = ["NelogicaProfitAdapter"]