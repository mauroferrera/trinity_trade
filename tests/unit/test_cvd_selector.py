"""Selector de fuente CVD (orderflow_engine.pick_cvd_source): puro y sin MT5.

Verifica que el snapshot etiquete la fuente REAL: la serie live solo gana si hay
suficientes puntos recientes; si no, cae a la sintética (con warning cuando la
feed está conectada pero no aporta) y nunca miente en el label.

Cambio respecto a la referencia: las etiquetas se INYECTAN. Antes vivían
hardcodeadas como "live (Databento 6E)" / "synthetic (tick volume MT5)", que
mentían en cuanto el feed no era Databento o el símbolo no era 6E. Aquí el
c llamador dice la verdad porque la sabe, y los tests lo comprueban.

Run:  python -m pytest tests/unit/test_cvd_selector.py -v
"""

from __future__ import annotations

import pytest

from core.orderflow_engine import pick_cvd_source


def _pts(n):
    return [{"time": 1_000_000 + i * 900, "value": round(float(i), 2)} for i in range(n)]


# Etiquetas que usa el llamador real para 6E + Databento.
LIVE = "live (Databento 6E)"
SYNTH = "synthetic (tick volume MT5)"


class TestPickCvdSource:
    def test_uses_live_when_sufficient(self):
        live, syn = _pts(8), _pts(4)
        points, source, warning = pick_cvd_source(
            live, syn, live_detail="Databento 6E", synthetic_detail="tick volume MT5")
        assert points == live
        assert source == LIVE
        assert warning is None

    def test_falls_back_when_live_insufficient(self):
        live, syn = _pts(2), _pts(6)
        points, source, warning = pick_cvd_source(
            live, syn, live_detail="Databento 6E", synthetic_detail="tick volume MT5")
        assert points == syn
        assert source == SYNTH
        assert warning is not None and "live" in warning

    def test_synthetic_without_feed_has_no_warning(self):
        syn = _pts(6)
        points, source, warning = pick_cvd_source(
            None, syn, live_detail="Databento 6E", synthetic_detail="tick volume MT5")
        assert source == SYNTH
        assert warning is None

    def test_no_data_at_all(self):
        points, source, warning = pick_cvd_source(None, None)
        assert points is None
        assert source is None
        assert "sin datos" in warning

    def test_custom_min_points(self):
        """Con min_live_points=3, 3 puntos live ya alcanzan."""
        live, syn = _pts(3), _pts(5)
        points, source, _ = pick_cvd_source(
            live, syn, min_live_points=3, live_detail="Databento 6E")
        assert source == LIVE
        assert points == live

    def test_synthetic_data_is_not_mutated(self):
        live = None
        syn = _pts(4)
        _, _, _ = pick_cvd_source(live, syn)
        assert syn == _pts(4)


class TestLabelsAreNotHardcoded:
    """La etiqueta describe lo que el llamador sabe, no un supuesto global."""

    def test_label_reflects_other_feed(self):
        live = _pts(9)
        _, source, _ = pick_cvd_source(live, None, live_detail="B3 MDP3 2024")
        assert source == "live (B3 MDP3 2024)"
        assert "Databento" not in source

    def test_label_without_detail_is_bare(self):
        _, source, _ = pick_cvd_source(_pts(9), None)
        assert source == "live"

    def test_default_labels_are_bare(self):
        """Sin detalles inyectados no se inventa el proveedor."""
        _, source, _ = pick_cvd_source(_pts(9), _pts(2))
        assert source == "live"