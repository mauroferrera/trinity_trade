"""B3 vía terminal MT5 (la vía barata de las tres).

Estado: ESQUELETO. Falta decidir a qué proveedor de B3 se paga, y eso es una
decisión de negocio, no de código. Ver `.agent/PROJECT_STATE.json`.

Por qué este esqueleto casi no tiene código
--------------------------------------------
La API del terminal MT5 es la misma para Forex y para B3. Todo el trabajo de
`adapters/forex/mt5_forex.py` (sesión única, normalización de velas y ticks,
lectura de specs) se reutiliza tal cual. Lo único específico de B3 es:

1. **Traducir símbolos.** El bróker publica el contrato con sufijo (`WINFUT26`,
   `PETR4`, `AAPL34`) y la sesión de MT5 puede devolver los de Forex y los de B3
   mezclados. `symbols()` filtra a los de B3.
2. **No mandar el símbolo normalizado.** `base_adapter.normalize_symbol()` quita
   sufijos para COMPARAR; para ENVIAR hay que mandar el nombre exacto que
   publica el bróker. Es la misma regla que en Forex y por eso no se reimplementa.
   Ojo: esa normalización solo corta en `.`, `_`, `-` y `#`, así que los futuros
   de B3 sin separador (`WINFUT26`) NO se normalizan. Ver `contrato_base()`.

Lo que NO se hace aquí, a propósito
-----------------------------------
- No se copia aquí la lista de contratos de B3. Ya existe y es la fuente de
  verdad: `config/trading_hours.json` -> `markets.B3_FUTURES.instrument_groups`
  (WIN, IND, MBR en mini_index). Una segunda lista en este archivo se
  desincronizaría de la primera sin que nada avise.
- No se calculan specs. El bróker los publica; si no, `spec()` devuelve `None`.
- No se programan las sesiones de B3 con las de Forex: la bolsa de São Paulo tiene
  un horario y un calendario de festivos propios. `config/trading_hours.json` es
  quien tiene esas ventanas reales; este archivo no las reescribe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from adapters.base_adapter import SymbolSpec, TerminalUnavailable


#: Sufijos con separador, que es lo único que `normalize_symbol()` sabe quitar.
#: Los contratos futuros de B3 no llevan separador (`WINFUT26`) y quedan fuera:
#: ver `contrato_base()`.
SUFIJOS_CONTRATO = (".", "_", "-")


class ProveedorB3NoElegido(TerminalUnavailable):
    """B3 no está disponible porque aún no se eligió proveedor.

    Hereda de `TerminalUnavailable` y no de `NotImplementedError` porque para
    quien llama NO es un bug: es un mercado no disponible, y debería poder
    tratarlo igual que "el bróker no tiene la terminal abierta".
    """


#: Sufijos con los que un bróker puede escribir un contrato futuro. Se usan para
#: RECONOCER el patrón, no para construir un símbolo: nunca se envía uno normalizado.
SUFIJOS_CONTRATO = (".", "_", "-")


class MT5B3Adapter:
    """B3 a través de un MT5 ya instalado con la corretora brasileira.

    Cuando se implemente, NO hereda de `MT5ForexAdapter` sino que lo COMPONE:
    la API del terminal es la misma y la lógica de lectura es la de Forex, así que
    duplicarla aquí es la forma segura de que las dos se desincronicen. Lo único
    que B3 aporta es el filtro de `symbols()` y el nombre del contrato.
    """

    name = "mt5_b3"

    def __init__(self, session: Any = None) -> None:
        # Construir NO falla: un registro de adaptadores tiene que poder listar
        # "mt5_b3" sin que eso tumbe el arranque. El fallo llega al USARLO, que
        # es cuando de verdad importa y donde el mensaje es accionable.
        self.session = session

    # Se declara la forma aunque no haya cuerpo: quien llame a `symbols()` antes
    # de elegir proveedor recibe el error de abajo, no un AttributeError.

    def symbols(self) -> List[str]:
        raise ProveedorB3NoElegido("B3 sin proveedor elegido.")

    def ohlc(self, symbol: str, timeframe: str = "M15", bars: int = 300) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("B3 sin proveedor elegido.")

    def trades(self, symbol: str, since_ts: int = 0) -> List[Dict[str, Any]]:
        raise ProveedorB3NoElegido("B3 sin proveedor elegido.")

    def spec(self, symbol: str) -> Optional[SymbolSpec]:
        raise ProveedorB3NoElegido("B3 sin proveedor elegido.")


def contrato_base(symbol: str) -> str:
    """Símbolo del bróker -> forma canónica, para COMPARAR.

    Delega en `normalize_symbol()` y por eso SOLO resuelve los sufijos que llevan
    separador: `EURUSD.a` -> `EURUSD`, `WIN_FUT26` -> `WIN`.

    Lo que NO resuelve, y conviene no prometer: los contratos futuros de B3 sin
    separador (`WINFUT26`, `WINQ26`, `INDZ26`) se devuelven tal cual. No hay
    separador donde cortar y adivinar dónde acaba el nombre del subyacente
    (`WIN` vs `WING` vs `WINZ`) es un parser de exchange, no una normalización.
    Si de verdad hace falta, la respuesta correcta es un mapa declarado por
    símbolo en la config, no una heurística.

    Enviar el resultado a la B3 NO funciona en ningún caso: para mandar una orden
    hay que usar el símbolo exacto que publica el bróker.
    """
    from adapters.base_adapter import normalize_symbol

    return normalize_symbol(symbol)


__all__ = [
    "MT5B3Adapter",
    "ProveedorB3NoElegido",
    "SUFIJOS_CONTRATO",
    "contrato_base",
]