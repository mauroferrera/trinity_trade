"""Shared fixtures and data factories for the core unit tests.

Provides:
  - load_scenario(name): load JSON from tests/scenarios/
  - make_candles(base_price, count, bias, spread, digits): deterministic OHLCV
  - make_cvd_from_candles(candles): convert candles to CVD [{time, value}] format
  - make_snapshot_from_scenario(scenario, current_price): build a full mock snapshot
    matching the shape the chart snapshot builder returns
  - make_symbol_spec(scenario): SymbolSpec del bloque `market` del escenario

Los scenarios cubren forex, B3 y cripto. Los de B3/cripto anaden el bloque
`market` (digits, point, tick, contract_size, lot_calculator) porque la forma
normalizada de una vela es la misma en los tres mercados pero su UNIDAD no: WIN
cotiza a 0 decimales en puntos de 5 y BTCUSDT a 1 decimal en tick de 0.1. Los
scenarios de forex no lo traen y todo lo que dependa de el se queda en los
defaults de 5 decimales.

Diferencias respecto a la referencia:

- No importa `strategy`. Las killzones se leen DIRECTAMENTE de
  `config/strategy.yaml`, que es la fuente de verdad de producción. Es lo mismo
  que pedía la referencia (leerlas de `strategy.get_trading_config()`) pero por el
  camino corto: aquí todavía no existe el módulo `strategy`, y mantener una copia
  intermedia de las ventanas era justo lo que dejaba que los tests midieran una
  ventana distinta de la real.
- El canario de la DB real mide `core.paths.DB_PATH` en vez de `store.PROJECT_DIR`,
  porque la Fase 1 no persiste todavía. El guard se conserva íntegro para que,
  cuando `database/store.py` llegue, la suite siga protegida por defecto.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from core import paths as _paths
from core import risk_engine as re_mod

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"

# Ventanas de killzone de los scenarios. Se leen de config/strategy.yaml (la fuente
# de verdad de producción) y no se hardcodean: tener una copia aquí era justamente
# lo que dejaba que los tests midieran una ventana distinta de la real.
with open(_paths.STRATEGY_PATH, "r", encoding="utf-8") as _fh:
    _STRATEGY_DOC = yaml.safe_load(_fh) or {}

DEFAULT_KILLZONES: List[Dict[str, str]] = _STRATEGY_DOC.get("killzones") or []


# ---------------------------------------------------------------------------
# Scenario loader
# ---------------------------------------------------------------------------

def load_scenario(name: str) -> Dict[str, Any]:
    """Load a scenario JSON by name (without .json extension)."""
    path = SCENARIOS_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Candle factory
# ---------------------------------------------------------------------------

def make_candles(
    base_price: float = 1.1030,
    count: int = 20,
    bias: str = "bullish",
    spread: float = 0.0003,
    seed_increment: float = 0.00015,
    digits: int = 5,
) -> List[Dict[str, Any]]:
    """Generate deterministic OHLCV candle array.

    bias:
      'bullish'  -> close > open (most candles), upward drift
      'bearish'  -> open > close (most candles), downward drift
      'neutral'  -> small bodies, equal up/down

    Returns list of {time, open, high, low, close, volume}.
    time is sequential epoch starting at 1000000 (arbitrary, tests don't use real time).

    `digits` son los decimales de la cotizacion del simbolo (D-048). Redondear a 5
    estaba bien para EURUSD y es una trampa en B3 y cripto: el minicontrato de WIN
    cotiza a 0 decimales y BTCUSDT a 1, asi que un `round(x, 5)` deja la serie con
    decimales que ningun feed emitiria. El default 5 conserva intactos los
    scenarios de forex.
    """
    candles: List[Dict[str, Any]] = []
    price = base_price

    for i in range(count):
        t = 1_000_000 + i * 900  # 15-min spacing

        if bias == "bullish":
            drift = seed_increment * (0.8 + 0.4 * (i % 3) / 2)
            o = price
            c = price + drift
            h = c + spread * 0.4
            l = o - spread * 0.2
        elif bias == "bearish":
            drift = seed_increment * (0.8 + 0.4 * (i % 3) / 2)
            o = price
            c = price - drift
            h = o + spread * 0.2
            l = c - spread * 0.4
        else:
            o = price
            c = price + (spread * 0.1 if i % 2 == 0 else -spread * 0.1)
            h = max(o, c) + spread * 0.15
            l = min(o, c) - spread * 0.15

        candles.append({
            "time": t,
            "open": round(o, digits),
            "high": round(h, digits),
            "low": round(l, digits),
            "close": round(c, digits),
            "volume": 500 + i * 50,
        })
        price = c

    return candles


# ---------------------------------------------------------------------------
# CVD factory
# ---------------------------------------------------------------------------

def make_cvd_from_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert OHLCV candles to CVD [{time, value}].

    Simulates cumulative delta: positive when close > open, negative otherwise.
    The *slope* of the result is what the risk engine evaluates.
    """
    cumulative = 0.0
    points: List[Dict[str, Any]] = []
    for c in candles:
        body = c["close"] - c["open"]
        cumulative += body * 100_000  # scale to realistic CVD magnitudes
        points.append({"time": c["time"], "value": round(cumulative, 2)})
    return points


def make_flat_cvd(count: int = 20, base_value: float = 0.0) -> List[Dict[str, Any]]:
    """CVD series with zero slope (all values identical)."""
    return [{"time": 1_000_000 + i * 900, "value": base_value} for i in range(count)]


# ---------------------------------------------------------------------------
# Order-flow trades factory
# ---------------------------------------------------------------------------

def make_trades(
    count: int = 120,
    base_price: float = 1.0985,
    side_bias: Optional[float] = None,
    size_range: tuple = (4, 7),
    seed: int = 0,
    jitter: Optional[float] = None,
    digits: int = 5,
    tick_size: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Generate deterministic order-flow trades [{ts, price, size, side}].

    side_bias = probability of "A" (buy aggressor). When None -> 50/50.
    `side` comes from a local random.Random(seed): same args -> same sequence.

    `jitter` es el ruido de precio por tick. Por defecto es una fraccion del precio
    (0.004%), que es la escala del forex; en BTCUSDT o WIN hay que pasar la unidad
    real del tick porque 0.004% de 65000 son 2.6 puntos de ruido (D-048).

    `tick_size`, si se pasa, engancha la serie a la rejilla de cotizacion. Sin eso
    el WIN (0 decimales, punto de 5) produce 129998, un precio que ningun feed
    emite: la serie parece real y no lo es.
    """
    import random

    rng = random.Random(seed)
    price = base_price
    noise = jitter if jitter is not None else abs(base_price) * 0.00004
    trades: List[Dict[str, Any]] = []
    for i in range(count):
        side = "A" if rng.random() < (side_bias if side_bias is not None else 0.5) else "B"
        size = rng.randint(*size_range)
        price += rng.uniform(-noise, noise)
        if tick_size:
            price = round(round(price / tick_size) * tick_size, digits)
        trades.append({
            "ts": 1_700_000_000 + i,
            "price": round(price, digits),
            "size": size,
            "side": side,
        })
    return trades


def make_rising_cvd(count: int = 20, start: float = -500.0, step: float = 120.0) -> List[Dict[str, Any]]:
    """CVD series with clear positive slope."""
    return [{"time": 1_000_000 + i * 900, "value": round(start + i * step, 2)} for i in range(count)]


def make_falling_cvd(count: int = 20, start: float = 500.0, step: float = 120.0) -> List[Dict[str, Any]]:
    """CVD series with clear negative slope."""
    return [{"time": 1_000_000 + i * 900, "value": round(start - i * step, 2)} for i in range(count)]


# ---------------------------------------------------------------------------
# Snapshot builder (matches the chart snapshot shape)
# ---------------------------------------------------------------------------

def make_snapshot_from_scenario(
    scenario: Dict[str, Any],
    current_price: Optional[float] = None,
    killzones: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Build a full mock snapshot dict matching the shape the chart snapshot returns.

    `current_price` sale del escenario salvo que se pase explicitamente (el kwarg
    gana). Si no esta en ninguno de los dos se levanta ValueError: antes el default
    era 1.1045 y un scenario de BTCUSDT sin precio se colaba valuing en 1.1045
    contra un SL de 400 puntos, que es un plan sin sentido en lugar de un fallo
    visible (D-048).
    """
    direction = scenario.get("direction", "BUY")
    now_str = scenario.get("now_utc", "2026-09-07T14:15:00Z")
    now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00"))

    kz = killzones or DEFAULT_KILLZONES
    cot = scenario.get("cot")
    cvd = scenario.get("cvd")
    patterns = scenario.get("patterns", {"fvgs": [], "order_blocks": [], "sweeps": []})

    market = scenario.get("market") or {}
    digits = int(market.get("digits", 5))

    if current_price is None:
        current_price = scenario.get("current_price")
    if current_price is None:
        raise ValueError(
            f"El scenario {scenario.get('name')!r} no trae current_price: un snapshot "
            f"sin precio no es un snapshot. Pasalo por el escenario o por el kwarg."
        )
    current_price = float(current_price)

    # Generate candles from scenario params or use provided ones
    candle_params = scenario.get("candle_params", {})
    candles = make_candles(
        base_price=candle_params.get("base_price", current_price - 0.001),
        count=candle_params.get("count", 20),
        bias=candle_params.get("bias", "bullish"),
        spread=candle_params.get("spread", 0.0003),
        seed_increment=candle_params.get("seed_increment", 0.00015),
        digits=digits,
    )

    # Compute risk engine for both directions
    inputs_base = {"cot": cot, "cvd": cvd, "patterns": patterns, "candles": candles}
    bull = re_mod.setup_score({**inputs_base, "direction": "BUY"}, killzones=kz, now=now_dt)
    bear = re_mod.setup_score({**inputs_base, "direction": "SELL"}, killzones=kz, now=now_dt)

    best = bull if direction == "BUY" else bear
    invalidation = re_mod.invalidation_level(patterns, direction)

    # Compute entry / SL / TP
    cfg = scenario.get("cfg", {})
    sl_dist = float(cfg.get("sl_distance") or 0.0015)
    tp_r = float(cfg.get("tp_ratio_r") or 2.0)

    if direction == "BUY":
        entry = current_price
        sl = round(entry - sl_dist, digits)
        tp = round(entry + sl_dist * tp_r, digits)
    else:
        entry = current_price
        sl = round(entry + sl_dist, digits)
        tp = round(entry - sl_dist * tp_r, digits)

    # Build the snapshot matching the chart snapshot shape
    snap = {
        "symbol": scenario.get("symbol", "EURUSD"),
        "timeframe": scenario.get("timeframe", "M15"),
        "time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "current_price": current_price,
        # El rango del dia se deriva de la distancia del SL del escenario en vez de
        # un 0.003 fijo: en EURUSD da 0.003 (lo que ponia antes) y en BTCUSDT da 800,
        # que es un rango creible, mientras que 0.003 sobre 65000 seria ruido.
        "PDH": round(current_price + sl_dist * 2, digits),
        "PDL": round(current_price - sl_dist * 2, digits),
        "recent_candles": [
            {"time": datetime.fromtimestamp(c["time"]).strftime("%H:%M"),
             "close": c["close"], "high": c["high"], "low": c["low"], "volume": c["volume"]}
            for c in candles[-5:]
        ],
        "analysis": {"patterns": patterns},
        "cvd": cvd,
        "cvd_source": "synthetic (tick volume)",
        "cot_macro_analysis": cot,
        "risk_engine": {
            "bull": bull,
            "bear": bear,
            "killzone": bull["components"]["killzone"],
            "regime": bull["regime"],
            "invalidation": {
                "BUY": re_mod.invalidation_level(patterns, "BUY"),
                "SELL": re_mod.invalidation_level(patterns, "SELL"),
            },
        },
        "_test_entry": entry,
        "_test_sl": sl,
        "_test_tp": tp,
        "_test_direction": direction,
        "_test_invalidation": invalidation,
        "_test_best": best,
    }
    return snap


def make_symbol_spec(scenario: Dict[str, Any]):
    """Construye el `SymbolSpec` del escenario a partir de su bloque `market`.

    Es el puente entre el JSON y las tres cosas que dependen de como cotiza un
    simbolo: la unidad de presentacion (`pip`/punto), el redondeo de los niveles y
    el calculador de lotes. Los scenarios de forex no traen bloque `market` y
    devuelven None: sus specs vienen del bróker real, no de un JSON de test.
    """
    from adapters.base_adapter import SymbolSpec  # perezoso: solo los tests de mercado

    market = scenario.get("market") or {}
    if not market:
        return None
    spec = SymbolSpec(
        symbol=scenario.get("symbol", market.get("symbol", "?")),
        digits=int(market.get("digits", 5)),
        point=float(market.get("point", 0.00001)),
        contract_size=float(market.get("contract_size", 0.0)),
        tick_size=float(market.get("tick_size", market.get("point", 0.00001))),
        tick_value=float(market.get("tick_value", 0.0)),
        volume_min=float(market.get("volume_min", 0.01)),
        volume_max=float(market.get("volume_max", 100.0)),
        volume_step=float(market.get("volume_step", 0.01)),
        pip_override=float(market.get("pip_override", 0.0)),
    )
    return spec


# ---------------------------------------------------------------------------
# Parametrized scenario fixture for pytest
# ---------------------------------------------------------------------------

ALL_SCENARIOS = [
    "sweep_valid_confluence",
    "trap_liquidity",
    "killzone_edge_1min",
    "no_rr_reject",
    "missing_cvd",
    "contradicting_cot",
    "b3_win_mini",
    "b3_wdo_mini",
    "crypto_btc_perp",
]


# ---------------------------------------------------------------------------
# Canary: the suite NEVER writes to the real trading.db
# ---------------------------------------------------------------------------
# `trading.db` is the most valuable asset in the project: the post-mortem, the
# per-component win-rates and the calibration all come from it. A test that opens it
# by accident does not error: it produces contaminated data that looks fine. So the
# file's fingerprint is measured before and after the whole session and the suite
# fails if it changed.
#
# The CONTENT is compared (sha256 + size), not the mtime: OneDrive rewrites the
# timestamp on sync and that is not a write of ours.

def _db_fingerprint(path: Path) -> tuple:
    if not path.exists():
        return ("missing",)
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return (path.stat().st_size, h.hexdigest())


@pytest.fixture(scope="session", autouse=True)
def _real_db_readonly_canary():
    real = Path(_paths.DB_PATH)
    before = _db_fingerprint(real)
    wal_before = Path(str(real) + "-wal").exists()
    yield
    after = _db_fingerprint(real)
    wal_after = Path(str(real) + "-wal").exists()
    if before != after:
        raise AssertionError(
            "The test suite MODIFIED the real trading.db (%s):\n"
            "  before:   %s\n  after:    %s\n"
            "Any test that needs to write must redirect core.paths.DB_PATH to a "
            "tmp_path." % (real, before, after)
        )
    if wal_after and not wal_before:
        raise AssertionError(
            "The suite left the real trading.db open (%s-wal): someone opened it "
            "without closing it. Isolate that test or its DB_PATH." % real
        )


@pytest.fixture(params=ALL_SCENARIOS)
def scenario_name(request):
    return request.param


@pytest.fixture
def scenario(scenario_name):
    return load_scenario(scenario_name)