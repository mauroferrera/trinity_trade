"""Tests del normalizador de tamaño de posición (`core/lot_calculator.py`).

Lo que se verifica aquí no es la aritmética (es una división), sino las TRES
decisiones que un cálculo de lotes suele tomar mal en silencio:

1. Redondear hacia ABAJO. Un redondeo al alza puede devolver un tamaño cuyo riesgo
   real excede el autorizado, y como el número sigue siendo plausible nadie lo
   detecta hasta el post-mortem.
2. La unidad del mercado. Forex (100.000/lote), B3 minicontratos (punto/contrato)
   y cripto (nocional) no son la misma pregunta, aunque la fórmula sea idéntica.
3. El mínimo del broker. Cuando el cálculo da por debajo de un lote, se devuelve
   un lote y se marca `clamped_to_min`, porque un mínimo que sube el riesgo sin
   avisar es peor que un tamaño ligeramente grande.

Run:  python -m pytest tests/unit/test_lot_calculator.py -v
"""

from __future__ import annotations

import pytest

from core import lot_calculator as lc


# --- Specs de referencia ------------------------------------------------------
# EURUSD: pip = 0.00001, un lote estándar mueve 100.000 unidades, y cada pip de
# un lote vale 1.00 USD de la cuenta.
EURUSD = lc.LotSpec(lot_size=100_000, tick_value=1.0, tick_size=0.00001,
                    min_lot=0.01, lot_step=0.01)
# WIN mini B3: el contrato vale 5 puntos; cada punto del contrato = 0.20 USD.
WIN_MINI = lc.LotSpec(lot_size=1, tick_value=0.20, tick_size=5.0,
                      min_lot=1, lot_step=1)
# BTCUSDT perpetuo: tick de 1 USD con 0.01 USD por unidad.
BTC_PERP = lc.LotSpec(lot_size=1, tick_value=0.01, tick_size=1.0,
                      min_lot=0.001, lot_step=0.001)


class TestForexLots:
    def test_tamano_coherente_con_el_riesgo(self):
        # 120 ticks de SL a 1.00 USD/tick = 120 USD por lote. Con 100 USD de
        # riesgo caben 0.83 lotes (redondeado ABAJO a pasos de 0.01).
        r = lc.standard_forex_lots(100.0, 0.0012, EURUSD)
        assert r["lots"] == pytest.approx(0.83)
        assert r["clamped_to_min"] is False

    def test_riesgo_real_nunca_excede_al_autorizado(self):
        """La invariante que importa: riesgo real <= riesgo autorizado."""
        for sl in (0.0005, 0.0012, 0.0030, 0.0100):
            for risk in (25.0, 100.0, 400.0):
                r = lc.standard_forex_lots(risk, sl, EURUSD)
                sl_ticks = sl / EURUSD.tick_size
                real_risk = r["lots"] * sl_ticks * EURUSD.tick_value
                assert real_risk <= risk + 1e-9, f"SL={sl} risk={risk} -> {r}"

    def test_sl_mas_cercano_da_mas_lotes(self):
        cerca = lc.standard_forex_lots(100.0, 0.0005, EURUSD)["lots"]
        lejos = lc.standard_forex_lots(100.0, 0.0030, EURUSD)["lots"]
        assert cerca > lejos

    def test_mas_riesgo_mas_lotes(self):
        a = lc.standard_forex_lots(50.0, 0.0012, EURUSD)["lots"]
        b = lc.standard_forex_lots(100.0, 0.0012, EURUSD)["lots"]
        assert b > a

    def test_sl_cero_o_negativo_es_error(self):
        with pytest.raises(ValueError):
            lc.standard_forex_lots(100.0, 0.0, EURUSD)
        with pytest.raises(ValueError):
            lc.standard_forex_lots(100.0, -0.001, EURUSD)

    def test_sl_por_debajo_del_tick_no_inventa_riesgo(self):
        """Un SL menor que el tick hace el cálculo numérico inválido, no "barato".

        Con sl_distance=0.000001 y tick de 0.00001, sl_ticks sale 0.1 y el riesgo
        por lote 0.1 USD: el módulo devolvería 1000 lotes creyendo que son válidos.
        Un stop más fino que el tick no es colocable, así que se falla con un
        mensaje que lo diga, en vez de devolver un tamaño absurdo.
        """
        with pytest.raises(ValueError) as exc:
            lc.standard_forex_lots(100.0, 0.000001, EURUSD)
        assert "tick" in str(exc.value)

    def test_tick_value_cero_es_error(self):
        """Un spec sin valor de tick no puede calcular riesgo: no devuelve 0 lotes."""
        spec = lc.LotSpec(lot_size=100_000, tick_value=0.0, tick_size=0.00001)
        with pytest.raises(ValueError):
            lc.standard_forex_lots(100.0, 0.0012, spec)


class TestRiskPerUnit:
    """La función que la usa el ejecutor para contrastar el lote con el presupuesto.

    Existe como nombre público porque la respuesta la necesitan DOS sitios —el que
    dimensiona el lote y el que comprueba que ese lote cabe en el presupuesto— y
    tienen que dar la misma cifra. Si uno calculara el riesgo por su cuenta, una
    diferencia entre ambos no daría error: daría un lote dimensionado con una
    cuenta y validado contra otra, con los dos números redondeados y con aspecto de
    razonables.
    """

    def test_es_la_misma_cifra_que_devuelve_el_dimensionado(self):
        """La coherencia, en forma de test: no basta con que cada una acierte.

        Este es el invariante que importa. El ejecutor ataca `risk_per_unit` para
        validar el presupuesto, y `_lote_por_riesgo` usa `standard_forex_lots` para
        decidir el tamaño. Si divergieran en un factor, el gate de presupuesto
        compararía la pérdida de un lote medido con una cuenta contra un lote
        dimensionado con otra, y el fallo aparecería como un rechazo con un motivo
        que no lleva a ninguna parte.
        """
        for sl in (0.0005, 0.0010, 0.0012, 0.0030, 0.0100):
            dimensionado = lc.standard_forex_lots(100.0, sl, EURUSD)
            directo = lc.risk_per_unit(sl, EURUSD)
            assert directo == pytest.approx(dimensionado["risk_per_unit"]), \
                f"SL={sl}: {directo} != {dimensionado['risk_per_unit']}"

    def test_el_lot_size_no_se_multiplica_en_el_riesgo(self):
        """El invariante que se rompió: riesgo por lote = ticks × tick_value.

        En MT5, `SYMBOL_TRADE_TICK_VALUE` es el dinero que mueve la cuenta por un
        tick de UN lote — el contrato ya está dentro. Multiplicarlo otra vez por el
        `lot_size` inflaría el riesgo del lote 100.000 veces en EURUSD, y el
        síntoma sería que toda operación se rechaza por "riesgo excede el
        presupuesto" con cifras de millones cuando el presupuesto son decenas.
        """
        riesgo = lc.risk_per_unit(0.0012, EURUSD)
        # 0.0012 / 0.00001 = 120 ticks, a 1.00 USD por tick y lote = 120 USD.
        assert riesgo == pytest.approx(120.0)
        # Y con 0.10 lotes son 12 USD, no 1.200.000.
        assert riesgo * 0.10 == pytest.approx(12.0)

    def test_un_lot_size_distinto_no_cambia_el_riesgo_por_lote(self):
        """El tamaño del contrato no entra: el tick value ya lo trae dentro.

        Dos specs que se diferencian solo en `lot_size` describen el mismo
        instrumento a efectos de riesgo por lote. Si el cálculo dependiera del
        contrato, un símbolo con contrato de 10.000 daría un riesgo distinto del
        mismo símbolo con 100.000 sin que nada hubiera cambiado en el mercado.
        """
        grande = lc.LotSpec(lot_size=100_000, tick_value=1.0, tick_size=0.00001,
                            min_lot=0.01, lot_step=0.01)
        pequeno = lc.LotSpec(lot_size=1_000, tick_value=1.0, tick_size=0.00001,
                             min_lot=0.01, lot_step=0.01)
        assert lc.risk_per_unit(0.0012, grande) == pytest.approx(
            lc.risk_per_unit(0.0012, pequeno))

    def test_sl_mas_fino_que_el_tick_es_error_tambien_aqui(self):
        """La guarda de los stops no colocables se comparte, no se reimplementa."""
        with pytest.raises(ValueError) as exc:
            lc.risk_per_unit(0.000001, EURUSD)
        assert "tick" in str(exc.value)

    def test_sl_cero_o_negativo_es_error(self):
        with pytest.raises(ValueError):
            lc.risk_per_unit(0.0, EURUSD)
        with pytest.raises(ValueError):
            lc.risk_per_unit(-0.0012, EURUSD)


class TestB3Contracts:
    def test_tamano_en_contratos(self):
        # 50 puntos de SL son 10 ticks del minicontrato (punto de 5), a 0.20 USD
        # por tick: 2.00 USD de riesgo por contrato. Con 100 USD caben 50.
        r = lc.b3_mini_contracts(100.0, 50.0, WIN_MINI)
        assert r["lots"] == pytest.approx(50.0)
        assert r["risk_per_unit"] == pytest.approx(2.0)
        assert r["risk_at_sl"] == pytest.approx(100.0)

    def test_contratos_es_entero(self):
        """El minicontrato no admite fracciones."""
        r = lc.b3_mini_contracts(105.0, 50.0, WIN_MINI)
        assert r["lots"] == float(int(r["lots"]))

    def test_misma_formula_distinta_unidad(self):
        """Mismo número de ticks y mismo riesgo, unidades distintas, tamaño distinto.

        Se comparan 120 ticks en cada mercado (los dos S Coversalen a 120 ticks):
        0.0012 en EURUSD y 600 puntos en WIN. Si ambos calculadores devolvieran lo
        mismo, el argumento de mercado-agnóstico sería falso.
        """
        r_fx = lc.standard_forex_lots(100.0, 0.0012, EURUSD)
        r_b3 = lc.b3_mini_contracts(100.0, 600.0, WIN_MINI)
        assert r_fx["lots"] != r_b3["lots"]
        # Y el riesgo por unidad también difiere, que es donde se ve la unidad.
        assert r_fx["risk_per_unit"] != r_b3["risk_per_unit"]

    def test_bonds_delega_en_minicontratos(self):
        r = lc.b3_bonds(100.0, 50.0, WIN_MINI)
        assert r["lots"] == lc.b3_mini_contracts(100.0, 50.0, WIN_MINI)["lots"]

    def test_riesgo_real_no_excede(self):
        for sl in (5.0, 50.0, 200.0):
            r = lc.b3_mini_contracts(250.0, sl, WIN_MINI)
            real = r["lots"] * (sl / WIN_MINI.tick_size) * WIN_MINI.tick_value
            assert real <= 250.0 + 1e-9


class TestCryptoNotional:
    def test_devuelve_nocional_cuando_hay_precio(self):
        r = lc.crypto_notional_usdt(100.0, 500.0, BTC_PERP, entry_price=60_000.0)
        # 500 USD de SL son 500 ticks a 0.01 USD/unidad = 5.00 USD por unidad.
        # 100 USD de riesgo -> 20 unidades -> 1.200.000 USDT de nocional.
        assert r["lots"] == pytest.approx(20.0)
        assert r["risk_per_unit"] == pytest.approx(5.0)
        assert r["notional_usdt"] == pytest.approx(1_200_000.0)

    def test_sin_precio_no_inventa_nocional(self):
        """Sin precio de entrada no hay nocional. Se dice, no se estima."""
        r = lc.crypto_notional_usdt(100.0, 500.0, BTC_PERP)
        assert r["notional_usdt"] is None
        assert "entry_price" in r["notional_error"]

    def test_precio_no_positivo_es_error(self):
        with pytest.raises(ValueError):
            lc.crypto_notional_usdt(100.0, 500.0, BTC_PERP, entry_price=0)

    def test_paso_minimo_de_lotes(self):
        """El tamaño cae en múltiplos exactos del lot_step del exchange.

        Se comprueba con `round(lots/step)` entero, no con `%`, porque 20.0 % 0.001
        en punto flotante deja residuo (0.000999...) y el test fallaría sin que
        hubiera ningún error real.
        """
        r = lc.crypto_notional_usdt(137.77, 733.0, BTC_PERP, entry_price=60_000.0)
        assert r["lots"] / 0.001 == pytest.approx(round(r["lots"] / 0.001))


class TestMinLotAndRounding:
    def test_riesgo_minimo_sube_al_minimo_y_lo_reporta(self):
        """0.001 USD de riesgo en EURUSD no son 0.0008 lotes: son 0.01 lotes.

        Y tiene que SOBRAR riesgo, así que el resultado marca `clamped_to_min`:
        quien llama necesita saber que el mínimo del broker, y no su cálculo, es
        lo que youngsters el tamaño final.
        """
        r = lc.standard_forex_lots(0.001, 0.0012, EURUSD)
        assert r["lots"] == pytest.approx(0.01)
        assert r["clamped_to_min"] is True

    def test_sin_clamp_cuando_alcanza_el_minimo(self):
        r = lc.standard_forex_lots(10.0, 0.0012, EURUSD)
        assert r["clamped_to_min"] is False

    def test_redondeo_siempre_hacia_abajo(self):
        """Nunca hacia arriba: el riesgo real no puede exceder el autorizado."""
        for risk in (10.0, 33.33, 77.77, 199.99):
            r = lc.standard_forex_lots(risk, 0.0012, EURUSD)
            exacto = risk / ((0.0012 / EURUSD.tick_size) * EURUSD.tick_value)
            assert r["lots"] <= exacto + 1e-12
            assert exacto - r["lots"] < 0.01 + 1e-9  # nunca se aleja un paso entero

    def test_paso_de_lote_se_respeta(self):
        r = lc.standard_forex_lots(100.0, 0.0012, EURUSD)
        assert round(r["lots"] / 0.01, 6) == pytest.approx(round(r["lots"] / 0.01))

    def test_sin_decimales_falsos(self):
        r = lc.standard_forex_lots(100.0 / 3.0, 0.0012, EURUSD)
        assert r["lots"] == round(r["lots"], 8)
        assert str(r["lots"])


class TestLotSpec:
    def test_from_dict_acepta_alias(self):
        spec = lc.LotSpec.from_dict({
            "contract_size": 100_000, "value_per_tick": 1.0, "point": 0.00001,
        })
        assert spec.lot_size == 100_000
        assert spec.tick_value == 1.0
        assert spec.tick_size == 0.00001
        assert spec.min_lot == lc.MIN_LOT

    def test_spec_invalido_falla_al_construirse(self):
        for kwargs in (
            {"lot_size": 0, "tick_value": 1.0, "tick_size": 0.00001},
            {"lot_size": 100, "tick_value": 1.0, "tick_size": 0},
            {"lot_size": 100, "tick_value": 1.0, "tick_size": 0.00001, "min_lot": 0},
        ):
            with pytest.raises(ValueError):
                lc.LotSpec(**kwargs)

    def test_spec_incompleto_falla_con_el_nombre_que_falta(self):
        with pytest.raises(ValueError) as exc:
            lc.LotSpec.from_dict({"lot_size": 100000})
        assert "tick_value" in str(exc.value)


class TestDispatch:
    def test_todos_los_calculadores_del_yaml_existen(self):
        """`lot_calculator` de asset_sources_map.yaml debe existir en el registro.

        Si el YAML nombra un calculador que no está, el fallo tiene que aparecer al
        cargar el mapa, no con una operación en vuelo.
        """
        import yaml
        from core import paths as _paths

        with open(_paths.CONFIG_DIR + "/asset_sources_map.yaml", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        usados = {v["lot_calculator"] for v in doc.values() if isinstance(v, dict)}
        assert usados, "asset_sources_map.yaml no declara lot_calculator"
        assert usados <= set(lc.CALCULATORS), (
            f"calculadores declarados sin implementar: {sorted(usados - set(lc.CALCULATORS))}"
        )

    def test_dispatch_con_spec_dict(self):
        r = lc.calculate_size("standard_forex_lots", 100.0, 0.0012, {
            "lot_size": 100_000, "tick_value": 1.0, "tick_size": 0.00001,
            "min_lot": 0.01, "lot_step": 0.01,
        })
        assert r["lots"] == pytest.approx(0.83)

    def test_dispatch_pasa_kwargs(self):
        r = lc.calculate_size("crypto_notional_usdt", 100.0, 500.0,
                              BTC_PERP, entry_price=60_000.0)
        assert r["notional_usdt"] == pytest.approx(1_200_000.0)

    def test_calculador_desconocido_lista_los_disponibles(self):
        with pytest.raises(KeyError) as exc:
            lc.calculate_size("no_existe", 100.0, 0.0012, EURUSD)
        assert "standard_forex_lots" in str(exc.value)


class TestModulePurity:
    def test_no_importa_la_configuracion(self):
        """El cálculo no lee strategy.yaml ni ningún fichero.

        Si lo hiciera, cada test unitario dependería del disco y un cambio de
        umbral en el YAML movería los tamaños sin que nada lo anunciara.
        """
        from pathlib import Path
        source = Path(lc.__file__).read_text(encoding="utf-8")
        for prohibido in ("open(", "import yaml", "adapters", "import api"):
            assert prohibido not in source