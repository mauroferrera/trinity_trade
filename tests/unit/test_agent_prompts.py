"""Tests de `agent/prompt_templates.py`: lo que el modelo lee antes de redactar.

El prompt es la parte del agente donde un error no sale como excepción: sale como
una respuesta plausible y equivocada. Por eso los tests fijan cosas que "son lo
mismo de siempre" y que un refactor rompería sin avisar:

- Las UNIDADES del mercado. "SL de 12" son 12 pips en EURUSD, 12 PUNTOS en oro y
  12 puntos de índice en el DAX. Decir la unidad equivocada es un error de sizing
  disfrazado de redacción.
- El PESO de la killzone sale de `risk_weights`, no de un literal. REF escribía
  "20%" a mano y ya sabía que era mentira.
- La AUSENCIA se declara. Si el estado diario no vino, la política lo dice; si no
  lo dijera, el modelo leería una política completa donde no lo hay.
- El símbolo SIN FICHA no se degrada a forex. Adivinar la convención de unidades de
  un símbolo desconocido es el error más caro del agente.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from agent import prompt_templates as pt
from core import risk_engine


UTC = timezone.utc


def _dt(hora: int, minuto: int = 0) -> datetime:
    return datetime(2026, 10, 3, hora, minuto, tzinfo=UTC)


# ---------------------------------------------------------------------------
# market_for: tres pasos y ningún `else` que diga "forex"
# ---------------------------------------------------------------------------


def test_market_for_usa_el_mapa_de_activos():
    mapa = {"EURUSD": {"market_type": "FOREX_SPOT"}}
    assert pt.market_for("EURUSD", mapa) == "FOREX_SPOT"


def test_market_for_normaliza_a_mayusculas():
    assert pt.market_for("  eurusd ", {"EURUSD": {"market_type": "FOREX_SPOT"}}) == "FOREX_SPOT"


def test_market_for_usa_la_tabla_para_lo_que_el_mapa_no_declara():
    """XAUUSD no está en `asset_sources_map.yaml` y sí es un mercado que se sabe."""
    assert pt.market_for("XAUUSD") == "FOREX_SPOT"
    assert pt.market_for("DAX") == "INDEX_CFD"


def test_market_for_resuelve_establecoins_por_sufijo():
    assert pt.market_for("BTCUSDT") == "CRYPTO_PERPETUAL"
    assert pt.market_for("ETHUSDC") == "CRYPTO_PERPETUAL"


def test_market_for_desconocido_no_es_forex():
    """El bug caro: adivinar 'forex' para un símbolo sin ficha hace que el modelo
    traduzca 'pips' donde no son pips."""
    assert pt.market_for("PERDIDOR123") == pt.DESCONOCIDO
    assert pt.market_for("") == pt.DESCONOCIDO


def test_market_for_manda_el_mapa_sobre_la_tabla():
    mapa = {"XAUUSD": {"market_type": "B3_FUTURES"}}
    assert pt.market_for("XAUUSD", mapa) == "B3_FUTURES"


def test_load_asset_map_degrada_a_vacio_si_el_yaml_no_existe(tmp_path):
    assert pt.load_asset_map(str(tmp_path / "no-existe.yaml")) == {}


def test_load_asset_map_ignora_las_entradas_que_no_son_dict(tmp_path):
    import yaml

    ruta = tmp_path / "asset_sources_map.yaml"
    ruta.write_text(yaml.safe_dump({"EURUSD": {"market_type": "FOREX_SPOT"}, "RARO": "texto"}), encoding="utf-8")
    mapa = pt.load_asset_map(str(ruta))
    assert mapa == {"EURUSD": {"market_type": "FOREX_SPOT"}}


# ---------------------------------------------------------------------------
# Unidades: el motivo de que exista este módulo
# ---------------------------------------------------------------------------


def test_cada_mercado_dice_como_expresa_la_unidad():
    forex = pt.market_block("FOREX_SPOT")
    assert "pips" in forex
    assert "5 decimales" in forex

    indice = pt.market_block("INDEX_CFD")
    assert "PUNTOS" in indice

    b3 = pt.market_block("B3_FUTURES")
    assert "contrato" in b3

    cripto = pt.market_block("CRYPTO_PERPETUAL")
    assert "USDT" in cripto


def test_oro_dice_puntos_no_pips():
    """XAUUSD es FOREX_SPOT (pip = 0.0001) pero su SL se cuenta en PUNTOS."""
    perfil = pt.profile_for("FOREX_SPOT")
    assert "0.0001" in perfil["unidad_stop"]
    assert "XAUUSD" in perfil["unidad_stop"]
    assert "PUNTOS" in perfil["unidad_stop"]


def test_sin_ficha_no_inventa_unidades():
    """El mercado sin ficha NO dice pip, ni punto, ni lote. Dice que no lo sabe."""
    texto = pt.market_block(pt.DESCONOCIDO)
    assert "No lo sé" in texto
    assert "NO inventes unidades" in texto
    assert pt.profile_for("NO_EXISTE") is pt.profile_for(pt.DESCONOCIDO)


def test_el_bloque_de_mercado_avisa_de_los_activos_sin_implementar():
    """WIN/WDO/B3_NT están en el mapa pero sin pipeline: decirlo aquí y no en el
    mensaje de error 500 que vería el usuario tres horas después."""
    b3 = pt.market_block("B3_FUTURES")
    assert "Aviso" in b3
    tesouro = pt.market_block("B3_TESOURO")
    assert "Aviso" in tesouro


def test_el_bloque_de_mercado_dice_el_avalise_si_el_mapa_lo_declara():
    entrada = {"session_id": "fx_24h", "data_adapter": "mt5", "lot_calculator": "forex_lots"}
    texto = pt.market_block("FOREX_SPOT", entrada)
    assert "session_id=fx_24h" in texto
    assert "lot_calculator=forex_lots" in texto


def test_el_bloque_de_mercado_no_inventa_un_avalise():
    assert "session_id" not in pt.market_block("FOREX_SPOT")


# ---------------------------------------------------------------------------
# Política de riesgo
# ---------------------------------------------------------------------------


def _cfg(pesos: Any = "AUSENTE", **extra: Any) -> Dict[str, Any]:
    """El shape REAL de `config/strategy.yaml`, no un dict plano inventado.

    La primera versión de este test pasaba `risk_weights` y `prop_enabled`, que son
    las claves que D-006 dejó planas para la API. El YAML no las tiene: leerlas era
    un no-op silencioso, y con `prop.enabled: true` en el YAML el prompt anunciaba
    "desactivado". Los tests usan el YAML anidado para que ese forget no vuelva.
    """
    base: Dict[str, Any] = {
        "killzones": [{"name": "Londres", "start": "07:00", "end": "10:00"}],
        "data_sources": {"news": True, "cot": True, "dxy": True},
        "execution": {"news_buffer_min": 15},
        "prop": {"enabled": False},
    }
    if pesos != "AUSENTE":
        base["score"] = {"weights": pesos}
    base.update(extra)
    return base


def test_el_peso_de_la_killzone_se_calcula_no_se_escribe():
    """REF ponía 'peso 20%' a mano y la cuota real es `peso/total`."""
    pesos = {"cot": 0.0, "cvd_of": 20.0, "smc": 50.0, "killzone": 20.0, "smr_dxy": 10.0}
    lineas = pt.risk_policy_lines(_cfg(pesos), None, _dt(8))
    cuota = risk_engine.component_share(pesos, "killzone")
    assert "peso {0:.0f}%".format(cuota * 100) in "\n".join(lineas)


def test_el_peso_de_la_killzone_cero_no_se_veste_de_20():
    """`score.weights.killzone` es 0 en la config real. Decir '20%' ahí sería
    inventar peso; decir 0% es lo que el score usa de verdad."""
    pesos = {"cot": 0.0, "cvd_of": 20.0, "smc": 50.0, "killzone": 0.0, "smr_dxy": 15.0}
    texto = "\n".join(pt.risk_policy_lines(_cfg(pesos), None, _dt(8)))
    assert "peso 0% del score" in texto
    assert "peso 20%" not in texto


def test_el_peso_cambia_si_cambian_los_otros_pesos():
    kz = [{"name": "Londres", "start": "07:00", "end": "10:00"}]
    a = pt.risk_policy_lines(_cfg({"killzone": 20.0, "smc": 30.0, "cvd_of": 0.0}, killzones=kz), None, _dt(8))
    b = pt.risk_policy_lines(_cfg({"killzone": 20.0, "smc": 80.0, "cvd_of": 0.0}, killzones=kz), None, _dt(8))
    assert _peso_de(a) != _peso_de(b)


def _peso_de(lineas) -> str:
    for linea in lineas:
        if "peso" in linea:
            return linea
    return ""


def test_pesos_ilegibles_se_dicen_en_vez_de_desaparecer():
    """El `except: pass` de REF hacía que una línea de política se fuera sin que
    nadie lo notase: indistinguible de una política que no tiene killzone."""
    lineas = pt.risk_policy_lines(_cfg(pesos="{no soy json"), None, _dt(8))
    assert any("no calculado" in l for l in lineas)


def test_pesos_ausentes_no_se_toman_de_los_defaults_sin_decirlo():
    """`component_share` rellena con `DEFAULT_WEIGHTS`. Sin `score.weights` el
    prompt imprimiría ese número, presentado como el de la config."""
    texto = "\n".join(pt.risk_policy_lines(_cfg(), None, _dt(8)))
    assert "no calculado" in texto
    assert "del score" not in texto


def test_el_estado_ausente_no_se_omite():
    lineas = pt.risk_policy_lines(_cfg(), None, _dt(8))
    assert not any("DD" in l and "día de trading" in l for l in lineas)


def test_sin_estado_vivo_se_dice_que_no_se_puede_afirmar_dd():
    lineas = pt.risk_policy_lines(_cfg(), {"error": "MT5 no responde"}, _dt(8))
    texto = "\n".join(lineas)
    assert "SIN ESTADO EN VIVO" in texto
    assert "MT5 no responde" in texto
    assert "No puedes afirmar DD" in texto


def test_operar_bloqueado_se_anuncia():
    estado = {"blocked": True, "reasons": ["dd diario"]}
    texto = "\n".join(pt.risk_policy_lines(_cfg(), estado, _dt(8)))
    assert "OPERAR BLOQUEADO" in texto
    assert "dd diario" in texto


def test_el_estado_diario_normal_da_los_cuatro_numeros():
    estado = {
        "trading_day": "2026-10-03",
        "dd_daily": 120.5,
        "dd_pct": 1.2,
        "trades_today": 2,
        "max_trades_day": 4,
        "max_loss_fixed": 300.0,
        "max_loss_pct": 3.0,
    }
    texto = "\n".join(pt.risk_policy_lines(_cfg(), estado, _dt(8)))
    assert "2026-10-03" in texto
    assert "120.50" in texto
    assert "2/4 operaciones" in texto


def _prop_cfg(**extra: Any) -> Dict[str, Any]:
    return _cfg(prop={"enabled": True, "max_dd_daily_pct": 2, "max_dd_total_pct": 5, "max_profit_day_pct": 3, **extra})


def test_prop_firm_deshabilitado_se_dice():
    texto = "\n".join(pt.risk_policy_lines(_cfg(), None, _dt(8)))
    assert "prop firm: desactivado" in texto


def test_prop_firm_lee_el_yaml_anidado():
    """`prop.enabled: true` en el YAML tiene que llegar al prompt. Con las claves
    planas (`prop_enabled`) el prompt decía 'desactivado' con el prop encendido."""
    texto = "\n".join(pt.risk_policy_lines(_prop_cfg(), None, _dt(8)))
    assert "ENFORCED" in texto


def test_prop_firm_sin_metricas_vivas_dice_los_topes_del_yaml():
    texto = "\n".join(pt.risk_policy_lines(_prop_cfg(), None, _dt(8)))
    assert "ENFORCED" in texto
    assert "DD diario 2.0/2.0" in texto or "2.0%" in texto
    assert "métricas en vivo no disponibles" in texto


def test_prop_firm_con_metricas_vivas_las_da():
    estado = {"prop_enabled": True, "prop_dd_daily_pct": 0.4, "prop_dd_total_pct": 1.1, "prop_day_profit_pct": 0.2}
    texto = "\n".join(pt.risk_policy_lines(_prop_cfg(), estado, _dt(8)))
    assert "0.4" in texto
    assert "1.1" in texto
    assert "métricas en vivo no disponibles" not in texto


def test_consistency_days_no_se_promete():
    """`consistency_days` está en la config y NINGÚN código lo mira. Anunciarlo en
    la política que lee el decisor es prometer un filtro que el sistema no tiene."""
    texto = "\n".join(pt.risk_policy_lines(_prop_cfg(consistency_days=5), None, _dt(8)))
    assert "consistencia" in texto
    assert "NINGÚN" in texto


def test_consistency_days_no_se_anuncia_con_el_prop_apagado():
    """Sin prop encendido la línea sobra: anuncia un filtro de un modo que no corre."""
    texto = "\n".join(pt.risk_policy_lines(_cfg(), None, _dt(8)))
    assert "consistencia" not in texto


def test_el_enforcement_dice_que_la_killzone_no_congela():
    texto = "\n".join(pt.risk_policy_lines(_cfg(), None, _dt(8)))
    assert "La killzone solo avisa" in texto


def test_las_noticias_bloquean_o_no_segun_el_yaml():
    con = "\n".join(pt.risk_policy_lines(_cfg(data_sources={"news": True}), None, _dt(8)))
    sin = "\n".join(pt.risk_policy_lines(_cfg(data_sources={"news": False}), None, _dt(8)))
    assert "BLOQUEAN duro" in con
    assert "NO bloquean" in sin
    assert "fail-open" in con


def test_risk_policy_lines_tolera_cfg_vacia():
    """Un `strategy.yaml` corrupto no puede tumbar el prompt entero."""
    lineas = pt.risk_policy_lines(None, None, _dt(8))
    assert isinstance(lineas, list)
    assert lineas


def test_risk_policy_lines_tolera_un_now_raro():
    assert pt.risk_policy_lines(_cfg(), None, "no soy una fecha")


# ---------------------------------------------------------------------------
# Fuentes de datos
# ---------------------------------------------------------------------------


def test_data_sources_policy_marca_las_stale():
    lecturas = {"cot": {"stale": True, "reason": "CFTC sin fichero nuevo"}}
    texto = "\n".join(pt.data_sources_policy(_cfg(), lecturas))
    assert "cot ON (stale)" in texto
    assert "Fuentes degradadas" in texto
    assert "CFTC sin fichero nuevo" in texto


def test_data_sources_policy_sin_lecturas_no_inventa_degradaciones():
    texto = "\n".join(pt.data_sources_policy(_cfg(), {}))
    assert "Fuentes degradadas" not in texto


# ---------------------------------------------------------------------------
# Relojes
# ---------------------------------------------------------------------------


def test_clock_lines_da_las_tres_horas():
    lineas = pt.clock_lines(
        "2026-10-03 14:30:00 UTC", "2026-10-03 11:30:00 BRT", "2026-10-03"
    )
    texto = "\n".join(lineas)
    assert "UTC" in texto
    assert "broker" in texto
    assert "Día de trading" in texto
    assert "BRT" in texto


def test_clock_lines_sin_bróker_no_inventa_una_hora():
    lineas = pt.clock_lines("2026-10-03 14:30:00 UTC")
    assert not any("broker" in l.lower() for l in lineas)


def test_clock_lines_sin_argumentos_usa_el_reloj_canonico():
    assert pt.clock_lines()[0].endswith("UTC")


# ---------------------------------------------------------------------------
# system_prompt: el orden es la decisión
# ---------------------------------------------------------------------------


def test_el_rol_del_usuario_va_primero():
    prompt = pt.system_prompt("Sos un analista escéptico.", "FOREX_SPOT", _cfg())
    assert prompt.startswith("Sos un analista escéptico.")
    assert prompt.index("Sos un analista") < prompt.index("## Mercado")


def test_la_regla_dura_va_al_final():
    """Lo que se lee último es lo que sobrevive al contexto largo, y la regla dura
    es la que compite con el impulso de redactar entrada + SL + TP."""
    prompt = pt.system_prompt("Rol", "FOREX_SPOT", _cfg())
    assert prompt.rstrip().endswith(pt.REGLAS_DURA[-1])


def test_la_regla_dura_dice_que_el_score_no_lo_calcula_el_modelo():
    texto = pt.REGLAS_DURA
    assert "Setup Score" in texto
    assert "core/risk_engine.py" in texto
    assert "NO los recalcules" in texto


def test_system_prompt_sin_rol_todavia_manda():
    prompt = pt.system_prompt(None, "FOREX_SPOT", _cfg())
    assert "## Mercado" in prompt
    assert prompt.rstrip().endswith(pt.REGLAS_DURA[-1])


def test_system_prompt_incluye_la_politica_de_fuentes():
    prompt = pt.system_prompt("Rol", "FOREX_SPOT", _cfg(), None, {"cot": {"stale": True}})
    assert "Fuentes" in prompt or "fuentes" in prompt


def test_live_data_block_une_datos_y_politica():
    texto = pt.live_data_block(["- Hora: 14:30"], ["- Sin noticias"], "CABECERA:")
    assert texto.startswith("CABECERA:")
    assert "- Hora: 14:30" in texto
    assert texto.index("- Hora: 14:30") < texto.index("- Sin noticias")


def test_live_data_block_vacio_no_inyecta_cabecera_sola():
    assert pt.live_data_block([]) == ""


# ---------------------------------------------------------------------------
# Temas y complejidad
# ---------------------------------------------------------------------------


def test_menciona_usa_la_raiz_del_token():
    assert pt.menciona("¿me compras EURUSD?", ["compra"])
    assert pt.menciona("quiero vender", ["vend"])
    assert not pt.menciona("nothing", ["compra"])


def test_menciona_no_dispara_con_sl_en_medio_del_token():
    """Lo que arregla el prefijo es 'isla': contiene 'sl' pero no lo empieza."""
    assert not pt.menciona("una isla", ["sl"])


def test_menciona_acepta_los_falsos_positivos_que_siempiezan_por_la_raiz():
    """El prefijo es deliberado: 'sla' y 'slide' empiezan por 'sl' y el LLM escribe
    'sl', no 'stop loss'. Un test que prometa que no dispara con ellos estaría
    mintiendo sobre el compromiso que hace `menciona`."""
    assert pt.menciona("un sla en la mesa", ["sl"])


def test_menciona_sigue_disparando_con_la_raiz_del_token():
    assert pt.menciona("ponme un sl de 20", ["sl"])
    assert pt.menciona("quiero stop loss", ["stop"])


def test_menciona_tolera_una_lista_vacia():
    assert not pt.menciona("lo que sea", [])


def test_topics_for_añade_el_mercado_sin_pisar_el_base():
    base = {"trade": ["sl"], "account": ["saldo"]}
    salida = pt.topics_for("B3_FUTURES", base)
    assert "sl" in salida["trade"]
    assert "contrato" in salida["trade"]
    assert salida["account"] == ["saldo"]


def test_topics_for_no_muta_el_base():
    base = {"trade": ["sl"]}
    pt.topics_for("B3_FUTURES", base)
    assert base == {"trade": ["sl"]}


def test_is_complex_request_con_cupom_de_b3_es_complejo():
    """REF no lo detectaba: su lista era toda de forex y el modelo respondía de
    memoria sin consultar nada."""
    assert pt.is_complex_request("analiza el cupom del DI1", "B3_FUTURES")


def test_is_complex_request_con_cupom_treasury_usa_su_propia_parte():
    assert pt.is_complex_request("como va la duration", "B3_TESOURO")


def test_topics_for_con_funding_de_cripto_es_complejo():
    assert pt.is_complex_request("cómo va el funding", "CRYPTO_PERPETUAL")


def test_is_complex_request_de_forex_sigue_funcionando():
    assert pt.is_complex_request("hazme un análisis con order flow", "FOREX_SPOT")


def test_is_complex_request_no_es_complejo_si_no_toca_nada():
    assert not pt.is_complex_request("hola", "FOREX_SPOT")
    assert not pt.is_complex_request("", "FOREX_SPOT")


# ---------------------------------------------------------------------------
# Traza de herramientas
# ---------------------------------------------------------------------------


def test_tool_digest_lleva_los_argumentos():
    """Sin args la traza no es reproducible: no se puede saber qué se consultó."""
    sobre = json.loads(pt.tool_digest("price", {"symbol": "XAUUSD"}, {"status": "ok", "data": {"bid": 1.0}}))
    assert sobre["args"] == {"symbol": "XAUUSD"}
    assert sobre["tool"] == "price"
    assert sobre["status"] == "ok"
    assert sobre["data"] == {"bid": 1.0}


def test_tool_digest_declara_el_recorte_y_el_tamaño_real():
    """`bytes` es el tamaño ANTES de recortar: sin él, un recorte es indistinguible
    de una respuesta corta."""
    grande = {"items": list(range(2000))}
    sobre = json.loads(pt.tool_digest("chart_snapshot", {}, {"status": "ok", "data": grande}))
    assert sobre["truncado"] is True
    assert sobre["bytes"] > pt.MAX_DIGEST_CHARS


def test_tool_digest_no_declara_recorte_si_no_hace_falta():
    sobre = json.loads(pt.tool_digest("now", {}, {"status": "ok", "data": {"a": 1}}))
    assert "truncado" not in sobre


def test_tool_digest_lleva_el_error():
    sobre = json.loads(pt.tool_digest("price", {}, {"status": "failed", "data": None, "error": "símbolo"}))
    assert sobre["error"] == "símbolo"
    assert sobre["data"] is None
    assert sobre["bytes"] == 0


def test_tool_digest_usa_el_reloj_inyectado():
    ahora = _dt(14, 30)
    sobre = json.loads(pt.tool_digest("now", {}, {"status": "ok", "data": {}}, 1, 12.0, ahora))
    assert sobre["at"] == "2026-10-03T14:30:00+00:00"
    assert sobre["round"] == 1
    assert sobre["elapsed_ms"] == 12.0


def test_tool_digest_es_texto_json_serializable():
    """La columna es TEXT y el sobre tiene que poder releerse con un `SELECT` y un
    `json.loads` sin este repo."""
    texto = pt.tool_digest("price", {"symbol": "EURUSD"}, {"status": "ok", "data": {"bid": 1}})
    assert isinstance(texto, str)
    assert json.loads(texto)["tool"] == "price"


def test_tool_digest_recorta_los_argumentos_grandes():
    sobre = json.loads(pt.tool_digest("x", {"notas": "a" * 5000}, {"status": "ok", "data": {}}))
    assert len(json.dumps(sobre["args"])) < 1000


# ---------------------------------------------------------------------------
# Dibujos
# ---------------------------------------------------------------------------


def test_summarize_drawings_lee_mas_de_lo_que_dibuja():
    salida = pt.summarize_drawings(
        [
            {"tool": "hline", "p0": 1.16500, "origin": "user"},
            {"tool": "text", "p0": 1.16, "t0": 5, "text": "resistencia", "origin": "user"},
            {"tool": "line", "t0": 1, "p0": 1.16, "t1": 2, "p1": 1.17, "origin": "agent"},
        ],
        "EURUSD",
        "M15",
    )
    assert salida["count"] == 3
    assert "nivel 1.16500" in salida["lectura"]
    assert "resistencia" in salida["lectura"]
    assert "[agente]" in salida["lectura"]
    assert salida["drawings"][0]["tool"] == "hline"


def test_summarize_drawings_sin_dibujos_lo_dice():
    salida = pt.summarize_drawings([], "EURUSD", "M15")
    assert salida["count"] == 0
    assert "No hay dibujos" in salida["lectura"]


def test_summarize_drawings_ignora_lo_que_no_es_dict():
    salida = pt.summarize_drawings([{"tool": "hline", "p0": 1.1}, "basura", None], "EURUSD", "M15")
    assert salida["count"] == 3
    assert salida["lectura"].count(";") == 0


def test_summarize_drawings_con_coordenadas_basura_no_revienta():
    salida = pt.summarize_drawings([{"tool": "line", "t0": 1, "p0": None, "t1": 2, "p1": "x"}], "EURUSD", "M15")
    assert salida["count"] == 1
    assert "tendencia" in salida["lectura"]


# ---------------------------------------------------------------------------
# describe_number
# ---------------------------------------------------------------------------


def test_describe_number_da_interrogante_y_no_none():
    """Un `None` impreso es 'None' y el modelo lo rellena; `?` es una respuesta."""
    assert pt.describe_number(None) == "?"
    assert pt.describe_number("") == "?"
    assert pt.describe_number(float("nan")) == "?"
    assert pt.describe_number(float("inf")) == "?"


def test_describe_number_formatea():
    assert pt.describe_number(1.165) == "1.16500"
    assert pt.describe_number(1.165, 2) == "1.17"


def test_describe_number_deja_pasar_lo_que_no_es_numero():
    assert pt.describe_number("EURUSD") == "EURUSD"