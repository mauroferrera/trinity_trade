"""Tests deterministas del motor de Mean Reversion VWAP (`research/mr.py`, F3).

La aritmética de la convalidación se fija aquí con datos sintéticos de mano
(expectativas calculables a lápiz) ANTES de que ningún backtest toque datos
reales. Misma regla de oro que en F2: primero tests, después veredicto.

Run:  python -m pytest tests/unit/test_mr.py -v
"""

from __future__ import annotations

import pytest

from research import mr


class TestVWAP:
    def test_precio_tipico(self):
        assert mr.typical_price([10], [8], [9]) == [9.0]

    def test_warmup_hasta_n_menos_1(self):
        v = mr.vwap([1, 2, 3], [0, 1, 2], [1, 2, 3], [1, 1, 1], n=4)
        assert v == [None, None, None]

    def test_volumen_constante_es_media(self):
        out = mr.vwap(
            highs=[12, 14, 16, 18], lows=[8, 10, 12, 14],
            closes=[10, 12, 14, 16], volumes=[1, 1, 1, 1], n=3,
        )
        # tp = (h+l+c)/3 = [10, 12, 14, 16]
        assert out[0] is None and out[1] is None
        assert out[2] == pytest.approx(12.0)
        assert out[3] == pytest.approx(14.0)

    def test_ponderacion_por_volumen(self):
        out = mr.vwap(
            highs=[10, 20], lows=[10, 20], closes=[10, 20],
            volumes=[1, 3], n=2,
        )
        # tp = [10, 20]; vwap = (10*1 + 20*3) / 4 = 17.5, no la media 15.
        assert out[1] == pytest.approx(17.5)

    def test_volumen_cero_no_contamina(self):
        out = mr.vwap(
            highs=[10, 20, 30], lows=[10, 20, 30], closes=[10, 20, 30],
            volumes=[0, 1, 1], n=3,
        )
        assert out[2] == pytest.approx(25.0)

    def test_n_invalido_lanza(self):
        with pytest.raises(ValueError):
            mr.vwap([1], [1], [1], [1], n=0)


class TestBands:
    def test_bandas(self):
        sup, inf = mr.bands([None, 100.0, 100.0], [None, 2.0, 2.0], k=1.5)
        assert sup == [None, 103.0, 103.0]
        assert inf == [None, 97.0, 97.0]


class TestMRSignals:
    def test_direcciones(self):
        closes = [100, 104, 101, 95, 100]
        sup = [None, 103, 103, 103, 103]
        inf = [None, 97, 97, 97, 97]
        assert mr.mr_signals(closes, sup, inf) == [("short", 1), ("long", 3)]

    def test_cierre_igual_a_banda_no_dispara(self):
        assert mr.mr_signals([103.0], [103.0], [97.0]) == []
        assert mr.mr_signals([97.0], [103.0], [97.0]) == []

    def test_banda_none_no_sena(self):
        assert mr.mr_signals([100], [None], [None]) == []


class TestSimulateMR:
    def test_long_tp_en_vwap(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=108.0, sl=97.0,
            opens=[100], highs=[110], lows=[99], closes=[105], max_hold=5,
        )
        assert r["closed_by"] == "TP"
        assert r["exit_raw"] == pytest.approx(108.0)
        assert r["exit_i"] == 0.0
        assert r["mfe"] == pytest.approx(10.0)
        assert r["mae"] == pytest.approx(-1.0)

    def test_long_tp_con_hueco_llena_en_target(self):
        """Hueco a favor NO se acredita (cota conservadora, no optimista)."""
        r = mr.simulate_mr(
            "long", 0, 100.0, target=108.0, sl=97.0,
            opens=[112], highs=[115], lows=[111], closes=[113], max_hold=5,
        )
        assert r["closed_by"] == "TP"
        assert r["exit_raw"] == pytest.approx(108.0)

    def test_long_sl(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=110.0, sl=97.0,
            opens=[100], highs=[101], lows=[96], closes=[97], max_hold=5,
        )
        assert r["closed_by"] == "SL"
        assert r["exit_raw"] == pytest.approx(97.0)

    def test_long_sl_con_hueco_llena_peor(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=110.0, sl=97.0,
            opens=[95], highs=[98], lows=[94], closes=[96], max_hold=5,
        )
        assert r["closed_by"] == "SL"
        assert r["exit_raw"] == pytest.approx(95.0)

    def test_misma_barra_toca_ambos_sl_primero(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=108.0, sl=97.0,
            opens=[100], highs=[112], lows=[95], closes=[101], max_hold=5,
        )
        assert r["closed_by"] == "SL"
        assert r["exit_raw"] == pytest.approx(97.0)

    def test_short_tp_y_sl(self):
        base = dict(entry_i=0, entry=100.0, max_hold=5)
        tp = mr.simulate_mr(
            "short", target=95.0, sl=105.0, opens=[100],
            highs=[103], lows=[94], closes=[99], **base,
        )
        assert tp["closed_by"] == "TP"
        assert tp["exit_raw"] == pytest.approx(95.0)
        assert tp["mfe"] == pytest.approx(6.0)
        assert tp["mae"] == pytest.approx(-3.0)

        sl = mr.simulate_mr(
            "short", target=95.0, sl=105.0, opens=[108],
            highs=[109], lows=[107], closes=[108], **base,
        )
        assert sl["closed_by"] == "SL"
        assert sl["exit_raw"] == pytest.approx(108.0)  # hueco por encima del SL

    def test_max_hold_cierra_al_cierre_de_la_barra_limite(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=120.0, sl=90.0,
            opens=[100, 101, 102, 103], highs=[102, 103, 104, 105],
            lows=[99, 100, 101, 102], closes=[101, 102, 103, 104],
            max_hold=2,
        )
        assert r["closed_by"] == "MAX_HOLD"
        assert r["exit_raw"] == pytest.approx(102.0)
        assert r["exit_i"] == 1.0
        assert r["bars_held"] == 1.0

    def test_end_of_data_censurado(self):
        r = mr.simulate_mr(
            "long", 0, 100.0, target=120.0, sl=90.0,
            opens=[100, 101], highs=[102, 103], lows=[99, 100],
            closes=[101, 102], max_hold=100,
        )
        assert r["closed_by"] == "END_OF_DATA"
        assert r["exit_raw"] == pytest.approx(102.0)
        assert r["exit_i"] == 1.0
