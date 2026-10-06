"""El gate del setup, sin terminal y sin HTTP.

Lo que se prueba aquí es la aritmética que en REF vivía dentro de `watcher.py` y que
salía a la UI sin que nadie la comprobara: qué zona de entrada se elige, cuándo se
rechaza por lejana, y qué hace el gate cuando falta la distancia de SL.

La regla de los tests: cada aserción dice POR QUÉ el valor importa. Un
`assert sl == 1.0848` solo congela un número; `assert sl == entry - dist` congela la
regla, y la regla sigue valiendo si mañana el SL pasa a ser otro número.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest

from core import setup_gate


def _snapshot(
    direction: str = "BUY",
    score: float = 80.0,
    in_killzone: bool = True,
    precio: Optional[float] = 1.0850,
    zonas: Optional[Dict[str, Any]] = None,
    invalidacion: Optional[float] = None,
) -> Dict[str, Any]:
    """Un snapshot con la forma que produce `MT5Market.chart_snapshot`."""
    return {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "current_price": precio,
        "last_time": 1_700_000_000,
        "analysis": {"patterns": zonas or {}},
        "risk_engine": {
            "bull": {
                "score": score if direction == "BUY" else 10.0,
                "verdict": "setup",
                "components": {"killzone": {"in_killzone": in_killzone, "name": "London"}},
            },
            "bear": {
                "score": score if direction == "SELL" else 10.0,
                "verdict": "setup",
                "components": {"killzone": {"in_killzone": in_killzone, "name": "London"}},
            },
            "invalidation": {direction: invalidacion},
        },
    }


CONFIG = {"sl_distance": 0.0012, "min_score": 70.0, "tp_ratio_r": 2.0}


class TestEntryZone:
    def test_buy_agrega_la_zona_mas_alta_que_queda_debajo(self):
        """La de rebote, no la primera que aparece en la lista.

        Las dos zonas están debajo del precio, pero el mercado va a la más cercana:
        elegir la más lejana convertiría la zona en un precio que el mercado no
        visita.
        """
        zonas = {
            "fvgs": [
                {"type": "BULLISH_FVG", "top": 1.0700, "bottom": 1.0600},
                {"type": "BULLISH_FVG", "top": 1.0840, "bottom": 1.0800},
            ]
        }

        r = setup_gate.entry_zone(zonas, 1.0850, "BUY", 0.05)

        assert r["zone"]["price"] == 1.0800
        assert r["zone"]["kind"] == "BULLISH_FVG"
        assert r["reason"] == ""

    def test_sell_agrega_la_zona_mas_baja_que_queda_encima(self):
        zonas = {
            "order_blocks": [
                {"type": "BEARISH_OB", "top": 1.1200, "bottom": 1.1100},
                {"type": "BEARISH_OB", "top": 1.0900, "bottom": 1.0860},
            ]
        }

        r = setup_gate.entry_zone(zonas, 1.0850, "SELL", 0.05)

        assert r["zone"]["price"] == 1.0900
        assert r["zone"]["kind"] == "BEARISH_OB"

    def test_el_tipo_via_en_kind_para_no_depender_de_la_clave(self):
        """`fvgs` y `order_blocks` traen las MISMAS claves, así que la etiqueta sale
        del `type` del patrón. Si no se copiara, `kind` sería siempre "FVG" y el
        operador vería un order block dibujado como un hueco."""
        r = setup_gate.entry_zone(
            {"fvgs": [{"top": 1.09, "bottom": 1.08}]},
            1.0850,
            "BUY",
            0.05,
        )

        assert r["zone"]["kind"] == "FVG"
        r2 = setup_gate.entry_zone(
            {"order_blocks": [{"top": 1.09, "bottom": 1.08}]},
            1.0850,
            "BUY",
            0.05,
        )

        assert r2["zone"]["kind"] == "OB"

    def test_una_zona_del_lado_contrario_no_es_entrada(self):
        """Para un SELL, un order block por DEBAJO no es una zona de venta.

        Sin este filtro, el gate mediría la distancia hasta un precio que el mercado
        no va a buscar por el lado del SELL, y publicaría un plan imposible.
        """
        r = setup_gate.entry_zone(
            {"order_blocks": [{"top": 1.0700, "bottom": 1.0600}]},
            1.0850,
            "SELL",
            0.05,
        )

        assert r["zone"] is None
        assert r["reason"] == setup_gate.ZONE_NO_AXIS

    def test_lejos_se_rechaza_en_vez_de_caer_a_mercado(self):
        """El motivo es lo importante: si la zona existe pero no es alcanzable, la
        respuesta NO puede ser "entra a mercado", porque parecería aprobado."""
        r = setup_gate.entry_zone(
            {"fvgs": [{"top": 1.0400, "bottom": 1.0300}]},
            1.0850,
            "BUY",
            0.01,
        )

        assert r["zone"] is None
        assert r["reason"] == setup_gate.ZONE_TOO_FAR
        assert round(r["span"], 4) == 0.055

    def test_el_borde_exacto_no_se_rechaza_por_representacion_binaria(self):
        """`1.0850 - 1.0750` da `0.010000000000000009`. Sin `SPAN_EPS` el umbral de
        100 pips rechazaba una zona que está exactamente en el límite."""
        r = setup_gate.entry_zone(
            {"fvgs": [{"top": 1.0800, "bottom": 1.0750}]},
            1.0850,
            "BUY",
            setup_gate.ENTRY_ZONE_MAX_SPAN,
        )

        assert r["zone"] is not None

    def test_sin_precio_o_sin_patrones_no_inventa(self):
        assert setup_gate.entry_zone({}, 1.0850, "BUY", 0.05)["zone"] is None
        assert setup_gate.entry_zone({"fvgs": [{"top": 1.08, "bottom": 1.07}]}, None, "BUY", 0.05)["zone"] is None

    def test_ignora_lo_que_no_tiene_los_dos_bordes(self):
        """Un patrón a medio construir no es una zona. Si entrara con `bottom=None`,
        la comparación de precio reventaría con `TypeError` y la ruta devolvería 500."""
        r = setup_gate.entry_zone({"fvgs": [{"top": 1.08}, "basura"]}, 1.0850, "BUY", 0.05)

        assert r["zone"] is None
        assert r["reason"] == setup_gate.ZONE_NO_AXIS


class TestMaxZoneSpan:
    def test_es_un_multiplo_de_la_distancia_de_sl(self):
        """Dimensionless: 12 pips x8 en EURUSD, y en oro 8 x su stop, no 0.01."""
        assert setup_gate.max_zone_span({}, 0.0012) == pytest.approx(0.0012 * 8.0)

    def test_sin_distancia_de_sl_cae_al_absoludo_historico(self):
        assert setup_gate.max_zone_span({}, None) == setup_gate.ENTRY_ZONE_MAX_SPAN

    def test_la_config_puede_ajustar_el_multiplo(self):
        assert setup_gate.max_zone_span({"zone_max_span_sl_mult": 2.0}, 0.0012) == pytest.approx(0.0024)

    def test_un_multiplo_inutil_no_deja_el_span_en_cero(self):
        """`0` o un texto en la config no pueden volver el gate permisivo: si el
        límite cae a 0, TODA zona se rechaza y el sistema deja de operar en silencio."""
        for malo in (0, -1, "mucho", None):
            assert setup_gate.max_zone_span({"zone_max_span_sl_mult": malo}, 0.0012) > 0


class TestPlanLevels:
    def test_buy_sl_debajo_y_tp_encima_con_el_ratio(self):
        niveles = setup_gate.plan_levels(1.0850, "BUY", 0.0012, 2.0, digits=5)

        assert niveles == {"sl": 1.0838, "tp": 1.0874}

    def test_sell_es_el_espejo(self):
        niveles = setup_gate.plan_levels(1.0850, "SELL", 0.0012, 2.0, digits=5)

        assert niveles == {"sl": 1.0862, "tp": 1.0826}


    def test_sin_entrada_o_sin_distancia_no_inventa_un_stop(self):
        """El motivo de ser de esta función: un SL de 0.0012 en oro es un stop cien
        veces más corto de lo que el usuario cree, y se ve «medido»."""
        assert setup_gate.plan_levels(None, "BUY", 0.0012) == {"sl": None, "tp": None}
        assert setup_gate.plan_levels(1.0850, "BUY", None) == {"sl": None, "tp": None}
        assert setup_gate.plan_levels(1.0850, "BUY", 0) == {"sl": None, "tp": None}
        assert setup_gate.plan_levels(1.0850, "BUY", -0.0012) == {"sl": None, "tp": None}

    def test_redondea_a_los_digitos_del_simbolo(self):
        """Un nivel que no cuadra con el tick no es enviable: el bróker lo ajusta."""
        assert setup_gate.plan_levels(1.08503, "BUY", 0.001234, 2.0, digits=5)["sl"] == 1.0838


class TestKillzoneForcedNow:
    def test_usa_una_ventana_real_de_la_config(self):
        """`killzone=1` tiene que caer en una sesión que el operador reconoce, o el
        flag de test no probaría lo que dice que prueba."""
        momento = setup_gate.killzone_forced_now(
            [{"name": "London", "start": "07:00", "end": "10:00"}],
            now=datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc),
        )

        assert momento is not None
        assert (momento.hour, momento.minute) == (7, 30)
        assert momento.day == 5

    def test_no_cae_en_el_borde_de_la_ventana(self):
        """Un reloj parado en el borde exacto daría un resultado distinto del de uno
        que avanza."""
        momento = setup_gate.killzone_forced_now(
            [{"name": "NY", "start": "13:00", "end": "16:00"}],
            now=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
        )

        assert momento != datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)

    def test_sin_ventanas_parseables_no_tercampa_en_horas_inventadas(self):
        """Escribir horas a mano aquí pondría el score en una sesión que el usuario no
        tiene configurada, y el resultado sería del test, no del sistema."""
        assert setup_gate.killzone_forced_now([]) is None
        assert setup_gate.killzone_forced_now(None) is None
        assert setup_gate.killzone_forced_now([{"start": "siete"}]) is None
        assert setup_gate.killzone_forced_now([{"start": "99:00"}]) is None

    def test_salto_una_ventana_ilegible_y_usa_la_siguiente(self):
        momento = setup_gate.killzone_forced_now(
            [{"start": "no"}, {"start": "08:00"}],
            now=datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc),
        )

        assert momento is not None and momento.hour == 8


class TestEvaluateGate:
    def test_gana_el_lado_con_mas_score(self):
        """No es simétrico: el gate decide con el MEJOR de los dos lados, no con el
        que toca por la tendencia."""
        snap = _snapshot(direction="SELL", score=40.0)
        snap["risk_engine"]["bear"]["score"] = 90.0
        snap["risk_engine"]["bull"]["score"] = 40.0

        gate = setup_gate.evaluate_gate(snap, CONFIG)

        assert gate is not None
        assert gate["direction"] == "SELL"
        assert gate["score"] == 90.0

    def test_un_setup_completo_se_aprueba_y_publica_el_plan(self):
        gate = setup_gate.evaluate_gate(
            _snapshot(zonas={"fvgs": [{"type": "BULLISH_FVG", "top": 1.0840, "bottom": 1.0800}]}),
            CONFIG,
        )

        assert gate is not None
        assert gate["approved"] is True
        assert gate["reasons"] == []
        assert gate["entry_kind"] == "BULLISH_FVG"
        assert gate["entry"] == 1.0800
        assert gate["sl"] == 1.0788
        assert gate["tp"] == 1.0824
        assert gate["sl_distance_source"] == "global"

    def test_sin_tp_ratio_configurado_el_tp_no_cae_en_la_entrada(self):
        """Con 0 el TP se iría al propio precio de entrada: un plan que nunca alcanza
        su objetivo y que parece conservador."""
        cfg = {"sl_distance": 0.0012, "min_score": 70.0}

        gate = setup_gate.evaluate_gate(_snapshot(), cfg)

        assert gate is not None
        assert gate["tp"] == setup_gate.plan_levels(1.0850, "BUY", 0.0012, 2.0)["tp"]

    def test_fuera_de_killzone_no_aprueba_aunque_el_score_sea_alto(self):
        """Es un AND duro, no un factor: la ventana es acceso, no puntuación."""
        gate = setup_gate.evaluate_gate(_snapshot(score=99.0, in_killzone=False), CONFIG)

        assert gate is not None
        assert gate["approved"] is False
        assert "Fuera de killzone." in gate["reasons"]

    def test_score_bajo_el_umbral_lo_dice_con_los_numeros(self):
        gate = setup_gate.evaluate_gate(_snapshot(score=40.0), CONFIG)

        assert gate is not None
        assert gate["approved"] is False
        assert any("40.0" in r and "70" in r for r in gate["reasons"])

    def test_sin_distancia_de_sl_se_rechaza_y_lo_explica(self):
        """Un SL de 0.0012 en oro es una stop de risa. Antes que inventar, el gate
        rechaza y nombra la clave que hay que rellenar."""
        gate = setup_gate.evaluate_gate(_snapshot(), {"min_score": 70.0})

        assert gate is not None
        assert gate["approved"] is False
        assert gate["sl"] is None and gate["tp"] is None
        assert any("sl_distance" in r for r in gate["reasons"])

    def test_el_origen_de_la_distancia_del_simbolo_manda(self):
        cfg = {"sl_distance": 0.0012, "sl_distance_by_symbol": {"EURUSD": 0.0020}}

        gate = setup_gate.evaluate_gate(_snapshot(), cfg)

        assert gate is not None
        assert gate["sl_distance"] == 0.0020
        assert gate["sl_distance_source"] == "symbol"

    def test_min_score_de_la_peticion_gana_pero_se_declara(self):
        """Un umbral distinto del del YAML que solo consta en la URL es un umbral que
        no se puede auditar después: por eso vuelve en `min_score_used`."""
        gate = setup_gate.evaluate_gate(_snapshot(score=50.0), CONFIG, min_score=40.0)

        assert gate is not None
        assert gate["min_score_used"] == 40.0
        assert gate["approved"] is True

    def test_una_zona_lejana_rechaza_en_vez_de_entrar_a_mercado(self):
        gate = setup_gate.evaluate_gate(
            _snapshot(zonas={"fvgs": [{"type": "BULLISH_FVG", "top": 1.0100, "bottom": 1.0000}]}),
            CONFIG,
        )

        assert gate is not None
        assert gate["approved"] is False
        assert gate["entry_zone_reject"] == setup_gate.ZONE_TOO_FAR
        assert any("demasiado" in r or "lejana" in r or "alcanzable" in r for r in gate["reasons"])

    def test_sin_zona_cae_a_entrada_a_mercado_y_lo_declara(self):
        gate = setup_gate.evaluate_gate(_snapshot(), CONFIG)

        assert gate is not None
        assert gate["entry"] == 1.0850
        assert gate["entry_kind"] == "market"
        assert gate["entry_zone_reject"] == setup_gate.ZONE_NO_AXIS

    def test_un_fallo_del_motor_de_validacion_es_un_rechazo_no_una_excepcion(self):
        """El veredicto cae a `approved: False` con el motivo. Un `500` aquí sería un
        fallo de infraestructura pintado como si fuera de trading."""

        def revienta(*_a: Any, **_k: Any) -> Dict[str, Any]:
            raise RuntimeError("boom")

        original = setup_gate.risk_engine.validate_entry
        setup_gate.risk_engine.validate_entry = revienta
        try:
            gate = setup_gate.evaluate_gate(_snapshot(), CONFIG)
        finally:
            setup_gate.risk_engine.validate_entry = original

        assert gate is not None
        assert gate["approved"] is False
        assert any("risk_engine error" in r for r in gate["reasons"])

    def test_sin_scores_devuelve_none_y_no_un_veredicto_inventado(self):
        assert setup_gate.evaluate_gate({"symbol": "EURUSD"}, CONFIG) is None
        assert setup_gate.evaluate_gate({"risk_engine": {"bull": None, "bear": None}}, CONFIG) is None

    def test_sin_precio_no_planifica_una_entrada_de_mercado_falsa(self):
        """Sin cotización no hay "precio actual" del que caer a mercado: devolver un
        plan con entry None parece un setup sin entrada en vez de uno sin datos."""
        gate = setup_gate.evaluate_gate(_snapshot(precio=None), CONFIG)

        assert gate is not None
        assert gate["entry"] is None
        assert gate["sl"] is None and gate["tp"] is None
        assert gate["approved"] is False

    def test_la_invalidez_estructural_via_en_el_plano(self):
        gate = setup_gate.evaluate_gate(_snapshot(invalidacion=1.0500), CONFIG)

        assert gate is not None
        assert gate["invalidate_level"] == 1.0500
