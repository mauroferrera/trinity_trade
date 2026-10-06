"""Los scenarios de B3 y cripto contra el mismo núcleo que el forex.

Un scenario por mercado: EURUSD (referencia), minicontrato de índice de la B3
(WIN, 0 decimales), minicontrato de dólar de la B3 (WDO, 3 decimales con
`pip_override`, y con un R:R que el gate tiene que rechazar) y un perpetuo
(BTCUSDT, 1 decimal, nocional en USDT).

Los tres de B3/cripto son CLONES ESTRUCTURALES de `killzone_edge_1min`: la misma
confluencia (FVG + order block + barrido del mínimo previo + CVD en pendiente),
el mismo CVD punto por punto y las mismas 20 velas en forma. Lo único que cambia
es la escala del precio, cuántos decimales tiene, cómo se llama la sesión y qué
unidad de dinero significa una distancia. Por eso la aserción central de este
archivo es una igualdad de scores y no un mínimo: si el score de BTCUSDT no es
EXACTAMENTE el de EURUSD, el núcleo tiene un supuesto de forex metido dentro, y
eso es un bug aunque los dos scores parezcan razonables.

Lo que sí es específico de cada mercado (la unidad de presentación, el
dimensionamiento, la etiqueta "pips"/"puntos", la ausencia de índice COT) se
comprueba con las cifras que el propio escenario declara en `expected`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from ..conftest import (
    load_scenario,
    make_candles,
    make_snapshot_from_scenario,
    make_symbol_spec,
    make_trades,
)
from core import risk_engine as re_mod
from core import setup_gate
from core.lot_calculator import calculate_size

# El escenario forex que hace de referencia de estructura y de score.
REFERENCIA = "killzone_edge_1min"
MERCADOS = ["b3_win_mini", "b3_wdo_mini", "crypto_btc_perp"]
CON_MARKET = MERCADOS + [REFERENCIA]


def _now(scenario: Dict[str, Any]) -> datetime:
    return datetime.fromisoformat(scenario["now_utc"].replace("Z", "+00:00"))


def _score(scenario: Dict[str, Any], direction: str = "BUY") -> Dict[str, Any]:
    """Pasa el escenario por `setup_score` con las velas que declara."""
    candles = make_candles(**scenario["candle_params"])
    return re_mod.setup_score(
        {
            "direction": direction,
            "cot": scenario.get("cot"),
            "cvd": scenario.get("cvd"),
            "patterns": scenario["patterns"],
            "candles": candles,
        },
        killzones=scenario["killzones"],
        now=_now(scenario),
    )


def _utc_offset(market: Dict[str, Any]) -> int:
    """El offset del exchange en minutos, con el signo que usa el YAML (-03:00)."""
    signo = -1 if str(market["utc_offset_standard"]).startswith("-") else 1
    horas, minutos = str(market["utc_offset_standard"]).lstrip("+-").split(":")
    return signo * (int(horas) * 60 + int(minutos))


def _hora_en_ventana(hora_utc: str, inicio_local: str, fin_local: str, offset: int) -> bool:
    """¿La hora UTC cae dentro de la ventana local (start <= t <= end)?"""
    def minutos(hora: str) -> int:
        h, m = hora.strip().split(":")
        return int(h) * 60 + int(m)

    # Local -> UTC: al exchange se le suma el offset inverso.
    inicio_utc = (minutos(inicio_local) - offset) % (24 * 60)
    fin_utc = (minutos(fin_local) - offset) % (24 * 60)
    t = minutos(hora_utc)
    if inicio_utc <= fin_utc:
        return inicio_utc <= t <= fin_utc
    return t >= inicio_utc or t <= fin_utc


# ---------------------------------------------------------------------------
# El núcleo no sabe de forex
# ---------------------------------------------------------------------------

class TestNucleoAgnostico:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_el_score_no_cambia_con_el_mercado(self, nombre):
        """Misma confluencia, mismo score. Es la razón de existir de estos scenarios."""
        ref = _score(load_scenario(REFERENCIA))
        otro = _score(load_scenario(nombre))
        assert otro["score"] == ref["score"], (
            f"{nombre} puntua {otro['score']} y el forex de referencia puntua "
            f"{ref['score']}. Con la misma confluencia el score no puede depender "
            f"del mercado: revisa que ningun componente mire precios o unidades."
        )
        assert otro["verdict"] == ref["verdict"]
        for comp in ("cvd_of", "smc", "cot"):
            assert otro["components"][comp]["value"] == ref["components"][comp]["value"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_el_lado_propio_gana_al_contrario(self, nombre):
        """Un clon del BUY de forex no puede puntuar alto como SELL."""
        sc = load_scenario(nombre)
        assert _score(sc, "BUY")["score"] > _score(sc, "SELL")["score"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_el_score_supera_el_minimo_del_escenario(self, nombre):
        sc = load_scenario(nombre)
        exp = sc["expected"]
        score = _score(sc)
        assert score["score"] >= exp["score_min"]
        assert score["verdict"] == exp["verdict"]
        assert score["components"]["killzone"]["in_killzone"] is exp["killzone_active"]
        assert score["components"]["killzone"]["name"] == exp["killzone_name"]


# ---------------------------------------------------------------------------
# La sesión es la del mercado, no la de forex
# ---------------------------------------------------------------------------

class TestSesionPorMercado:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_dentro_de_su_propia_sesion(self, nombre):
        sc = load_scenario(nombre)
        kz = re_mod.killzone_score(sc["killzones"], _now(sc))
        assert kz["in_killzone"] is True
        assert kz["name"] == sc["expected"]["killzone_name"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_fuera_de_su_sesion_no_entra(self, nombre):
        """La misma sesion, cuatro horas antes de que abra: fuera."""
        sc = load_scenario(nombre)
        inicio = datetime.fromisoformat(
            "2000-01-01T" + sc["killzones"][0]["start"] + ":00+00:00"
        ) - timedelta(hours=4)
        kz = re_mod.killzone_score(sc["killzones"], inicio)
        assert kz["in_killzone"] is False
        assert kz["value"] == 0.25
        assert kz["name"] is None

    def test_la_sesion_de_cripto_no_es_la_de_la_bolsa(self):
        """A las 15:00 UTC hay sesion cripto y no hay pregon de B3 ni de forex."""
        momento = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
        cripto = load_scenario("crypto_btc_perp")
        b3 = load_scenario("b3_win_mini")
        assert re_mod.killzone_score(cripto["killzones"], momento)["in_killzone"] is True
        assert re_mod.killzone_score(b3["killzones"], momento)["in_killzone"] is True
        # 02:00 UTC es de noche en Sao Paulo: la B3 esta cerrada y el core no
        # distingue "fuera de sesion" de "no se que sesion es".
        noche = momento.replace(hour=2)
        assert re_mod.killzone_score(b3["killzones"], noche)["in_killzone"] is False

    def test_el_forex_no_hereda_la_sesion_de_b3(self):
        """El scenario de forex declara las suyas; las de B3 no se cuelan."""
        forex = load_scenario(REFERENCIA)
        nombres = {w["name"] for w in forex["killzones"]}
        for nombre in MERCADOS:
            otros = {w["name"] for w in load_scenario(nombre)["killzones"]}
            assert nombres.isdisjoint(otros)


# ---------------------------------------------------------------------------
# La unidad del símbolo: aquí es donde fallaba el forex
# ---------------------------------------------------------------------------

class TestUnidadDelSimbolo:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_la_unidad_y_la_etiqueta_son_las_del_mercado(self, nombre):
        sc = load_scenario(nombre)
        spec = make_symbol_spec(sc)
        cfg = sc["cfg"]
        assert spec.unit_label == sc["expected"]["unit_label"]
        assert spec.from_price(cfg["sl_distance"]) == pytest.approx(
            sc["expected"]["sl_units"]
        )
        # to_price es el camino inverso: la distancia que declara el escenario
        # tiene que volver a ser la distancia que el broker aceptaria.
        assert spec.to_price(sc["expected"]["sl_units"]) == pytest.approx(cfg["sl_distance"])

    def test_win_cotiza_a_puntos_de_cinco(self):
        """WIN: 0 decimales, punto de 5. Ningun feed emite 130000.0."""
        spec = make_symbol_spec(load_scenario("b3_win_mini"))
        assert spec.digits == 0
        assert spec.pip == 5.0
        assert spec.pip_quote is False
        assert spec.unit_label == "puntos"
        assert spec.format_units(500) == "100.0 puntos"

    def test_btc_cotiza_a_un_decimal_y_la_unidad_es_el_tick(self):
        spec = make_symbol_spec(load_scenario("crypto_btc_perp"))
        assert spec.digits == 1
        assert spec.pip == 0.1
        assert spec.pip_quote is False
        assert spec.format_units(400) == "4000.0 puntos"

    def test_wdo_necesita_el_override_para_no_llamar_pips_a_los_puntos(self):
        """3 decimales + override: la unidad es el punto, y el motivo lo dice."""
        sc = load_scenario("b3_wdo_mini")
        spec = make_symbol_spec(sc)
        exp = sc["expected"]
        assert spec.digits == 3
        assert spec.unit_label == exp["unit_label"] == "puntos"
        assert spec.from_price(sc["cfg"]["sl_distance"]) == pytest.approx(exp["sl_units"])
        # Sin el override la regla de digitos da point*10 = 0.01 y el mismo SL de
        # 0.030 se reporta como 3 "pips": diez veces menos de lo que se pidio.
        sin_override = make_symbol_spec({**sc, "market": {**sc["market"], "pip_override": 0.0}})
        assert sin_override.pip == 0.01
        assert sin_override.pip_quote is True
        assert sin_override.from_price(sc["cfg"]["sl_distance"]) == pytest.approx(
            exp["sl_units_without_override"]
        )
        assert sin_override.format_units(sc["cfg"]["sl_distance"]) == "3.0 pips"

    def test_sin_bloque_market_no_hay_spec(self):
        """Los scenarios de forex no inventan specs: vienen del broker."""
        assert make_symbol_spec(load_scenario(REFERENCIA)) is None


# ---------------------------------------------------------------------------
# El gate con la aritmética de cada mercado
# ---------------------------------------------------------------------------

class TestGatePorMercado:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_veredicto_del_gate_segun_el_escenario(self, nombre):
        sc = load_scenario(nombre)
        snap = make_snapshot_from_scenario(sc)
        res = setup_gate.evaluate_gate(snap, sc["cfg"], make_symbol_spec(sc))
        assert res is not None
        exp = sc["expected"]
        assert res["approved"] is exp["approved"], res.get("reasons")
        if exp["approved"]:
            assert not res.get("reasons")
        else:
            assert any(exp["reject_contains"] in r for r in res["reasons"]), res["reasons"]

    def test_wdo_se_rechaza_por_rr_y_no_por_el_mercado(self):
        """El motivo del rechazo es el R:R, no 'es B3' ni 'son 3 decimales'."""
        sc = load_scenario("b3_wdo_mini")
        res = setup_gate.evaluate_gate(
            make_snapshot_from_scenario(sc), sc["cfg"], make_symbol_spec(sc)
        )
        assert res["approved"] is False
        assert any("R:R" in r for r in res["reasons"])
        assert any("1.50" in r for r in res["reasons"])

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_la_entrada_es_la_zona_y_no_el_precio_de_pantalla(self, nombre):
        """El precio cae dentro de la zona, asi que la entrada es el borde que mira al precio."""
        sc = load_scenario(nombre)
        res = setup_gate.evaluate_gate(
            make_snapshot_from_scenario(sc), sc["cfg"], make_symbol_spec(sc)
        )
        assert res["entry_kind"] != "market"
        assert res["entry_zone_reject"] == ""
        assert res["zone_span"] is not None
        assert res["entry"] == pytest.approx(sc["expected"]["entry"])
        assert res["entry"] < sc["current_price"]
        assert res["zone_span"] <= res["zone_span_limit"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_el_rr_real_se_eleva_del_plan(self, nombre):
        """Ninguno de los tres puede colar un plan con R:R por debajo del minimo."""
        sc = load_scenario(nombre)
        res = setup_gate.evaluate_gate(
            make_snapshot_from_scenario(sc), sc["cfg"], make_symbol_spec(sc)
        )
        real = abs(res["tp"] - res["entry"]) / abs(res["entry"] - res["sl"])
        esperado = sc["cfg"]["tp_ratio_r"]
        assert real == pytest.approx(esperado, abs=1e-6)
        if esperado < sc["cfg"]["min_rr"]:
            assert real < sc["cfg"]["min_rr"] and res["approved"] is False

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_la_invalidez_es_la_de_las_zonas_del_mercado(self, nombre):
        sc = load_scenario(nombre)
        snap = make_snapshot_from_scenario(sc)
        assert snap["risk_engine"]["invalidation"]["BUY"] == pytest.approx(
            sc["expected"]["invalidation"]
        )
        assert sc["current_price"] > sc["expected"]["invalidation"]


# ---------------------------------------------------------------------------
# Dimensionamiento: aquí las unidades SÍ son distintas
# ---------------------------------------------------------------------------

class TestDimensionamiento:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_el_riesgo_del_escenario_es_el_riesgo_real(self, nombre):
        """El tamano sale del riesgo pedido y el riesgo al SL es ese mismo riesgo."""
        sc = load_scenario(nombre)
        market = sc["market"]
        out = calculate_size(
            market["lot_calculator"],
            market["risk_amount"],
            sc["cfg"]["sl_distance"],
            market,
        )
        assert out["lots"] == pytest.approx(sc["expected"]["lots"])
        assert out["risk_at_sl"] == pytest.approx(market["risk_amount"])
        assert out["clamped_to_min"] is False

    def test_win_un_contrato_y_medio_punto_nada(self):
        """Un minicontrato de WIN son 130.000 puntos: el tamano va en contratos."""
        sc = load_scenario("b3_win_mini")
        market = sc["market"]
        assert market["tick_value"] == 1.0  # 0,20 R$ por punto x 5 puntos
        out = calculate_size("b3_mini_contracts", 100.0, 500, market)
        assert out["risk_per_unit"] == 100.0
        assert out["lots"] == 1.0
        # El riesgo es lineal en contratos: 250 de presupuesto son 2 y medio, y el
        # paso entero los deja en 2.
        menos = calculate_size("b3_mini_contracts", 250.0, 500, market)
        assert menos["lots"] == 2.0
        assert menos["risk_at_sl"] == 200.0

    def test_btc_se_dimensiona_en_unidades_y_devuelve_nocional(self):
        """0,25 BTC, no 0,25 lotes: el nocional es lo que realmente se expone."""
        sc = load_scenario("crypto_btc_perp")
        market = sc["market"]
        out = calculate_size("crypto_notional_usdt", 100.0, 400, market, entry_price=65000.0)
        assert out["risk_per_unit"] == 400.0  # 4.000 ticks de 0,1 x 0,1 USDT
        assert out["lots"] == pytest.approx(0.25)
        assert out["notional_usdt"] == pytest.approx(
            sc["expected"]["notional_usdt_at_market"]
        )
        assert out["risk_at_sl"] == pytest.approx(100.0)

    def test_btc_sin_precio_no_inventa_nocional(self):
        """Sin entrada no hay nocional, y el calculador lo dice en vez de rellenar."""
        sc = load_scenario("crypto_btc_perp")
        out = calculate_size("crypto_notional_usdt", 100.0, 400, sc["market"])
        assert out["notional_usdt"] is None
        assert "entry_price" in out["notional_error"]
        assert out["lots"] == pytest.approx(0.25)

    def test_un_sl_mas_fino_que_el_tick_se_rechaza_en_los_tres_mercados(self):
        """Un stop que el bróker no puede colocar no es un stop barato."""
        for nombre in MERCADOS:
            sc = load_scenario(nombre)
            market = sc["market"]
            fino = market["tick_size"] / 2.0
            with pytest.raises(ValueError, match="tick"):
                calculate_size(market["lot_calculator"], 100.0, fino, market)

    def test_el_calculador_viene_del_mapa_de_activos(self):
        """El calculador y la sesion de cada escenario estan en la config, no en el test."""
        import yaml

        from core import paths

        with open(paths.CONFIG_DIR + "/asset_sources_map.yaml", encoding="utf-8") as fh:
            mapa = yaml.safe_load(fh)
        for nombre in MERCADOS:
            sc = load_scenario(nombre)
            entradas = [v for v in mapa.values() if v.get("symbol") == sc["symbol"]]
            assert len(entradas) == 1, f"{sc['symbol']} no esta en el mapa de activos"
            entrada = entradas[0]
            assert entrada["lot_calculator"] == sc["market"]["lot_calculator"]
            assert entrada["market_type"] == sc["market"]["market_type"]
            assert entrada["session_id"] == sc["market"]["session_id"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_la_sesion_del_escenario_existe_en_el_calendario(self, nombre):
        """La ventana UTC del escenario es la del calendario, no una hora escrita a mano."""
        import yaml

        from core import paths

        with open(paths.CONFIG_DIR + "/trading_hours.json", encoding="utf-8") as fh:
            calendario = json.load(fh)
        with open(paths.CONFIG_DIR + "/asset_sources_map.yaml", encoding="utf-8") as fh:
            mapa = yaml.safe_load(fh)
        sc = load_scenario(nombre)
        session_id = sc["market"]["session_id"]
        assert session_id in calendario["sessions"]
        sesion = calendario["sessions"][session_id]
        entrada = next(v for v in mapa.values() if v.get("symbol") == sc["symbol"])
        assert entrada["session_id"] == session_id
        kz = sc["killzones"][0]
        ventanas = sesion.get("windows") or []
        if ventanas and "start_utc" in ventanas[0]:
            # Cripto: el calendario ya esta en UTC.
            assert any(
                w["start_utc"] == kz["start"] and w["end_utc"] == kz["end"] for w in ventanas
            ), f"la ventana {kz} no esta en {session_id}"
        else:
            # B3: el calendario esta en hora local del exchange y hay que correrlo
            # a UTC con el offset del mercado.
            offset = _utc_offset(calendario["markets"][sc["market"]["market_type"]])
            inicio = ventanas[0]["start_local"]
            fin = ventanas[0]["end_local"]
            assert _hora_en_ventana(kz["start"], inicio, fin, offset)
            assert _hora_en_ventana(kz["end"], inicio, fin, offset)


# ---------------------------------------------------------------------------
# El macro no es COT fuera de forex
# ---------------------------------------------------------------------------

class TestMacroHonesto:
    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_sin_indice_cot_el_detalle_lo_dice(self, nombre):
        """Ni B3 ni cripto tienen informe COT de la CFTC: no hay indice que reportar."""
        sc = load_scenario(nombre)
        assert "cot_index_26w" not in sc["cot"]
        comp = _score(sc)["components"]["cot"]
        assert comp["value"] == 1.0  # el macro sesga a favor
        assert "n/d" in comp["detail"]

    def test_forex_si_reporta_su_indice(self):
        comp = _score(load_scenario(REFERENCIA))["components"]["cot"]
        assert "n/d" not in comp["detail"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_un_macro_contrario_baja_el_componente(self, nombre):
        """El sesgo macro en contra penaliza igual que en forex: no hay trato distinto."""
        sc = load_scenario(nombre)
        contrario = {**sc, "cot": {"macro_bias": "BEARISH"}}
        assert _score(contrario)["components"]["cot"]["value"] == 0.15


# ---------------------------------------------------------------------------
# El snapshot: la forma es la misma, la escala no
# ---------------------------------------------------------------------------

class TestSnapshotPorMercado:
    @pytest.mark.parametrize("nombre", CON_MARKET)
    def test_todos_los_scenarios_arman_un_snapshot(self, nombre):
        snap = make_snapshot_from_scenario(load_scenario(nombre))
        assert snap["current_price"] > 0
        assert snap["PDH"] > snap["current_price"] > snap["PDL"]
        assert snap["_test_sl"] and snap["_test_tp"]
        assert snap["_test_best"]["score"] > 0

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_las_velas_cotan_con_los_decimales_del_mercado(self, nombre):
        sc = load_scenario(nombre)
        digits = sc["market"]["digits"]
        snap = make_snapshot_from_scenario(sc)
        for vela in snap["recent_candles"]:
            for campo in ("close", "high", "low"):
                assert vela[campo] == round(vela[campo], digits)

    def test_los_niveles_caen_en_la_rejilla_del_tick(self):
        """Un nivel que no cuadra con el tick no se puede enviar."""
        for nombre in MERCADOS:
            sc = load_scenario(nombre)
            tick = sc["market"]["tick_size"]
            snap = make_snapshot_from_scenario(sc)
            res = setup_gate.evaluate_gate(
                snap, sc["cfg"], make_symbol_spec(sc)
            )
            for nivel in (res["entry"], res["sl"], res["tp"]):
                assert nivel is not None
                ticks = nivel / tick
                assert abs(ticks - round(ticks)) < 1e-6, (
                    f"{nombre}: el nivel {nivel} no cae en la rejilla de {tick}"
                )

    def test_un_snapshot_sin_precio_falla_en_vez_de_valorar_en_eurusd(self):
        sc = {**load_scenario("crypto_btc_perp")}
        sc.pop("current_price")
        with pytest.raises(ValueError, match="current_price"):
            make_snapshot_from_scenario(sc)

    def test_el_precio_pasado_por_kwarg_gana_al_del_escenario(self):
        sc = load_scenario("b3_win_mini")
        assert make_snapshot_from_scenario(sc, current_price=131000)["current_price"] == 131000
        assert make_snapshot_from_scenario(sc)["current_price"] == sc["current_price"]

    @pytest.mark.parametrize("nombre", MERCADOS)
    def test_la_serie_de_trades_usa_el_tick_del_simbolo(self, nombre):
        """A 0,004% del precio, sobre BTCUSDT el ruido seria de 2,6 puntos."""
        sc = load_scenario(nombre)
        market = sc["market"]
        tick = market["tick_size"]
        trades = make_trades(
            count=60,
            base_price=sc["current_price"],
            side_bias=0.6,
            jitter=tick,
            digits=market["digits"],
            tick_size=tick,
        )
        digitos = market["digits"]
        precios = {t["price"] for t in trades}
        assert len(precios) > 1, "toda la serie en un precio no es una serie de ticks"
        for t in trades:
            assert t["price"] == round(t["price"], digitos)
            ticks = t["price"] / tick
            assert abs(ticks - round(ticks)) < 1e-6
