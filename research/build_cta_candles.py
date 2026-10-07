"""Velas D1 de MT5 demo para la convalidación del CTA Swing (F2, D-071).

De dónde salen los datos
------------------------
D-071 decidió que el CTA se convalida con OHLCV D1 de la demo de MT5
(`copy_rates`), no con Databento: Databento queda solo para microestructura
intradía (M1/M15). Este script baja, por símbolo:

  EURUSD, XAUUSD, US500, GBPUSD, AUDUSD

las últimas `BARS` velas D1 (`copy_rates_from_pos`) y las deja como Parquet +
sidecar sha256 en la raíz de datos externa (`research/cta/d1/`), bajo el mismo
guardián que `build_6e_candles.py`/`fetch_databento.py`.

Qué contiene cada registro
--------------------------
`time` es el epoch en SEGUNDOS de la apertura de la vela (shape de
`base_adapter.Candle`), `day` la fecha ISO del día de trading del broker (para
clasificar el split IS/OOS/embargo sin ambigüedad de zona horaria), más
open/high/low/close/volume. El sidecar guarda `specs` del instrumento
(tick_size/tick_value/digits/point/pip/volumenes) que `backtest_cta.py` necesita
para el vol-targeting y la fricción, y el sha256 del Parquet para que nadie
regrese un dataset editado a mano sin que se note.

Por qué no se toca NADA de ejecución
------------------------------------
Regla de oro de las fases de investigación (D-071): este script solo construye
el artefacto de datos; el backtest y el veredicto vienen en `backtest_cta.py`, y
ninguna de las dos cosas se consume en ejecución.

Uso: `python research/build_cta_candles.py`
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import MetaTrader5 as mt5
import pandas as pd

# Misma guarda que el resto del pipeline: este script se invoca con cwd=raíz y
# necesita importar core.* (ver research/fetch_databento.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import clock, paths  # noqa: E402


SYMBOLS: List[str] = ["EURUSD", "XAUUSD", "US500", "GBPUSD", "AUDUSD"]
BARS: int = 1200  # ~4.7 años de D1: los indicadores del CTA (donchian+ATR) calientan sin comerse la ventana de convalidación.
OUTPUT_DIR: str = os.path.join(paths.RESEARCH_DATA_DIR, "cta", "d1")
SRC_LABEL: str = "MetaQuotes-Demo copy_rates (D1)"


def sha256_of(path: str) -> str:
    dig = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            dig.update(bloque)
    return dig.hexdigest()


def spec_mt5(symbol: str) -> Dict[str, Any]:
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError("MT5 no devuelve symbol_info de {0}".format(symbol))
    digits = int(info.digits)
    point = float(info.point)
    # Misma regla de pip que `adapters.base_adapter.SymbolSpec.pip`: el pip es
    # el point x10 cuando el instrumento cotiza con 3 o más decimales.
    pip = point * 10.0 if digits >= 3 else point
    return {
        "digits": digits,
        "point": point,
        "pip": pip,
        "tick_size": float(info.trade_tick_size),
        "tick_value": float(info.trade_tick_value),
        "volume_min": float(info.volume_min),
        "volume_step": float(info.volume_step),
    }


def velas_d1(symbol: str) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, BARS)
    if rates is None:
        raise RuntimeError(
            "MT5 no devolvió D1 de {0}: {1}".format(symbol, mt5.last_error())
        )
    df = pd.DataFrame(rates)
    # En esta versión del paquete MT5, `time` llega como int64 en SEGUNDOS (no
    # datetime64): interpretarlo por defecto como nanos daría 1970 en todo.
    tiempo = pd.to_datetime(df["time"], unit="s", utc=True)
    df["time"] = tiempo.astype("int64") // 1_000_000_000  # epoch en segundos
    df["day"] = tiempo.dt.strftime("%Y-%m-%d")
    df = df.rename(columns={"tick_volume": "volume"})
    df = df[["time", "day", "open", "high", "low", "close", "volume"]]
    df = df.sort_values("time").reset_index(drop=True)
    return df


def write_sidecar(meta: Dict[str, Any], path: str) -> None:
    meta["sha256"] = sha256_of(path)
    with open(path + ".meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def main() -> None:
    paths.data_root_guard_error(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    inicializado = mt5.initialize()
    if not inicializado:
        raise RuntimeError(
            "no se pudo inicializar MT5: {0}".format(mt5.last_error())
        )
    try:
        disponibles = {s.name for s in mt5.symbols_get()}
        for symbol in SYMBOLS:
            if symbol not in disponibles:
                raise RuntimeError(
                    "{0} no existe en este demo de MT5 (tiene: {1})".format(
                        symbol, ", ".join(sorted(disponibles))
                    )
                )
            df = velas_d1(symbol)
            if len(df) < 200:
                raise RuntimeError(
                    "{0} solo trajo {1} velas; el CTA necesita al menos 200.".format(
                        symbol, len(df)
                    )
                )
            specs = spec_mt5(symbol)
            salida = os.path.join(OUTPUT_DIR, "{0}_D1.parquet".format(symbol))
            df.to_parquet(salida, index=False)
            n = len(df)
            resumen = {
                "dataset": "CTA_D1",
                "symbol": symbol,
                "source": SRC_LABEL,
                "bars": n,
                "first_day": df["day"].iloc[0],
                "last_day": df["day"].iloc[-1],
                "columns": list(df.columns),
                "specs": specs,
                "built_at_utc": clock.now_iso(),
            }
            write_sidecar(resumen, salida)
            print(
                "{0}: {1} velas D1, {2} -> {3}, sha {4}".format(
                    symbol, n, resumen["first_day"], resumen["last_day"],
                    resumen["sha256"][:12],
                )
            )
        print("dataset CTA D1 listo en {0}".format(OUTPUT_DIR))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()