"""Reality-check: el motor de Order Flow contra la cinta REAL de Databento.

El resto de `test_orderflow.py` alimenta el motor con fixtures sintéticos: eso
mide el código contra lo que el código esperaba. Este archivo hace lo contrario.
Corre `OrderFlowEngine` sobre `tests/fixtures/orderflow_6e_real.jsonl` (21926
trades reales de 6EZ6, 8 h de NY del 2026-10-05) y fija los números que
`core/orderflow_config.py` cita en sus comentarios.

Qué protege:

- Que el umbral derive del techo y siga POR DEBAJO de él. Si se clava un valor
  absoluto mayor que `zscore_ceiling(ema_window)`, el detector se apaga sin
  error y en silencio: era exactamente el fallo del 4.5 fijo con ventana 20.
- Que sobre cinta real el detector no se quede muerto (0 alertas) ni se
  desborde (alertas en cada trade). Entre medias están las 8 zonas medidas.
- Que la prosa de `orderflow_config.py` no se desincronice del fixture: los
  conteos que afirma el módulo son los mismos que aquí se miden, y encadenados
  contra el sidecar, que a su vez queda fijado por sha256.

Procedencia: `research/build_real_fixture.py` regenera fixture y sidecar a
partir del Parquet descargado (fuera del repo). Si este test falla por sha, el
fixture se tocó a mano o cambió la sesión: se regenera, no se parchea.

100% offline: no toca la API de Databento.

Run:  python -m pytest tests/unit/test_orderflow_real.py -v
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

import pytest

from adapters import synthetic_feed as mock_feed
from core.orderflow_config import (
    OF_ABSORB_VOL_MIN,
    OF_EMA_WINDOW,
    OF_SYMBOL,
    OF_ZSCORE_MIN_SIZE,
    OF_ZSCORE_THRESHOLD,
    zscore_ceiling,
)
from core.orderflow_engine import OrderFlowEngine

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURE = FIXTURES / "orderflow_6e_real.jsonl"
SIDECAR = FIXTURES / "orderflow_6e_real.meta.json"


@pytest.fixture(scope="module")
def sidecar() -> dict:
    return json.loads(SIDECAR.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cinta() -> list[dict]:
    """`load_fixture` ya valida shape completo y side en ("A", "B")."""
    return mock_feed.load_fixture(str(FIXTURE))


@pytest.fixture(scope="module")
def medido(cinta: list[dict]) -> dict:
    """Una sola pasada del motor sobre el fixture: los tests leen el resultado.

    Dos motores, porque la comprobación del techo necesita el umbral movido a
    5.0: es el único modo de demostrar que ese 5.0 es inalcanzable de verdad y
    no solo que aquí no pasó nada.
    """
    engine = OrderFlowEngine()
    z25 = z45 = z45_gated = 0
    for t in cinta:
        payload = engine.add_trade(t["price"], t["size"], t["side"], t["ts"])
        if payload is None:
            continue
        if payload["zscore"] >= 2.5:
            z25 += 1
        if payload["zscore"] >= 4.5:
            z45 += 1
            if t["size"] >= OF_ZSCORE_MIN_SIZE:
                z45_gated += 1
    snap = engine.snapshot()

    estricto = OrderFlowEngine()
    estricto.update_settings(zscore_threshold=5.0)
    z50 = sum(
        1
        for t in cinta
        if (
            payload := estricto.add_trade(t["price"], t["size"], t["side"], t["ts"])
        ) is not None
        and payload["zscore"] >= 5.0
    )

    sizes = [t["size"] for t in cinta]
    vols_300 = [sum(sizes[i - 299 : i + 1]) for i in range(299, len(sizes))]
    return {
        "n": len(cinta),
        "media": sum(sizes) / len(sizes),
        "mediana": statistics.median(sizes),
        "max_size": max(sizes),
        "precios": len({t["price"] for t in cinta}),
        "lados": {t["side"] for t in cinta},
        "ge75": sum(1 for s in sizes if s >= OF_ZSCORE_MIN_SIZE),
        "vol300_min": min(vols_300),
        "vol300_media": sum(vols_300) / len(vols_300),
        "z25": z25,
        "z45": z45,
        "z45_gated": z45_gated,
        "z50": z50,
        "spikes": snap["spike_count"],
        "zonas_spike": snap["spike_episodes"],
        "zonas_absorcion": snap["absorb_episodes"],
        "cvd": snap["cvd"],
        "buy_vol": snap["buy_vol"],
        "sell_vol": snap["sell_vol"],
        "total_vol": snap["total_vol"],
    }


class TestProcedenciaDelFixture:
    """El fixture es un artefacto derivado: tiene que ser regenerable y trazable."""

    def test_sha_y_filas_coinciden_con_el_sidecar(self, cinta: list[dict], sidecar: dict):
        data = FIXTURE.read_bytes()
        sha = hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
        assert sha == sidecar["sha256_lf"], (
            "el fixture ya no es el que describe el sidecar; regenerarlo con "
            "python research/build_real_fixture.py en lugar de editarlo"
        )
        assert sidecar["rows"] == len(cinta)

    def test_identidad_y_contrato_negociado(self, sidecar: dict):
        db = sidecar["databento"]
        assert db["identity_symbol"] == OF_SYMBOL, "la identidad del símbolo no se toca"
        assert db["resolved_symbol"] == "6EZ6", "pero hay que seguir descargando el líquido"
        assert db["window_end_utc"] > db["window_start_utc"]
        assert db["dataset"] == "GLBX.MDP3"

    def test_sidecar_refleja_lo_que_el_motor_responde(self, medido: dict, sidecar: dict):
        c, m = sidecar["stats"]["cinta"], sidecar["stats"]["motor"]
        assert c["trades"] == medido["n"] == 21926
        assert c["precios_distintos"] == medido["precios"]
        assert c["trades_size_ge_75"] == medido["ge75"]
        assert c["vol_300_min"] == medido["vol300_min"]
        assert m["prints_z_ge_2_5"] == medido["z25"]
        assert m["prints_z_ge_4_5"] == medido["z45"]
        assert m["zonas_spike"] == medido["zonas_spike"]
        assert m["zonas_absorcion"] == medido["zonas_absorcion"]


class TestEstadisticosDeLaCintaReal:
    """Lo que la cinta real es, medido: esto es lo que obligó a recalibrar."""

    def test_mediana_dos_y_media_lejos_del_gate(self, medido: dict):
        # El mock asumía sizes de ~12; la cinta real tiene mediana 2. El gate
        # institucional (>=75) queda muy por encima del print típico.
        assert medido["mediana"] == 2.0
        assert medido["media"] == pytest.approx(4.156, abs=0.005)
        assert medido["media"] < OF_ZSCORE_MIN_SIZE

    def test_el_tallaje_institucional_es_excepcional(self, medido: dict):
        assert medido["ge75"] == 16, "solo 16 prints en 21926 llegan al gate"
        assert medido["max_size"] == 230
        assert medido["ge75"] < medido["n"] / 1000, "el gate no puede ser rutinario"

    def test_ambos_lados_del_libro_presentes(self, medido: dict):
        assert medido["lados"] == {"A", "B"}
        # CVD sin compradores o sin vendedores no mide nada.
        assert medido["buy_vol"] + medido["sell_vol"] == medido["total_vol"]
        assert medido["cvd"] != 0.0

    def test_ventana_de_300_acumula_lejos_del_vol_min(self, medido: dict):
        # El vol_min (40) no puede ser el gate vinculante en una sesión real:
        # la ventana más flaca de toda la sesión ya acumula veinte veces eso,
        # así que en la práctica lo que filtra es el rango y el delta.
        assert medido["vol300_min"] == 837
        assert medido["vol300_min"] >= 20 * OF_ABSORB_VOL_MIN
        assert medido["vol300_media"] == pytest.approx(1249, abs=10)


class TestUmbralesSobreCintaReal:
    """El detector con los umbrales vigentes, contra datos que no son suyos."""

    def test_umbral_derivado_y_por_debajo_del_techo(self):
        techo = zscore_ceiling(OF_EMA_WINDOW)
        assert OF_ZSCORE_THRESHOLD == pytest.approx(4.5043, abs=1e-4)
        assert OF_ZSCORE_THRESHOLD < techo, (
            "un umbral >= techo apaga el detector sin error, en silencio"
        )
        assert OF_EMA_WINDOW == 50, "el techo de 4.9497 solo vale para w=50"

    def test_conteos_de_zscore_en_la_cinta_real(self, medido: dict, sidecar: dict):
        # La prosa de orderflow_config.py cita exactamente estos números.
        assert sidecar["stats"]["motor"]["prints_z_ge_2_5"] == medido["z25"] == 631
        assert sidecar["stats"]["motor"]["prints_z_ge_4_5"] == medido["z45"] == 61

    def test_el_gate_de_size_reduce_a_zonas_significativas(self, medido: dict):
        # 61 prints a Z>=4.5, pero solo 8 superan size>=75: el gate es lo que
        # convierte ruido microestructural en episodios accionables.
        assert medido["z45_gated"] == 8
        assert medido["spikes"] == 8
        assert medido["zonas_spike"] == 8
        assert medido["z45_gated"] < medido["z45"]

    def test_el_techo_hace_5_0_inalcanzable(self, medido: dict):
        # Con el umbral movido a 5.0 no cabe NI UN print por debajo del techo
        # (4.9497 con w=50): la prosa "4.5 -> 61, 5.0 -> 0" es exacta.
        assert zscore_ceiling(OF_EMA_WINDOW) < 5.0
        assert medido["z50"] == 0

    def test_detector_acotado_ni_muerto_ni_desbordado(self, medido: dict):
        # 8 zonas en 8 h: ni 0 (detector muerto por umbral mal derivado) ni
        # miles (umbral tan bajo que todo es alerta).
        assert 1 <= medido["zonas_spike"] <= 20
        assert 1 <= medido["zonas_absorcion"] <= 10
        assert medido["zonas_spike"] < medido["z25"] / 10
