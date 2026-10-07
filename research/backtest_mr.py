"""Backtest D1 del Mean Reversion VWAP para la convalidación F3 (D-071/D-075).

Replay vela a vela del motor puro `research/mr.py` (VWAP rolling + bandas
±k*ATR + target=VWAP + SL fijo + max_hold) sobre el MISMO dataset D1 que
convalidó el CTA en F2 (5 símbolos, mismo split), con `regime()` de
`core/risk_engine.py` como INTERRUPTOR: la tesis del sistema multi-estrategia
(D-071) dice expansión→CTA, compresión→VWAP, así que este backtest solo
opera cuando `regime() == "rango"` en la barra de señal. Se reporta además la
misma estrategia SIN el gate como referencia, para medir qué compra el
interruptor.

Reglas del simulador (decididas ANTES de ver resultados, D-075):
  - Señal al cierre de la barra `i`: cierre > banda superior → SHORT;
    cierre < banda inferior → LONG. La banda de `i` se conoce al cerrar `i`
    (VWAP y ATR de `i` son datos ya cerrados), el fill va al open de `i+1`
    (mismo convenio que F2; sin lookahead).
  - Target fijo = VWAP de la barra de señal; SL = fill ∓ SL_K*ATR de la barra
    de señal. Si una misma barra toca ambos, SL PRIMERO (conservador).
  - Hueco: solo el SL se rellena peor (al open); el TP llena en target exacto
    (nunca se acredita un hueco a favor).
  - `max_hold` barras como techo temporal: sin toque, cierre al cierre de la
    barra límite ("MAX_HOLD"); datos agotados, "END_OF_DATA".
  - Fricción de 1 TICK adversa por lado SIEMPRE (cota superior de coste), con
    el `tick_size` real del sidecar de cada símbolo. Tamaño por
    `vol_target_lots(equity=100k, atr, spec, vol_target=0.10)`, como F2.
  - Una posición a la vez por símbolo; el rol IS/embargo/OOS se asigna por el
    DÍA de la señal (idéntico a F2: mismas fechas de corte).

Gate de la F3 (mismo umbral que F2): PASS si en OOS, con la fricción ya
descontada, `exp_ticks_net > 0` y hay al menos `MIN_OOS_N` trades.

Uso::

    python research/backtest_mr.py [--data-dir <dir>]

Salida:
    trinity_data/research/results/backtest_mr_<stamp>.json
    y un resumen en consola.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

# Misma guarda que el resto del pipeline: este script se invoca con cwd=raíz y
# necesita importar core.* (ver research/fetch_databento.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import lot_calculator as lc  # noqa: E402
from core import risk_engine as re_mod  # noqa: E402
from core.paths import (  # noqa: E402
    RESEARCH_RESULTS_DIR,
    data_root_guard_error,
    ensure_dir,
)
from research import cta, mr  # noqa: E402
# Reuso el pipeline de F2 para que el split, los specs y las stats sean la MISMA
# aritmética en las dos convalidaciones (un solo eje temporal, una sola verdad).
from research.backtest_cta import (  # noqa: E402
    DEFAULT_DATA_DIR,
    EMBARGO_DAYS,
    EQUITY,
    FRICTION_TICKS,
    MIN_OOS_N,
    SYMBOLS,
    VOL_TARGET,
    load_symbol_data,
    role_of_day,
    segment_stats,
    spec_from_meta,
    split_days,
    verdict,
)

# --- Parámetros de la convalidación (decididos, NO recalibrados) ---------------

ATR_N = 14               # ventana ATR (Wilder), idéntica a F2
VWAP_N = 20              # ventana del VWAP rolling (precio típico ponderado)
K_BANDS = 2.0            # bandas ± k*ATR alrededor del VWAP
SL_K = 2.0               # stop = SL_K*ATR de la barra de señal desde el fill
MAX_HOLD = 10            # techo temporal en barras D1 (~2 semanas de sesiones)
REGIME_LOOKBACK = 60     # ventana de regime() (default de core/risk_engine)
TRADE_REGIME = re_mod.REGIME_RANGO  # el interruptor: solo compresión opera VWAP


def run_symbol(
    symbol: str,
    df: pd.DataFrame,
    spec: lc.LotSpec,
    is_end: date,
    emb_end: date,
    gate_regime: bool,
) -> Dict[str, Any]:
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    opens = [float(v) for v in df["open"]]
    highs = [float(v) for v in df["high"]]
    lows = [float(v) for v in df["low"]]
    closes = [float(v) for v in df["close"]]
    volumes = [float(v) for v in df["volume"]]
    days = list(df["day"])
    tick = spec.tick_size
    friction = FRICTION_TICKS * tick

    atrs = cta.atr(highs, lows, closes, n=ATR_N)
    vwap_s = mr.vwap(highs, lows, closes, volumes, n=VWAP_N)
    sup, inf = mr.bands(vwap_s, atrs, k=K_BANDS)
    signals = mr.mr_signals(closes, sup, inf)

    # Velas para regime(): el slice [i+1] es OBLIGATORIO para que la
    # heurística jamás vea el futuro de la barra de señal.
    candles = [{"open": opens[i], "high": highs[i],
                "low": lows[i], "close": closes[i]} for i in range(n)]

    trades: List[Dict[str, Any]] = []
    counts = {"bars": n, "signals": 0, "fills": 0, "skipped_in_position": 0,
              "skipped_no_atr": 0, "skipped_no_vwap": 0,
              "skipped_no_fill_bar": 0, "skipped_embargo": 0,
              "skipped_regime": 0, "skipped_bad_target": 0}
    in_position_until = -1
    regime_tally: Dict[str, int] = {}

    for direction, bar_i in signals:
        counts["signals"] += 1
        if bar_i <= in_position_until:
            counts["skipped_in_position"] += 1
            continue
        atr_dec = atrs[bar_i]
        target = vwap_s[bar_i]
        if atr_dec is None:
            counts["skipped_no_atr"] += 1
            continue
        if target is None:
            counts["skipped_no_vwap"] += 1
            continue
        if bar_i + 1 >= n:
            counts["skipped_no_fill_bar"] += 1
            continue
        # El embargo es contexto, no ventana de trades (misma regla que F2).
        if role_of_day(days[bar_i], is_end, emb_end) == "embargo":
            counts["skipped_embargo"] += 1
            continue

        reg = re_mod.regime(candles[: bar_i + 1], lookback=REGIME_LOOKBACK)
        regime_tally[reg["regime"]] = regime_tally.get(reg["regime"], 0) + 1
        if gate_regime and reg["regime"] != TRADE_REGIME:
            counts["skipped_regime"] += 1
            continue

        entry = opens[bar_i + 1]
        # El target es el VWAP de la señal: si el open ya quedó del lado
        # equivocado, el setup no existe (no se "arregla" moviendo el target).
        if (direction == "long" and target <= entry) or (
            direction == "short" and target >= entry
        ):
            counts["skipped_bad_target"] += 1
            continue

        size = lc.vol_target_lots(EQUITY, atr_dec, spec, VOL_TARGET)
        lots = size["lots"]
        sl = (entry - SL_K * atr_dec) if direction == "long" else (
            entry + SL_K * atr_dec)
        entry_i = bar_i + 1
        r = mr.simulate_mr(
            direction, entry_i, entry, target, sl,
            opens, highs, lows, closes, MAX_HOLD,
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
            "target_raw": round(target, 8),
            "sl_raw": round(sl, 8),
            "regime": reg["regime"],
            "regime_ratio": reg.get("ratio"),
            "fills_lots": lots,
            "exit_i": r["exit_i"],
            "exit_raw": r["exit_raw"],
            "closed_by": r["closed_by"],
            "ticks_net": round(ticks_net, 4),
            "pnl_usd": round(pnl_usd, 2),
            "mfe_usd": round(usd_of(r["mfe"]), 2),
            "mae_usd": round(usd_of(-r["mae"]), 2),
            # En D1 barra = sesión, así que el conteo de barras del motor es
            # directamente días; la clave la exige segment_stats de F2.
            "days_held": int(r["exit_i"] - entry_i),
        })
        counts["fills"] += 1
        in_position_until = int(r["exit_i"])

    return {"trades": trades, "counts": counts,
            "regime_tally": dict(sorted(regime_tally.items()))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    if not os.path.isdir(args.data_dir):
        raise SystemExit(f"no existe el dataset D1 de F2: {args.data_dir}\n"
                         f"  primero ejecuta: python research/build_cta_candles.py")

    frames = []
    metas: Dict[str, Any] = {}
    for symbol in SYMBOLS:
        df, meta = load_symbol_data(symbol, args.data_dir)
        frames.append(df)
        metas[symbol] = meta
    is_end, emb_end = split_days(frames)

    def run_all(gate: bool) -> Dict[str, Any]:
        per_symbol: Dict[str, Any] = {}
        all_trades: List[Dict[str, Any]] = []
        total_counts: Dict[str, int] = {}
        regime_tally: Dict[str, int] = {}
        for symbol, df in zip(SYMBOLS, frames):
            spec = spec_from_meta(metas[symbol])
            res = run_symbol(symbol, df, spec, is_end, emb_end, gate)
            per_symbol[symbol] = {
                "specs": metas[symbol]["specs"],
                "first_day": df["day"].iloc[0],
                "last_day": df["day"].iloc[-1],
                "bars": res["counts"]["bars"],
                "counts": res["counts"],
                "regime_tally": res["regime_tally"],
            }
            for k, v in res["counts"].items():
                total_counts[k] = total_counts.get(k, 0) + v
            for k, v in res["regime_tally"].items():
                regime_tally[k] = regime_tally.get(k, 0) + v
            all_trades.extend(res["trades"])
        all_trades.sort(key=lambda t: t["sig_time"])
        return {"per_symbol": per_symbol, "trades": all_trades,
                "counts": total_counts,
                "regime_tally": dict(sorted(regime_tally.items()))}

    gated = run_all(True)          # configuración canónica: con interruptor
    ungated = run_all(False)       # referencia: lo mismo SIN el gate

    def split_roles(trades: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        return {
            "is": [t for t in trades if t["role"] == "is"],
            "oos": [t for t in trades if t["role"] == "oos"],
        }

    g_role = split_roles(gated["trades"])
    u_role = split_roles(ungated["trades"])
    perf = {"is": segment_stats(g_role["is"]), "oos": segment_stats(g_role["oos"]),
            "all": segment_stats(gated["trades"])}
    v = verdict(perf)

    exit_tally: Dict[str, int] = {}
    for t in gated["trades"]:
        exit_tally[t["closed_by"]] = exit_tally.get(t["closed_by"], 0) + 1

    result = {
        "meta": {
            "kind": "backtest_convalidacion_mean_reversion_vwap_d1",
            "params": {
                "atr_n": ATR_N,
                "vwap_n": VWAP_N,
                "k_bands": K_BANDS,
                "sl_k": SL_K,
                "max_hold_bars": MAX_HOLD,
                "regime_lookback": REGIME_LOOKBACK,
                "trade_regime": TRADE_REGIME,
                "regime_gate": True,
                "friction_ticks": FRICTION_TICKS,
                "equity": EQUITY,
                "vol_target": VOL_TARGET,
            },
            "split_days": {
                "is_end": is_end.isoformat(),
                "embargo_end": emb_end.isoformat(),
                "note": ("mismo eje temporal que F2: derivado del span real del "
                         "dataset D1 reusado."),
            },
            "symbols": {s: gated["per_symbol"][s]["first_day"] + " -> "
                         + gated["per_symbol"][s]["last_day"] for s in SYMBOLS},
            "data_dir": args.data_dir,
            "note": ("solo opera en regime()==rango (interruptor F3); fricción "
                     "1 tick adverso por lado; tamaño vol-target; una posición "
                     "a la vez por símbolo; rol por día de la señal."),
        },
        "counts": gated["counts"],
        "regime_tally_signals": gated["regime_tally"],
        "per_symbol": gated["per_symbol"],
        "exit_reasons": dict(sorted(exit_tally.items(), key=lambda kv: -kv[1])),
        "perf": perf,
        "verdict": v,
        "reference_no_gate": {
            "counts": ungated["counts"],
            "regime_tally_signals": ungated["regime_tally"],
            "perf": {"is": segment_stats(u_role["is"]),
                     "oos": segment_stats(u_role["oos"]),
                     "all": segment_stats(ungated["trades"])},
        },
        "trades": gated["trades"],
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    motivo = data_root_guard_error(str(RESEARCH_RESULTS_DIR))
    if motivo:
        raise SystemExit(f"raíz de resultados inválida: {motivo}")
    ensure_dir(str(RESEARCH_RESULTS_DIR))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(RESEARCH_RESULTS_DIR, f"backtest_mr_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"símbolos: {len(SYMBOLS)}   señales: {gated['counts']['signals']:,}   "
          f"fills: {gated['counts']['fills']:,}   "
          f"regime skip: {gated['counts']['skipped_regime']:,}   "
          f"embargo skip: {gated['counts']['skipped_embargo']:,}")
    print(f"regime de las señales: {gated['regime_tally']}  "
          f"(gate: solo {TRADE_REGIME!r})")
    print(f"split: IS hasta {is_end} | embargo {EMBARGO_DAYS}d | OOS desde {emb_end}")
    for label, seg in (("IS", g_role["is"]), ("OOS", g_role["oos"])):
        ss = segment_stats(seg)
        print(f"--- {label} ---  trades {ss['n']:>3}  win {ss['win_rate']:.3f}  "
              f"exp ${ss['exp_usd_net']:+.2f}  ticks {ss['exp_ticks_net']:+.4f}  "
              f"pnl ${ss['total_pnl_usd']:+.2f}  maxdd ${ss['max_dd_usd']:.2f}")
    ss_ung = segment_stats(u_role["oos"])
    print(f"referent   ----  sin gate -> OOS trades {ss_ung['n']:>3}  "
          f"exp ${ss_ung['exp_usd_net']:+.2f}  ticks {ss_ung['exp_ticks_net']:+.4f}")
    print(f"verdict: {v['state']}  ->  {v['reason']}")
    print(f"JSON: {out_path}")


if __name__ == "__main__":
    main()
