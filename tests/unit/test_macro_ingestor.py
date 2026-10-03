"""Tests de `macro_ingestor/`: COT, DXY/SMR y calendario.

Fase 4. Ninguno de estos tests toca la red: el `opener` se inyecta siempre. Eso no
es solo comodidad, es la razón de que exista el seam: si el feed se puede sustituir
por una función, un fallo de red se puede reproducir a voluntad y se puede
comprobar que **degrada a neutro** en vez de propagar la excepción.

Los tres bloques reúnen el mismo criterio: una fuente macro que falla no puede tirar
el bot. Donde ese criterio es discutible (el gate de noticias) el test documenta el
porqué en vez de dejarlo en un comentario.
"""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

import pytest

from macro_ingestor import registry as reg
from macro_ingestor.base_ingestor import (
    FeedUnreadable,
    FeedUnavailable,
    IngestorError,
    TTLCache,
    neutro,
    reading,
)
from macro_ingestor.forex import calendar_news as cn
from macro_ingestor.forex import cot_service as cot
from macro_ingestor.forex import dxy_service as dxy

T0 = 1_760_000_000  # base fija: ningún test depende de la hora real


def make_opener(handler: Callable[[str], str]) -> Callable[..., Any]:
    """Convierte `url -> texto` en el `opener` que espera `http_text`.

    El seam entrega un `urllib.request.Request` y espera algo con `read()` en un
    `with`. Los tests no deberían tener que saber de eso, así que el adaptador se
    paga una vez aquí.
    """

    def opener(req, timeout=None):
        url = getattr(req, "full_url", None) or str(req)
        return io.BytesIO(handler(url).encode("utf-8"))

    return opener


def caido(mensaje: str = "red caída") -> Callable[[str], str]:
    """Un opener que siempre falla, como se cae una fuente de verdad."""

    def handler(url: str) -> str:
        raise OSError(mensaje)

    return make_opener(handler)


def candle(t: int, o: float, h: float, low: float, c: float) -> Dict[str, Any]:
    return {"time": int(t), "open": o, "high": h, "low": low, "close": c, "volume": 0.0}


def flat(n: int, *, start: int = 0, step: int = 900, h: float = 105.0,
         low: float = 103.0, c: float = 104.0) -> List[Dict[str, Any]]:
    """`n` velas planas, para que el único extremo interesante sea el último."""
    return [candle(start + i * step, 104.0, h, low, c) for i in range(n)]


def serie_cot(n: int = 30) -> List[Dict[str, Any]]:
    """30 semanas con `nc_net` decreciente: sé lo extreme corta, índice bajo."""
    return [
        {
            "report_date": f"2026-01-{i + 1:02d}",
            "am_net": 500 + i * 5,
            "lf_net": -200,
            "nc_net": 5000 - i * 120,
        }
        for i in range(n)
    ]


# ===========================================================================
# base_ingestor: el contrato
# ===========================================================================


class TestContrato:
    def test_los_errores_macro_no_son_errores_de_adaptador(self):
        """Un bróker caído y una fuente macro caída no son el mismo problema.

        `AdapterError` significa "el mercado no está disponible, no operes".
        `FeedUnavailable` significa "un componente opcional del score no está, sigue
        sin él". Colapsarlos haría que un feed opcional pareciera al motor de riesgo
        lo mismo que el mercado entero.
        """
        from adapters.base_adapter import AdapterError

        assert issubclass(FeedUnavailable, IngestorError)
        assert issubclass(FeedUnreadable, IngestorError)
        assert not issubclass(FeedUnavailable, AdapterError)
        assert not issubclass(IngestorError, AdapterError)

    def test_una_lectura_siempre_lleva_los_cinco_campos(self):
        """El contrato no es negociable: una lectura sin `stale` es ambigua."""
        r = reading(source="X", payload={"a": 1}, asof_ts=123, stale=True, reason="viejo")
        assert set(r) == {"source", "asof_ts", "payload", "stale", "reason"}
        assert r["stale"] is True
        assert r["asof_ts"] == 123

    def test_neutro_no_es_un_payload_vacio(self):
        """El motivo es lo que permite diagnosticar después sin logs.

        Un `payload={}` con `reason=""` obliga a volver a preguntar "¿por qué no hay
        sesgo macro?", y la respuesta no está en ninguna parte.
        """
        r = neutro("X", "porque sí")
        assert r["payload"] is None
        assert r["reason"] == "porque sí"
        assert r["stale"] is False

    def test_asof_es_del_dato_y_no_de_la_consulta(self):
        """Un COT de hace tres semanas con `asof_ts` de ahora parece fresco."""
        r = reading(source="X", payload=None, asof_ts=1000, stale=False)
        assert r["asof_ts"] == 1000


class TestTTLCache:
    def test_dentro_del_ttl_no_vuelve_a_consultar(self):
        reloj = {"t": 0.0}
        llamadas = {"n": 0}

        def fetch():
            llamadas["n"] += 1
            return "dato"

        c = TTLCache(fetch=fetch, ttl=60.0, now=lambda: reloj["t"])
        assert c.get().value == "dato"
        reloj["t"] = 59.0
        assert c.get().value == "dato"
        assert llamadas["n"] == 1

    def test_un_ttl_no_positivo_se_rechaza_al_construir(self):
        """Un TTL de 0 parece "sin caché" pero en realidad es "siempre reintenta".

        Falla aquí y no en producción, que es donde dolería.
        """
        with pytest.raises(ValueError):
            TTLCache(fetch=lambda: "x", ttl=0.0)

    def test_vencido_sin_respaldo_devuelve_none_y_el_error(self):
        reloj = {"t": 0.0}

        def fetch():
            raise FeedUnavailable("se cayó")

        c = TTLCache(fetch=fetch, ttl=10.0, now=lambda: reloj["t"])
        r = c.get()
        assert r.value is None
        assert r.ok is False
        assert "se cayó" in r.reason()

    def test_vencido_con_respaldo_sirve_el_viejo_marcado(self):
        """Servir lo viejo es correcto; servirlo **sin decirlo** es el bug de REF."""
        reloj = {"t": 0.0}
        caida = {"v": False}

        def fetch():
            if caida["v"]:
                raise FeedUnavailable("se cayó")
            return "viejo"

        c = TTLCache(fetch=fetch, ttl=10.0, now=lambda: reloj["t"])
        assert c.get().stale is False
        caida["v"] = True
        reloj["t"] = 999.0
        r = c.get()
        assert r.value == "viejo"
        assert r.stale is True
        assert r.ok is True  # utilizable, aunque no fresco
        assert r.fresh is False  # y no fresco
        assert "se cayó" in r.reason()

    def test_un_error_de_fetch_no_deja_la_cache_rota(self):
        """Después de fallar, el siguiente intento debe volver a intentarlo."""
        intentos = {"n": 0}

        def fetch():
            intentos["n"] += 1
            if intentos["n"] == 1:
                raise FeedUnavailable("primer fallo")
            return "recuperado"

        reloj = {"t": 0.0}
        c = TTLCache(fetch=fetch, ttl=10.0, now=lambda: reloj["t"])
        assert c.get().value is None
        reloj["t"] = 50.0  # fuerza el reintento
        assert c.get().value == "recuperado"
        assert intentos["n"] == 2

    def test_invalidate_hace_que_se_vuelva_a_consultar(self):
        reloj = {"t": 0.0}
        llamadas = {"n": 0}

        def fetch():
            llamadas["n"] += 1
            return f"dato{llamadas['n']}"

        c = TTLCache(fetch=fetch, ttl=10_000.0, now=lambda: reloj["t"])
        assert c.get().value == "dato1"
        c.invalidate()
        assert c.get().value == "dato2"


# ===========================================================================
# COT
# ===========================================================================


def json_tff(n: int = 30) -> str:
    return json.dumps([
        {
            "report_date_as_yyyy_mm_dd": f"2026-01-{i + 1:02d}T00:00:00.000",
            "asset_mgr_positions_long": 1000 + i * 10,
            "asset_mgr_positions_short": 500,
            "lev_money_positions_long": 400,
            "lev_money_positions_short": 900,
        }
        for i in range(n)
    ])


def json_legacy(n: int = 30) -> str:
    return json.dumps([
        {
            "report_date_as_yyyy_mm_dd": f"2026-01-{i + 1:02d}T00:00:00.000",
            "noncomm_positions_long_all": 5000 - i * 120,
            "noncomm_positions_short_all": 5000,
        }
        for i in range(n)
    ])


def cot_ok() -> Callable[..., Any]:
    def handler(url: str) -> str:
        if cot.LEGACY_DATASET in url:
            return json_legacy()
        return json_tff()

    return make_opener(handler)


class TestCOTIngestor:
    def test_la_url_soql_va_codificada(self):
        """Un espacio sin codificar rompe la consulta entera."""
        url = cot.socrata_url(cot.TFF_DATASET, "report_date_as_yyyy_mm_dd")
        assert "$query=" in url
        assert " " not in url.split("$query=")[1]

    def test_la_url_escapa_el_nombre_del_mercado(self):
        """El nombre va dentro de la consulta, entre comillas.

        Sin escapar, un apóstrofo cierra la cadena de SoQL y la consulta deja de ser
        la que se cree: devolvería datos equivocados en lugar de fallar.
        """
        from urllib.parse import unquote

        url = cot.socrata_url(cot.TFF_DATASET, "report_date_as_yyyy_mm_dd",
                              market="CRUDE OIL - LIGHT SWEET")
        assert "'CRUDE OIL - LIGHT SWEET'" in unquote(url)

    def test_una_falla_de_red_no_se_convierte_en_serie_vacia(self):
        """El bug más caro de este módulo, y el más fácil de reintroducir.

        Si un `FeedUnavailable` se convierte en `[]`, la caché toma `[]` por un
        resultado válido, **tira semanas de historia** y con ellas el único respaldo
        que tenía. El error se paga justo cuando la red vuelve a caer.
        """
        with pytest.raises(FeedUnavailable):
            cot.fetch_reports(opener=caido())

    def test_sin_red_y_sin_cache_devuelve_neutro_con_la_causa(self):
        """El motivo tiene que decir POR QUÉ no hay dato.

        "No devolvió reportes" y "se cayó la red" llegan igual en el `payload is
        None`, y solo uno es un fallo. Sin la causa, quien lea el motivo pensará que
        la CFTC no tiene nada nuevo cuando en realidad no respondió.
        """
        cot.clear_cache()
        r = cot.poll(opener=caido("CFTC no responde"))
        assert r["payload"] is None
        assert "CFTC no responde" in r["reason"]

    def test_el_respaldo_vencido_lleva_stale_true(self):
        """Copia vieja + `stale=False` es la combinación que miente.

        Cubre el bug de la fase: la caché sabía que el respaldo era vencido y
        `poll()` descartaba la marca, así que un COT de tres semanas se debilitaba
        con la misma confianza que uno de ayer.
        """
        cot.clear_cache()
        caida = {"v": False}

        def handler(url: str) -> str:
            if caida["v"]:
                raise OSError("CFTC caída")
            return json_legacy() if cot.LEGACY_DATASET in url else json_tff()

        r1 = cot.poll(opener=make_opener(handler), ttl=0.001)
        assert r1["payload"] is not None
        assert r1["stale"] is False
        assert r1["reason"] == ""
        fecha = r1["payload"]["report_date"]

        time.sleep(0.005)  # el TTL vence
        caida["v"] = True
        r2 = cot.poll(opener=make_opener(handler), ttl=0.001)
        assert r2["payload"] is not None, "el respaldo debía servir"
        assert r2["payload"]["report_date"] == fecha
        assert r2["stale"] is True
        assert "vencida" in r2["reason"]

    def test_la_red_caída_no_hace_perder_la_historia(self):
        """El mismo bug desde el otro lado: la serie no puede evaporarse."""
        cot.clear_cache()
        caida = {"v": False}

        def handler(url: str) -> str:
            if caida["v"]:
                raise OSError("CFTC caída")
            return json_legacy() if cot.LEGACY_DATASET in url else json_tff()

        assert cot.poll(opener=make_opener(handler), ttl=0.001)["payload"] is not None
        time.sleep(0.005)
        caida["v"] = True
        r = cot.poll(opener=make_opener(handler), ttl=0.001)
        assert r["payload"] is not None
        assert r["stale"] is True

    def test_stored_vacio_no_dispara_una_descarga(self):
        """`stored=[]` significa "no hay nada", no "búscalo tú".

        Si el llamador dice que consultó y no encontró nada, yendo a la red por
        detrás convierte una respuesta en una descarga incontrolada.
        """
        llamadas = {"n": 0}

        def handler(url: str) -> str:
            llamadas["n"] += 1
            return "[]"

        r = cot.poll(stored=[], opener=make_opener(handler))
        assert llamadas["n"] == 0
        assert r["payload"] is None

    def test_stored_con_datos_evita_la_red(self):
        llamadas = {"n": 0}

        def handler(url: str) -> str:
            llamadas["n"] += 1
            return "[]"

        r = cot.poll(stored=serie_cot(), opener=make_opener(handler))
        assert llamadas["n"] == 0
        assert r["payload"]["report_date"] == "2026-01-30"

    def test_clear_cache_descarta_el_opener_anterior(self):
        """Si no, un test inyecta su `opener` y el siguiente sigue con el de antes.

        Los tests seguirían pasando probando la fuente equivocada, que es peor que
        no tener tests.
        """
        cot.clear_cache()
        cot.poll(opener=cot_ok(), ttl=3600.0)
        cot.clear_cache()
        # Ahora la fuente está caída: si el opener viejo siguiera vivo, habría dato.
        r = cot.poll(opener=caido(), ttl=3600.0)
        assert r["payload"] is None

    def test_un_json_basura_es_ilegible_y_no_un_fallo_de_red(self):
        """Dos reacciones distintas: reintentar, o avisar de que cambió el formato."""

        def handler(url: str) -> str:
            return "<html>quedate atrás</html>"

        cot.clear_cache()
        r = cot.poll(opener=make_opener(handler), ttl=3600.0)
        assert r["payload"] is None
        assert "JSON ilegible" in r["reason"]


class TestCOTInforme:
    def test_una_serie_plana_no_divide_entre_cero(self):
        """Sin movimiento no hay sesgo. No es "el sesgo es la mitad"."""
        serie = [
            {"report_date": f"2026-01-{i + 1:02d}", "am_net": 0, "lf_net": 0, "nc_net": 0}
            for i in range(30)
        ]
        rep = cot.build_report(serie)
        assert rep["cot_index_26w"] == 50.0
        assert rep["score"] == 0.0
        assert rep["macro_bias"] == "NEUTRAL"

    def test_posiciones_cortas_extremas_marca_alcista(self):
        """Percentil bajo = especulación extrema larga del euro."""
        rep = cot.build_report(serie_cot(26))
        assert rep["cot_index_26w"] < 25.0
        assert rep["macro_bias"] == "BULLISH"

    def test_posiciones_largas_extremas_marca_bajista(self):
        serie = [
            {"report_date": f"2026-01-{i + 1:02d}", "am_net": 0, "lf_net": 0,
             "nc_net": 5000 + i * 120}
            for i in range(26)
        ]
        rep = cot.build_report(serie)
        assert rep["cot_index_26w"] > 75.0
        assert rep["macro_bias"] == "BEARISH"

    def test_una_sola_semana_no_inventa_índice(self):
        """Con una semana, min=max y el percentil es 50 por construcción."""
        rep = cot.build_report(
            [{"report_date": "2026-01-01", "am_net": 10, "lf_net": 5, "nc_net": 100}]
        )
        assert rep["index_valid"] is False
        assert rep["score"] is None
        assert rep["macro_bias"] == "NEUTRAL"

    def test_sin_datos_devuelve_none_y_no_una_lectura_mentirosa(self):
        assert cot.build_report([]) is None


# ===========================================================================
# DXY / SMR
# ===========================================================================


def eu_quebrando() -> List[Dict[str, Any]]:
    """EURUSD haciendo un mínimo nuevo en la última vela."""
    v = flat(10, start=T0, h=1.11, low=1.10, c=1.105)
    v.append(candle(T0 + 10 * 900, 1.1, 1.105, 1.085, 1.09))
    return v


def dxy_plano() -> List[Dict[str, Any]]:
    """DXY sin hacer máximo nuevo: la mitad de una divergencia alcista."""
    v = flat(10, start=T0)
    v.append(candle(T0 + 10 * 900, 104.0, 104.5, 103.0, 104.2))
    return v


class TestAlineacion:
    """Lo que separa un detector que funciona de uno que miente."""

    def test_alinea_por_hora_y_no_por_posición(self):
        """Con un hueco en el DXY, comparar por posición desplaza la ventana."""
        d = [candle(T0 + i * 900, 104.0, 105.0, 103.0, 104.0) for i in range(10) if i != 5]
        d.append(candle(T0 + 10 * 900, 104.0, 104.5, 103.0, 104.2))

        al = dxy.align_on_time(eu_quebrando(), d)
        assert al["common"] == 10
        assert al["unmatched"] == 1
        # La ventana común termina en la última vela del EURUSD, no en una vieja.
        assert al["last"] == T0 + 10 * 900

    def test_detecta_el_desfase_de_una_vela_entre_fuentes(self):
        """Apertura contra cierre: un desfase constante, y silencioso.

        Sin esta comprobación, la intersección deja una ventana vieja pero completa
        y el detector declara una divergencia calculada sobre otro periodo.
        """
        d = [candle(T0 + i * 900 + 900, 104.0, 105.0, 103.0, 104.0) for i in range(10)]
        d.append(candle(T0 + 10 * 900 + 900, 104.0, 104.5, 103.0, 104.2))

        r = dxy.detect_smr(eu_quebrando(), d, "BUY", lookback=8, period=900)
        assert r["confirmed"] is False
        assert "Desfase" in r["detail"]

    def test_sin_una_sola_hora_común_no_hay_veredicto(self):
        r = dxy.detect_smr(flat(11, start=T0), flat(11, start=T0 + 7 * 86400),
                           "BUY", lookback=8)
        assert r["confirmed"] is False
        assert "timestamp" in r["detail"]

    def test_una_vela_sin_hora_no_rompe_la_alineación(self):
        """Velas sin `time` no se pueden alinear, pero tampoco deben tumbar el resto."""
        d = dxy_plano()
        d[-1] = {k: v for k, v in d[-1].items() if k != "time"}
        al = dxy.align_on_time(eu_quebrando(), d)
        assert al["common"] == 10  # la que sí tiene hora


class TestVelaEnCurso:
    def test_descarta_la_vela_que_todavía_no_ha_cerrado(self):
        """Una vela formándose y una cerrada no son comparables."""
        r = dxy.detect_smr(eu_quebrando(), dxy_plano(), "BUY", lookback=8,
                           period=900, now=T0 + 10 * 900 + 60)
        al = r["data"]["aligned"]
        assert al["dropped_forming"] == 1
        assert al["common"] == 10

    def test_una_vela_cerrada_sí_cuenta(self):
        r = dxy.detect_smr(eu_quebrando(), dxy_plano(), "BUY", lookback=8,
                           period=900, now=T0 + 10 * 900 + 1000)
        al = r["data"]["aligned"]
        assert al["dropped_forming"] == 0
        assert al["common"] == 11


class TestDeteccion:
    def test_eurusd_quebrando_sin_dxy_quebrando_es_divergencia(self):
        r = dxy.detect_smr(eu_quebrando(), dxy_plano(), "BUY", lookback=8)
        assert r["confirmed"] is True
        assert "no quiebra" in r["detail"].lower()

    def test_si_el_dxy_confirma_no_hay_divergencia(self):
        """El DXY yendo en el mismo sentido no es divergencia: es confirmación."""
        d = flat(10, start=T0)
        d.append(candle(T0 + 10 * 900, 104.0, 106.0, 103.0, 105.0))  # nuevo máximo
        r = dxy.detect_smr(eu_quebrando(), d, "BUY", lookback=8)
        assert r["confirmed"] is False

    def test_la_ruptura_se_mide_contra_velas_anteriores(self):
        """Si la vela actual contara en su propio extremo, rompería siempre."""
        w = flat(5, start=T0, h=100.0, low=100.0, c=100.0)
        assert dxy.swing_break(w, w, "high")["broke"] is False

    def test_la_dirección_inventada_es_error_del_llamador(self):
        with pytest.raises(ValueError) as exc:
            dxy.detect_smr(flat(5), flat(5), "LONG")
        assert "BUY o SELL" in str(exc.value)
        assert not isinstance(exc.value, IngestorError)

    def test_sin_datos_es_neutro_con_motivo(self):
        assert dxy.detect_smr([], flat(5), "BUY")["confirmed"] is False
        r = dxy.detect_smr(flat(5), [], "BUY")
        assert r["confirmed"] is False
        assert "DXY" in r["detail"]

    def test_velas_insuficientes_lo_dice(self):
        r = dxy.detect_smr(flat(4, start=T0), flat(4, start=T0), "BUY", lookback=8)
        assert r["confirmed"] is False
        assert "insuficientes" in r["detail"]


def cuerpo_yahoo(n: int = 3) -> str:
    return json.dumps({
        "chart": {"result": [{
            "timestamp": [T0 + i * 900 for i in range(n)],
            "indicators": {"quote": [{
                "open": [104.0 + i for i in range(n)],
                "high": [105.0 + i for i in range(n)],
                "low": [103.0 + i for i in range(n)],
                "close": [104.0 + i for i in range(n)],
            }]},
        }]}
    })


class TestYahoo:
    def test_parsea_el_chart_endpoint(self):
        velas = dxy.parse_yahoo_chart(cuerpo_yahoo(2))
        assert len(velas) == 2
        assert velas[0]["time"] == T0
        assert velas[1]["close"] == 105.0

    def test_descarta_los_huecos_en_vez_de_poner_ceros(self):
        """Un 0.0 en la serie rompería el `min`/`max` de la detección."""
        cuerpo = json.dumps({
            "chart": {"result": [{
                "timestamp": [T0, T0 + 900, T0 + 1800],
                "indicators": {"quote": [{
                    "open": [104.0, None, 106.0],
                    "high": [105.0, None, 107.0],
                    "low": [103.0, None, 105.0],
                    "close": [104.0, None, 106.0],
                }]},
            }]}
        })
        velas = dxy.parse_yahoo_chart(cuerpo)
        assert len(velas) == 2
        assert all(v["close"] > 0 for v in velas)

    def test_un_json_basura_es_ilegible_y_no_una_excepción_de_red(self):
        with pytest.raises(FeedUnreadable):
            dxy.parse_yahoo_chart("<html>quedate atrás</html>")

    def test_un_timeframe_inventado_es_error_de_argumento(self):
        with pytest.raises(ValueError) as exc:
            dxy.validate_timeframe("M7")
        assert not isinstance(exc.value, IngestorError)


class TestDXYCache:
    def test_cada_timeframe_tiene_su_propia_cache(self):
        """Compartirla alinearía velas de H1 contra DXY de M15.

        El resultado no sería "una señal degradada": sería una señal calculada
        sobre dos escalas de tiempo distintas, que es peor.
        """
        dxy.clear_cache()
        vistos = []

        def handler(url: str) -> str:
            vistos.append(url)
            return cuerpo_yahoo(3)

        opener = make_opener(handler)
        dxy.poll(timeframe="M15", opener=opener, now=T0 + 100_000)
        dxy.poll(timeframe="H1", opener=opener, now=T0 + 100_000)
        assert "interval=15m" in vistos[0]
        assert "interval=60m" in vistos[1]

    def test_un_timeframe_malo_degrada_a_neutro_y_no_lanza(self):
        r = dxy.poll(timeframe="M7")
        assert r["payload"] is None
        assert "timeframe" in r["reason"]

    def test_sin_velas_devuelve_neutro_con_motivo(self):
        dxy.clear_cache()
        r = dxy.poll(opener=make_opener(lambda u: "[]"), ttl=3600.0)
        assert r["payload"] is None
        assert r["reason"]

    def test_clear_cache_descarta_el_opener_anterior(self):
        dxy.clear_cache()
        dxy.poll(opener=make_opener(lambda u: cuerpo_yahoo(3)), ttl=3600.0,
                 now=T0 + 100_000)
        dxy.clear_cache()
        r = dxy.poll(opener=caido(), ttl=3600.0)
        assert r["payload"] is None


# ===========================================================================
# Calendario / gate
# ===========================================================================


MD_BASICO = """# Forex Factory
| [12:00pm](x?timezone=Eastern) | **Time** |  |  |  |
| | USD | ![red](ff-impact-red.png) | **Non-Farm Payrolls** |
| | 1:00pm | USD | ![red](ff-impact-red.png) | **Unemployment Rate** |
| | EUR | ![ora](ff-impact-ora.png) | ECB Press Conference |
| | 2:00pm | GBP | ![yel](ff-impact-yel.png) | **Retail Sales** |
"""

#: El reloj del markdown está a las 12:00 y el primer evento también, así que en
#: cuanto `clock_min` coincide con la hora actual el desplazamiento es 0 y las
#: horas del calendario son directamente minutos UTC. Todos los tests del gate usan
#: esa convención: si no, cada aserción dependería de un desplazamiento de más.
HORA_RELOJ = 12 * 60


def reloj_de(hora: int, minuto: int = 0) -> int:
    return hora * 60 + minuto


def a_utc(h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, 2, h, m, tzinfo=timezone.utc)


class TestParseoCalendario:
    def test_el_reloj_del_sitio_se_lee(self):
        """Es la referencia de zona horaria de toda la página."""
        reloj, _ = cn.parse_calendar(MD_BASICO)
        assert reloj == HORA_RELOJ

    def test_una_fila_sin_hora_hereda_la_anterior(self):
        """FF agrupa en filas separadas los eventos de una misma hora.

        Sin heredar, un evento quedaría sin hora y el gate lo ignoraría en
        silencio: justo el titular de alto impacto que debía bloquear.
        """
        _, evs = cn.parse_calendar(MD_BASICO)
        nfp = [e for e in evs if "Non-Farm" in e["title"]][0]
        assert nfp["minutes"] == HORA_RELOJ

    def test_los_eventos_con_hora_propia_conservan_la_suya(self):
        _, evs = cn.parse_calendar(MD_BASICO)
        por_titulo = {e["title"]: e["minutes"] for e in evs}
        assert por_titulo["Unemployment Rate"] == 13 * 60
        assert por_titulo["ECB Press Conference"] == 13 * 60
        assert por_titulo["Retail Sales"] == 14 * 60

    def test_el_impacto_se_lee_del_icono(self):
        _, evs = cn.parse_calendar(MD_BASICO)
        por_titulo = {e["title"]: e["impact"] for e in evs}
        assert por_titulo["Non-Farm Payrolls"] == "red"
        assert por_titulo["ECB Press Conference"] == "ora"
        assert por_titulo["Retail Sales"] == "yel"

    def test_los_asteriscos_del_markdown_no_llegan_al_título(self):
        _, evs = cn.parse_calendar(MD_BASICO)
        assert "**" not in evs[0]["title"]

    def test_una_tabla_que_no_es_una_tabla_da_cero_eventos(self):
        reloj, evs = cn.parse_calendar("no hay nada aquí")
        assert evs == []
        assert reloj is None

    def test_cambiar_el_impacto_de_un_icono_no_rompe_el_parseo(self):
        """Si FF renombra el PNG, el parseo sigue y el impacto baja a `yel`.

        Es degradación silenciosa, y por eso el impacto mínimo por defecto es `red`:
        un fallo de parseo cierra el gate, un cambio de nombre lo abre sin avisar.
        """
        md = MD_BASICO.replace("ff-impact-red.png", "ff-impact-rojo.png")
        _, evs = cn.parse_calendar(md)
        assert evs[0]["impact"] == "yel"
        assert evs[0]["title"] == "Non-Farm Payrolls"


class TestHoras:
    @pytest.mark.parametrize(
        "texto,esperado",
        [
            ("12:00am", 0),
            ("12:30am", 30),
            ("1:30am", 90),
            ("12:00pm", 720),
            ("1:30pm", 810),
            ("11:59pm", 1439),
        ],
    )
    def test_las_horas_de_la_tarde_no_pueden_dar_24(self, texto, esperado):
        """`12:00pm` son las 12, no las 0. Sin las dos ramas, un evento del mediodía
        se convierte en medianoche y el gate bloquea el día entero equivocado."""
        assert cn.to_minutes(texto) == esperado

    def test_una_hora_ilegible_no_explota(self):
        assert cn.to_minutes("no es una hora") is None
        assert cn.to_minutes("") is None


class TestGate:
    def eventos(self) -> List[Dict[str, Any]]:
        return [
            {"time": "1:00pm", "minutes": 13 * 60, "currency": "USD",
             "impact": "red", "title": "NFP"},
            {"time": "1:00pm", "minutes": 13 * 60, "currency": "EUR",
             "impact": "ora", "title": "ECB"},
            {"time": "2:00pm", "minutes": 14 * 60, "currency": "GBP",
             "impact": "yel", "title": "Retail"},
        ]

    def test_un_evento_lejos_no_bloquea(self):
        g = cn.high_impact_in_window(self.eventos(), clock_min=HORA_RELOJ,
                                     now_utc=a_utc(12, 0), buffer_min=15)
        assert g["blocked"] is False

    def test_un_evento_dentro_de_la_ventana_bloquea(self):
        g = cn.high_impact_in_window(self.eventos(), clock_min=reloj_de(12, 52),
                                     now_utc=a_utc(12, 52), buffer_min=15)
        assert g["blocked"] is True
        assert g["event"]["title"] == "NFP"
        assert "USD" in g["detail"]

    def test_el_buffer_es_simétrico(self):
        """Un dato que sale 10 minutos ANTES ya mueve el mercado.

        Esperar solo hacia adelante deja entrar la mitad del riesgo.
        """
        evs = [{"minutes": 13 * 60, "currency": "USD", "impact": "red", "title": "NFP"}]
        antes = cn.high_impact_in_window(evs, clock_min=reloj_de(13, 10),
                                         now_utc=a_utc(13, 10), buffer_min=15)
        despues = cn.high_impact_in_window(evs, clock_min=reloj_de(12, 50),
                                           now_utc=a_utc(12, 50), buffer_min=15)
        assert antes["blocked"] is True, "el evento ya pasó y sigue dentro del buffer"
        assert despues["blocked"] is True, "el evento viene y está dentro del buffer"

    def test_fuera_del_buffer_no_bloquea(self):
        evs = [{"minutes": 13 * 60, "currency": "USD", "impact": "red", "title": "NFP"}]
        g = cn.high_impact_in_window(evs, clock_min=reloj_de(12, 40),
                                     now_utc=a_utc(12, 40), buffer_min=15)
        assert g["blocked"] is False

    def test_la_ventana_cruza_la_medianoche(self):
        """Un evento a las 23:50 con `now` a las 00:05 está "hace 15 minutos".

        Sin el módulo, `delta` sería +1435 ("en casi 24 horas") y el titular de
        anoche volvería a aparecer mañana como si fuera nuevo.
        """
        evs = [{"minutes": 23 * 60 + 50, "currency": "USD", "impact": "red",
                "title": "NFP"}]
        g = cn.high_impact_in_window(evs, clock_min=5,
                                     now_utc=a_utc(0, 5), buffer_min=15)
        assert g["blocked"] is True
        assert "hace" in g["detail"]

    def test_el_umbral_de_impacto_se_respeta(self):
        evs = [{"minutes": 12 * 60, "currency": "EUR", "impact": "ora", "title": "ECB"}]
        g = cn.high_impact_in_window(evs, clock_min=HORA_RELOJ, now_utc=a_utc(12, 0),
                                     buffer_min=15, min_impact="red")
        assert g["blocked"] is False

    def test_otra_moneda_no_bloquea_el_eurusd(self):
        evs = [{"minutes": 12 * 60, "currency": "GBP", "impact": "red", "title": "BoE"}]
        g = cn.high_impact_in_window(evs, clock_min=HORA_RELOJ, now_utc=a_utc(12, 0),
                                     buffer_min=15)
        assert g["blocked"] is False

    def test_el_más_intenso_gana_a_otro_del_mismo_minuto(self):
        evs = [
            {"minutes": 12 * 60, "currency": "EUR", "impact": "ora", "title": "ECB"},
            {"minutes": 12 * 60, "currency": "USD", "impact": "red", "title": "NFP"},
        ]
        g = cn.high_impact_in_window(evs, clock_min=HORA_RELOJ, now_utc=a_utc(12, 0),
                                     buffer_min=15)
        assert g["event"]["title"] == "NFP"


class TestZonaHoraria:
    def test_el_reloj_se_traduce_a_hora_utc(self):
        """Si el reloj del sitio va 2 horas por delante, los eventos van 2 antes.

        Es el error que hace que el gate compare horas de dos zonas: el titular de
        las 13:00 del sitio ocurre a las 11:00 UTC, y con las horas sin corregir el
        gate cerraría 2 horas tarde.
        """
        # El reloj del sitio marca las 14:00 y son las 12:00 UTC: el sitio va 2 h
        # por delante, así que a un evento del sitio hay que restarle 2 h.
        evs = [{"minutes": 13 * 60, "currency": "USD", "impact": "red", "title": "NFP"}]
        g = cn.high_impact_in_window(evs, clock_min=reloj_de(14),
                                     now_utc=a_utc(12, 0), buffer_min=15)
        # 13:00 del sitio = 11:00 UTC. Hace una hora: fuera de la ventana.
        assert g["blocked"] is False

        # 14:15 del sitio = 12:15 UTC. Dentro de 15 minutos: bloquea.
        cerca = [{"minutes": reloj_de(14, 15), "currency": "USD",
                  "impact": "red", "title": "NFP"}]
        g2 = cn.high_impact_in_window(cerca, clock_min=reloj_de(14),
                                      now_utc=a_utc(12, 0), buffer_min=15)
        assert g2["blocked"] is True

    def test_sin_reloj_se_asume_utc_y_se_avisa(self):
        """Comparar horas de dos zonas en silencio es peor que apagarse."""
        evs = [{"minutes": 12 * 60, "currency": "USD", "impact": "red", "title": "NFP"}]
        g = cn.high_impact_in_window(evs, clock_min=None,
                                     now_utc=a_utc(12, 0), buffer_min=15)
        assert g["blocked"] is True  # asume UTC

        cn.clear_cache()
        r = cn.poll(now_utc=a_utc(12, 0), opener=make_opener(lambda u: "sin tabla"))
        assert r["payload"]["timezone_assumed"] is True

    def test_con_reloj_leído_no_se_supone_la_zona(self):
        cn.clear_cache()
        r = cn.poll(now_utc=a_utc(12, 0), opener=make_opener(lambda u: MD_BASICO))
        assert r["payload"]["timezone_assumed"] is False


class TestGateFailOpen:
    """La decisión más discutible de esta fase, y por eso tiene su propia clase."""

    def test_si_la_fuente_cae_se_operar(self):
        """El gate existe para no entrar en un NFP. Es una precaución, no una
        condición de supervivencia.

        Un gate que se cierra por un fallo de red protege de un riesgo hipotético
        (entrar sin querer en un NFP) y arriesga uno real (dejar de operar para
        siempre porque el proxy se cayó un día).
        """
        cn.clear_cache()
        r = cn.poll(opener=caido("proxy caído"), now_utc=a_utc(12, 0))
        assert r["payload"]["ok"] is True
        assert r["payload"]["fail_open"] is True
        assert "Fail-open" in r["reason"]

    def test_el_porqué_del_fail_open_queda_escrito(self):
        """Si no, el post-mortem no ve que el gate estaba degradado."""
        cn.clear_cache()
        r = cn.poll(opener=caido("proxy caído"), now_utc=a_utc(12, 0))
        assert r["reason"]
        assert r["payload"]["ok"] is True

    def test_con_calendario_el_gate_bloquea_de_verdad(self):
        cn.clear_cache()
        r = cn.poll(opener=make_opener(lambda u: MD_BASICO),
                    now_utc=a_utc(12, 52), buffer_min=15)
        assert r["payload"]["ok"] is False
        assert r["payload"]["block"]["title"] == "Non-Farm Payrolls"

    def test_un_markdown_ilegible_no_cierra_el_gate(self):
        """Cambió el formato de la página. Se avisa, no se bloquea el bot."""
        cn.clear_cache()
        r = cn.poll(opener=make_opener(lambda u: "FF ha cambiado el HTML"),
                    now_utc=a_utc(12, 0))
        assert r["payload"]["ok"] is True
        assert r["payload"]["events_considered"] == 0

    def test_clear_cache_descarta_el_opener_anterior(self):
        cn.clear_cache()
        cn.poll(opener=make_opener(lambda u: MD_BASICO), now_utc=a_utc(12, 0))
        cn.clear_cache()
        r = cn.poll(opener=caido(), now_utc=a_utc(12, 0))
        assert r["payload"]["fail_open"] is True


class TestContextoCalendario:
    def test_el_próximo_evento_se_distingue_del_último_pasado(self):
        cn.clear_cache()
        r = cn.poll(opener=make_opener(lambda u: MD_BASICO), now_utc=a_utc(12, 0))
        # El NFP es a las 12:00, justo ahora: cuenta como pasado (`delta <= 0`).
        assert r["payload"]["relevant"]["title"] == "Non-Farm Payrolls"
        assert r["payload"]["next"]["title"] == "Unemployment Rate"

    def test_el_relevante_pasado_es_el_más_intenso(self):
        """Para decidir ahora importa "el titular que más movió y que aún pesa", no
        "el titular de hace cuatro minutos que no movió nada"."""
        cn.clear_cache()
        r = cn.poll(opener=make_opener(lambda u: MD_BASICO), now_utc=a_utc(13, 30))
        assert r["payload"]["relevant"]["impact"] == "red"


class TestRegistro:
    """El puente entre `asset_sources_map.yaml` y los módulos reales.

    El fallo que se evita aquí es silencioso: un nombre del YAML que nadie resuelve
    no da error, simplemente deja de aportar al score. Estos tests comprueban que la
    ausencia se reporta.

    Ninguno llama a un `poll()` real con red: donde hace falta un `poll`, se
    inyecta uno falso. El punto de estos tests es el registro, no los feeds.
    """

    def test_los_tres_nombres_del_forex_resuelven(self):
        assert reg.faltantes(["cot_report", "dxy_correlation", "ecb_fed_calendar"]) == []

    def test_cada_nombre_apunta_a_su_módulo(self):
        assert reg.resolve("cot_report").__module__ == "macro_ingestor.forex.cot_service"
        assert reg.resolve("dxy_correlation").__module__ == "macro_ingestor.forex.dxy_service"
        assert reg.resolve("ecb_fed_calendar").__module__ == "macro_ingestor.forex.calendar_news"

    def test_un_nombre_desconocido_no_es_error(self):
        assert reg.resolve("fuente_inventada") is None
        assert reg.resolve("") is None
        assert reg.resolve(None) is None

    def test_faltantes_detecta_lo_declarado_sin_implementar(self):
        assert reg.faltantes(reg.PENDIENTES) == sorted(reg.PENDIENTES)

    def test_una_fuente_pendiente_vuelve_neutra_y_con_motivo(self):
        """El `payload: None` sin motivo es indistinguible de "no había nada"."""
        r = reg.resolve_all(["bcb_focus"])["bcb_focus"]
        assert r["payload"] is None
        assert r["stale"] is False
        assert "sin implementación" in r["reason"]
        assert "pendiente" in r["reason"]

    def test_el_pendiente_se_distingue_del_olvidado(self):
        """Un nombre en PENDIENTES está decidido; uno que no, se nos ha pasado."""
        r = reg.resolve_all(["fuente_inventada"])["fuente_inventada"]
        assert "sin implementación" in r["reason"]
        assert "pendiente" not in r["reason"]

    def test_resolve_all_no_filtra_en_silencio(self, monkeypatch):
        def fake_resolve(nombre):
            if nombre == "cot_report":
                return None  # implementada en el mapa, pero sin módulo detrás
            return lambda **kw: reading(source=nombre, payload={"ok": True})

        monkeypatch.setattr(reg, "resolve", fake_resolve)
        r = reg.resolve_all(["cot_report", "bcb_focus", "inventada"])
        assert set(r) == {"cot_report", "bcb_focus", "inventada"}
        assert r["inventada"]["payload"] == {"ok": True}
        assert r["cot_report"]["payload"] is None

    def test_un_poll_que_reventa_degrada_en_vez_de_tirar(self, monkeypatch):
        """El registro es el borde del sistema: aquí ya no queda nadie que atrape."""

        def poll_que_explota(**_kwargs):
            raise RuntimeError("la CFTC devolvió HTML")

        monkeypatch.setattr(reg, "resolve", lambda n: poll_que_explota)
        r = reg.resolve_all(["cot_report"])["cot_report"]
        assert r["payload"] is None
        assert "RuntimeError" in r["reason"]
        assert "HTML" in r["reason"]

    def test_el_argumento_llega_al_poll(self, monkeypatch):
        """Si el `poll` no recibe `**kwargs` estás, no puede inyectarse nada."""
        recibido: Dict[str, Any] = {}

        def poll_espia(**kwargs):
            recibido.update(kwargs)
            return reading(source="x", payload={"ok": True})

        monkeypatch.setattr(reg, "resolve", lambda n: poll_espia)
        reg.resolve_all(["cot_report"], opener=caido(), ttl=0.5)
        assert set(recibido) == {"opener", "ttl"}

    def test_un_registro_roto_degrada_con_motivo_propio(self, monkeypatch):
        """Un módulo que no existe es un bug nuestro, no una fuente caída."""
        monkeypatch.setitem(reg.IMPLEMENTADAS, "fantasma", "macro_ingestor.forex.no_existe")
        r = reg.resolve_all(["fantasma"])["fantasma"]
        assert r["payload"] is None
        assert "registro roto" in r["reason"]

    def test_implementadas_no_incluye_pendientes(self):
        assert not set(reg.implementadas()) & set(reg.PENDIENTES)

    def test_los_pendientes_son_los_del_yaml(self):
        """Cada fuente que nombra el YAML está implementada o declarada pendiente.

        Sin este test, añadir `soft_data_sources: - algo_nuevo` al mapa no produce
        ningún error: simplemente nadie lo consulta. El fallo tiene que aparecer al
        leer el YAML, no con una fuente callada en producción.
        """
        import yaml
        from core import paths as _paths

        with open(_paths.CONFIG_DIR + "/asset_sources_map.yaml", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        declaradas = {
            nombre
            for spec in doc.values()
            if isinstance(spec, dict)
            for nombre in (spec.get("soft_data_sources") or [])
        }
        assert declaradas, "asset_sources_map.yaml no declara soft_data_sources"
        conocidas = set(reg.implementadas()) | set(reg.PENDIENTES)
        assert declaradas <= conocidas, (
            "fuentes declaradas y ni implementadas ni pendientes: {0}".format(
                sorted(declaradas - conocidas)
            )
        )