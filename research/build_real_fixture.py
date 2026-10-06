"""Genera tests/fixtures/orderflow_6e_real.jsonl a partir del Parquet ya descargado.

El fixture es un CORTE de la cinta real que vive fuera del repo
(``DATA_ROOT/databento/raw``), así que este script es lo que hace el artefacto
regenerable sin volver a pagar la API de Databento.

Ventana elegida: **8 h de NY, 10:00-18:00 UTC del 2026-10-05 sobre 6EZ6**.

Por qué 8 h y no 4 h (la prosa original decía "4 h NY"): el docstring
citaba ~20.5k trades, 620 prints a Z>=2.5 y 42 a Z>=4.5 en una misma sesión.
En la cinta de 2026-10-05 ninguna ventana de 4 h pasa de 14.8k trades, pero
las de 8 h rondan los 20-23k y ahí Z>=2.5 cae en 627-653: el rango original
era ~8 h, no 4 h. Esta ventana es la que mejor reproduce Z>=2.5 (631) y a la
vez cae dentro del horario de Nueva York.

Salida:
    tests/fixtures/orderflow_6e_real.jsonl        (una línea por trade, LF)
    tests/fixtures/orderflow_6e_real.meta.json    (procedencia + estadísticos)

Run:  python research/build_real_fixture.py
"""

from __future__ import annotations

import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orderflow_config import (  # noqa: E402  (path inyectado arriba)
    OF_ABSORB_VOL_MIN,
    OF_EMA_WINDOW,
    OF_SYMBOL,
    OF_ZSCORE_MIN_SIZE,
    OF_ZSCORE_THRESHOLD,
    zscore_ceiling,
)
from core.orderflow_engine import OrderFlowEngine  # noqa: E402
from core.paths import DATABENTO_RAW_DIR  # noqa: E402
from research.fetch_databento import jsonl_record  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "orderflow_6e_real.jsonl"
SIDECAR = FIXTURE.with_suffix(".meta.json")

# Contrato líquido y ventana. `OF_SYMBOL` sigue siendo "6E.c.0" (identidad
# normalizada); este es el instrumento al que hay que descargar.
RESOLVED_SYMBOL = "6EZ6"
SESSION_DATE = "2026-10-05"
START_UTC = "2026-10-05T10:00:00+00:00"
END_UTC = "2026-10-05T18:00:00+00:00"


def find_source_parquet() -> Path:
    """El Parquet diario ya descargado de ese contrato y esa sesión."""
    pattern = f"GLBX_{RESOLVED_SYMBOL}_trades_{SESSION_DATE}_*.parquet"
    hits = sorted(Path(DATABENTO_RAW_DIR).glob(pattern))
    if not hits:
        raise SystemExit(
            f"no hay Parquet de {RESOLVED_SYMBOL} en {DATABENTO_RAW_DIR} "
            f"(patrón {pattern}); baja la cinta primero con "
            "python research/fetch_databento.py"
        )
    return hits[-1]


def load_window(parquet: Path) -> List[Dict[str, Any]]:
    """Trades de la ventana, en orden de tape (estable por `ts_event`).

    El orden importa: el motor es secuencial y la EMA depende de él. Se ordena
    por `ts_event` de forma estable, de modo que los prints con el mismo
    instante conservan el orden en que los entregó la cinta.
    """
    df = pd.read_parquet(parquet)
    t0 = pd.Timestamp(START_UTC)
    t1 = pd.Timestamp(END_UTC)
    win = df[(df["ts_event"] >= t0) & (df["ts_event"] < t1)]
    records = [r for r in (jsonl_record(row) for row in win.to_dict("records")) if r]
    records.sort(key=lambda r: r["ts"])
    return records


def write_fixture(records: List[Dict[str, Any]]) -> bytes:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(r, separators=(", ", ": ")) + "\n" for r in records
    )
    FIXTURE.write_text(payload, encoding="utf-8", newline="\n")
    return payload.encode("utf-8")


def normalized_sha(data: bytes) -> str:
    """sha256 sobre bytes con fin de línea LF, para que no dependa del checkout.

    `.gitattributes` marca `* text=auto`: el repo guarda LF y el working tree
    de Windows puede tener CRLF. Hashear sin normalizar haría fallar el test
    de procedencia en cuanto alguien relea el archivo.
    """
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def measure(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estadísticos de cinta + respuesta del motor con los umbrales vigentes."""
    sizes = [r["size"] for r in records]
    vols_300 = [sum(sizes[i - 299 : i + 1]) for i in range(299, len(sizes))]

    engine = OrderFlowEngine()
    z25 = z45 = z45_gated = 0
    for r in records:
        payload = engine.add_trade(r["price"], r["size"], r["side"], r["ts"])
        if payload is None:
            continue
        if payload["zscore"] >= 2.5:
            z25 += 1
        if payload["zscore"] >= 4.5:
            z45 += 1
            if r["size"] >= OF_ZSCORE_MIN_SIZE:
                z45_gated += 1
    snap = engine.snapshot()

    # El techo del z-score deja 5.0 inalcanzable: comprobación del porqué
    # el salto 4.5 -> 5.0 apaga el detector.
    strict = OrderFlowEngine()
    strict.update_settings(zscore_threshold=5.0)
    z50 = sum(
        1
        for r in records
        if (p := strict.add_trade(r["price"], r["size"], r["side"], r["ts"])) is not None
        and p["zscore"] >= 5.0
    )

    return {
        "cinta": {
            "trades": len(records),
            "media_size": round(sum(sizes) / len(sizes), 3),
            "mediana_size": statistics.median(sizes),
            "max_size": max(sizes),
            "precios_distintos": len({r["price"] for r in records}),
            "trades_size_ge_75": sum(1 for s in sizes if s >= OF_ZSCORE_MIN_SIZE),
            "vol_300_media": round(sum(vols_300) / len(vols_300)),
            "vol_300_min": min(vols_300),
            "vol_300_max": max(vols_300),
        },
        "motor": {
            "zscore_threshold": round(OF_ZSCORE_THRESHOLD, 4),
            "zscore_techo": round(zscore_ceiling(OF_EMA_WINDOW), 4),
            "prints_z_ge_2_5": z25,
            "prints_z_ge_4_5": z45,
            "prints_z_ge_4_5_con_gate_size": z45_gated,
            "spikes": snap["spike_count"],
            "zonas_spike": snap["spike_episodes"],
            "zonas_absorcion": snap["absorb_episodes"],
            "prints_z_ge_5_0": z50,
            "cvd": snap["cvd"],
            "buy_vol": snap["buy_vol"],
            "sell_vol": snap["sell_vol"],
        },
    }


def main() -> int:
    parquet = find_source_parquet()
    records = load_window(parquet)
    if not records:
        raise SystemExit("la ventana no contiene trades: revisa START_UTC/END_UTC")
    data = write_fixture(records)
    stats = measure(records)
    jsonl_bytes = FIXTURE.stat().st_size

    source_meta = json.loads((parquet.parent / f"{parquet.name}.meta.json").read_text(encoding="utf-8"))
    sidecar = {
        "fixture": FIXTURE.name,
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_by": "research/build_real_fixture.py",
        "source_parquet": parquet.name,
        "source_parquet_sha256": source_meta.get("sha256"),
        "databento": {
            "dataset": source_meta.get("dataset"),
            "schema": source_meta.get("schema"),
            "requested_symbol": source_meta.get("requested_symbol"),
            "resolved_symbol": RESOLVED_SYMBOL,
            "identity_symbol": OF_SYMBOL,
            "window_start_utc": START_UTC,
            "window_end_utc": END_UTC,
            "downloaded_at_utc": source_meta.get("downloaded_at_utc"),
            "cost_usd": source_meta.get("cost_usd"),
        },
        "rows": len(records),
        "rows_en_parquet": source_meta.get("rows"),
        "bytes": jsonl_bytes,
        "sha256_lf": normalized_sha(data),
        "stats": stats,
    }
    SIDECAR.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    c, m = stats["cinta"], stats["motor"]
    print(f"fixture : {FIXTURE}")
    print(f"  {c['trades']} trades, {jsonl_bytes / 1024:.1f} KiB")
    print(f"  media={c['media_size']} mediana={c['mediana_size']} precios={c['precios_distintos']}")
    print(f"  Z>=2.5={m['prints_z_ge_2_5']}  Z>=4.5={m['prints_z_ge_4_5']} "
          f"(gate={m['prints_z_ge_4_5_con_gate_size']})  Z>=5.0={m['prints_z_ge_5_0']}")
    print(f"  spikes={m['spikes']} zonas_spike={m['zonas_spike']} "
          f"zonas_absorcion={m['zonas_absorcion']} vol300={c['vol_300_media']}")
    print(f"sidecar : {SIDECAR}")
    print(f"  sha256_lf={sidecar['sha256_lf']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
