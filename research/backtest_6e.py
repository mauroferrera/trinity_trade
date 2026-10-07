"""Backtest M15 de la estrategia sobre la cinta real 6E (convalidación D-068).

Replay vela a vela con el MISMO código que ejecuta en vivo (no `sim.py` de REF):
`smc_engine.analyze` -> `risk_engine.setup_score` -> `setup_gate.evaluate_gate`.
El gate devuelve dir/score/plan (entry/SL/TP) y `reasons[]`, que es donde salen
los failure modes sin coste adicional (aprobado en D-070).

Reglas del simulador (documentadas, cota conservadora):
  - Detección al cierre de cada vela M15; análisis con las últimas
    `ANALYSIS_BARS` velas + PDH/PDL del día UTC anterior (como el runtime con
    300 velas). Un candidato por vela (= una banda granular M15, no ticks).
  - Entrada LIMIT en la zona (FVG/OB) seleccionada por `entry_zone`, con TTL
    40 min; entrada a mercado (sin zona) -> apertura de la vela siguiente.
    Si la vela de fill cruza la zona por GAP, se rellena al open (peor precio).
  - Ejecución con fricción plana de 1 pip (0.0001 en 6E = spread 0.5 + slip 0.5)
    en entrada y salida SIEMPRE adversa: es una cota superior de coste, no una
    esperanza de relleno. Sin TBBO no hay algo mejor.
  - SL y TP evaluados barra a barra; si la misma vela toca ambos, SL primero
    (conservador). MFE/MAE calculados sobre precios raw (sin fricción).
  - Riesgo: lote implícito 1 contrato (6E: 1 pip = USD 25). `max_trades_day` = 3
    por día UTC, contado por FILL.

Split (60/30 + embargo 24h, D-065/D-069):
  - IS:     [2026-07-08 00:00Z, 2026-09-06 00:00Z)    (60 días, contiene la
             degradación de 2026-08-29)
  - Embargo: [2026-09-06 00:00Z, 2026-09-07 00:00Z)   (contexto, sin setups)
  - OOS:    [2026-09-07 00:00Z, fin)                  (~30 días, contiene el roll)
  - Roll ban: setups que se INICIAN en [2026-09-10T00:00Z, 2026-09-12T00:00Z)
    se cuentan como "excluidos", no como rechazos.

Distancia de SL: `sl_distance_by_symbol["6E"] = 0.0012` calibrado SOLO con datos
IS (nominal igual al global; origen queda marcado como `symbol`). El resto de
umbrales (min_score 59.5, pesos, TTL 40) son los de strategy.yaml, NO se
recalibran: es convalidación, no optimización (D-065).

Uso::

    python research/backtest_6e.py [--candles <parquet>]

Salida:
    trinity_data/research/results/backtest_6e_<stamp>.json   (todo el detalle)
    y un resumen en consola.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

# Misma guarda que el resto del pipeline: este script se invoca con cwd=raíz y
# necesita importar core.* (ver research/fetch_databento.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import risk_engine as re_mod  # noqa: E402
from core import setup_gate as gate_mod  # noqa: E402
from core import smc_engine as smc  # noqa: E402
from core import strategy as strat  # noqa: E402
from core.paths import (  # noqa: E402
    DATABENTO_RAW_DIR,
    RESEARCH_RESULTS_DIR,
    STRATEGY_PATH,
    data_root_guard_error,
    ensure_dir,
)

# --- Parámetros del backtest (fijados en IS o decididos, NO recalibrados) -------

ANALYSIS_BARS = 300          # velas de contexto (mismo orden que el runtime)
CVD_BARS = 30                # serie CVD "reciente" (CVD_LIVE_WINDOW_S = 30 velas M15)
FRICTION = 0.0001            # 1 pip 6E, adversa a la entrada y a la salida
LOT_USD_PER_PIP = 25.0       # 1 contrato 6E: pip de 0.0001 = USD 25
SL_6E = 0.0012               # distancia SL calibrada en IS (nominal 12 pips)

START_TS = datetime(2026, 7, 8, 0, 0, 0, tzinfo=timezone.utc)
IS_END = START_TS + timedelta(days=60)          # 2026-09-06 00:00Z
EMBARGO_END = IS_END + timedelta(days=1)        # 2026-09-07 00:00Z
END_TS = datetime(2026, 10, 6, 0, 0, 0, tzinfo=timezone.utc)
ROLL_BAN_START = datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc)
ROLL_BAN_END = datetime(2026, 9, 12, 0, 0, 0, tzinfo=timezone.utc)

DEFAULT_CANDLES = os.path.join(
    os.path.dirname(DATABENTO_RAW_DIR), "candles", "6E_M15_2026-07-08_2026-10-06.parquet"
)


@dataclass
class Order:
    direction: str
    entry: float
    entry_kind: str
    sl: float
    tp: float
    sig_time: datetime
    role: str
    bar_i: int


@dataclass
class Trade:
    direction: str
    role: str
    sig_time: datetime
    entry: float
    entry_kind: str
    fill_time: datetime
    fill_raw: float
    sl: float
    tp: float
    exit_time: Optional[datetime] = None
    exit_raw: Optional[float] = None
    closed_by: str = ""
    pnl_pips_net: float = 0.0
    pnl_usd: float = 0.0
    mfe_pips: float = 0.0
    mae_pips: float = 0.0
    bars_held: int = 0
    result: str = ""


def role_of(t: datetime) -> str:
    if t < START_TS or t >= END_TS:
        return "outside"
    if t < IS_END:
        return "is"
    if t < EMBARGO_END:
        return "embargo"
    return "oos"


def build_cfg() -> Dict[str, Any]:
    """Config efectiva de strategy.yaml (aplanada), con SL de 6E fijado en IS."""
    with open(STRATEGY_PATH, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    cfg: Dict[str, Any] = strat.build_flat(doc)
    # Decodificar los JSON string a estructura: multiplican las mismas formas que
    # ya aceptan `risk_weights_for` / `killzones_for`, y son las del runtime.
    try:
        cfg["risk_weights"] = json.loads(cfg["risk_weights"])
    except (TypeError, json.JSONDecodeError):
        pass
    try:
        cfg["killzones"] = json.loads(cfg["killzones"])
    except (TypeError, json.JSONDecodeError):
        pass
    by_sym: Dict[str, Any] = dict(cfg.get("sl_distance_by_symbol") or {})
    by_sym["6E"] = SL_6E
    cfg["sl_distance_by_symbol"] = by_sym
    return cfg


def cvd_points(cum: List[float], times: List[datetime], i: int) -> List[dict]:
    lo = max(0, i - CVD_BARS + 1)
    return [{"time": times[k], "value": float(cum[k])} for k in range(lo, i + 1)]


def analyze_bar(bars: pd.DataFrame, i: int) -> Dict[str, Any]:
    lo = max(0, i - ANALYSIS_BARS + 1)
    win = bars.iloc[lo : i + 1]
    candles = [
        {"time": r["time"], "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"]}
        for _, r in win.iterrows()
    ]
    bar = bars.iloc[i]
    analysis = smc.analyze(candles, bar["pdh"], bar["pdl"], symbol="6E", timeframe="M15")
    return {"candles": candles, "analysis": analysis}


def fill_and_run(
    o: Order,
    bars: pd.DataFrame,
    trades: List[Trade],
    fills_per_day: Dict,
    ttl: timedelta,
    max_trades_day: int,
    mode: str = "zone_ttl",
) -> Optional[str]:
    """Simula fill y vida del trade.

    `mode`:
      - "zone_ttl": orden LIMIT en la zona seleccionada, viva `ttl` (fiel a como
        ejecuta el runtime; el 90% de aprobados expira sin rellenarse).
      - "next_open": simplificación aprobada en el plan (fill a la apertura de la
        vela siguiente para TODO approved; el TTL deja de ser relevante en la
        entrada). Se genera para comparar y se etiqueta como cota distinta.
    """
    n = len(bars)
    deadline = o.sig_time + ttl
    if mode == "next_open" or o.entry_kind == "market":
        j = o.bar_i + 1
        if j >= n:
            return "unfilled_no_bar"
        raw = float(bars.iloc[j]["open"])
    else:
        j = None
        raw = None
        for j in range(o.bar_i + 1, n):
            bj = bars.iloc[j]
            if bj["time"] >= deadline:
                break
            if o.direction == "BUY" and bj["low"] <= o.entry:
                raw = min(o.entry, float(bj["open"]))
                break
            if o.direction == "SELL" and bj["high"] >= o.entry:
                raw = max(o.entry, float(bj["open"]))
                break
        if raw is None:
            return "unfilled_ttl"

    d = bars.iloc[j]["time"].date()
    if fills_per_day.get(d, 0) >= max_trades_day:
        return "blocked_max_trades_day"

    buy = o.direction == "BUY"
    exec_in = raw + FRICTION if buy else raw - FRICTION

    mfe = 0.0
    mae = 0.0
    exit_raw = None
    exit_time = None
    closed_by = ""
    k_exit = j

    for k in range(j, n):
        b = bars.iloc[k]
        h, l = float(b["high"]), float(b["low"])
        if buy:
            mfe = max(mfe, h - raw)
            mae = min(mae, l - raw)
            sl_hit = l <= o.sl
            tp_hit = h >= o.tp
        else:
            mfe = max(mfe, raw - l)
            mae = min(mae, raw - h)
            sl_hit = h >= o.sl
            tp_hit = l <= o.tp
        if sl_hit and tp_hit:
            exit_raw, closed_by = o.sl, "SL"
        elif sl_hit:
            exit_raw, closed_by = o.sl, "SL"
        elif tp_hit:
            exit_raw, closed_by = o.tp, "TP"
        if exit_raw is not None:
            k_exit = k
            exit_time = bars.iloc[k]["time"]
            break

    if exit_raw is None:
        k_exit = n - 1
        exit_time = bars.iloc[n - 1]["time"]
        exit_raw = float(bars.iloc[n - 1]["close"])
        closed_by = "END_OF_DATA"

    exec_out = exit_raw - FRICTION if buy else exit_raw + FRICTION
    gross_pips = (exec_out - exec_in) * 10000.0 if buy else (exec_in - exec_out) * 10000.0

    tr = Trade(
        direction=o.direction,
        role=o.role,
        sig_time=o.sig_time,
        entry=o.entry,
        entry_kind=o.entry_kind,
        fill_time=exit_time if False else bars.iloc[j]["time"],
        fill_raw=raw,
        sl=o.sl,
        tp=o.tp,
        exit_time=exit_time,
        exit_raw=exit_raw,
        closed_by=closed_by,
        pnl_pips_net=round(gross_pips, 4),
        pnl_usd=round(gross_pips * LOT_USD_PER_PIP, 2),
        mfe_pips=round(mfe * 10000.0, 4),
        mae_pips=round(-mae * 10000.0, 4),
        bars_held=k_exit - j,
        result=closed_by,
    )
    fills_per_day[d] = fills_per_day.get(d, 0) + 1
    trades.append(tr)
    return "filled"


def segment_stats(seg: List[Trade]) -> Dict[str, Any]:
    if not seg:
        base = {"n": 0}
        for k in ("wins", "losses", "win_rate", "exp_pips_net", "total_pnl_usd",
                  "avg_mfe_pips", "avg_mae_pips", "avg_bars", "max_dd_pips", "profit_factor"):
            base[k] = 0.0 if k != "n" else 0
        return base
    pnls = [t.pnl_pips_net for t in seg]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    cum = []
    run = 0.0
    peak = 0.0
    mdd = 0.0
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
        "exp_pips_net": round(sum(pnls) / len(pnls), 4),
        "total_pnl_usd": round(sum(t.pnl_usd for t in seg), 2),
        "avg_win_pips": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_pips": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "avg_mfe_pips": round(sum(t.mfe_pips for t in seg) / len(seg), 4),
        "avg_mae_pips": round(sum(t.mae_pips for t in seg) / len(seg), 4),
        "avg_bars": round(sum(t.bars_held for t in seg) / len(seg), 2),
        "max_dd_pips": round(mdd, 4),
        "profit_factor": round(pf, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candles", default=DEFAULT_CANDLES)
    args = ap.parse_args()

    candles_path = args.candles
    if not os.path.exists(candles_path):
        raise SystemExit(f"no existe el parquet de velas: {candles_path}\n"
                         f"  primero ejecuta: python research/build_6e_candles.py")

    df = pd.read_parquet(candles_path)
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    times = df["time"].tolist()
    cum = df["delta"].cumsum().tolist()

    cfg = build_cfg()
    killzones = strat.killzones_for(cfg) or re_mod.DEFAULT_KILLZONES
    weights = strat.risk_weights_for(cfg)
    umbral = strat.min_score_for(cfg)
    ttl = timedelta(minutes=float(cfg.get("setup_ttl_minutes") or 40.0))
    max_day = int(cfg.get("max_trades_day") or 10)
    MAX_TRADES_DAY = max_day  # parametro decidido (3) para el conteo diario

    smr = {"confirmed": False, "reason": "backtest sin feed DXY (neutro)",
           "bull": {"confirmed": False}, "bear": {"confirmed": False}}

    detections: List[Dict[str, Any]] = []
    trades_zt: List[Trade] = []
    trades_no: List[Trade] = []
    fills_zt: Dict = {}
    fills_no: Dict = {}

    def mode_counts() -> Dict[str, int]:
        return {"detected": 0, "approved": 0, "filled": 0, "unfilled_ttl": 0,
                "blocked_max_trades_day": 0}

    counts_zt = mode_counts()
    counts_no = mode_counts()
    counts = {"bars": n, "warmup": 0, "embargo": 0, "excluded_roll": 0,
              "detected": 0, "approved": 0, "filled": 0, "unfilled_ttl": 0,
              "blocked_max_trades_day": 0, "market_entries": 0, "zone_entries": 0}
    reason_tally: Dict[str, int] = {}

    for i in range(ANALYSIS_BARS, n):
        t = times[i]
        if t >= END_TS:
            break
        role = role_of(t)
        if role == "embargo":
            counts["embargo"] += 1
            continue
        if ROLL_BAN_START <= t < ROLL_BAN_END:
            counts["excluded_roll"] += 1
            continue

        ctx = analyze_bar(df, i)
        bar = df.iloc[i]
        cvd = cvd_points(cum, times, i)

        inputs_buy = {"direction": "BUY", "cot": None, "cvd": cvd,
                      "patterns": ctx["analysis"]["patterns"], "candles": ctx["candles"], "smr": smr}
        inputs_sell = {**inputs_buy, "direction": "SELL"}

        bull = re_mod.setup_score(inputs_buy, weights, killzones, now=t)
        bear = re_mod.setup_score(inputs_sell, weights, killzones, now=t)
        inv = {
            "BUY": re_mod.invalidation_level(ctx["analysis"]["patterns"], "BUY"),
            "SELL": re_mod.invalidation_level(ctx["analysis"]["patterns"], "SELL"),
        }
        snap = {
            "symbol": "6E",
            "current_price": float(bar["close"]),
            "analysis": ctx["analysis"],
            "risk_engine": {"bull": bull, "bear": bear, "invalidation": inv},
        }
        gate = gate_mod.evaluate_gate(snap, cfg, spec=None)
        if gate is None:
            continue

        counts["detected"] += 1
        for r in gate.get("reasons") or []:
            reason_tally[r] = reason_tally.get(r, 0) + 1

        det = {
            "time": t.isoformat(),
            "role": role,
            "direction": gate["direction"],
            "score_buy": bull["score"],
            "score_bear": bear["score"],
            "best_score": gate["score"],
            "approved": gate["approved"],
            "entry_kind": gate["entry_kind"],
            "reasons": gate.get("reasons") or [],
        }
        detections.append(det)

        if not gate["approved"]:
            continue
        counts["approved"] += 1

        order = Order(
            direction=gate["direction"],
            entry=float(gate["entry"]),
            entry_kind=gate["entry_kind"],
            sl=float(gate["sl"]),
            tp=float(gate["tp"]),
            sig_time=t,
            role=role,
            bar_i=i,
        )
        if order.entry_kind == "market":
            counts["market_entries"] += 1
        else:
            counts["zone_entries"] += 1

        for mode_name, tl, fpd, cnt in (
            ("zone_ttl", trades_zt, fills_zt, counts_zt),
            ("next_open", trades_no, fills_no, counts_no),
        ):
            outcome = fill_and_run(order, df, tl, fpd, ttl, MAX_TRADES_DAY, mode=mode_name)
            if outcome == "filled":
                cnt["filled"] += 1
            elif outcome == "unfilled_ttl":
                cnt["unfilled_ttl"] += 1
            elif outcome == "blocked_max_trades_day":
                cnt["blocked_max_trades_day"] += 1

    is_trades = [t for t in trades_zt if t.role == "is"]
    oos_trades = [t for t in trades_zt if t.role == "oos"]
    is_trades_no = [t for t in trades_no if t.role == "is"]
    oos_trades_no = [t for t in trades_no if t.role == "oos"]

    # El bloque `counts` expone el modo canónico (zone_ttl) para los temas de fill.
    counts["filled"] = counts_zt["filled"]
    counts["unfilled_ttl"] = counts_zt["unfilled_ttl"]
    counts["blocked_max_trades_day"] = counts_zt["blocked_max_trades_day"]

    result = {
        "meta": {
            "kind": "backtest_convalidacion_6e",
            "params": {
                "analysis_bars": ANALYSIS_BARS,
                "cvd_bars": CVD_BARS,
                "friction_pips": FRICTION * 10000,
                "ttl_minutes": ttl.total_seconds() / 60,
                "max_trades_day": MAX_TRADES_DAY,
                "sl_distance_6e": SL_6E,
                "sl_source": "sl_distance_by_symbol.6E (calibrado solo en IS)",
                "min_score": umbral,
                "weights": weights,
                "killzones": killzones,
            },
            "split_utc": {
                "is": [START_TS.isoformat(), IS_END.isoformat()],
                "embargo": [IS_END.isoformat(), EMBARGO_END.isoformat()],
                "oos": [EMBARGO_END.isoformat(), END_TS.isoformat()],
            },
            "roll_ban_window_utc": [ROLL_BAN_START.isoformat(), ROLL_BAN_END.isoformat()],
            "degraded_day": "2026-08-29",
            "candles": candles_path,
            "note": ("MFE/MAE sobre precios raw (sin friccion); SL primero si la misma "
                     "vela toca SL y TP; friccion 1 pip adversa en entrada y salida."),
        },
        "counts": counts,
        "rejections": {
            "detected": counts["detected"],
            "approved": counts["approved"],
            "reasons": dict(sorted(reason_tally.items(), key=lambda kv: -kv[1])),
        },
        "fill_modes": {
            "canonical": "zone_ttl",
            "zone_ttl": {
                "counts": counts_zt,
                "perf": {
                    "is": segment_stats(is_trades),
                    "oos": segment_stats(oos_trades),
                    "all": segment_stats(trades_zt),
                },
            },
            "next_open": {
                "counts": counts_no,
                "perf": {
                    "is": segment_stats(is_trades_no),
                    "oos": segment_stats(oos_trades_no),
                    "all": segment_stats(trades_no),
                },
            },
        },
        "perf": {
            "is": segment_stats(is_trades),
            "oos": segment_stats(oos_trades),
            "all": segment_stats(trades_zt),
        },
        "failure_breakdown": {
            "detected": counts["detected"],
            "approved_gate": counts["approved"],
            "mode": {
                "market_entries": counts["market_entries"],
                "zone_entries": counts["zone_entries"],
                "filled": counts_zt["filled"],
                "unfilled_ttl": counts_zt["unfilled_ttl"],
                "blocked_max_trades_day": counts_zt["blocked_max_trades_day"],
            },
        },
        "trades": [
            {
                "role": t.role,
                "direction": t.direction,
                "sig_time": t.sig_time.isoformat(),
                "entry_kind": t.entry_kind,
                "fill_time": t.fill_time.isoformat(),
                "entry_raw": t.entry,
                "fill_raw": t.fill_raw,
                "sl": t.sl,
                "tp": t.tp,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "exit_raw": t.exit_raw,
                "closed_by": t.closed_by,
                "pnl_pips_net": t.pnl_pips_net,
                "pnl_usd": t.pnl_usd,
                "mfe_pips": t.mfe_pips,
                "mae_pips": t.mae_pips,
                "bars_held": t.bars_held,
            }
            for t in trades_zt
        ],
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    motivo = data_root_guard_error(str(RESEARCH_RESULTS_DIR))
    if motivo:
        raise SystemExit(f"raíz de resultados inválida: {motivo}")
    ensure_dir(str(RESEARCH_RESULTS_DIR))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(RESEARCH_RESULTS_DIR, f"backtest_6e_{stamp}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    print(f"velas:            {n:,}  (evaluadas desde barra {ANALYSIS_BARS})")
    print(f"detectados:       {counts['detected']:,}  aprobados: {counts['approved']:,}")
    print(f"embargo skip:     {counts['embargo']:,}   roll ban: {counts['excluded_roll']:,}")
    for label, seg_zt, seg_no in (("IS", is_trades, is_trades_no),
                                   ("OOS", oos_trades, oos_trades_no)):
        ss = segment_stats(seg_zt)
        ss_no = segment_stats(seg_no)
        print(f"--- {label} ---")
        print(f"  zone_ttl : trades {ss['n']:>3}  win {ss['win_rate']:.3f}  "
              f"exp {ss['exp_pips_net']:+.3f} pips  pnl {ss['total_pnl_usd']:+.2f} USD")
        print(f"  next_open: trades {ss_no['n']:>3}  win {ss_no['win_rate']:.3f}  "
              f"exp {ss_no['exp_pips_net']:+.3f} pips  pnl {ss_no['total_pnl_usd']:+.2f} USD")
    print(f"JSON: {out_path}")


if __name__ == "__main__":
    main()