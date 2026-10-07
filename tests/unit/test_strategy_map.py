"""El mapa de estrategias (D-071, F1): magic -> perfil y SL por perfil.

`core/strategy_map.py` es un módulo PURO — no puede abrir ficheros (test de
pureza), así que aquí se le pasa el texto del mapa real y los dicts de config a
mano, igual que `test_strategy.py` le pasa el texto del `strategy.yaml` a su
parte pura.

Lo que importa, en orden:

  1. El fichero REAL `strategy_map.yaml` se lee y se parsea contra la firma de
     `parse_map`: un shape roto (magics decimales, perfil vacío) debe LLANZAR,
     no degradar a "default" — un default silencioso escondería un mapa que nadie
     pudo leer.
  2. El "default" es el heredero histórico: un magic desconocido, o la ausencia
     del fichero, resuelve SIEMPRE al perfil "default", que es `strategy.yaml`.
     Cambiar eso cambiaría el comportamiento de la estrategia que ya opera.
  3. `by_profile_by_symbol` es el antidoto del contagio: la distancia de un
     símbolo en el slot del CTA no puede caerle al ILOF. Y para el perfil
     default, el `sl_distance_by_symbol` legacy sigue mandando: su precedencia
     no se toca.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from core import paths as core_paths
from core import strategy as st
from core import strategy_map
from settings import strategy_map_source

YAML_MAP_REAL = Path(core_paths.STRATEGY_MAP_PATH)
MAPA_CTA = {8882026: "default", 9999001: "cta"}


def _fixture_flat() -> dict:
    """Una config plana con los tres shapes: slots por perfil, legacy y global."""
    return {
        "sl_distance": 0.0012,
        "sl_distance_by_symbol": {"XAUUSD": 3.0},
        "by_profile_by_symbol": {"cta": {"XAUUSD": 7.0}},
        "sl_default_pips": 20,
    }


def _spec(pip: Optional[float] = 1.0) -> Any:
    return SimpleNamespace(pip=pip)


# ---------------------------------------------------------------------------
# Parseo del mapa
# ---------------------------------------------------------------------------


class TestParseMap:
    def test_el_fichero_real_se_parsea(self) -> None:
        import yaml

        with open(YAML_MAP_REAL, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        mapa = strategy_map.parse_map(doc)

        assert mapa == {8882026: "default", 8882027: "cta"}

    def test_acepta_el_mapeo_pelado(self) -> None:
        assert strategy_map.parse_map({8882026: "default", 9999001: "cta"}) == MAPA_CTA

    def test_acepta_magic_en_string(self) -> None:
        assert strategy_map.parse_map({"8882026": "default"}) == {8882026: "default"}

    def test_magic_decimal_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map({8882026.5: "default"})

    def test_magic_no_numerico_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map({"abc": "default"})

    def test_perfil_vacio_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map({8882026: ""})

    def test_perfil_no_texto_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map({8882026: 7})

    def test_documento_vacio_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map({"map": {}})

    def test_no_es_mapeo_lanza(self) -> None:
        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map.parse_map(["8882026"])


# ---------------------------------------------------------------------------
# Resolución de perfil
# ---------------------------------------------------------------------------


class TestProfileForMagic:
    def test_magic_del_mapa(self) -> None:
        assert strategy_map.profile_for_magic(9999001, MAPA_CTA) == "cta"

    def test_magic_desconocido_cae_en_default(self) -> None:
        assert strategy_map.profile_for_magic(12345, MAPA_CTA) == "default"

    def test_sin_mapa_todo_es_default(self) -> None:
        assert strategy_map.profile_for_magic(9999001) == "default"

    def test_magic_nulo_es_default(self) -> None:
        assert strategy_map.profile_for_magic(None) == "default"

    def test_magics_de_un_perfil_ordenados(self) -> None:
        mapa = {8882026: "default", 2: "cta", 1: "cta", 9999001: "default"}
        assert strategy_map.magics_for("cta", mapa) == [1, 2]
        assert strategy_map.magics_for("default", mapa) == [8882026, 9999001]

    def test_magics_de_perfil_que_no_existe(self) -> None:
        assert strategy_map.magics_for("cta") == []


# ---------------------------------------------------------------------------
# Distancia de SL por perfil
# ---------------------------------------------------------------------------


class TestSLDistancePorPerfil:
    def test_el_slot_del_perfil_manda(self) -> None:
        dist, origen = strategy_map.sl_distance_by_profile(
            _fixture_flat(), 9999001, "XAUUSD", None, MAPA_CTA)

        assert dist == pytest.approx(7.0)
        assert origen == "profile_symbol"

    def test_el_slot_no_contagia_entre_perfiles(self) -> None:
        """El slot del CTA no le cae al ILOF: el default ni lo mira."""
        dist, origen = strategy_map.sl_distance_by_profile(
            _fixture_flat(), 8882026, "XAUUSD", None, MAPA_CTA)

        assert dist == pytest.approx(3.0)
        assert origen == "symbol"

    def test_el_default_tiene_sus_propios_slots(self) -> None:
        cfg = dict(_fixture_flat(),
                   by_profile_by_symbol={"default": {"XAUUSD": 9.0}})

        dist, _ = strategy_map.sl_distance_by_profile(cfg, 8882026, "XAUUSD", None)

        assert dist == pytest.approx(9.0)

    def test_un_simbolo_fuera_del_slot_cae_al_global(self) -> None:
        dist, origen = strategy_map.sl_distance_by_profile(
            _fixture_flat(), 9999001, "EURUSD", None, MAPA_CTA)

        assert dist == pytest.approx(0.0012)
        assert origen == "global"

    def test_el_slot_acepta_json_string(self) -> None:
        cfg = dict(_fixture_flat())
        cfg["by_profile_by_symbol"] = json.dumps({"cta": {"XAUUSD": 7.0}})

        dist, _ = strategy_map.sl_distance_by_profile(cfg, 9999001, "XAUUSD", None, MAPA_CTA)

        assert dist == pytest.approx(7.0)

    def test_el_global_no_engaña_al_default(self) -> None:
        """Para el default, el legacy manda sobre el global: precedencia histórica."""
        dist, origen = strategy_map.sl_distance_by_profile(
            _fixture_flat(), 8882026, "EURJPY", None, MAPA_CTA)

        assert dist == pytest.approx(0.0012)
        assert origen == "global"

    def test_sin_distancia_nada_usar_pips_para_un_perfil_nominado(self) -> None:
        cfg = {"by_profile_by_symbol": {"cta": {"XAUUSD": 7.0}}, "sl_default_pips": 20}

        dist, origen = strategy_map.sl_distance_by_profile(
            cfg, 9999001, "EURUSD", _spec(pip=0.01), MAPA_CTA)

        assert dist == pytest.approx(0.2)
        assert origen == "legacy_pips"

    def test_sin_nada_es_none(self) -> None:
        dist, origen = strategy_map.sl_distance_by_profile({}, 9999001, "EURUSD", None, MAPA_CTA)

        assert dist is None
        assert origen == "none"

    def test_comportamiento_idéntico_al_legacy_para_el_default(self) -> None:
        """Sin mapa ni slots, el default resuelve EXACTAMENTE como sl_distance_for:
        la estrategia que ya opera no cambia ni un decimal."""
        cfg = _fixture_flat()
        for magic, symbol in ((8882026, "XAUUSD"), (8882026, "EURUSD")):
            assert strategy_map.sl_distance_by_profile(cfg, magic, symbol, None) == (
                st.sl_distance_for(cfg, symbol, None))


# ---------------------------------------------------------------------------
# El lector del fichero (settings.strategy_map_source)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mapa_tmp(tmp_path, monkeypatch):
    """Un `strategy_map.yaml` temporal, con la caché del source vaciada."""
    target = tmp_path / "strategy_map.yaml"
    target.write_text("map:\n  8882026: default\n  9999001: cta\n", encoding="utf-8")
    monkeypatch.setattr(strategy_map_source, "STRATEGY_MAP_PATH", str(target))
    strategy_map_source.invalidate()
    yield target
    strategy_map_source.invalidate()


class TestStrategyMapSource:
    def test_load_del_fichero_temporal(self, mapa_tmp: Path) -> None:
        assert strategy_map_source.load() == {8882026: "default", 9999001: "cta"}

    def test_load_devuelve_una_copia(self, mapa_tmp: Path) -> None:
        primero = strategy_map_source.load()
        primero[111] = "cta"

        assert 111 not in strategy_map_source.load()

    def test_fichero_ausente_es_el_mapa_de_fabrica(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            strategy_map_source, "STRATEGY_MAP_PATH", str(tmp_path / "no_existe.yaml"))
        strategy_map_source.invalidate()

        assert strategy_map_source.load() == strategy_map.DEFAULT_MAP

    def test_fichero_mal_formado_lanza(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "strategy_map.yaml"
        target.write_text("map: [", encoding="utf-8")
        monkeypatch.setattr(strategy_map_source, "STRATEGY_MAP_PATH", str(target))
        strategy_map_source.invalidate()

        with pytest.raises(strategy_map.StrategyMapError):
            strategy_map_source.load()

    def test_la_caché_se_invalida_sola_al_editar_el_fichero(self, mapa_tmp: Path) -> None:
        assert strategy_map_source.load() == {8882026: "default", 9999001: "cta"}
        mapa_tmp.write_text("map:\n  8882026: default\n", encoding="utf-8")

        assert strategy_map_source.load() == {8882026: "default"}