"""Doble de prueba del paquete `MetaTrader5`, sin terminal.

Por qué un doble y no un parche de `sys.modules`
---------------------------------------------
El paquete `MetaTrader5` es un import nativo: en una máquina sin terminal
instalada, `import MetaTrader5` falla en la propia línea del import, y ni
`monkeypatch.setattr` ni `unittest.mock.patch` llegan a tiempo. Poner un módulo
falso en `sys.modules` ANTES del import sí funciona, y es la única forma de
probar la lógica del adaptador en CI o en un portátil donde no hay bróker.

Este doble no pretende parecerse a MT5. Pretende ser lo que los tests
necesitan comprobar:

  - Que el adaptador llama a la función correcta con los argumentos correctos.
  - Que normaliza bien lo que MT5 devuelve (que es un namedtuple POSICIONAL).
  - Que un fallo del bróker sale como `AdapterError` y no como una excepción
    cruda de la librería.

La última es la que más importa. Si el adaptador deja que un
`copy_rates_from_pos` devuelva `None` sin comprobarlo, el llamante recibe un
`None` donde esperaba velas, y el error aparece tres capas más arriba como un
`TypeError: 'NoneType' object is not subscriptable`.

Que el doble NO levante excepciones al detectar concurrencia es deliberado: si
`shutdown()` fallara, el test probaría que el doble funciona y no que el
adaptador serializa el acceso. Se registra el hecho (`shutdown_while_busy`) y
es el test quien afirma que sigue a False.

Uso
---
    mt5 = FakeMT5.install(monkeypatch)      # devuelve el doble, ya en sys.modules
    mt5.set_rates([...])                     # qué devuelve copy_rates_from_pos
    ...
    monkeypatch.undo()                       # implícito al final del test
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List, Optional, Sequence

# Constantes reales de MT5. Los valores importan: `TIMEFRAME_H1` en MT5 NO es
# 60 sino 16385 (los timeframes de horas empiezan en el bit 15). Un doble que
# usara 60 "para que se entienda" dejaría pasar el bug de pasar 60 donde el
# bróker espera 16385, que es justo lo que este test existe para cazar.
TIMEFRAME_M1 = 1
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H4 = 16388
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
COPY_TICKS_ALL = 0xFFFF


class FakeTick:
    """Tick de MT5: los campos son atributos, no índices."""

    def __init__(
        self,
        time: int,
        bid: float,
        ask: float,
        last: float = 0.0,
        volume: int = 1,
        flags: int = 0,
    ) -> None:
        self.time = int(time)
        self.bid = float(bid)
        self.ask = float(ask)
        self.last = float(last)
        self.volume = int(volume)
        self.time_msc = int(time) * 1000
        self.flags = int(flags)
        self.volume_real = float(volume)


class FakeSymbolInfo:
    """`symbol_info()`: los specs de cotización, sin los de contrato."""

    def __init__(
        self,
        name: str,
        digits: int = 5,
        point: float = 0.00001,
        visible: bool = True,
        **extra: Any,
    ) -> None:
        self.name = name
        self.digits = int(digits)
        self.point = float(point)
        self.visible = bool(visible)
        for k, v in extra.items():
            setattr(self, k, v)


class FakeSymbolInfoTrade:
    """`symbol_info_trade()`: los campos `SYMBOL_TRADE_*` de los paquetes nuevos."""

    def __init__(
        self,
        symbol: str,
        trade_contract_size: float = 100000.0,
        trade_tick_size: float = 0.00001,
        trade_tick_value: float = 1.0,
        volume_min: float = 0.01,
        volume_max: float = 100.0,
        volume_step: float = 0.01,
    ) -> None:
        self.symbol = symbol
        self.trade_contract_size = float(trade_contract_size)
        self.trade_tick_size = float(trade_tick_size)
        self.trade_tick_value = float(trade_tick_value)
        self.volume_min = float(volume_min)
        self.volume_max = float(volume_max)
        self.volume_step = float(volume_step)


class FakeMT5(types.ModuleType):
    """Módulo MT5 falso. Se instancia y se registra en `sys.modules`."""

    def __init__(self) -> None:
        super().__init__("MetaTrader5")
        self.TIMEFRAME_M1 = TIMEFRAME_M1
        self.TIMEFRAME_M5 = TIMEFRAME_M5
        self.TIMEFRAME_M15 = TIMEFRAME_M15
        self.TIMEFRAME_M30 = TIMEFRAME_M30
        self.TIMEFRAME_H1 = TIMEFRAME_H1
        self.TIMEFRAME_H4 = TIMEFRAME_H4
        self.TIMEFRAME_D1 = TIMEFRAME_D1
        self.TIMEFRAME_W1 = TIMEFRAME_W1
        self.COPY_TICKS_ALL = COPY_TICKS_ALL

        # Estado: lo que el test configura.
        self.initialize_ok = True
        self.last_error_value = 0
        self.symbols_list: List[Any] = []
        self.info_by_symbol: Dict[str, Any] = {}
        self.trade_by_symbol: Dict[str, Any] = {}
        self.rates_by_key: Dict[Any, List[Any]] = {}
        self.ticks_by_symbol: Dict[str, List[Any]] = {}
        self.tick_by_symbol: Dict[str, Any] = {}
        self.terminal_common_path = "/fake/common"

        # Registro de llamadas: para afirmar QUÉ se pidió y con qué args.
        self.calls: List[tuple] = []
        self.initialized = False
        self.shutdown_count = 0
        # Concurrencia: los dos counters que el bug del lock único corrompía.
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self.shutdown_while_busy = False

    # -- instalación -----------------------------------------------------------

    @classmethod
    def install(cls, monkeypatch, **kwargs: Any) -> "FakeMT5":
        """Crea el doble y lo pone en `sys.modules`. Devuélvelo para configurarlo."""
        fake = cls()
        for k, v in kwargs.items():
            setattr(fake, k, v)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        return fake

    # -- ciclo de vida ---------------------------------------------------------

    def initialize(self, path: Optional[str] = None) -> bool:
        self.calls.append(("initialize", path))
        self.initialized = bool(self.initialize_ok)
        return self.initialized

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self.shutdown_count += 1
        # El bug que motivó la sesión única: un shutdown con una llamada en
        # vuelo. El doble NO lanza; lo registra, para que el test pueda
        # afirmar que ya no ocurre.
        if self.concurrent_calls > 0:
            self.shutdown_while_busy = True
        self.initialized = False

    def last_error(self) -> int:
        return int(self.last_error_value)

    # -- información -----------------------------------------------------------

    def terminal_info(self):
        self.calls.append(("terminal_info",))
        if not self.initialized:
            return None
        return types.SimpleNamespace(common_data_path=self.terminal_common_path)

    def account_info(self):
        self.calls.append(("account_info",))
        if not self.initialized:
            return None
        return types.SimpleNamespace(login=1234, balance=10000.0, equity=10000.0)

    def symbols_get(self, group: Optional[str] = None):
        self.calls.append(("symbols_get", group))
        return tuple(self.symbols_list)

    def symbol_select(self, symbol: str, visible: bool = True) -> bool:
        self.calls.append(("symbol_select", symbol, visible))
        return symbol in self.info_by_symbol or symbol in self.tick_by_symbol

    def symbol_info(self, symbol: str):
        self.calls.append(("symbol_info", symbol))
        return self.info_by_symbol.get(symbol)

    def symbol_info_trade(self, symbol: str):
        self.calls.append(("symbol_info_trade", symbol))
        return self.trade_by_symbol.get(symbol)

    def symbol_info_tick(self, symbol: str):
        self.calls.append(("symbol_info_tick", symbol))
        return self.tick_by_symbol.get(symbol)

    # -- precios ---------------------------------------------------------------

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int):
        self.calls.append(("copy_rates_from_pos", symbol, timeframe, start, count))
        return self.rates_by_key.get((symbol, timeframe))

    def copy_rates_range(self, symbol: str, frm: int, to: int, timeframe: int):
        self.calls.append(("copy_rates_range", symbol, frm, to, timeframe))
        return self.rates_by_key.get((symbol, timeframe))

    def copy_ticks_range(self, symbol: str, frm: int, to: int, flags: int = COPY_TICKS_ALL):
        self.calls.append(("copy_ticks_range", symbol, frm, to, flags))
        return [_as_tick_row(t) for t in self.ticks_by_symbol.get(symbol, [])]

    def copy_ticks_from(self, symbol: str, date_from: float, count: int, flags: int = COPY_TICKS_ALL):
        self.calls.append(("copy_ticks_from", symbol, date_from, count, flags))
        return [_as_tick_row(t) for t in self.ticks_by_symbol.get(symbol, [])]

    # -- configuración desde el test -------------------------------------------

    def set_rates(self, symbol: str, timeframe: int, rows: Sequence[Sequence[Any]]) -> None:
        """Qué debe devolver `copy_rates_from_pos` para (symbol, timeframe)."""
        self.rates_by_key[(symbol, timeframe)] = list(rows)

    def set_ticks(self, symbol: str, ticks: Sequence[FakeTick]) -> None:
        self.ticks_by_symbol[symbol] = list(ticks)

    def calls_named(self, name: str) -> List[tuple]:
        """Llamadas registradas con ese nombre."""
        return [c for c in self.calls if c and c[0] == name]

    def called(self, name: str) -> bool:
        return bool(self.calls_named(name))


def _as_tick_row(tick: Any) -> tuple:
    """`FakeTick` -> tupla posicional, como la que devuelve MT5 de verdad.

    MT5 devuelve namedtuples POSICIONALES, no objetos con atributos. El doble
    se configura con `FakeTick` porque se lee mucho mejor (`FakeTick(t, bid,
    ask)`), pero lo que sale de `copy_ticks_range` debe ser una tupla de 8
    campos como la real: si el doble devolviera objetos con atributos,
    `normalize_trade()` acabaría usando la rama de dict por `__getitem__` y el
    test no estaría probando la ruta que se usa en producción.
    """
    return (
        int(getattr(tick, "time", 0)),
        float(getattr(tick, "bid", 0.0)),
        float(getattr(tick, "ask", 0.0)),
        float(getattr(tick, "last", 0.0)),
        int(getattr(tick, "volume", 0)),
        int(getattr(tick, "time_msc", 0)),
        int(getattr(tick, "flags", 0)),
        float(getattr(tick, "volume_real", 0.0)),
    )


def rates_fixture(
    count: int = 5, start_ts: int = 1_700_000_000, base: float = 1.1000, step: float = 0.0001
) -> List[tuple]:
    """Filas de `copy_rates_from_pos` con la forma POSICIONAL de MT5.

    MT5 devuelve namedtuples de 8 campos, no de 6: los dos últimos son `spread`
    y `real_volume`. Se devuelven tuplas de 8 para que el test se parezca a lo que
    el bróker da de verdad, y para que un adaptador que leyera más allá del
    volumen fallara aquí en vez de en producción.
    """
    out = []
    for i in range(count):
        price = base + i * step
        out.append(
            (
                start_ts + i * 900,
                price,
                price + step,
                price - step,
                price + step / 2,
                100 + i,  # tick_volume
                10,  # spread
                100 + i,  # real_volume
            )
        )
    return out