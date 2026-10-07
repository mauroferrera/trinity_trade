"""Backtest D1 del CTA Swing para la convalidación F2 (D-071).

Replay vela a vela del MISMO motor que convalidará en vivo: `research/cta.py`
(atr Wilder, donchian, breakout próximo-open, trailing chandelier) y
`core/lot_calculator.vol_target_lots` (tamaño por volatilidad objetivo, NO por
riesgo a SL: otra clase de pregunta, documentada en D-071). Dentro del catálogo
que ya existe, este backtest es el homólogo de `backtest_6e.py` pero para un
swing diario multi-símbolo.

Reglas del simulador (documentadas, cota conservadora):
  - Señal al cierre de la vela D1 `i`: el cierre rompe el canal de Donchian de
    las `n` barras ANTERIORES (`close[i]` vs `upper[i-1]`/`lower[i-1]`), sin
    mirar el rango de `i` (sin lookahead: el fill va al open de `i+1`).
  - Fill siempre a mercado al open de `i+1`. La barra de fill puede salir por
    el trailing el mismo día (misma convención que 6E: la barra de entrada se
    evalúa completa).
  - Exit con trailing chandelier `extremo - k*ATR` (long) / `+ k*ATR` (short):
    el stop de la barra `k` se ratchea con la barra `k-1`, y si la barra abre
    más allá del stop (hueco) se rellena al open, peor precio. Sin toque, el
    trade se cierra censurado al cierre del dataset ("END_OF_DATA").
  - Fricción de 1 TICK adversa en entrada y salida, SIEMPRE: cota superior de
    coste (spread + slip), no una esperanza de relleno. Cada símbolo usa SU
    `tick_size` (del sidecar de MT5), no un pip plano.
  - Vol-targeting: tamaño `vol_target_lots(equity=100k, atr_d1, spec,
    vol_target=0.10)`. PnL en USD = lotes * (vtick_value/tick_size); MFE/MAE
    sobre precios raw sin fricción (como 6E).
  - Una posición a la vez por símbolo: las señales que llegan con la posición
    abierta se descartan (no hay piramidiar). El split/clasificación por rol usa
    el DÍA de la señal (igual que 6E usa el tiempo de señal).

Split (60/30 + embargo, D-065/D-069):
  - El eje de tiempo D1 se calcula del DATASET (primer día global -> último día
    global). IS = primer 60% de los días; embargo = 30 días siguientes (contexto,
    sin trades); OOS = el resto.
  - Los umbrales NO se recalibran: es convalidación, no optimización (D-065).
    Se reportan dos ventanas de Donchian (20 y 55) y el resultado IS/OOS de
    cada una, pero el veredicto es el de la configuración canónica.

Gate de la F2 (ROADMAP.md: "solo pasa si OOS supera la fricción"):
  - Con la fricción ya descontada (1 tick por lado), "superar la fricción"
    equivale a que la esperanza NETA de USD/ticks en OOS sea > 0 con un mínimo
    de `MIN_OOS_N` trades:   verdict = "PASS" | "FAIL" | "INCONCLUSIVE".

Uso::

    python research/backtest_cta.py [--data-dir <dir>]

Salida:
    trinity_data/research/results/backtest_cta_<stamp>.json   (todo el detalle)
    y un resumen en consola.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Misma guarda que el resto del pipeline: este script se invoca con cwd=raíz y
# necesita importar core.* (ver research/fetch_databento.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from research import cta  # noqa: E402
from core import lot_calculator as lc  # noqa: E402
from core.paths import (  # noqa: E402
    DATA_ROOT,
    RESEARCH_RESULTS_DIR,
    data_root_guard_error,
    ensure_dir,
)

# --- Parámetros de la convalidación (decididos, NO recalibrados) ---------------

ATR_N = 14               # ventana ATR (Wilder), como el batch del CTA
DON_N = 20               # ventana de Donchian de la configuración canónica
DON_N_LONG = 55          # ventana larga, solo como referencia IS/OOS
MULT = 3.0               # multiplicador del trailing chandelier
ATR_DEC = 1.0            # el ATR se usa DECIMAL-izado (atr_dec en cta.py)
FRICTION_TICKS = 1.0     # fricción adversa por lado, en TICKS del símbolo
EQUITY = 100_000.0       # equity nominal de la cuenta máster
VOL_TARGET = 0.10        # = lc.VOL_TARGET_DEFAULT
CONTRAST_SIZE = 100_000.0  # lot_size nominal (no usado por vol_target_lots)
IS_FRACTION = 0.60       # 60% del span D1 para IS
EMBARGO_DAYS = 30        # contexto sin trades entre IS y OOS
MIN_OOS_N = 10           # mínimo de trades OOS para que el veredicto no sea inconcluso

SYMBOLS: List[str] = ["EURUSD", "XAUUSD", "US500", "GBPUSD", "AUDUSD"]
DEFAULT_DATA_DIR = os.path.join(DATA_ROOT, "research", "cta", "d1")


def load_symbol_data(symbol: str, data_dir: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    base = os.path.join(data_dir, "{0}_D1".format(symbol))
    df = pd.read_parquet(base + ".parquet")
    # El sidecar lo cierra junto al parquet (build_cta_candles.write_sidecar).
    with open(base + ".parquet.meta.json", "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    return df, meta


def spec_from_meta(meta: Dict[str, Any]) -> lc.LotSpec:
    s = meta["specs"]
    return lc.LotSpec.from_dict({
        "contract_size": CONTRAST_SIZE,
        "tick_value": s["tick_value"],
        "tick_size": s["tick_size"],
        "min_lot": s["volume_min"],
        "lot_step": s["volume_step"],
    })


def split_days(frames: List[pd.DataFrame]) -> Tuple[date, date]:
    """Límites del split (60/30) derivados del span REAL del dataset."""
    first = min(df["day"].iloc[0] for df in frames)
    last = max(df["day"].iloc[-1] for df in frames)
    d0 = date.fromisoformat(first)
    d1 = date.fromisoformat(last)
    span = (d1 - d0).days
    is_end = d0 + timedelta(days=int(span * IS_FRACTION))
    emb_end = is_end + timedelta(days=EMBARGO_DAYS)
    return is_end, emb_end


def role_of_day(day: str, is_end: date, emb_end: date) -> str:
    d = date.fromisoformat(day)
    if d < is_end:
        return "is"
    if d < emb_end:
        return "embargo"
    return "oos"


def run_symbol(
    symbol: str,
    df: pd.DataFrame,
    spec: lc.LotSpec,
    is_end: date,
    emb_end: date,
    don_n: int,
    atr_n: int,
    mult: float,
    friction_ticks: float,
) -> Dict[str, Any]:
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    opens = [float(v) for v in df["open"]]
    highs = [float(v) for v in df["high"]]
    lows = [float(v) for v in df["low"]]
    closes = [float(v) for v in df["close"]]
    days = list(df["day"])
    tick = spec.tick_size
    friction = friction_ticks * tick

    atrs = cta.atr(highs, lows, closes, n=atr_n)
    upper, lower = cta.donchian(highs, lows, n=don_n)
    signals = cta.breakout_signals(closes, upper, lower)

    trades: List[Dict[str, Any]] = []
    counts = {"bars": n, "signals": 0, "fills": 0, "skipped_in_position": 0,
              "skipped_no_atr": 0, "skipped_no_fill_bar": 0, "skipped_embargo": 0}
    in_position_until = -1

    for direction, bar_i in signals:
        counts["signals"] += 1
        if bar_i <= in_position_until:
            counts["skipped_in_position"] += 1
            continue
        atr_dec = atrs[bar_i]
        if atr_dec is None or bar_i + 1 >= n:
            if atr_dec is None:
                counts["skipped_no_atr"] += 1
            else:
                counts["skipped_no_fill_bar"] += 1
            continue
        # El embargo es contexto, no ventana de trades (igual que 6E): los setups
        # que se INICIAN dentro no entran ni en IS ni en OOS.
        if role_of_day(days[bar_i], is_end, emb_end) == "embargo":
            counts["skipped_embargo"] += 1
            continue

        entry = opens[bar_i + 1]
        size = lc.vol_target_lots(EQUITY, atr_dec, spec, VOL_TARGET)
        lots = size["lots"]
        entry_i = bar_i + 1
        r = cta.simulate_trade(
            direction, entry_i, entry, opens, highs, lows, closes,
            atrs, mult=mult, atr_dec=atr_dec,
        )

        buy = direction == "long"
        exec_in = entry + friction if buy else entry - friction
        exec_out = r["exit_raw"] - friction if buy else r["exit_raw"] + friction
        ticks_net = (exec_out - exec_in) / tick if buy else (exec_in - exec_out) / tick
        pnl_usd = ticks_net * spec.tick_value * lots

        def usd_of(price_delta: float) -> float:
            return price_delta / tick * spec.tick_value * lots

        trades.append({
            "symbol": symbol,
            "day": days[bar_i],
            "role": role_of_day(days[bar_i], is_end, emb_end),
            "direction": direction,
            "sig_time": days[bar_i],
            "entry_raw": round(entry, 8),
            "fills_lots": lots,
            "exit_i": r["exit_i"],
            "exit_raw": r["exit_raw"],
            "closed_by": r["closed_by"],
            "ticks_net": round(ticks_net, 4),
            "pnl_usd": round(pnl_usd, 2),
            "mfe_usd": round(usd_of(r["mfe"]), 2),
            "mae_usd": round(usd_of(-r["mae"]), 2),
            "days_held": int(r["exit_i"] - entry_i),
        })
        counts["fills"] += 1
        in_position_until = r["exit_i"]

    return {"trades": trades, "counts": counts}


def segment_stats(seg: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not seg:
        base = {"n": 0}
        for k in ("wins", "losses", "win_rate", "exp_usd_net", "exp_ticks_net",
                  "total_pnl_usd", "avg_mfe_usd", "avg_mae_usd", "avg_days",
                  "max_dd_usd", "profit_factor"):
            base[k] = 0.0 if k != "n" else 0
        return base
    pnls = [t["pnl_usd"] for t in seg]
    ticks = [t["ticks_net"] for t in seg]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    run, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        run += p
        peak = max(peak, run)
        mdd = min(mdd, run - peak)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (999.0 if wins else 0.0)
    return {
        "n": len(seg),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(seg), 4),
        "exp_usd_net": round(sum(pnls) / len(pnls), 2),
        "exp_ticks_net": round(sum(ticks) / len(ticks), 4),
        "total_pnl_usd": round(sum(pnls), 2),
        "exp_win_usd": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "exp_loss_usd": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "avg_mfe_usd": round(sum(t["mfe_usd"] for t in seg) / len(seg), 2),
        "avg_mae_usd": round(sum(t["mae_usd"] for t in seg) / len(seg), 2),
        "avg_days": round(sum(t["days_held"] for t in seg) / len(seg), 2),
        "max_dd_usd": round(mdd, 2),
        "profit_factor": round(pf, 3),
    }


def verdict(perf: Dict[str, Any]) -> Dict[str, str]:
    oos = perf["oos"]
    if oos["n"] < MIN_OOS_N:
        return {"state": "INCONCLUSIVE",
                "reason": f"OOS n={oos['n']} < {MIN_OOS_N}: no alcanza para un veredicto."}
    if oos["exp_ticks_net"] > 0.0:
        return {"state": "PASS",
                "reason": (f"OOS exp_ticks_net={oos['exp_ticks_net']:.4f} > 0: con la "
                           "fricción (1 tick/lado) ya descontada, la estrategia "
                           "supera la fricción.")}
    return {"state": "FAIL",
            "reason": (f"OOS exp_ticks_net={oos['exp_ticks_net']:.4f} <= 0: no supera "
                       "la fricción con los parámetros canónicos.")}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    if not os.path.isdir(args.data_dir):
        raise SystemExit(f"no existe el dataset CTA: {args.data_dir}\n"
                         f"  primero ejecuta: python research/build_cta_candles.py")

    frames = []
    for symbol in SYMBOLS:
        df, _ = load_symbol_data(symbol, args.data_dir)
        frames.append(df)
    is_end, emb_end = split_days(frames)

    per_symbol: Dict[str, Any] = {}
    all_trades: List[Dict[str, Any]] = []
    total_counts = {"bars": 0, "signals": 0, "fills": 0, "skipped_in_position": 0,
                    "skipped_no_atr": 0, "skipped_no_fill_bar": 0, "skipped_embargo": 0}
    exit_tally: Dict[str, int] = {}

    for symbol, df in zip(SYMBOLS, frames):
        _, meta = load_symbol_data(symbol, args.data_dir)
        spec = spec_from_meta(meta)
        res = run_symbol(symbol, df, spec, is_end, emb_end,
                         DON_N, ATR_N, MULT, FRICTION_TICKS)
        per_symbol[symbol] = {
            "specs": meta["specs"],
            "first_day": df["day"].iloc[0],
            "last_day": df["day"].iloc[-1],
            "bars": res["counts"]["bars"],
            "counts": res["counts"],
        }
        for k in total_counts:
            total_counts[k] += res["counts"][k]
        for t in res["trades"]:
            exit_tally[t["closed_by"]] = exit_tally.get(t["closed_by"], 0) + 1
        all_trades.extend(res["trades"])

    all_trades.sort(key=lambda t: t["sig_time"])
    is_tr = [t for t in all_trades if t["role"] == "is"]
    oos_tr = [t for t in all_trades if t["role"] == "oos"]

    # Vista de referencia con Donchian largo (55): misma aritmética but otra ventana.
    per_symbol_long: Dict[str, Any] = {}
    trades_long: List[Dict[str, Any]] = []
    for symbol, df in zip(SYMBOLS, frames):
        _, meta = load_symbol_data(symbol, args.data_dir)
        spec = spec_from_meta(meta)
        res = run_symbol(symbol, df, spec, is_end, emb_end,
                         DON_N_LONG, ATR_N, MULT, FRICTION_TICKS)
        per_symbol_long[symbol] = res["counts"]
        trades_long.extend(res["trades"])
    trades_long.sort(key=lambda t: t["sig_time"])
    is_tr_long = [t for t in trades_long if t["role"] == "is"]
    oos_tr_long = [t for t in trades_long if t["role"] == "oos"]

    perf = {"is": segment_stats(is_tr), "oos": segment_stats(oos_tr),
            "all": segment_stats(all_trades)}
    v = verdict(perf)

    result = {
        "meta": {
            "kind": "backtest_convalidacion_cta_swing_d1",
            "params": {
                "atr_n": ATR_N,
                "donchian_n": DON_N,
                "donchian_n_long_reference": DON_N_LONG,
                "mult": MULT,
                "atr_decimalized": ATR_DEC,
                "friction_ticks": FRICTION_TICKS,
                "equity": EQUITY,
                "vol_target": VOL_TARGET,
                "min_vol_target_lots_uses_lot_calculator": True,
            },
            "split_days": {
                "is_end": is_end.isoformat(),
                "embargo_end": emb_end.isoformat(),
                "note": "eje de tiempo derivado del span real del dataset D1.",
            },
            "symbols": {s: per_symbol[s]["first_day"] + " -> " + per_symbol[s]["last_day"]
                         for s in SYMBOLS},
            "data_dir": args.data_dir,
            "note": ("MFE/MAE y pnl en USD con el tamaño vol-target (1 posición a la "
                     "vez por símbolo); fricción 1 tick adversa en entrada y salida; "
                     "rol por día de la señal; una posición abierta descarta señales."),
        },
        "counts": total_counts,
        "per_symbol": per_symbol,
        "exit_reasons": dict(sorted(exit_tally.items(), key=lambda kv: -kv[1])),
        "perf": perf,
        "verdict": v,
        "reference_donchian_55": {
            "counts": {s: per_symbol_long[s] for s in SYMBOLS},
            "perf": {"is": segment_stats(is_tr_long),
                     "oos": segment_stats(oos_tr_long),
                     "all": segment_stats(trades_long)},
        },
        "trades": all_trades,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    motivo = data_root_guard_error(str(RESEARCH_RESULTS_DIR))
    if motivo:
        raise SystemExit(f"raíz de resultados inválida: {motivo}")
    ensure_dir(str(RESEARCH_RESULTS_DIR))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(RESEARCH_RESULTS_DIR, f"backtest_cta_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"símbolos: {len(SYMBOLS)}   señales: {total_counts['signals']:,}   "
          f"fills: {total_counts['fills']:,}   embargo skip: "
          f"{total_counts['skipped_embargo']:,}")
    print(f"split: IS hasta {is_end} | embargo 30d | OOS desde {emb_end}")
    for label, seg in (("IS", is_tr), ("OOS", oos_tr)):
        ss = segment_stats(seg)
        print(f"--- {label} ---  trades {ss['n']:>3}  win {ss['win_rate']:.3f}  "
              f"exp ${ss['exp_usd_net']:+.2f}  ticks {ss['exp_ticks_net']:+.4f}  "
              f"pnl ${ss['total_pnl_usd']:+.2f}  maxdd ${ss['max_dd_usd']:.2f}")
    ss55 = segment_stats(oos_tr_long)
    print(f"referent   ----  Donchian 55 -> OOS trades {ss55['n']:>3}  "
          f"exp ${ss55['exp_usd_net']:+.2f}  ticks {ss55['exp_ticks_net']:+.4f}")
    print(f"verdict: {v['state']}  ->  {v['reason']}")
    print(f"JSON: {out_path}")


if __name__ == "__main__":
    main()