"""Construye velas M15 de 6E desde los parquets de cinta real de Databento.

Pipeline del backtest de convalidación (DECISIONS.md D-068): los dos contratos
descargados (6EU6 julio-agosto, 6EZ6 septiembre) se unen en UNA serie continua de
velas M15 con PDH/PDL por día UTC. La cinta se deja en `trinity_data` (fuera de
OneDrive); este script solo la AGRUPA.

Reglas de unión del roll (medidas el 2026-10-06):
  - Punto de roll  -> 2026-09-11T00:00:00Z. Antes se usa 6EU6, desde ahí 6EZ6.
  - La cola de 6EU6 posterior al roll y la cabeza de 6EZ6 anterior (si existen)
    se DESCARTAN: son el contrato viejo en ventana de transición, y mezclarlos en
    la misma vela no es un precio limpio.

Advertencias documentadas en el sidecar `meta`:
  - 2026-08-29 calidad reducida (degraded) según Databento.
  - La ventana de restricción del roll (setup no iniciado): 2026-09-10T00:00Z a
    2026-09-12T00:00Z. Aquí la vela se genera igual; quien juega setups la ignora.

Uso::

    python research/build_6e_candles.py

Salida (PATRÓN de build_real_fixture.py):
    C:\\Users\\fmaur\\Desktop\\trinity_data\\databento\\candles\\
        6E_M15_2026-07-08_2026-10-06.parquet   (time/open/high/low/close/volume/delta/pdh/pdl)
        ... .meta.json                          (procedencia + sha256 + conteos)
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# `python research/build_6e_candles.py` pone `research/` en sys.path[0]; sin
# este insert, `import core.paths` falla siempre (mismo problema que el resto
# del pipeline: ver research/fetch_databento.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.paths import DATABENTO_RAW_DIR, data_root_guard_error, ensure_dir  # noqa: E402

# --- Puntos de corte y ventanas (medidos el 2026-10-06, ver DECISIONS.md D-069) ---

ROLL_TS = datetime(2026, 9, 11, 0, 0, 0, tzinfo=timezone.utc)
# Ventana en la que el backtest NO inicia setups (48h alrededor del roll).
RESTRICT_START = datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc)
RESTRICT_END = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)
# Día degradado según Databento (calidad reducida). Solo se documenta.
DEGRADED_DAY = "2026-08-29"

SYMBOL = "6E"
SCHEMA = "trades"
CANDLES_BAR = "15min"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def find_source(prefix: str) -> Path:
    matches = sorted(Path(DATABENTO_RAW_DIR).glob(f"{prefix}*.parquet"))
    if not matches:
        raise SystemExit(f"no hay cinta {prefix!r} en {DATABENTO_RAW_DIR}")
    for p in matches:
        sidecar = Path(str(p) + ".meta.json")
        if not sidecar.exists():
            continue
        rows = 0
        try:
            rows = int(json.loads(sidecar.read_text(encoding="utf-8")).get("rows") or 0)
        except (json.JSONDecodeError, TypeError):
            pass
        if rows > 0:
            return p
    return max(matches, key=lambda p: p.stat().st_size)


def load_trades(parquet: Path, src: str) -> pd.DataFrame:
    df = pd.read_parquet(parquet)
    df = df.reset_index(drop=False)  # suelta el índice ts_recv (timestamp de recibo)
    if "ts_recv" in df.columns:
        df = df.drop(columns=["ts_recv"])
    df = df[["ts_event", "symbol", "action", "side", "price", "size"]].copy()
    df["_src"] = src
    return df


def attach_pd_pdl(bars: pd.DataFrame) -> pd.DataFrame:
    """PDH/PDL = extremos del día UTC ANTERIOR CON DATOS a cada vela.

    No es "día calendario anterior": el fin de semana no tiene cinta, así que el
    lunes tomaría el sábado. `shift(1)` sobre los días que realmente existen da
    el último día negociado, que es el que ve el runtime con sus sesiones.
    """
    bars = bars.copy()
    bars["_day"] = bars["time"].dt.normalize()
    daily = bars.groupby("_day").agg(_hi=("high", "max"), _lo=("low", "min"))
    daily = daily.sort_index()
    daily["_prev"] = daily.index.to_series().shift(1)
    out = bars.merge(daily, left_on="_day", right_on="_prev", how="left")
    out["pdh"] = out["_hi"]
    out["pdl"] = out["_lo"]
    return out.drop(columns=["_day", "_hi", "_lo", "_prev"])


def main() -> None:
    eu6_p = find_source("GLBX_6EU6_trades_2026-07-08_")
    ez6_p = find_source("GLBX_6EZ6_trades_2026-09-11_")
    print(f"6EU6: {eu6_p.name} ({eu6_p.stat().st_size / 1024:.1f} KiB)")
    print(f"6EZ6: {ez6_p.name} ({ez6_p.stat().st_size / 1024:.1f} KiB)")

    eu6 = load_trades(eu6_p, "6EU6")
    ez6 = load_trades(ez6_p, "6EZ6")

    eu6_tail = int((eu6["ts_event"] >= ROLL_TS).sum())
    ez6_head = int((ez6["ts_event"] < ROLL_TS).sum())

    frames = [eu6[eu6["ts_event"] < ROLL_TS], ez6[ez6["ts_event"] >= ROLL_TS]]
    tape = pd.concat(frames, ignore_index=True)

    mask_side = tape["side"].isin(("A", "B"))
    dropped_side = int((~mask_side).sum())
    dropped_action = int((tape["action"] != "T").sum())
    tape = tape[mask_side & (tape["action"] == "T")].copy()

    tape["sign"] = (tape["side"] == "A").astype(int) * 2 - 1
    tape["delta"] = tape["sign"].astype("int64") * tape["size"].astype("int64")

    if tape.empty:
        raise SystemExit("la cinta unida queda vacía; revisar rangos/roll.")

    tape["time"] = tape["ts_event"].dt.floor(CANDLES_BAR)
    grouped = tape.groupby("time")
    bars = pd.DataFrame(
        {
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "volume": grouped["size"].sum(),
            "delta": grouped["delta"].sum(),
            "trades": grouped["price"].count(),
        }
    ).reset_index()
    bars["delta"] = bars["delta"].astype("int64")
    bars["volume"] = bars["volume"].astype("int64")

    bars = attach_pd_pdl(bars)

    first, last = bars["time"].min(), bars["time"].max()
    symbols = sorted(tape["symbol"].unique())

    candles_dir = Path(DATABENTO_RAW_DIR).parent / "candles"
    motivo = data_root_guard_error(str(candles_dir))
    if motivo:
        raise SystemExit(f"raíz de datos inválida para velas: {motivo}")
    ensure_dir(str(candles_dir))

    end_label = last.date() + timedelta(days=1)  # end EXCLUSIVO en el nombre, como fetch_databento
    out_parquet = candles_dir / f"{SYMBOL}_M15_{first.date().isoformat()}_{end_label.isoformat()}.parquet"
    bars.to_parquet(out_parquet, index=False)

    meta = {
        "kind": "candles_m15",
        "symbol": SYMBOL,
        "schema": SCHEMA,
        "bars": "M15 UTC-aligned",
        "bar_count": int(len(bars)),
        "range_utc": [first.isoformat(), last.isoformat()],
        "prices": {"low": float(bars["low"].min()), "high": float(bars["high"].max())},
        "roll": {
            "point_utc": ROLL_TS.isoformat(),
            "rule": "6EU6 antes del roll, 6EZ6 desde el roll (cruce de volumen 2026-09-11)",
            "restrict_window_utc": [RESTRICT_START.isoformat(), RESTRICT_END.isoformat()],
            "note": "La ventana define donde el backtest NO inicia setups, no que se borren velas.",
            "dropped_eu6_tail_rows": eu6_tail,
            "dropped_ez6_head_rows": ez6_head,
        },
        "degraded_days": [DEGRADED_DAY],
        "sources": [
            {
                "parquet": eu6_p.name,
                "sha256": sha256_of(eu6_p),
                "rows_used": int(len(eu6)),
            },
            {
                "parquet": ez6_p.name,
                "sha256": sha256_of(ez6_p),
                "rows_used": int(len(ez6)),
            },
        ],
        "tape_stats": {
            "rows_total": int(len(tape)),
            "rows_dropped_not_AB": dropped_side,
            "rows_dropped_action_not_T": dropped_action,
            "symbols_in_tape": symbols,
            "trades_per_bar_mean": round(float(bars["trades"].mean()), 2),
        },
        "columns": ["time", "open", "high", "low", "close", "volume", "delta", "pdh", "pdl"],
        "sha256": sha256_of(out_parquet),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    sidecar = Path(str(out_parquet) + ".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"velas: {out_parquet.name} ({out_parquet.stat().st_size / 1024:.1f} KiB, {len(bars):,} barras)")
    print(f"rango: {first} -> {last}  |  {symbols}")
    print(f"ticks usados: {len(tape):,}  (cola EU6 tras roll: {eu6_tail:,}  cabeza EZ6 antes: {ez6_head:,})")
    print(f"barras sin PDH/PDL (primer día): {(bars[['pdh', 'pdl']].isna().all(axis=1)).sum()}")
    print(f"meta:  {sidecar.name}")
    print(f"sha256: {meta['sha256']}")


if __name__ == "__main__":
    main()