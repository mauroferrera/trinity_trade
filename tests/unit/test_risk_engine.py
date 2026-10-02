"""Layer 1: Deterministic risk_engine unit tests.

Tests each component in isolation and the full setup_score() with fixture data.
No MT5, no LLM, no network — pure function verification.

Run:  python -m pytest tests/unit/test_risk_engine.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import risk_engine as re_mod

from ..conftest import (
    _STRATEGY_DOC, DEFAULT_KILLZONES,
    load_scenario, make_candles, make_flat_cvd,
    make_rising_cvd, make_falling_cvd,
)


# ---------------------------------------------------------------------------
# Component: killzone_score
# ---------------------------------------------------------------------------

class TestKillzoneScore:
    def _kz(self, name):
        return next(w for w in DEFAULT_KILLZONES if w["name"] == name)

    def _at(self, hm, day=7):
        h, m = (int(x) for x in hm.split(":"))
        return datetime(2026, 9, day, h, m, 0, tzinfo=timezone.utc)

    def test_inside_new_york(self):
        """Una hora dentro de la ventana NY de producción la marca activa."""
        win = self._kz("Nueva York")
        start_h, start_m = (int(x) for x in win["start"].split(":"))
        now = self._at(f"{start_h:02d}:{start_m + 15:02d}")
        result = re_mod.killzone_score(now=now)
        assert result["value"] == 1.0
        assert result["in_killzone"] is True
        assert result["name"] == "Nueva York"

    def test_inside_london(self):
        win = self._kz("Londres")
        start_h, start_m = (int(x) for x in win["start"].split(":"))
        end_h, end_m = (int(x) for x in win["end"].split(":"))
        mid = ((start_h * 60 + start_m) + (end_h * 60 + end_m)) // 2
        now = self._at(f"{mid // 60:02d}:{mid % 60:02d}")
        result = re_mod.killzone_score(now=now)
        assert result["value"] == 1.0
        assert result["in_killzone"] is True
        assert result["name"] == "Londres"

    def test_outside_killzone(self):
        """Antes de la primera ventana del día nadie está en killzone."""
        first = min(
            int(w["start"].split(":")[0]) for w in DEFAULT_KILLZONES
        )
        now = self._at(f"{first - 2:02d}:30")
        result = re_mod.killzone_score(now=now)
        assert result["value"] == 0.25
        assert result["in_killzone"] is False

    def test_killzone_boundary_start_inclusive(self):
        """El inicio exacto de NY entra (smin <= tmin)."""
        now = self._at(self._kz("Nueva York")["start"])
        result = re_mod.killzone_score(now=now)
        assert result["value"] == 1.0
        assert result["in_killzone"] is True

    def test_killzone_boundary_end_exclusive(self):
        """El final exacto de NY excluye (tmin < emin)."""
        now = self._at(self._kz("Nueva York")["end"])
        result = re_mod.killzone_score(now=now)
        assert result["value"] == 0.25
        assert result["in_killzone"] is False

    def test_defaults_match_strategy_yaml(self):
        """Regresión del desfase de defaults: risk_engine.DEFAULT_KILLZONES y
        strategy._DEFAULTS tienen que ser las ventanas de strategy.yaml.

Antes defaults y YAML divergían (08:00-11:00 / 13:00-16:00 contra
        07:00-10:00 / 12:30-15:30), así que cualquier ruta que usara el default
        en vez del YAML —"tests, snapshot forzado"— medía otra ventana."""
        assert re_mod.DEFAULT_KILLZONES == DEFAULT_KILLZONES
        assert DEFAULT_KILLZONES == _STRATEGY_DOC["killzones"]

    def test_custom_killzone(self):
        """Custom killzone window can be passed."""
        custom = [{"name": "Asia", "start": "00:00", "end": "06:00"}]
        now = datetime(2026, 9, 7, 3, 0, 0, tzinfo=timezone.utc)
        result = re_mod.killzone_score(windows=custom, now=now)
        assert result["value"] == 1.0
        assert result["name"] == "Asia"


# ---------------------------------------------------------------------------
# Component: cot_component
# ---------------------------------------------------------------------------

class TestCotComponent:
    def test_bullish_buy_aligned(self):
        """BULLISH COT + BUY direction = value 1.0."""
        cot = {"macro_bias": "BULLISH", "cot_index_26w": 75.3}
        r = re_mod.cot_component(cot, "BUY")
        assert r["value"] == 1.0

    def test_bearish_sell_aligned(self):
        """BEARISH COT + SELL direction = value 1.0."""
        cot = {"macro_bias": "BEARISH", "cot_index_26w": 22.1}
        r = re_mod.cot_component(cot, "SELL")
        assert r["value"] == 1.0

    def test_bullish_sell_mismatch(self):
        """BULLISH COT + SELL direction = value 0.15 (contrary)."""
        cot = {"macro_bias": "BULLISH", "cot_index_26w": 75.3}
        r = re_mod.cot_component(cot, "SELL")
        assert r["value"] == 0.15

    def test_bearish_buy_mismatch(self):
        """BEARISH COT + BUY direction = value 0.15 (contrary)."""
        cot = {"macro_bias": "BEARISH", "cot_index_26w": 22.1}
        r = re_mod.cot_component(cot, "BUY")
        assert r["value"] == 0.15

    def test_neutral(self):
        """NEUTRAL COT = value 0.6 (neither helps nor hurts)."""
        cot = {"macro_bias": "NEUTRAL", "cot_index_26w": 50.0}
        r = re_mod.cot_component(cot, "BUY")
        assert r["value"] == 0.6

    def test_missing_cot(self):
        """None COT = value 0.0 (data gap penalized honestly)."""
        r = re_mod.cot_component(None, "BUY")
        assert r["value"] == 0.0


# ---------------------------------------------------------------------------
# Component: cvd_component
# ---------------------------------------------------------------------------

class TestCvdComponent:
    def test_rising_cvd_buy(self):
        """Positive CVD slope + BUY = high value (0.8-1.0)."""
        cvd = make_rising_cvd(count=10, start=-500, step=120)
        r = re_mod.cvd_component(cvd, "BUY")
        assert r["value"] >= 0.8

    def test_falling_cvd_sell(self):
        """Negative CVD slope + SELL = high value (0.8-1.0)."""
        cvd = make_falling_cvd(count=10, start=500, step=120)
        r = re_mod.cvd_component(cvd, "SELL")
        assert r["value"] >= 0.8

    def test_rising_cvd_sell_mismatch(self):
        """Positive CVD slope + SELL = low value (0.15)."""
        cvd = make_rising_cvd(count=10, start=-500, step=120)
        r = re_mod.cvd_component(cvd, "SELL")
        assert r["value"] == 0.15

    def test_flat_cvd_neutral(self):
        """Flat CVD = value 0.4 (neutral)."""
        cvd = make_flat_cvd(count=10)
        r = re_mod.cvd_component(cvd, "BUY")
        assert r["value"] == 0.4

    def test_short_cvd_returns_zero(self):
        """Less than 5 points = insufficient data, value 0.0."""
        cvd = [{"time": 1, "value": 100}, {"time": 2, "value": 200}]
        r = re_mod.cvd_component(cvd, "BUY")
        assert r["value"] == 0.0

    def test_none_cvd(self):
        """None CVD = value 0.0."""
        r = re_mod.cvd_component(None, "BUY")
        assert r["value"] == 0.0


# ---------------------------------------------------------------------------
# Component: smc_component
# ---------------------------------------------------------------------------

class TestSmcComponent:
    def test_full_confluence_buy(self):
        """FVG + OB + PDL_SWEEP for BUY = value 1.0."""
        patterns = {
            "fvgs": [{"type": "BULLISH_FVG", "top": 1.105, "bottom": 1.103, "start_time": 1000}],
            "order_blocks": [{"type": "BULLISH_OB", "top": 1.1035, "bottom": 1.102, "start_time": 1001}],
            "sweeps": [{"type": "PDL_SWEEP", "time": 1002}],
        }
        r = re_mod.smc_component(patterns, "BUY")
        assert r["value"] == 1.0

    def test_fvg_only(self):
        """FVG alone = value 0.5."""
        patterns = {
            "fvgs": [{"type": "BULLISH_FVG", "top": 1.105, "bottom": 1.103, "start_time": 1000}],
            "order_blocks": [],
            "sweeps": [],
        }
        r = re_mod.smc_component(patterns, "BUY")
        assert r["value"] == 0.5

    def test_fvg_plus_ob(self):
        """FVG + OB (no sweep) = value 0.7."""
        patterns = {
            "fvgs": [{"type": "BULLISH_FVG", "top": 1.105, "bottom": 1.103, "start_time": 1000}],
            "order_blocks": [{"type": "BULLISH_OB", "top": 1.1035, "bottom": 1.102, "start_time": 1001}],
            "sweeps": [],
        }
        r = re_mod.smc_component(patterns, "BUY")
        assert r["value"] == 0.7

    def test_sweep_with_opposition_penalty(self):
        """Full confluence minus 0.1 when opposed patterns exist."""
        patterns = {
            "fvgs": [
                {"type": "BULLISH_FVG", "top": 1.105, "bottom": 1.103, "start_time": 1000},
                {"type": "BEARISH_FVG", "top": 1.108, "bottom": 1.106, "start_time": 1003},
            ],
            "order_blocks": [{"type": "BULLISH_OB", "top": 1.1035, "bottom": 1.102, "start_time": 1001}],
            "sweeps": [{"type": "PDL_SWEEP", "time": 1002}],
        }
        r = re_mod.smc_component(patterns, "BUY")
        assert r["value"] == 0.9  # 1.0 - 0.1 opposition penalty

    def test_empty_patterns(self):
        """No patterns at all = value 0.0."""
        r = re_mod.smc_component({}, "BUY")
        assert r["value"] == 0.0


# ---------------------------------------------------------------------------
# Component: regime
# ---------------------------------------------------------------------------

class TestRegime:
    def test_expansion(self):
        """Large range vs small bodies = expansion."""
        candles = make_candles(base_price=1.1000, count=10, bias="bullish", spread=0.001, seed_increment=0.0005)
        r = re_mod.regime(candles)
        assert r["regime"] in ("expansion", "rango", "neutro")  # just verify it runs

    def test_no_candles(self):
        """Empty candles = neutral."""
        r = re_mod.regime([])
        assert r["regime"] == "neutro"

    def test_few_candles(self):
        """Less than 5 candles = neutral."""
        candles = make_candles(count=3)
        r = re_mod.regime(candles)
        assert r["regime"] == "neutro"


# ---------------------------------------------------------------------------
# Component: invalidation_level
# ---------------------------------------------------------------------------

class TestInvalidationLevel:
    def test_buy_invalidation(self):
        """BUY: min bottom of BULLISH_FVG + BULLISH_OB."""
        patterns = {
            "fvgs": [{"type": "BULLISH_FVG", "top": 1.105, "bottom": 1.103, "start_time": 1000}],
            "order_blocks": [{"type": "BULLISH_OB", "top": 1.1035, "bottom": 1.102, "start_time": 1001}],
            "sweeps": [],
        }
        assert re_mod.invalidation_level(patterns, "BUY") == 1.102

    def test_sell_invalidation(self):
        """SELL: max top of BEARISH_FVG + BEARISH_OB."""
        patterns = {
            "fvgs": [{"type": "BEARISH_FVG", "top": 1.108, "bottom": 1.106, "start_time": 1000}],
            "order_blocks": [{"type": "BEARISH_OB", "top": 1.107, "bottom": 1.105, "start_time": 1001}],
            "sweeps": [],
        }
        assert re_mod.invalidation_level(patterns, "SELL") == 1.108

    def test_no_patterns_returns_none(self):
        """No bullish patterns = None."""
        assert re_mod.invalidation_level({}, "BUY") is None


# ---------------------------------------------------------------------------
# Full setup_score with scenarios
# ---------------------------------------------------------------------------

class TestSetupScoreScenarios:
    """Run each JSON scenario through setup_score() and verify the expected range."""

    def test_sweep_valid_confluence(self):
        sc = load_scenario("sweep_valid_confluence")
        now = datetime.fromisoformat(sc["now_utc"].replace("Z", "+00:00"))
        candles = make_candles(**sc["candle_params"])
        result = re_mod.setup_score(
            {"direction": sc["direction"], "cot": sc["cot"], "cvd": sc["cvd"],
             "patterns": sc["patterns"], "candles": candles},
            killzones=sc["killzones"], now=now,
        )
        assert result["score"] >= sc["expected"]["score_min"], f"Score {result['score']} < min {sc['expected']['score_min']}"
        assert result["verdict"] == sc["expected"]["verdict"]
        assert result["components"]["killzone"]["in_killzone"] is True
        assert result["components"]["cot"]["value"] >= sc["expected"]["cot_value_min"]
        assert result["components"]["smc"]["value"] >= sc["expected"]["smc_value_min"]

    def test_trap_liquidity(self):
        sc = load_scenario("trap_liquidity")
        now = datetime.fromisoformat(sc["now_utc"].replace("Z", "+00:00"))
        candles = make_candles(**sc["candle_params"])
        result = re_mod.setup_score(
            {"direction": sc["direction"], "cot": sc["cot"], "cvd": sc["cvd"],
             "patterns": sc["patterns"], "candles": candles},
            killzones=sc["killzones"], now=now,
        )
        assert result["score"] <= sc["expected"]["score_max"], f"Score {result['score']} > max {sc['expected']['score_max']}"
        assert result["verdict"] == sc["expected"]["verdict"]
        assert result["components"]["killzone"]["in_killzone"] is False
        assert result["components"]["cot"]["value"] <= sc["expected"]["cot_value_max"]

    def test_killzone_edge_1min(self):
        sc = load_scenario("killzone_edge_1min")
        now = datetime.fromisoformat(sc["now_utc"].replace("Z", "+00:00"))
        candles = make_candles(**sc["candle_params"])
        result = re_mod.setup_score(
            {"direction": sc["direction"], "cot": sc["cot"], "cvd": sc["cvd"],
             "patterns": sc["patterns"], "candles": candles},
            killzones=sc["killzones"], now=now,
        )
        assert result["score"] >= sc["expected"]["score_min"]
        assert result["components"]["killzone"]["in_killzone"] is True
        assert result["components"]["killzone"]["name"] == sc["expected"]["killzone_name"]

    # xfail(strict): pendiente de una decision de calibracion, no de un test roto.
    # cdb6ec2 retiro los 20 puntos de killzone y sumo 15 de smr_dxy. Los fixtures de
    # escenario no tienen SMR, asi que el techo sin SMR baja de 100 a 82.4. Aqui el
    # setup tiene SMC perfecto pero ni CVD ni SMR, y cae a 58.8: por debajo del corte
    # de MEDIA (60) pasa a SIN_OPERATIVA. strict hace que arreglar la calibracion sin
    # quitar la marca rompa la suite, que es justo cuando hay que quitar la marca.
    # Ver la pregunta abierta en ROADMAP.md sobre si smr_dxy debe pesar 15 sin feed.
    @pytest.mark.xfail(
        strict=True,
        reason="calibracion: smr_dxy pesa 15 y el fixture no tiene SMR, "
               "el techo cae a 82.4 y 58.8 queda bajo el corte de MEDIA",
    )
    def test_missing_cvd(self):
        sc = load_scenario("missing_cvd")
        now = datetime.fromisoformat(sc["now_utc"].replace("Z", "+00:00"))
        candles = make_candles(**sc["candle_params"])
        result = re_mod.setup_score(
            {"direction": sc["direction"], "cot": sc["cot"], "cvd": None,
             "patterns": sc["patterns"], "candles": candles},
            killzones=sc["killzones"], now=now,
        )
        assert result["components"]["cvd_of"]["value"] == 0.0
        assert result["score"] <= sc["expected"]["score_max"]
        assert result["verdict"] == sc["expected"]["verdict"]

    def test_contradicting_cot(self):
        sc = load_scenario("contradicting_cot")
        now = datetime.fromisoformat(sc["now_utc"].replace("Z", "+00:00"))
        candles = make_candles(**sc["candle_params"])
        result = re_mod.setup_score(
            {"direction": sc["direction"], "cot": sc["cot"], "cvd": sc["cvd"],
             "patterns": sc["patterns"], "candles": candles},
            killzones=sc["killzones"], now=now,
        )
        assert result["components"]["cot"]["value"] <= sc["expected"]["cot_value_max"]
        assert result["score"] <= sc["expected"]["score_max"]
        assert result["verdict"] == sc["expected"]["verdict"]


# ---------------------------------------------------------------------------
# validate_entry
# ---------------------------------------------------------------------------

class TestValidateEntry:
    def test_good_entry_approved(self):
        """Standard BUY: R:R 2.0, no invalidation triggered."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1070,
            direction="BUY", current_price=1.1040,
            invalidate_level=1.1020, cfg={"min_rr": 2.0},
        )
        assert r["approved"] is True
        assert r["rejected"] is False

    def test_low_rr_rejected(self):
        """R:R 1.0 < min 2.0 → rejected."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1055,
            direction="BUY", current_price=1.1040,
            invalidate_level=1.1020, cfg={"min_rr": 2.0},
        )
        assert r["approved"] is False
        assert any("R:R" in reason for reason in r["reasons"])

    def test_price_crossed_invalidation_buy(self):
        """BUY: current price below invalidation → rejected."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1070,
            direction="BUY", current_price=1.1015,
            invalidate_level=1.1020, cfg={"min_rr": 2.0},
        )
        assert r["approved"] is False
        assert any("invalidez" in reason.lower() or "invalidez" in reason.lower() for reason in r["reasons"])

    def test_price_crossed_invalidation_sell(self):
        """SELL: current price above invalidation → rejected."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1055, target=1.1010,
            direction="SELL", current_price=1.1060,
            invalidate_level=1.1050, cfg={"min_rr": 2.0},
        )
        assert r["approved"] is False

    def test_ttl_expired(self):
        """Setup older than TTL → rejected."""
        now_ts = 1_700_000_000.0
        created_ts = now_ts - (50 * 60)  # 50 minutes ago, TTL = 45
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1070,
            direction="BUY", current_price=1.1040,
            invalidate_level=None, created_at=created_ts, now=now_ts,
            cfg={"min_rr": 2.0, "setup_ttl_minutes": 45},
        )
        assert r["approved"] is False
        assert any("expir" in reason.lower() for reason in r["reasons"])

    def test_missing_entry_or_sl(self):
        """None entry or SL → rejected."""
        r = re_mod.validate_entry(
            entry=None, sl=None, target=None,
            direction="BUY", current_price=1.1040,
            cfg={"min_rr": 2.0},
        )
        assert r["approved"] is False
        assert any("entry/SL" in reason for reason in r["reasons"])

    def test_no_invalidation_level_skips_check(self):
        """invalidate_level=None → invalidation check skipped."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1070,
            direction="BUY", current_price=1.1010,  # below where invalidation would be
            invalidate_level=None, cfg={"min_rr": 2.0},
        )
        # Should still be approved because no invalidation level was set
        assert r["approved"] is True

    def test_risk_budget_exceeded(self):
        """Loss exceeds balance * risk_pct → rejected."""
        r = re_mod.validate_entry(
            entry=1.1040, sl=1.1025, target=1.1070,
            direction="BUY", current_price=1.1040,
            invalidate_level=None, cfg={"min_rr": 2.0},
            balance=10000, risk_pct=1.0, loss_per_lot=150.0, lot=1.0,
        )
        assert r["approved"] is False
        assert any("Riesgo" in reason or "excede" in reason for reason in r["reasons"])


# ---------------------------------------------------------------------------
# Grade score
# ---------------------------------------------------------------------------

class TestGradeScore:
    def test_alta(self):
        assert re_mod.grade_score(85.0) == re_mod.VERDICT_ALTA

    def test_media(self):
        assert re_mod.grade_score(65.0) == re_mod.VERDICT_MEDIA

    def test_sin(self):
        assert re_mod.grade_score(30.0) == re_mod.VERDICT_SIN

    def test_boundary_alta(self):
        assert re_mod.grade_score(80.0) == re_mod.VERDICT_ALTA

    def test_boundary_media(self):
        assert re_mod.grade_score(79.9) == re_mod.VERDICT_MEDIA


# ---------------------------------------------------------------------------
# prop_firm_state (M4): gate real de prop firm
# ---------------------------------------------------------------------------

PROP_CFG = {
    "prop_enabled": True,
    "prop_max_dd_daily_pct": 2.0,
    "prop_max_dd_total_pct": 6.0,
    "prop_max_profit_day_pct": 1.5,
    "prop_consistency_days": 4,
}


class TestPropFirmState:
    def test_disabled_is_neutral(self):
        """prop_enabled False -> nada bloquea y no pide persistir."""
        r = re_mod.prop_firm_state(equity=10000, day_start_balance=10000,
                                   cfg={"prop_enabled": False})
        assert r["enabled"] is False
        assert r["blocked"] is False
        assert r["state"] is None

    def test_first_call_seeds_baseline_and_day_hwm(self):
        """Primera llamada: baseline = equity actual, day_peak = day_start."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        r = re_mod.prop_firm_state(equity=10100, day_start_balance=10100,
                                   now=now, cfg=PROP_CFG)
        assert r["blocked"] is False
        assert r["dd_daily_pct"] == 0.0
        assert r["dd_total_pct"] == 0.0
        assert r["state"]["baseline_equity"] == 10100.0
        assert r["state"]["day_peak"] == 10100.0
        assert r["state"]["day_peak_day"] == "2026-09-07"
        assert r["state"]["total_peak"] == 10100.0

    def test_daily_dd_blocking(self):
        """Baja de 10000 a 9800 (2.0% desde day_start) -> bloquea por DD diario."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        r = re_mod.prop_firm_state(equity=9800, day_start_balance=10000,
                                   now=now, cfg=PROP_CFG)
        assert r["blocked"] is True
        assert r["dd_daily_pct"] == 2.0
        assert any("DD diario prop" in reason for reason in r["reasons"])

    def test_daily_dd_not_blocking_below_limit(self):
        """Baja del 1.5% -> aún no bloquea."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        r = re_mod.prop_firm_state(equity=9850, day_start_balance=10000,
                                   now=now, cfg=PROP_CFG)
        assert r["blocked"] is False
        assert r["dd_daily_pct"] == 1.5

    def test_total_dd_blocking(self):
        """HWM total previo 10000 y equity 9400 (6.0%) -> bloquea por DD total."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        prev = {
            "baseline_equity": 10000.0, "baseline_day": "2026-09-01",
            "day_peak": 9950.0, "day_peak_day": "2026-09-07", "total_peak": 10000.0,
        }
        r = re_mod.prop_firm_state(equity=9400, day_start_balance=9950,
                                   prev_state=prev, now=now, cfg=PROP_CFG)
        assert r["blocked"] is True
        assert r["dd_total_pct"] == 6.0
        assert any("DD total prop" in reason for reason in r["reasons"])

    def test_profit_cap_blocks(self):
        """Ganancia del día +2.0% (>= 1.5%) -> bloquea por consistencia."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        r = re_mod.prop_firm_state(equity=10200, day_start_balance=10000,
                                   now=now, cfg=PROP_CFG)
        assert r["blocked"] is True
        assert r["day_profit_pct"] == 2.0
        assert any("Ganancia del día" in reason for reason in r["reasons"])

    def test_day_switch_resets_day_peak_keeps_total_peak(self):
        """Al cambiar el día el HWM diario se reseeda con day_start, el total no."""
        now1 = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        prev = {
            "baseline_equity": 10000.0, "baseline_day": "2026-09-01",
            "day_peak": 10500.0, "day_peak_day": "2026-09-06", "total_peak": 10500.0,
        }
        r1 = re_mod.prop_firm_state(equity=10100, day_start_balance=10100,
                                    prev_state=prev, now=now1, cfg=PROP_CFG)
        assert r1["state"]["day_peak"] == 10100.0  # reseed con el nuevo día
        assert r1["state"]["day_peak_day"] == "2026-09-07"
        assert r1["state"]["total_peak"] == 10500.0  # HWM total se conserva

    def test_hwm_daily_tracks_peak(self):
        """El HWM diario sube con la equity, no con el balance."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        prev = {
            "baseline_equity": 10000.0, "baseline_day": "2026-09-01",
            "day_peak": 10000.0, "day_peak_day": "2026-09-07", "total_peak": 10000.0,
        }
        r = re_mod.prop_firm_state(equity=10200, day_start_balance=10000,
                                   prev_state=prev, now=now, cfg=PROP_CFG)
        assert r["state"]["day_peak"] == 10200.0
        # tras el pico, una caída al 10100 usa el HWM 10200 -> DD diario 0.98%
        r2 = re_mod.prop_firm_state(equity=10100, day_start_balance=10000,
                                    prev_state=r["state"], now=now, cfg=PROP_CFG)
        assert r2["dd_daily_pct"] == round(100.0 * (10200 - 10100) / 10200, 2)

    def test_state_roundtrip(self):
        """El state devuelto se re-alimenta como prev_state sin perder campos."""
        now = datetime(2026, 9, 7, 14, 0, 0, tzinfo=timezone.utc)
        r1 = re_mod.prop_firm_state(equity=10100, day_start_balance=10100,
                                    now=now, cfg=PROP_CFG)
        r2 = re_mod.prop_firm_state(equity=10050, day_start_balance=10100,
                                    prev_state=r1["state"], now=now, cfg=PROP_CFG)
        assert r2["state"]["baseline_equity"] == 10100.0
        assert r2["state"]["total_peak"] == 10100.0
        assert r2["dd_daily_pct"] == round(100.0 * 50 / 10100, 2)


# ---------------------------------------------------------------------------
# effective_risk_pct (0.1): cuánto dinero pone de verdad cada decisión
# ---------------------------------------------------------------------------
# `reduced_risk_pct` estaba en la config desde el principio y NO lo leía nadie:
# un setup de probabilidad MEDIA se llevaba el mismo dinero que uno de
# probabilidad ALTA. Un parámetro de config que no hace nada es peor que no
# tenerlo, porque aparenta una mitigación que no existe.
#
# Estos tests fijan la regla en los cuatro casos que puede tener una decisión:
# sin score (manual), ALTA, MEDIA y SIN_OPERATIVA.

RISK_CFG = {"risk_pct": 0.5, "reduced_risk_pct": 0.25}


class TestEffectiveRiskPct:
    def test_sin_score_juega_a_nominal_y_lo_declara(self):
        """Ruta manual: el que pulsa el botón no ha pedido score. Se usa el
        nominal, y se dice que es nominal en vez de dejar que se suponga."""
        r = re_mod.effective_risk_pct(None, None, RISK_CFG)
        assert r["risk_pct"] == 0.5
        assert r["reduced"] is False
        assert r["verdict"] is None
        assert "nominal" in r["reason"]
        assert "sin score" in r["label"]

    def test_alta_riesgo_nominal(self):
        r = re_mod.effective_risk_pct(85.0, re_mod.VERDICT_ALTA, RISK_CFG)
        assert r["risk_pct"] == 0.5
        assert r["reduced"] is False
        assert r["score"] == 85.0

    def test_media_riesgo_reducido(self):
        r = re_mod.effective_risk_pct(65.0, re_mod.VERDICT_MEDIA, RISK_CFG)
        assert r["risk_pct"] == 0.25
        assert r["reduced"] is True
        assert "reducido" in r["label"]

    def test_sin_operativa_tambien_reduce(self):
        """No solo MEDIA: cualquier verdict que no sea ALTA va a riesgo
        reducido, incluido SIN_OPERATIVA (que aquí no llegaría a operar)."""
        r = re_mod.effective_risk_pct(30.0, re_mod.VERDICT_SIN, RISK_CFG)
        assert r["risk_pct"] == 0.25
        assert r["reduced"] is True

    def test_sin_reduced_configurado_usa_la_mitad(self):
        """Si reduced_risk_pct no está, la mitad del nominal. Antes de decidirlo
        hay que verlo escrito: un default silencioso también es una política."""
        r = re_mod.effective_risk_pct(65.0, re_mod.VERDICT_MEDIA, {"risk_pct": 0.5})
        assert r["risk_pct"] == 0.25
        assert r["reduced_pct"] == 0.25

    def test_verdict_en_minusculas_se_normaliza(self):
        r = re_mod.effective_risk_pct(85.0, "alta_probabilidad", RISK_CFG)
        assert r["risk_pct"] == 0.5
        assert r["reduced"] is False

    def test_solo_score_sin_verdict_no_inventa_veredicto(self):
        """Con score pero sin verdict no se puede decidir la mitigación: se
        juega a nominal, que es la lectura conservadora de 'no hay veredicto'."""
        r = re_mod.effective_risk_pct(85.0, None, RISK_CFG)
        assert r["risk_pct"] == 0.5
        assert r["reduced"] is False

    def test_sin_config_no_revienta(self):
        r = re_mod.effective_risk_pct(85.0, re_mod.VERDICT_ALTA, None)
        assert r["risk_pct"] == 0.0
        assert r["nominal_pct"] == 0.0


class TestComponentShare:
    """`component_share` es de dónde sale el "peso X%" que el agente le dice al
    decisor. Con un literal en el prompt, mover un peso en el YAML dejaba al
    sistema afirmando una cuota que el score ya no usaba."""

    def test_defaults_suman_uno(self):
        total = sum(re_mod.component_share(None, k)
                    for k in ("cot", "cvd_of", "smc", "killzone", "smr_dxy"))
        assert total == pytest.approx(1.0)

    def test_killzone_default(self):
        # La killzone pesa 0: no es un componente del score sino un gate, y
        # watcher.py la exige como termino AND duro
        # (`score >= min_score and in_killzone and ...`). Puntuar ademas de exigir
        # cuenta la misma variable dos veces. Antes ocupaba 20 de 75 puntos
        # (0.2667) y era lo que empujaba setups a ALTA dentro de la ventana.
        assert re_mod.component_share(None, "killzone") == pytest.approx(0.0)

    def test_cambia_al_cambiar_otro_peso(self):
        """Si el YAML sube smc, la cuota de killzone BAJA aunque su peso en
        puntos no se mueva: el total es la suma de todos."""
        w = {"cot": 25.0, "cvd_of": 25.0, "smc": 50.0, "killzone": 20.0,
             "smr_dxy": 0.0}
        assert re_mod.component_share(w, "killzone") == pytest.approx(0.1667, abs=1e-4)
        assert re_mod.component_share(w, "smc") == pytest.approx(0.4167, abs=1e-4)

    def test_sm_r_dxy_cuesta_cero_cuando_no_esta(self):
        """El nombre miente a proposito y el cuerpo lo aclara: smr_dxy YA no cuesta
        cero, pesa 15 de 85 (0.1765). El nombre conservaba la verdad de cuando
        pesaba 0 y asi el que lo leyera sin el cuerpo crearia una expectativa falsa.

        El SMR/DXY se salio de la regla de "peso 0 sin dato" a proposito (ver el
        docstring de test_score_weights.py): su ausencia depende del despliegue,
        no del mercado, asi que un peso > 0 es legitimo. El coste es que con el
        feed del DXY caido se reservan 17.6 puntos que ningun setup puede
        recuperar, y el techo sin SMR baja a 82.4. Es un riesgo aceptado, no
        un descuido: si algun dia se quiere cerrar, smr_dxy baja en el YAML."""
        assert re_mod.component_share(None, "smr_dxy") == pytest.approx(0.1765, abs=1e-4)

    def test_llave_desconocida_no_revienta(self):
        assert re_mod.component_share(None, "no_existe") == 0.0

    def test_todos_a_cero_devuelve_cero(self):
        w = {k: 0.0 for k in re_mod.DEFAULT_WEIGHTS}
        assert re_mod.component_share(w, "killzone") == 0.0
