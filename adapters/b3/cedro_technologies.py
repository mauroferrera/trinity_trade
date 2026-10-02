"""B3 vía Cedro Technologies (API REST de deeds/cotizaciones).

Estado: ESQUELETO. Ver `adapters/b3/mt5_b3.py` para por qué los tres proveedores
de B3 están sin implementar.

A diferencia de Nelogica (WebSocket de eventos) y MT5 (terminal local), Cedro es
puro HTTP request/response. Para este proyecto eso la vuelve la opción más fácil
de testear, porque un mock de `requests` es determinista y un mock de socket no.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec
from adapters.b3.mt5_b3 import ProveedorB3NoElegido


class CedroTechnologiesAdapter:
    """B3 por la API REST de Cedro Technologies."""

    name = "cedro_technologies"

    def __init__(self, **credenciales: Any) -> None:
        self.credenciales = credenciales

    def symbols(self) -> List[str]:
        raise ProveedorB3NoElegido(
            "Cedro Technologies requiere credenciales de API; ver "
            "adapters/b3/cedro_technologies.py."
        )

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("Cedro Technologies sin implementar.")

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("Cedro Technologies sin implementar.")

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise ProveedorB3NoElegido("Cedro Technologies sin implementar.")


__all__ = ["CedroTechnologiesAdapter"]