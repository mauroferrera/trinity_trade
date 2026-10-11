"""Frecuencias del régimen nuevo (D-079) ANTES de convalidar F3.

Dos modos:

  python research/regime_scan.py --null
      Calibra `hurst_exponent` contra su DISTRIBUCIÓN NULA (300 series RW
      semilladas de 252 retornos) y contra procesos AR(1) con persistencia
      (rho=+0.8) y antipersistencia (rho=-0.8). Verifica que los cortes
      congelados REGIME_HURST_LO/HI caen en los percentiles previstos del
      ruido (p10/p95). Es la medición que justifica los 0.45/0.70 de
      core/risk_engine.py; se ejecuta ANTES de mirar el PnL de MR (D-065).

  python research/regime_scan.py [--data-dir <dir>]
      Clasifica TODAS las barras D1 de los 5 símbolos y, aparte, las
      barras en las que MR genera señal (mismo cómputo de señales que
      research/backtest_mr.py). Devuelve la masa de rango/neutro/expansión
      en cada poblaci\'f3n. Criterios de no-degeneraci\'f3n registrados ANTES
      de correr:

        a) Panel (todas las barras): los tres estados con masa > 0 en la
           mayor\'eda de s\'edmbolos; "rango" entre ~5% y ~30%.
        b) Se\'f1ales: "rango" >= 10% de las se\'f1ales (margen c\'f3modo sobre
           el m\'ednimo MIN_OOS_N=10 de OOS); 5-10% = marginal, se documenta;
           < 5% = el gate degenera como el viejo ratio 6/3 (0 fills) y hay
           que evaluar el fallback de estado retrasado SIN tocar umbrales.

      Con --export-fixture <ruta> guarda 3 ventanas reales de 253 barras
      (una por estado) como fixture de tests/unit/test_risk_engine.py: la
      expectativa queda congelada contra datos reales (D-076), no contra
      la aritm\'e9tica de un s\'edntetico.

Salida: trinity_data/research/results/regime_scan_<stamp>.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core import risk_engine as re_mod  # noqa: E402
from core.paths import (  # noqa: E402
    RESEARCH_RESULTS_DIR,
    data_root_guard_error,
    ensure_dir,
)
from research import cta, mr  # noqa: E402
from research.backtest_mr import ATR_N, K_BANDS, VWAP_N  # noqa: E402
from research.backtest_cta import (  # noqa: E402
    DEFAULT_DATA_DIR,
    SYMBOLS,
    load_symbol_data,
)

FIXTURE_BARS = 253          # cubre el percentil (250) y el Hurst (252+1)
NULL_SERIES = 300            # series RW del modo --null
NULL_N = 252                 # retornos por serie (ventana de Hurst)


# ---------------------------------------------------------------------------
# Modo --null: distribución nula del estimador
# ---------------------------------------------------------------------------

def _rw_closes(n_returns: int, seed: int) -> List[float]:
    """Cierres de un caminante aleatorio gaussiano semillado (determinista)."""
    r = random.Random(seed)
    px = 100.0
    out = [px]
    for _ in range(n_returns):
        px *= math.exp(r.gauss(0.0, 0.01))
        out.append(px)
    return out


def _ar1_closes(n_returns: int, seed: int, rho: float) -> List[float]:
    """Cierres de un AR(1) sobre retornos: rho>0 persistente, rho<0 antipersistente."""
    r = random.Random(seed)
    px, prev = 100.0, 0.0
    out = [px]
    for _ in range(n_returns):
        prev = rho * prev + r.gauss(0.0, 0.01)
        px *= math.exp(prev)
        out.append(px)
    return out


def run_null(n_series: int = NULL_SERIES) -> Dict[str, Any]:
    """Percentiles de H bajo ruido/AR(1) y falsos positivos de los cortes."""
    def collect(fn) -> List[float]:
        hs = []
        for s in range(n_series):
            h = re_mod.hurst_exponent(fn(s), window=NULL_N)
            if h is not None:
                hs.append(h)
        hs.sort()
        return hs

    def pct(hs: List[float], p: float) -> Optional[float]:
        if not hs:
            return None
        return hs[min(len(hs) - 1, int(p * len(hs)))]

    rw = collect(lambda s: _rw_closes(NULL_N, s))
    pos = collect(lambda s: _ar1_closes(NULL_N, 10_000 + s, 0.8))
    neg = collect(lambda s: _ar1_closes(NULL_N, 20_000 + s, -0.8))

    lo, hi = re_mod.REGIME_HURST_LO, re_mod.REGIME_HURST_HI
    n = len(rw) or 1
    report = {
        "n_series": n_series,
        "n_returns": NULL_N,
        "cortes": {"REGIME_HURST_LO": lo, "REGIME_HURST_HI": hi},
        "rw": {
            "n": len(rw),
            "p10": round(pct(rw, 0.10) or 0.0, 4),
            "p50": round(pct(rw, 0.50) or 0.0, 4),
            "p95": round(pct(rw, 0.95) or 0.0, 4),
            "falso_antipersistente_pct": round(
                100.0 * sum(1 for h in rw if h <= lo) / n, 1),
            "falso_persistente_pct": round(
                100.0 * sum(1 for h in rw if h >= hi) / n, 1),
        },
        "ar1_rho_+0.8": {
            "n": len(pos),
            "p50": round(pct(pos, 0.50) or 0.0, 4),
            "deteccion_persistencia_pct": round(
                100.0 * sum(1 for h in pos if h >= hi) / (len(pos) or 1), 1),
        },
        "ar1_rho_-0.8": {
            "n": len(neg),
            "p50": round(pct(neg, 0.50) or 0.0, 4),
            "deteccion_antipersistencia_pct": round(
                100.0 * sum(1 for h in neg if h <= lo) / (len(neg) or 1), 1),
        },
        "leido": ("umbrales fijados en estos percentiles del ruido; sin "
                  "ningún dato de PnL de MR (D-065)"),
    }
    return report


# ---------------------------------------------------------------------------
# Modo scan: masa por estado en barras y en señales
# ---------------------------------------------------------------------------

def _tally_add(tally: Dict[str, int], key: str) -> None:
    tally[key] = tally.get(key, 0) + 1


def _pct_line(tally: Dict[str, int]) -> str:
    total = sum(tally.values()) or 1
    return "  ".join(
        f"{k}={v} ({100.0 * v / total:.1f}%)"
        for k, v in sorted(tally.items())
    )


def scan_symbol(symbol: str, df, lookback: int) -> Dict[str, Any]:
    df = df.sort_values("time").reset_index(drop=True)
    n = len(df)
    opens = [float(v) for v in df["open"]]
    highs = [float(v) for v in df["high"]]
    lows = [float(v) for v in df["low"]]
    closes = [float(v) for v in df["close"]]
    volumes = [float(v) for v in df["volume"]]
    days = list(df["day"])

    candles = [{"open": opens[i], "high": highs[i],
                "low": lows[i], "close": closes[i]} for i in range(n)]

    # Panel: todas las barras clasificables (calor del clasificador).
    bar_tally: Dict[str, int] = {}
    first_state_bar: Dict[str, int] = {}
    for i in range(re_mod.REGIME_MIN_BARS - 1, n):
        r = re_mod.regime(candles[: i + 1], lookback=lookback)
        _tally_add(bar_tally, r["regime"])
        first_state_bar.setdefault(r["regime"], i)

    # Señales MR: mismo cómputo que backtest_mr (F3), sin skips (medimos
    # el clasificador donde la estrategia mira, no el pipeline completo).
    atrs = cta.atr(highs, lows, closes, n=ATR_N)
    vwap_s = mr.vwap(highs, lows, closes, volumes, n=VWAP_N)
    sup, inf = mr.bands(vwap_s, atrs, k=K_BANDS)
    signals = mr.mr_signals(closes, sup, inf)

    sig_tally: Dict[str, int] = {}
    # Descomposición del gate ANTES de decidir la regla (sin PnL): ¿qué
    # condición es la que estrangula "rango", el percentil o el Hurst?
    alt: Dict[str, int] = {
        "senales": 0,
        "pct_le_20": 0, "pct_le_25": 0, "pct_le_30": 0,
        "hurst_le_lo": 0, "hurst_lt_hi_o_none": 0,
        "actual_pct20_y_hurst_le_lo": 0,
        "cand_pct20_y_hurst_lt_hi": 0,          # opción B: compresión sin
        "cand_pct20_sin_hurst": 0,              # opción A: solo percentil
        "lagged_actual": 0,                     # fallback pre-registrado
    }
    hurst_none = 0
    samples: Dict[str, Optional[Dict[str, Any]]] = {
        "rango": None, "neutro": None, "expansion": None,
    }
    for _direction, bar_i in signals:
        r = re_mod.regime(candles[: bar_i + 1], lookback=lookback)
        _tally_add(sig_tally, r["regime"])
        if r.get("hurst") is None:
            hurst_none += 1

        pct = r.get("vol_percentile")
        h = r.get("hurst")
        lo, hi = re_mod.REGIME_HURST_LO, re_mod.REGIME_HURST_HI
        alt["senales"] += 1
        if pct is not None:
            if pct <= 20:
                alt["pct_le_20"] += 1
            if pct <= 25:
                alt["pct_le_25"] += 1
            if pct <= 30:
                alt["pct_le_30"] += 1
            if h is not None and h <= lo:
                alt["hurst_le_lo"] += 1
            if h is None or h < hi:
                alt["hurst_lt_hi_o_none"] += 1
            if pct <= 20 and h is not None and h <= lo:
                alt["actual_pct20_y_hurst_le_lo"] += 1
            if pct <= 20 and (h is None or h < hi):
                alt["cand_pct20_y_hurst_lt_hi"] += 1
            if pct <= 20:
                alt["cand_pct20_sin_hurst"] += 1
        if bar_i >= 1:
            prev = re_mod.regime(candles[:bar_i], lookback=lookback)
            if prev["regime"] == re_mod.REGIME_RANGO:
                alt["lagged_actual"] += 1

        # Última ocurrencia por estado; exige historia completa para que la
        # ventana exportada cubra percentil (250) y Hurst (253).
        if samples.get(r["regime"]) is None and bar_i >= FIXTURE_BARS - 1:
            start = bar_i - (FIXTURE_BARS - 1)
            samples[r["regime"]] = {
                "symbol": symbol,
                "day": str(days[bar_i]),
                "bar_i": int(bar_i),
                "regime": r["regime"],
                "vol_percentile": r.get("vol_percentile"),
                "hurst": r.get("hurst"),
                "candles": candles[start:bar_i + 1],
            }

    # Si algún estado no aparece en señales con historia completa, toma la
    # última vez en el panel (misma exigencia de 253 barras).
    for state, cand in samples.items():
        if cand is None and state in first_state_bar:
            bar_i = first_state_bar[state]
            if bar_i >= FIXTURE_BARS - 1:
                start = bar_i - (FIXTURE_BARS - 1)
                r = re_mod.regime(candles[: bar_i + 1], lookback=lookback)
                samples[state] = {
                    "symbol": symbol,
                    "day": str(days[bar_i]),
                    "bar_i": int(bar_i),
                    "regime": r["regime"],
                    "vol_percentile": r.get("vol_percentile"),
                    "hurst": r.get("hurst"),
                    "candles": candles[start:bar_i + 1],
                    "fuente": "panel (no hubo señal MR con ese estado)",
                }

    return {
        "bars": n,
        "signals": len(signals),
        "bar_tally": dict(sorted(bar_tally.items())),
        "signal_tally": dict(sorted(sig_tally.items())),
        "signal_hurst_none": hurst_none,
        "alt": alt,
        "samples": {k: v for k, v in samples.items() if v},
    }


def verify_fixture(sample: Dict[str, Any], lookback: int) -> bool:
    """La ventana exportada debe reproducir exactamente la expectativa."""
    r = re_mod.regime(sample["candles"], lookback=lookback)
    return r["regime"] == sample["regime"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--null", action="store_true",
                    help="calibración nula del estimador de Hurst y nada más")
    ap.add_argument("--n-null", type=int, default=NULL_SERIES)
    ap.add_argument("--lookback", type=int,
                    default=re_mod.REGIME_PCT_WINDOW)
    ap.add_argument("--export-fixture", default="",
                    help="ruta JSON para las 3 ventanas reales de tests")
    args = ap.parse_args()

    if args.null:
        rep = run_null(args.n_null)
        rw = rep["rw"]
        print(f"NULL RW n={rw['n']} ({args.n_null} semillas, "
              f"{NULL_N} retornos):")
        print(f"  H percentiles  p10={rw['p10']}  p50={rw['p50']}  "
              f"p95={rw['p95']}")
        print(f"  cortes congelados  LO={re_mod.REGIME_HURST_LO}  "
              f"HI={re_mod.REGIME_HURST_HI}")
        print(f"  falsos sobre ruido: antipersistente "
              f"{rw['falso_antipersistente_pct']}%  persistente "
              f"{rw['falso_persistente_pct']}%")
        for k in ("ar1_rho_+0.8", "ar1_rho_-0.8"):
            print(f"  {k}: p50={rep[k]['p50']}  "
                  f"detección={list(rep[k].values())[-1]}%")
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return

    if not os.path.isdir(args.data_dir):
        raise SystemExit(f"no existe el dataset D1: {args.data_dir}\n"
                         f"  primero ejecuta: python research/build_cta_candles.py")

    symbols_out: Dict[str, Any] = {}
    panel: Dict[str, int] = {}
    sig_all: Dict[str, int] = {}
    sig_hurst_none = 0
    alt_total: Dict[str, int] = {}
    best_samples: Dict[str, Dict[str, Any]] = {}
    for symbol in SYMBOLS:
        df, _meta = load_symbol_data(symbol, args.data_dir)
        res = scan_symbol(symbol, df, args.lookback)
        symbols_out[symbol] = res
        for k, v in res["bar_tally"].items():
            panel[k] = panel.get(k, 0) + v
        for k, v in res["signal_tally"].items():
            sig_all[k] = sig_all.get(k, 0) + v
        for k, v in res["alt"].items():
            alt_total[k] = alt_total.get(k, 0) + v
        sig_hurst_none += res["signal_hurst_none"]
        for state, cand in res["samples"].items():
            if state not in best_samples:
                best_samples[state] = cand
        print(f"{symbol:7} barras={res['bars']}  señales={res['signals']}")
        print(f"   panel:   {_pct_line(res['bar_tally'])}")
        print(f"   señales: {_pct_line(res['signal_tally'])}")

    n_sig = sum(sig_all.values()) or 1
    rango_sig_pct = 100.0 * sig_all.get("rango", 0) / n_sig
    print(f"\nPANEL total   {_pct_line(panel)}")
    print(f"SEÑALES total {_pct_line(sig_all)}")
    print(f"Hurst None en señales: {sig_hurst_none} "
          f"({100.0 * sig_hurst_none / n_sig:.1f}%)")

    n_alt = alt_total.get("senales", 1)
    print("\ndescomposición del gate EN SEÑALES (quién estrangula rango):")
    for key in ("pct_le_20", "pct_le_25", "pct_le_30", "hurst_le_lo",
                "hurst_lt_hi_o_none", "actual_pct20_y_hurst_le_lo",
                "cand_pct20_y_hurst_lt_hi", "cand_pct20_sin_hurst",
                "lagged_actual"):
        v = alt_total.get(key, 0)
        print(f"  {key:28} {v:5}  ({100.0 * v / n_alt:5.1f}%)")
    print(f"\ncriterio (b) rango actual en señales = {rango_sig_pct:.1f}% "
          f"-> {'OK >=10%' if rango_sig_pct >= 10 else 'MARGINAL 5-10%' if rango_sig_pct >= 5 else 'DEGENERA <5%'}")

    fixture: Dict[str, Any] = {}
    for state in ("rango", "neutro", "expansion"):
        cand = best_samples.get(state)
        if cand is None:
            print(f"fixture: sin muestra real de {state!r}")
            continue
        ok = verify_fixture(cand, args.lookback)
        print(f"fixture {state:9} {cand['symbol']} {cand['day']} "
              f"barras={len(cand['candles'])} reproducido={ok}")
        if not ok:
            raise SystemExit(f"fixture de {state} no reproducice: aborto")
        fixture[state] = cand

    motivo = data_root_guard_error(str(RESEARCH_RESULTS_DIR))
    if motivo:
        raise SystemExit(f"raíz de resultados inválida: {motivo}")
    ensure_dir(str(RESEARCH_RESULTS_DIR))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(RESEARCH_RESULTS_DIR, f"regime_scan_{stamp}.json")
    result = {
        "meta": {
            "kind": "regime_scan_d079",
            "lookback": args.lookback,
            "params": {
                "pct_window": re_mod.REGIME_PCT_WINDOW,
                "p_rango": re_mod.REGIME_P_RANGO,
                "p_expansion": re_mod.REGIME_P_EXPANSION,
                "min_bars": re_mod.REGIME_MIN_BARS,
                "hurst_window": re_mod.REGIME_HURST_WINDOW,
                "hurst_lo": re_mod.REGIME_HURST_LO,
                "hurst_hi": re_mod.REGIME_HURST_HI,
            },
            "criterios_previos": {
                "a_panel": "tres estados con masa > 0; rango 5-30%",
                "b_senales": "rango >= 10% (5-10 marginal, <5 degenera)",
            },
            "data_dir": args.data_dir,
            "built_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "panel_total": dict(sorted(panel.items())),
        "signal_total": dict(sorted(sig_all.items())),
        "signal_rango_pct": round(rango_sig_pct, 2),
        "signal_hurst_none": sig_hurst_none,
        "alt_descomposicion": dict(sorted(alt_total.items())),
        "per_symbol": symbols_out,
        "fixture": fixture,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"JSON: {out_path}")

    if args.export_fixture:
        if len(fixture) < 3:
            raise SystemExit("faltan estados para el fixture (¿rango ausente?)")
        fix_dir = os.path.dirname(os.path.abspath(args.export_fixture))
        ensure_dir(fix_dir)
        with open(args.export_fixture, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, ensure_ascii=False)
        print(f"fixture tests: {args.export_fixture}")


if __name__ == "__main__":
    main()
