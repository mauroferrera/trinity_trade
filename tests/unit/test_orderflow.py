"""Tests de Order Flow: fix de absorción, spikes Z-score, reset del engine y
feed sintético (determinismo, replay y fixtures .jsonl).

100% offline: inyecta trades directamente en OrderFlowEngine (core/) o
reproduce el feed sintético de adapters/synthetic_feed.py. No toca Databento ni MT5.

Alcance de esta Fase: las clases que cubrían guards de API y el passthrough
`synthetic` de los endpoints (`TestOfFeedGuard`, `TestSetupEvalSynthetic`,
`TestRiskSetupSynthetic`) se han POSPUESTO. Testean `app.py`, que pertenece a la Fase
de API; mantenerlas aquí exigía importar la aplicación entera para ejercitar el núcleo.

Run:  python -m pytest tests/unit/test_orderflow.py -v
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from adapters import synthetic_feed as mock_feed
from core.orderflow_config import OF_ZSCORE_THRESHOLD, zscore_ceiling
from core.orderflow_engine import OrderFlowEngine

from ..conftest import make_trades

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _feed(engine, trades):
    for t in trades:
        engine.add_trade(price=t["price"], size=t["size"], side=t["side"], ts=t["ts"])


def _feed_until_abs(engine, trades):
    """Inyecta trades hasta el primer evento de absorción (o agota la lista)."""
    for t in trades:
        payload = engine.add_trade(price=t["price"], size=t["size"], side=t["side"], ts=t["ts"])
        if payload and payload.get("absorption"):
            return engine, payload
    return engine, None


def _abs_with(trades, absorb_trades=20):
    eng = OrderFlowEngine()
    eng.update_settings(absorb_trades=absorb_trades)
    return _feed_until_abs(eng, trades)


class TestAbsorptionSideSplit:
    """Fase 1: buy/sell se computan por lado y total == buy + sell."""

    def test_full_window_asymmetric(self):
        # Patrón intercalado A(4), A(4), B(9): volumen asimétrico por lado pero
        # delta equilibrado en CUALQUIER ventana (|A-B|/total ~ 0.06 <= 0.25).
        trades = []
        for i in range(60):
            if i % 3 == 2:
                trades.append({"ts": 1e9 + i, "price": 1.0985, "size": 9, "side": "B"})
            else:
                trades.append({"ts": 1e9 + i, "price": 1.0985, "size": 4, "side": "A"})
        eng, payload = _abs_with(trades)
        assert payload is not None
        ab = payload["absorption"]
        assert ab["buy_vol"] != ab["sell_vol"]
        assert ab["buy_vol"] + ab["sell_vol"] == pytest.approx(ab["volume"])
        # invariante del engine tras consumir toda la ventana
        snap = eng.snapshot()
        assert abs(snap["buy_vol"] + snap["sell_vol"] - snap["total_vol"]) < 1e-6
        assert snap["buy_vol"] != snap["sell_vol"]

    def test_generated_70_30_split_snapshot(self):
        # 70% A / 30% B: el engine acumula por lado aunque el delta_ratio no cierre
        # la absorción. El fix garantiza buy != sell y buy + sell == total.
        trades = make_trades(count=150, side_bias=0.7, size_range=(4, 7), seed=3)
        eng = OrderFlowEngine()
        _feed(eng, trades)
        snap = eng.snapshot()
        buy = sum(t["size"] for t in trades if t["side"] == "A")
        sell = sum(t["size"] for t in trades if t["side"] == "B")
        assert snap["buy_vol"] != snap["sell_vol"]
        assert snap["buy_vol"] == pytest.approx(buy)
        assert snap["sell_vol"] == pytest.approx(sell)
        assert abs(snap["buy_vol"] + snap["sell_vol"] - snap["total_vol"]) < 1e-6


class TestZScoreSpike:
    def test_noise_does_not_spike(self):
        eng = OrderFlowEngine()
        _feed(eng, list(mock_feed.gen_stream("noise", count=300, seed=7)))
        assert eng.snapshot()["spike_count"] == 0

    def test_spike_regime_crosses_threshold(self):
        eng = OrderFlowEngine()
        _feed(eng, list(mock_feed.gen_stream("spike", count=300, seed=7)))
        assert eng.snapshot()["spike_count"] >= 1

    def test_spike_flag_on_individual_trade(self):
        eng = OrderFlowEngine()
        found = False
        for t in mock_feed.gen_stream("spike", count=300, seed=7):
            payload = eng.add_trade(price=t["price"], size=t["size"], side=t["side"], ts=t["ts"])
            if payload and payload.get("is_spike"):
                found = True
                break
        assert found
        assert eng.snapshot()["spike_count"] >= 1


class TestZScoreCeiling:
    """El umbral del detector es una FRACCIÓN del techo de Z, no un número fijo.

    El techo existe porque `_update_zscore` puntúa después de actualizar la EMA
    con el propio trade: la varianza del 2º print es exactamente la de ese
    print, y de ahí sale `sqrt((w-1)/2)`. Si el umbral absoluto no se expresa
    contra ese techo, una ventana más corta lo deja por encima de él y el
    detector se apaga en silencio.
    """

    def test_ceiling_matches_derived_bound(self):
        assert zscore_ceiling(50) == pytest.approx(math.sqrt(49 / 2), rel=1e-12)
        assert zscore_ceiling(20) == pytest.approx(math.sqrt(19 / 2), rel=1e-12)
        # `update_settings` acota la ventana a 2; una ventana aún menor no
        # debe poder producir techo 0 (dividiría entre cero al rederivar).
        assert zscore_ceiling(1) == pytest.approx(math.sqrt(1 / 2), rel=1e-12)

    def test_default_threshold_reproduces_calibration(self):
        # 0.91 del techo de w=50 = 4.5043, es decir el 4.5 calibrado.
        assert OF_ZSCORE_THRESHOLD == pytest.approx(4.5043, abs=5e-4)

    def test_z_never_exceeds_ceiling(self):
        """El techo es supremo alcanzable: ningún print puede superarlo."""
        eng = OrderFlowEngine()
        ceiling = zscore_ceiling(eng.ema_window)
        worst = 0.0
        sizes = [4, 3, 5, 4] * 12 + [1000, 1, 1, 5000, 75]
        for i, size in enumerate(sizes):
            payload = eng.add_trade(price=1.1, size=size, side="A", ts=1e9 + i)
            assert payload is not None
            worst = max(worst, abs(payload["zscore"]))
        # payload["zscore"] viene redondeado a 2 decimales.
        assert worst <= ceiling + 0.01

    def test_narrow_window_keeps_detector_alive(self):
        """Con w=20 el techo baja a 3.08: un umbral fijo de 4.5 lo apagaría."""
        eng = OrderFlowEngine()
        eng.update_settings(ema_window=20)
        settings = eng.settings()
        assert settings["zscore_threshold"] == pytest.approx(
            settings["zscore_threshold_frac"] * zscore_ceiling(20)
        )
        assert settings["zscore_threshold"] < zscore_ceiling(20), (
            "el umbral no puede superar el techo: el detector quedaría muerto"
        )

    def test_absolute_threshold_survives_window_change(self):
        """Un umbral explícito se fija en absoluto, pero su fracción se guarda."""
        eng = OrderFlowEngine()
        eng.update_settings(zscore_threshold=3.0)
        frac = eng.settings()["zscore_threshold_frac"]
        assert frac == pytest.approx(3.0 / zscore_ceiling(50))
        eng.update_settings(ema_window=20)
        assert eng.settings()["zscore_threshold"] == pytest.approx(
            frac * zscore_ceiling(20)
        )

    def test_explicit_absolute_wins_when_both_are_sent(self):
        eng = OrderFlowEngine()
        eng.update_settings(ema_window=20, zscore_threshold=3.0)
        assert eng.settings()["zscore_threshold"] == 3.0


class TestAbsorptionTrigger:
    def test_fixture_triggers_buy_direction(self):
        _, payload = _abs_with(mock_feed.load_fixture(str(FIXTURES / "orderflow_6e_absorption.jsonl")))
        assert payload is not None
        assert payload["absorption"]["direction"] == "compra"

    def test_sell_heavy_window_triggers_venta(self):
        # 24 ventas de 6 y 16 compras de 3 -> net_delta negativo, delta_ratio bajo.
        trades = ([{"ts": 1e9 + i, "price": 1.0985, "size": 6, "side": "B"} for i in range(24)]
                  + [{"ts": 1e9 + 24 + i, "price": 1.0985, "size": 3, "side": "A"} for i in range(16)])
        _, payload = _abs_with(trades)
        assert payload is not None
        assert payload["absorption"]["direction"] == "venta"


class TestAbsorptionCoalescing:
    """Level gating de absorción: una alerta por zona, bins con volumen
    consolidado y snapshot sin saturación."""

    def _count_absorb_emissions(self, engine, trades):
        emitted = 0
        for t in trades:
            p = engine.add_trade(price=t["price"], size=t["size"], side=t["side"], ts=t["ts"])
            if p and p.get("absorption"):
                emitted += 1
        return emitted

    def test_support_zone_emits_single_alert(self):
        trades = mock_feed.load_fixture(str(FIXTURES / "absorption_at_support.jsonl"))
        eng = OrderFlowEngine()
        emitted = self._count_absorb_emissions(eng, trades)
        assert emitted == 1
        assert eng.snapshot()["absorb_episodes"] == 1

    def test_bins_accumulate_consolidated_volume(self):
        trades = mock_feed.load_fixture(str(FIXTURES / "absorption_at_support.jsonl"))
        eng = OrderFlowEngine()
        self._count_absorb_emissions(eng, trades)
        bins = eng.snapshot()["absorb_bins"]
        assert bins, "el snapshot debe exponer los bins de absorción"
        for b in bins:
            assert b["volume"] > 0
            assert b["direction"] in ("compra", "venta")
            assert b["first_ts"] <= b["last_ts"]

    def test_snapshot_bins_sorted_by_volume(self):
        trades = mock_feed.load_fixture(str(FIXTURES / "absorption_at_support.jsonl"))
        eng = OrderFlowEngine()
        self._count_absorb_emissions(eng, trades)
        vols = [b["volume"] for b in eng.snapshot()["absorb_bins"]]
        assert vols == sorted(vols, reverse=True)

    def test_zone_reentry_after_gap_is_new_episode(self):
        # Mismo nivel de precio: dos tramos de absorción separados por > gap
        # reabren episodio (2 alertas, no 1 ni N).
        trades = mock_feed.load_fixture(str(FIXTURES / "absorption_at_support.jsonl"))
        base_ts = trades[-1]["ts"]
        gap_trades = [
            {"ts": base_ts + 200.0 + i, "price": 1.09729, "size": 6, "side": "B"}
            for i in range(24)
        ] + [
            {"ts": base_ts + 230.0 + i, "price": 1.09729, "size": 3, "side": "A"}
            for i in range(16)
        ]
        eng = OrderFlowEngine()
        emitted = self._count_absorb_emissions(eng, trades + gap_trades)
        assert emitted == 2


class TestMockDeterminism:
    def test_same_seed_same_stream(self):
        assert (list(mock_feed.gen_stream("noise", count=200, seed=7))
                == list(mock_feed.gen_stream("noise", count=200, seed=7)))

    def test_different_seed_differs(self):
        a = list(mock_feed.gen_stream("noise", count=200, seed=7))
        b = list(mock_feed.gen_stream("noise", count=200, seed=8))
        assert any(x != y for x, y in zip(a, b))

    def test_deterministic_across_regimes(self):
        for regime in ("noise", "spike", "absorption", "stress"):
            a = list(mock_feed.gen_stream(regime, count=100, seed=1))
            b = list(mock_feed.gen_stream(regime, count=100, seed=1))
            assert a == b, regime


class TestMockRealSizeDistribution:
    """Bloquea la distribución REAL de 6E (mediana ~2, media ~4.4) frente a la
    antigua constante/gaussiana ~12 que sobreestimaba el volumen 2-3x."""

    def _stream(self, seed=7, count=2000):
        return list(mock_feed.gen_stream("noise", count=count, seed=seed))

    def _stats(self, sizes):
        import statistics
        sizes = sorted(sizes)
        n = len(sizes)
        return {
            "median": statistics.median(sizes),
            "mean": statistics.mean(sizes),
            "p95": sizes[int(n * 0.95)],
            "min": min(sizes),
            "max": max(sizes),
        }

    def test_real_tail_not_constant_12(self):
        sizes = [t["size"] for t in self._stream()]
        st = self._stats(sizes)
        # La antigua ~12 constante/media gaussiana 12 quedaba fuera de rango.
        assert 2 <= st["median"] <= 3
        assert st["mean"] <= 6.0
        assert st["min"] >= 1
        # Distribución real (el fixture 6E real tiene 91 tamaños distintos en
        # 20.5k trades; el régimen noise capa a 40, así que un stream de 2000
        # debe cubrir una muestra amplia, no una constante).
        assert len(set(sizes)) >= 25
        assert max(sizes) <= 40  # cap del régimen noise

    def test_size_realistic_ceiling(self):
        # p95 del ruido 6E real ~14; el cap 40 del régimen noise no debe inflar
        # la media ni saturar el Z-score de forma sistemática.
        st = self._stats([t["size"] for t in self._stream(seed=3)])
        assert st["p95"] <= 30
        assert st["max"] <= 40  # cap del régimen noise

    def test_same_seed_reproducible_distribution(self):
        a = [t["size"] for t in self._stream(seed=11, count=500)]
        b = [t["size"] for t in self._stream(seed=11, count=500)]
        assert a == b


class TestMockReplay:
    def test_replay_matches_load_fixture(self):
        path = str(FIXTURES / "orderflow_6e_noise.jsonl")
        assert list(mock_feed.replay(path)) == mock_feed.load_fixture(path)

    def test_replay_reproducible(self):
        path = str(FIXTURES / "orderflow_6e_spike.jsonl")
        assert list(mock_feed.replay(path)) == list(mock_feed.replay(path))

    def test_replay_ordered_ts(self):
        trades = list(mock_feed.replay(str(FIXTURES / "orderflow_6e_absorption.jsonl")))
        ts = [t["ts"] for t in trades]
        assert ts == sorted(ts)


class TestPayloadTs:
    """El payload del WS debe llevar `ts` (epoch secs) para que el chart order
    flow pinte la curva CVD en tiempo real (ofUpdateCvd usa data.ts en el eje X)."""

    def test_trade_payload_includes_ts(self):
        eng = OrderFlowEngine()
        p = eng.add_trade(price=1.0985, size=10, side="A", ts=1_700_000_123.0)
        assert p is not None
        assert p["ts"] == 1_700_000_123.0

    def test_batch_payloads_preserve_ts(self):
        eng = OrderFlowEngine()
        trades = make_trades(count=8, seed=2)
        payloads = []
        for t in trades:
            p = eng.add_trade(t["price"], t["size"], t["side"], t["ts"])
            if p is not None:
                payloads.append(p)
        assert len(payloads) == len(trades)
        for t, p in zip(trades, payloads):
            assert p["ts"] == t["ts"]


class TestEngineReset:
    def test_snapshot_zero_after_reset(self):
        eng = OrderFlowEngine()
        _feed(eng, make_trades(count=60, seed=5))
        assert eng.snapshot()["cvd"] != 0.0
        eng.reset()
        snap = eng.snapshot()
        assert snap["cvd"] == 0.0
        assert snap["delta"] == 0.0
        assert snap["total_vol"] == 0.0
        assert snap["buy_vol"] == 0.0
        assert snap["sell_vol"] == 0.0
        assert snap["spike_count"] == 0
        assert snap["last_price"] is None
        assert snap["alerts"] == []
        assert snap["ema"] is None

    def test_cvd_series_empty_after_reset(self):
        eng = OrderFlowEngine()
        _feed(eng, make_trades(count=60, seed=5))
        assert eng.cvd_series() != []
        eng.reset()
        assert eng.cvd_series() == []

    def test_engine_usable_after_reset(self):
        eng = OrderFlowEngine()
        _feed(eng, make_trades(count=30, seed=2))
        eng.reset()
        _, payload = _feed_until_abs(eng, make_trades(count=150, side_bias=0.7, size_range=(4, 7), seed=1))
        assert eng.snapshot()["cvd"] != 0.0
        assert payload is None or payload.get("absorption") is not None


class TestFixturesLoad:
    def test_noise_fixture_invariants(self):
        eng = OrderFlowEngine()
        _feed(eng, mock_feed.load_fixture(str(FIXTURES / "orderflow_6e_noise.jsonl")))
        snap = eng.snapshot()
        assert snap["spike_count"] == 0
        assert snap["alerts"] == []

    def test_spike_fixture_invariants(self):
        eng = OrderFlowEngine()
        _feed(eng, mock_feed.load_fixture(str(FIXTURES / "orderflow_6e_spike.jsonl")))
        assert eng.snapshot()["spike_count"] >= 1

    def test_absorption_fixture_invariants(self):
        trades = mock_feed.load_fixture(str(FIXTURES / "orderflow_6e_absorption.jsonl"))
        assert len(trades) >= 120
        eng = OrderFlowEngine()
        eng.update_settings(absorb_trades=20)
        payloads = [p for p in (eng.add_trade(t["price"], t["size"], t["side"], t["ts"]) for t in trades) if p]
        absorbs = [p for p in payloads if p.get("absorption")]
        assert absorbs
        assert {p["absorption"]["direction"] for p in absorbs} == {"compra"}
        snap = eng.snapshot()
        assert snap["buy_vol"] != snap["sell_vol"]
        assert abs(snap["buy_vol"] + snap["sell_vol"] - snap["total_vol"]) < 1e-6
        assert eng.absorb_bins

    def test_each_trade_shape(self):
        for name in ("orderflow_6e_noise.jsonl", "orderflow_6e_spike.jsonl", "orderflow_6e_absorption.jsonl"):
            for t in mock_feed.load_fixture(str(FIXTURES / name)):
                assert {"ts", "price", "size", "side"} <= set(t)
                assert t["side"] in ("A", "B")
                assert isinstance(t["size"], int)
                assert isinstance(t["price"], (int, float))
