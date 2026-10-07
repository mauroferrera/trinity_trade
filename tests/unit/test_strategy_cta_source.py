"""El lector del perfil del CTA Swing D1 (`settings/strategy_cta_source.py`, F4).

Mismo reparto que `TestStrategyMapSource` en `test_strategy_map.py`: este fichero
prueba la FORMA del YAML (tipos) y la caché. La coherencia ENTRE ficheros —que el
magic exista en el mapa, que `symbols` no esté vacío, que el motor traiga sus tres
parámetros— es del servicio, y vive en `test_cta_alert_service.py`.

Las tres diferencias con los otros sources que el módulo documenta, comprobadas
aquí:

1. Ausente → `{}`, no un error: no desplegar una estrategia es un estado
   legítimo, no un fallo de arranque (a diferencia de `strategy.yaml`, que es
   obligatorio porque define si se opera).
2. Shape roto → `StrategyConfigError` con el campo culpable: un perfil inválido
   tiene que sonar, no convertirse en una estrategia que corre con lo que hubiera.
3. La copia es PROFUNDA: `engine` y `symbols` son objetos anidados, y con una
   copia superficial un consumidor que los editara corrompería la caché.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.strategy import StrategyConfigError
from settings import strategy_cta_source

PERFIL_TEXT = """\
enabled: true
magic: 8882027
comment: "Trinity CTA D1"
timeframe: D1
bars: 120
symbols:
  - EURUSD
  - XAUUSD
engine:
  atr_n: 14
  don_n: 20
  mult: 3.0
"""


@pytest.fixture()
def perfil_tmp(tmp_path, monkeypatch):
    """Un `strategy_cta.yaml` temporal, con la caché del source vaciada."""
    target = tmp_path / "strategy_cta.yaml"
    target.write_text(PERFIL_TEXT, encoding="utf-8")
    monkeypatch.setattr(strategy_cta_source, "STRATEGY_CTA_PATH", str(target))
    strategy_cta_source.invalidate()
    yield target
    strategy_cta_source.invalidate()


class TestStrategyCtaSource:
    def test_load_del_fichero_temporal(self, perfil_tmp: Path) -> None:
        perfil = strategy_cta_source.load()

        assert perfil["enabled"] is True
        assert perfil["magic"] == 8882027
        assert perfil["timeframe"] == "D1"
        assert perfil["symbols"] == ["EURUSD", "XAUUSD"]
        assert perfil["engine"] == {"atr_n": 14, "don_n": 20, "mult": 3.0}

    def test_load_devuelve_una_copia_profunda(self, perfil_tmp: Path) -> None:
        """`engine` y `symbols` son anidados: tocarlos no puede colar en la caché."""
        primero = strategy_cta_source.load()
        primero["engine"]["atr_n"] = 999
        primero["symbols"].append("GBPUSD")

        segundo = strategy_cta_source.load()

        assert segundo["engine"]["atr_n"] == 14
        assert segundo["symbols"] == ["EURUSD", "XAUUSD"]

    def test_fichero_ausente_es_un_perfil_vacio_no_un_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diferencia decisiva con `strategy_source`: aquí ausente es `{}`."""
        monkeypatch.setattr(
            strategy_cta_source, "STRATEGY_CTA_PATH",
            str(tmp_path / "no_existe.yaml"))
        strategy_cta_source.invalidate()

        assert strategy_cta_source.load() == {}

    def test_yaml_roto_es_strategy_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sintaxis rota traducida, no el error crudo de PyYAML escondiéndose."""
        target = tmp_path / "strategy_cta.yaml"
        target.write_text("enabled: [", encoding="utf-8")
        monkeypatch.setattr(strategy_cta_source, "STRATEGY_CTA_PATH", str(target))
        strategy_cta_source.invalidate()

        with pytest.raises(StrategyConfigError, match="no es un YAML válido"):
            strategy_cta_source.load()

    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("enabled: si", "'enabled' debe ser true/false"),
            ("enabled: 1", "'enabled' debe ser true/false"),
            ("bars: 0", "'bars' debe ser un entero > 0"),
            ("magic: 1.5", "'magic' debe ser un entero > 0"),
            ("magic: true", "'magic' debe ser un entero > 0"),
            ("timeframe: 7", "'timeframe' debe ser un texto no vacío"),
            ("comment: ''", "'comment' debe ser un texto no vacío"),
            ("symbols: EURUSD", "'symbols' debe ser una lista de textos"),
            ("symbols:\n  - 7", "'symbols' debe ser una lista de textos"),
            ("engine: [1, 2]", "'engine' debe ser un mapeo"),
            ("engine:\n  atr_n: -3", "'engine.atr_n' debe ser un entero > 0"),
            ("engine:\n  mult: alto", "'engine.mult' debe ser un número > 0"),
            ("engine:\n  mult: -1", "'engine.mult' debe ser un número > 0"),
        ],
    )
    def test_shape_invalido_dice_el_campo(
        self, perfil_tmp: Path, texto: str, esperado: str
    ) -> None:
        perfil_tmp.write_text(texto, encoding="utf-8")
        strategy_cta_source.invalidate()

        with pytest.raises(StrategyConfigError, match=esperado):
            strategy_cta_source.load()

    def test_no_valida_coherencia_solo_tipos(self, perfil_tmp: Path) -> None:
        """Un magic que no exista en el mapa no es problema DEL LECTOR.

        Esa coherencia entre dos ficheros la decide el servicio en cada scan; si el
        source la metiera aquí, `core/` y `settings/` se acoplarían a un mapa que
        pueden leer por separado.
        """
        perfil_tmp.write_text("enabled: true\nmagic: 1234567\n", encoding="utf-8")
        strategy_cta_source.invalidate()

        perfil = strategy_cta_source.load()

        assert perfil["magic"] == 1234567

    def test_el_fichero_real_tiene_la_forma_convalidada(self) -> None:
        """El perfil DESPLEGADO en F4: el que producción va a leer."""
        strategy_cta_source.invalidate()
        try:
            perfil = strategy_cta_source.load(force=True)
            assert perfil.get("enabled") is True
            assert perfil.get("magic") == 8882027
            assert str(perfil.get("timeframe") or "").upper() == "D1"
            assert perfil.get("engine", {}).get("don_n") == 20
            assert perfil.get("engine", {}).get("atr_n") == 14
            assert "EURUSD" in (perfil.get("symbols") or [])
        finally:
            strategy_cta_source.invalidate()

    def test_la_cache_se_invalida_sola_al_editar_el_fichero(
        self, perfil_tmp: Path
    ) -> None:
        assert strategy_cta_source.load()["magic"] == 8882027
        perfil_tmp.write_text("enabled: false\n", encoding="utf-8")

        assert strategy_cta_source.load() == {"enabled": False}

    def test_invalidate_obliga_a_releer(
        self, perfil_tmp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La caché es por firma de fichero; `invalidate` la salta a propósito."""
        original = strategy_cta_source._leer
        llamadas: list = []

        def _leer_cuenta() -> str:
            llamadas.append(1)
            return original()

        monkeypatch.setattr(strategy_cta_source, "_leer", _leer_cuenta)

        strategy_cta_source.load()
        strategy_cta_source.load()
        assert len(llamadas) == 1

        strategy_cta_source.invalidate()
        strategy_cta_source.load()
        assert len(llamadas) == 2
