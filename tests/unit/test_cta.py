"""Tests deterministas de los componentes CTA (`research/cta.py`, F2).

La aritmética de la convalidación se fija aquí con datos sintéticos de mano
(donde la expectativa se puede calcular a lápiz), antes de que ningún backtest
toque D1 real. Es la regla de oro de la fase: primero tests, después veredicto.

Run:  python -m pytest tests/unit/test_cta.py -v
"""

from __future__ import annotations

import pytest

from research import cta


class TestATR:
    def test_rango_constante_es_el_atr(self):
        highs = [2.0] * 17
        lows = [1.0] * 17
        closes = [1.5] * 17
        out = cta.atr(highs, lows, closes, n=14)
        assert out[:13] == [None] * 13
        for v in out[13:]:
            assert v == pytest.approx(1.0)

    def test_warmup_antes_de_n_minus_1(self):
        v = cta.atr([10.0] * 5, [9.0] * 5, [9.5] * 5, n=14)
        assert v == [None] * 5

    def test_suavizado_de_wilder(self):
        """Serie de 5 barras, n=3, esperada a lápiz.

        TR = [1, 2, 2, 2, 4]; atr[2] = media(1,2,2) = 5/3; luego RMA:
        atr[3] = (atr[2]*2 + 2)/3 = 16/9;  atr[4] = (atr[3]*2 + 4)/3 = 68/27.
        """
        highs = [10, 11, 12, 13, 10]
        lows = [9, 9, 10, 11, 8]
        closes = [9.5, 10.5, 11, 12, 9]
        out = cta.atr(highs, lows, closes, n=3)
        assert out[0:2] == [None, None]
        assert out[2] == pytest.approx(5.0 / 3.0)
        assert out[3] == pytest.approx(16.0 / 9.0)
        assert out[4] == pytest.approx(68.0 / 27.0)


class TestDonchian:
    def test_ventana_de_donchian(self):
        highs = [10, 11, 12, 13, 14]
        lows = [1, 2, 3, 4, 5]
        upper, lower = cta.donchian(highs, lows, n=3)
        assert upper[0:2] == [None, None]
        assert lower[0:2] == [None, None]
        assert upper[2] == 12 and lower[2] == 1
        assert upper[3] == 13 and lower[3] == 2
        assert upper[4] == 14 and lower[4] == 3


class TestBreakoutSignals:
    def test_senal_larga_con_canal_previo(self):
        closes = [100, 100, 105, 110, 120, 90]
        upper = [None, 110, 110, 110, 110, 110]
        lower = [None, 90, 90, 90, 90, 90]
        assert cta.breakout_signals(closes, upper, lower) == [("long", 4)]

    def test_senal_corta(self):
        closes = [100, 100, 95, 80]
        upper = [None, 115, 115, 115]
        lower = [None, 95, 95, 95]
        assert cta.breakout_signals(closes, upper, lower) == [("short", 3)]

    def test_el_breakout_usa_el_canal_sin_la_barra_actual(self):
        """El cierre de `i` se compara con el canal hasta `i-1`.

        Si el alto de la propia barra i engordara el canal, una barra que rompe
        hacia arriba al mismo tiempo que marca un mínimo interior se comería su
        propia señal: el alto de i no puede decidir si el cierre de i rompió.
        """
        closes = [100, 110, 105]
        upper = [105, None, None]  # canal hasta la barra 0 = 105
        lower = [90, None, None]
        # i=1: 110 > 105 -> long. i=2 no alcanza porque no hay canal previo.
        assert cta.breakout_signals(closes, upper, lower) == [("long", 1)]


class TestTrailing:
    def test_stop_inicial_del_chandelier(self):
        assert cta.chandelier("long", 100.0, 2.0, 3.0) == 94.0
        assert cta.chandelier("short", 100.0, 2.0, 3.0) == 106.0

    def test_el_trail_solo_ratcho(self):
        trail = cta.chandelier("long", 100.0, 2.0, 3.0)  # 94
        trail = cta.update_trail("long", trail, 102.0, 2.0, 3.0)  # 96
        assert trail == 96.0
        # Un nuevo "suelo" más bajo no puede aflojar el stop ganado.
        assert cta.update_trail("long", trail, 95.0, 2.0, 3.0) == 96.0

        s = cta.chandelier("short", 100.0, 2.0, 3.0)  # 106
        s = cta.update_trail("short", s, 98.0, 2.0, 3.0)  # 104
        assert s == 104.0
        assert cta.update_trail("short", s, 102.0, 2.0, 3.0) == 104.0

    def test_long_ganador_por_trail(self):
        opens = [100, 101, 102, 103]
        highs = [101, 103, 105, 103]
        lows = [99, 100, 102, 101]
        closes = [100.5, 102, 104, 102.5]
        atrs = [1.0, 1.0, 1.0, 1.0]
        r = cta.simulate_trade("long", 0, 100.0, opens, highs, lows, closes,
                               atrs, mult=2.0, atr_dec=1.0)
        # trail: 98 -> (101-2)=99 -> (103-2)=101 -> (105-2)=103; toca en barra 3.
        assert r["closed_by"] == "trail"
        assert r["exit_i"] == 3.0
        assert r["exit_raw"] == pytest.approx(103.0)
        assert r["bars_held"] == 3.0
        assert r["mfe"] == pytest.approx(5.0)
        assert r["mae"] == pytest.approx(-1.0)

    def test_long_stop_inicial_en_la_barra_de_entrada(self):
        opens = [100]
        highs = [100.5]
        lows = [97.0]
        closes = [98.0]
        r = cta.simulate_trade("long", 0, 100.0, opens, highs, lows, closes,
                               [1.0], mult=2.0, atr_dec=1.0)
        assert r["closed_by"] == "trail"
        assert r["exit_raw"] == pytest.approx(98.0)
        assert r["mae"] == pytest.approx(-3.0)

    def test_short_ganador_por_trail(self):
        opens = [100, 99, 98]
        highs = [100, 99, 98]
        lows = [98, 97, 96]
        closes = [99, 98, 97]
        r = cta.simulate_trade("short", 0, 100.0, opens, highs, lows, closes,
                               [1.0, 1.0, 1.0], mult=1.0, atr_dec=1.0)
        # trail: 101 -> (98+1)=99; high(1)=99 lo toca -> exit 99 en barra 1.
        assert r["closed_by"] == "trail"
        assert r["exit_i"] == 1.0
        assert r["exit_raw"] == pytest.approx(99.0)
        # mfe raw (rango de la barra de salida incluido, como 6E): low(1)=97.
        assert r["mfe"] == pytest.approx(3.0)
        assert r["mae"] == pytest.approx(0.0)

    def test_sin_toque_cierra_al_final_censurado(self):
        opens = [100, 101, 102]
        highs = [101, 102, 103]
        lows = [99, 100, 101]
        closes = [100.5, 101.5, 102.5]
        r = cta.simulate_trade("long", 0, 100.0, opens, highs, lows, closes,
                               [1.0, 1.0, 1.0], mult=3.0, atr_dec=1.0)
        assert r["closed_by"] == "END_OF_DATA"
        assert r["exit_raw"] == pytest.approx(102.5)
        assert r["exit_i"] == 2.0

    def test_sin_lookahead_en_el_trailing(self):
        """Una barra que dispara a 105 y cae a 97 se sale por el stop de ayer.

        Si el trail se ratcheara con el high de la barra 1 ANTES de evaluar su
        low, este trade se saldría a 103, no a 99: ese es exactamente el bug del
        lookahead que este test prohíbe.
        """
        opens = [100, 100, 100, 100]
        highs = [101, 105, 100, 100]
        lows = [99, 97, 99, 99]
        closes = [100, 100, 100, 100]
        r = cta.simulate_trade("long", 0, 100.0, opens, highs, lows, closes,
                               [1.0, 1.0, 1.0, 1.0], mult=2.0, atr_dec=1.0)
        assert r["closed_by"] == "trail"
        assert r["exit_i"] == 1.0
        assert r["exit_raw"] == pytest.approx(99.0)
        assert r["mfe"] == pytest.approx(5.0)