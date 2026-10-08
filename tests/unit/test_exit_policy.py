"""`core/exit_policy.py`: el trailing del CTA en forma pura (F5, D-078).

Tres cosas se prueban aquí, y ninguna es "que el código exista":

1. **Paridad con la convalidación.** `trailing_stop` tiene que devolver EXACTAMENTE
   lo que devolvería `research.cta.update_trail` con los mismos números: el
   trailing que corrió en el backtest de F2 (D-073/D-074) y el que moverá el stop
   de una posición real son la MISMA regla o no son el mismo sistema.
2. **Que nunca afloja.** El ratchet es la garantía entera del chandelier: un stop
   que baja cuando el precio retrocede devuelve el recorrido ganado. Se comprueba
   con secuencias de extremos, no con un caso aislado.
3. **Que no inventa un stop.** Sin stop previo (el `sl = 0` del bróker) no hay
   nada que ratchear y la respuesta es `None`: este módulo coloca stops en la
   apertura (`cta.chandelier`), no sobre posiciones abiertas.

Lo que AÑADE este módulo sobre el motor —el vocabulario BUY/SELL y el registro de
perfiles— es lo que no puede probar `research/cta.py`, y por eso vive aparte.
"""

from __future__ import annotations

import pytest

from core import exit_policy
from research import cta

# Un caso con números redondos: entrada 1.0500, ATR 0.0040, mult 3.0 → el
# chandelier inicial es 1.0380 (long) y el candidato de un extremo en 1.0520 es
# 1.0400. Con esos números se puede verificar un cálculo a mano en la revisión.
ENTRY = 1.05
ATR = 0.004
MULT = 3.0


# ---------------------------------------------------------------------------
# policy_for: el registro de perfiles
# ---------------------------------------------------------------------------


class TestPolicyFor:
    @pytest.mark.parametrize("perfil", ["cta", "CTA", " Cta "])
    def test_el_cta_usa_chandelier(self, perfil):
        assert exit_policy.policy_for(perfil) == exit_policy.POLICY_CHANDELIER

    @pytest.mark.parametrize("perfil", ["default", "copilot", "watcher", "otro", None, "", "   "])
    def test_cualquier_otro_perfil_es_static(self, perfil):
        """Mover stops es lo que NO se hace por defecto: el perfil tiene que estar declarado."""
        assert exit_policy.policy_for(perfil) == exit_policy.POLICY_STATIC

    def test_el_cta_es_el_unico_perfil_con_trailing(self):
        """Si un perfil nuevo aparece aquí sin convalidación, este test lo hace visible."""
        assert exit_policy.CHANDELIER_PROFILES == frozenset({"cta"})


# ---------------------------------------------------------------------------
# trailing_stop: paridad con la convalidación
# ---------------------------------------------------------------------------


class TestParidadConElMotor:
    @pytest.mark.parametrize("direccion", ["long", "short", "BUY", "SELL"])
    @pytest.mark.parametrize("stop", [1.0380, 1.0400, 1.0555])
    @pytest.mark.parametrize("ref,atr", [(1.0520, ATR), (1.0455, 0.0061), (1.0600, 0.0009)])
    def test_devuelve_lo_mismo_que_update_trail(self, direccion, stop, ref, atr):
        """Paridad con la única diferencia declarada: sin mejora, el motor devuelve
        el stop viejo (la memoria del ratchet) y aquí eso es `None`, que es la
        señal de "no hay nada que mandar al bróker".
        """
        motor_dir = "long" if direccion in ("long", "BUY") else "short"
        motor = cta.update_trail(motor_dir, stop, ref, atr, MULT)
        esperado = None if motor == float(stop) else motor

        assert exit_policy.trailing_stop(direccion, stop, ref, atr, MULT) == esperado

    def test_el_candidato_inicial_tambien_pasa_por_el_motor(self):
        """Un stop recién colocado (chandelier de la apertura) ratchea igual."""
        esperado = cta.update_trail("long", ENTRY - MULT * ATR, 1.052, ATR, MULT)

        assert exit_policy.trailing_stop("BUY", ENTRY - MULT * ATR, 1.052, ATR, MULT) == esperado


# ---------------------------------------------------------------------------
# trailing_stop: la garantía de "nunca afloja"
# ---------------------------------------------------------------------------


class TestNuncaAfloja:
    def test_un_long_sube_con_el_extremo_y_no_baja_con_el_retroceso(self):
        stop = exit_policy.trailing_stop("BUY", 1.0380, 1.0520, ATR, MULT)
        assert stop == pytest.approx(1.04)

        # El precio retrocede: el extremo de la barra cerrada queda por DEBAJO del
        # candidato y el stop tiene que quedarse donde estaba.
        despues = exit_policy.trailing_stop("BUY", stop, 1.0450, ATR, MULT)
        assert despues is None

    def test_un_short_baja_con_el_extremo_y_no_sube_con_el_retroceso(self):
        stop = exit_policy.trailing_stop("SELL", 1.0620, 1.0480, ATR, MULT)
        assert stop == pytest.approx(1.06)

        despues = exit_policy.trailing_stop("SELL", stop, 1.0550, ATR, MULT)
        assert despues is None

    def test_una_secuencia_de_extremos_es_monotona(self):
        """La propiedad completa: la serie de stops jamás se mueve en el mal sentido."""
        stops = []
        actual = 1.0380
        for extremo in [1.0500, 1.0540, 1.0520, 1.0560, 1.0490, 1.0600]:
            nuevo = exit_policy.trailing_stop("BUY", actual, extremo, ATR, MULT)
            if nuevo is not None:
                actual = nuevo
            stops.append(actual)

        assert stops == sorted(stops)

    def test_sin_cambio_devuelve_none_y_no_el_stop_viejo(self):
        """`None` es la señal de "no hay nada que mandar al bróker"."""
        stop = exit_policy.trailing_stop("long", 1.0400, 1.0450, ATR, MULT)

        assert stop is None


# ---------------------------------------------------------------------------
# trailing_stop: lo que no hace
# ---------------------------------------------------------------------------


class TestNoInventaStops:
    @pytest.mark.parametrize("stop", [0, 0.0, -1.0, None])
    def test_sin_stop_previo_no_hay_nada_que_ratchear(self, stop):
        """El `sl = 0` del bróker es "esta posición no tiene stop", no "stop en 0"."""
        assert exit_policy.trailing_stop("BUY", stop, 1.0520, ATR, MULT) is None
        assert exit_policy.trailing_stop("SELL", stop, 1.0480, ATR, MULT) is None

    @pytest.mark.parametrize("direccion", ["HOLD", "buy y sell", "", None])
    def test_una_direccion_desconocida_revienta_en_vez_de_adivinar(self, direccion):
        with pytest.raises(ValueError) as exc:
            exit_policy.trailing_stop(direccion, 1.04, 1.05, ATR, MULT)

        assert "dirección desconocida" in str(exc.value)

    def test_los_dos_vocabularios_son_equivalentes(self):
        """BUY y long son la misma orden vista desde el motor y desde el bróker."""
        largo_motor = exit_policy.trailing_stop("long", 1.0380, 1.0520, ATR, MULT)
        largo_broker = exit_policy.trailing_stop("BUY", 1.0380, 1.0520, ATR, MULT)
        corto_motor = exit_policy.trailing_stop("short", 1.0620, 1.0480, ATR, MULT)
        corto_broker = exit_policy.trailing_stop("SELL", 1.0620, 1.0480, ATR, MULT)

        assert largo_motor == largo_broker
        assert corto_motor == corto_broker
        assert largo_motor != corto_motor
