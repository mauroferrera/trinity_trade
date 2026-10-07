"""El CTA Swing D1 en alerta: evalúa, audita y NO manda órdenes (F4, D-077).

Espejo de `test_watcher_service.py` con las tres diferencias que definen F4:

1. **Decide en D1 sobre barras CERRADAS.** La señal es el breakout de la última
   barra cerrada (`research/cta.py`, convalidado en F2) y la barra EN FORMACIÓN no
   decide nunca: un close por encima del canal en el formante no abre alerta. A las
   25 h esa misma barra sí cuenta, porque la regla decide tarde, nunca temprano.
2. **La fila ES el motor convalidado.** El stop sale de `cta.chandelier` con el ATR
   que `cta.atr` calcula sobre las mismas velas: si la fila no coincide con lo que
   dice `research/cta.py`, la alerta no es el sistema que se convalidó en F2.
3. **Cero órdenes estructural.** No hay puerto de ejecución, ni parámetro que active
   una orden, ni ruta de auto-execute: se afirma mirando imports y atributos con AST
   y la firma del constructor, no con un `dry_run: true` que un `order_send` podría
   acompañar en silencio.

Las velas sintéticas están calculadas a mano y verificadas contra el motor: 29
barras cerradas con rangos de 0.002 (los primeros `tr` son todos 0.002, así que
`atr = (0.002*13 + 0.037)/14 = 0.0045` en la barra de ruptura) y el cierre de la
última por encima de todo el canal previo.

Lo que AQUÍ no se prueba: la validación del YAML (`test_strategy_cta_source.py`) y
las rutas HTTP (`tests/integration/test_api_routes.py`).
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from api.services import cta_alert_service as cta_mod
from api.services.cta_alert_service import CtaAlertService
from research import cta

DIA = 86400
T0 = 1_700_000_000

PERFIL_CTA: Dict[str, Any] = {
    "enabled": True,
    "magic": 8882027,
    "comment": "Trinity CTA D1",
    "timeframe": "D1",
    "bars": 120,
    "symbols": ["EURUSD"],
    "engine": {"atr_n": 14, "don_n": 20, "mult": 3.0},
}
MAPA_CTA = {8882026: "default", 8882027: "cta"}

RIESGO_DEFECTO = {"dd_pct": 0.0, "operations_today": 0, "blocked": False}


# ---------------------------------------------------------------------------
# Velas sintéticas D1
# ---------------------------------------------------------------------------


def _velas(
    n_cerradas: int = 29,
    breakout: bool = True,
    con_formante: bool = True,
    direccion: str = "long",
) -> List[Dict[str, Any]]:
    """Una serie D1 con la forma conocida: canal plano y ruptura en la última.

    - Barras 0..n-2: subida de 0.0005 por barra, rango 0.002 y cierre 0.0004 por
      encima del open: NUNCA rompen el canal (el cierre queda 0.0001 por debajo del
      máximo previo), así que una señal vieja es imposible por construcción.
    - Barra n-1 (`breakout`): long cierra en 1.05 (por encima de todo el canal);
      short cierra en 0.05 por debajo de todo el canal.
    - Formante: cierra POR ENCIMA del canal (1.0505 > 1.015 del canal): si el
      servicio lo contara como cerrado, abriría una alerta falsa. Ese es exactamente
      el bug que el test de "no decide" busca.
    """
    out: List[Dict[str, Any]] = []
    for k in range(n_cerradas):
        base = 1.0 + 0.0005 * k
        if breakout and k == n_cerradas - 1:
            if direccion == "long":
                vela = {"time": T0 + k * DIA, "open": base, "high": 1.05,
                        "low": base - 0.001, "close": 1.05, "volume": 1}
            else:
                vela = {"time": T0 + k * DIA, "open": base, "high": base + 0.001,
                        "low": 0.95, "close": 0.95, "volume": 1}
        else:
            vela = {"time": T0 + k * DIA, "open": base, "high": base + 0.001,
                    "low": base - 0.001, "close": base + 0.0004, "volume": 1}
        out.append(vela)
    if con_formante:
        if direccion == "long":
            out.append({"time": T0 + n_cerradas * DIA, "open": 1.0502,
                        "high": 1.051, "low": 1.049, "close": 1.0505, "volume": 1})
        else:
            out.append({"time": T0 + n_cerradas * DIA, "open": 0.9498,
                        "high": 0.951, "low": 0.949, "close": 0.9502, "volume": 1})
    return out


def _ahora(velas: List[Dict[str, Any]], horas: float = 12.0) -> datetime:
    """El reloj del scan, a `horas` desde la ÚLTIMA vela.

    12 h con formante presente deja la última barra sin cerrar (< 25 h); 30 h la da
    por cerrada, que es el caso de fin de semana (el bróker no publica formante).
    """
    return datetime.fromtimestamp(
        float(velas[-1]["time"]) + horas * 3600, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


class MarketFalso:
    """Velas D1 y riesgo del día. Cada petición queda registrada."""

    def __init__(
        self,
        velas: Optional[List[Dict[str, Any]]] = None,
        riesgo: Any = None,
        fallo: Optional[BaseException] = None,
        fallo_simbolo: Optional[Dict[str, BaseException]] = None,
    ) -> None:
        self._velas = _velas() if velas is None else velas
        self._riesgo = RIESGO_DEFECTO if riesgo is None else riesgo
        self._fallo = fallo
        self._fallo_simbolo = fallo_simbolo or {}
        self.pedidos: List[tuple] = []

    def candles(self, symbol: str, timeframe: str, bars: int) -> List[Dict[str, Any]]:
        self.pedidos.append((symbol, timeframe, bars))
        if self._fallo is not None:
            raise self._fallo
        if symbol in self._fallo_simbolo:
            raise self._fallo_simbolo[symbol]
        return [dict(v) for v in self._velas[:bars]]

    def daily_risk_state(self) -> Any:
        if isinstance(self._riesgo, Exception):
            raise self._riesgo
        return dict(self._riesgo) if isinstance(self._riesgo, dict) else self._riesgo


class StoreFalso:
    """`setup_log` en memoria. Sin disco y con interruptor de fallo."""

    def __init__(self, log_raises: bool = False) -> None:
        self._log_raises = log_raises
        self.filas: List[Dict[str, Any]] = []

    def log_setup(self, entry: Dict[str, Any]) -> int:
        if self._log_raises:
            raise RuntimeError("disco lleno")
        self.filas.append(dict(entry))
        return 1000 + len(self.filas)


def _servicio(
    market: Any = None,
    store: Any = None,
    perfil: Any = None,
    mapa: Any = None,
) -> CtaAlertService:
    return CtaAlertService(
        market=MarketFalso() if market is None else market,
        store=StoreFalso() if store is None else store,
        perfil=(lambda: dict(PERFIL_CTA)) if perfil is None else perfil,
        mapa=(lambda: dict(MAPA_CTA)) if mapa is None else mapa,
    )


def _escanea(
    market: Any = None,
    store: Any = None,
    perfil: Any = None,
    mapa: Any = None,
    velas: Optional[List[Dict[str, Any]]] = None,
    ahora: Optional[datetime] = None,
) -> Any:
    """`(servicio, cuerpo_del_scan)` con las velas y el reloj ya casados."""
    if market is None:
        market = MarketFalso(velas=_velas() if velas is None else velas)
    servicio = _servicio(market=market, store=store, perfil=perfil, mapa=mapa)
    reloj = ahora if ahora is not None else _ahora(market._velas)
    return servicio, servicio.escanear(ahora=reloj)


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------


class TestEstado:
    def test_publica_el_perfil_convalidado(self):
        estado = _servicio().estado()

        assert estado["enabled"] is True
        assert estado["motivo"] is None
        assert estado["profile"] == "cta"
        assert estado["magic"] == 8882027
        assert estado["comment"] == "Trinity CTA D1"
        assert estado["timeframe"] == "D1"
        assert estado["bars"] == 120
        assert estado["symbols"] == ["EURUSD"]
        assert estado["engine"] == {"atr_n": 14, "don_n": 20, "mult": 3.0}
        assert estado["alerts"] == {}

    def test_auto_execute_siempre_apagado_con_su_motivo(self):
        """Es la lectura de un interruptor que controla algo que no existe hoy.

        No hay `auto_execute_conf`: el YAML de este perfil ni siquiera tiene la
        bandera, y publicar un "lo que el YAML pide" que nadie pidió confundiría
        más que aclarar (D-077).
        """
        estado = _servicio().estado()

        assert estado["auto_execute"] is False
        assert estado["auto_execute_disponible"] is False
        assert estado["dry_run"] is True
        assert "F5" in estado["auto_execute_motivo"]
        assert "ExecutionService" in estado["auto_execute_motivo"]

    def test_un_perfil_deshabilitado_lo_dice_con_motivo(self):
        perfil = dict(PERFIL_CTA, enabled=False)

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["enabled"] is False
        assert estado["motivo"] == "perfil.enabled es false"

    def test_un_yaml_ilegible_es_un_motivo_no_un_dict_vacio(self):
        """Un perfil ilegible tiene que sonar, no parecerse a "no desplegado"."""

        def perfil_roto():
            raise RuntimeError("yaml roto")

        estado = _servicio(perfil=perfil_roto).estado()

        assert estado["enabled"] is False
        assert "no se pudo leer config/strategy_cta.yaml" in estado["motivo"]
        assert "yaml roto" in estado["motivo"]

    def test_sin_perfil_no_hay_nada_que_escanear(self):
        estado = _servicio(perfil=lambda: {}).estado()

        assert estado["enabled"] is False
        assert "no hay perfil" in estado["motivo"]

    def test_un_magic_que_no_resuelve_a_cta_no_pasa(self):
        """Alertar con el magic de otra estrategia escribiría filas ajenas.

        `profile_for_magic` devuelve "default" para un magic ausente: si el mapa no
        trae 8882027, el CTA no puede pretender ser el perfil cta.
        """
        estado = _servicio(mapa=lambda: {}).estado()

        assert estado["enabled"] is False
        assert "resuelve al perfil 'default'" in estado["motivo"]
        assert "8882027" in estado["motivo"]

    def test_un_magic_booleano_no_es_un_magic(self):
        """`True` es un `int` en Python; aquí no, porque compraría con el magic 1."""
        perfil = dict(PERFIL_CTA, magic=True)

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["enabled"] is False
        assert "perfil.magic no es un entero > 0" in estado["motivo"]

    def test_un_timeframe_distinto_de_d1_no_pasa(self):
        """El CTA está convalidado en D1: otra temporalidad es OTRA estrategia."""
        perfil = dict(PERFIL_CTA, timeframe="H1")

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["enabled"] is False
        assert "convalidado en D1" in estado["motivo"]

    def test_simbolos_vacios_no_pasa(self):
        perfil = dict(PERFIL_CTA, symbols=[])

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["enabled"] is False
        assert "perfil.symbols está vacío" in estado["motivo"]

    def test_un_engine_incompleto_no_pasa(self):
        perfil = dict(PERFIL_CTA, engine={"atr_n": 14, "don_n": 20})

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["enabled"] is False
        assert "perfil.engine incompleto, falta mult" in estado["motivo"]

    def test_un_mapa_ilegible_es_un_motivo_no_una_excepcion(self):
        def mapa_roto():
            raise RuntimeError("mapa caido")

        estado = _servicio(mapa=mapa_roto).estado()

        assert estado["enabled"] is False
        assert "no se pudo leer strategy_map.yaml" in estado["motivo"]

    def test_la_dedup_es_visible_en_estado(self):
        """La memoria de dedup, en la misma pantalla que la config, sin abrir la BD."""
        servicio, cuerpo = _escanea()

        assert cuerpo["results"][0]["status"] == "alerta"
        alertas = servicio.estado()["alerts"]
        assert list(alertas) == ["EURUSD"]
        assert alertas["EURUSD"] == datetime.fromtimestamp(
            T0 + 28 * DIA, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Escaneo: la alerta
# ---------------------------------------------------------------------------


class TestEscanear:
    def test_un_breakout_en_la_ultima_cerrada_escribe_la_fila(self):
        st = StoreFalso()
        cuerpo = _servicio(store=st).escanear(ahora=_ahora(_velas()))

        assert cuerpo["enabled"] is True
        assert cuerpo["symbols"] == 1
        assert cuerpo["errors"] == []
        assert cuerpo["dedup"] == 0
        assert cuerpo["audit_logged"] == 1
        assert cuerpo["audit_fallidos"] == 0
        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["auto_ejecute_disponible"] is False
        assert cuerpo["dry_run"] is True
        assert cuerpo["results"] == [
            {"symbol": "EURUSD", "timeframe": "D1", "status": "alerta",
             "bars_closed": 29, "auditado": True}]
        assert len(st.filas) == 1

        fila = st.filas[0]
        assert fila["symbol"] == "EURUSD"
        assert fila["timeframe"] == "D1"
        assert fila["direction"] == "BUY"
        assert fila["verdict"] == "CTA_BREAKOUT"
        assert fila["score"] == 0.0
        assert fila["target"] is None
        assert fila["invalidate_level"] is None
        assert fila["validated"] is True
        assert fila["reject_reasons"] == []
        assert fila["source"] == "cta_alert"
        assert fila["trade_result"] == {
            "executed": False, "dry_run": True, "mode": "alert"}
        assert fila["context"] == {
            "magic": 8882027, "profile": "cta", "comment": "Trinity CTA D1"}

        evento = cuerpo["events"][0]
        assert evento["event"] == "alerta"
        assert evento["direction"] == "BUY"
        assert evento["fill"] == "next_open"
        assert evento["auto_ejecutado"] is False
        assert evento["auditado"] is True

    def test_la_fila_es_identica_al_motor_convalidado(self):
        """Si `research/cta.py` no dice exactamente esto, la alerta NO es F2.

        Los números de abajo están calculados a mano sobre las velas sintéticas:
        ATR de la barra de ruptura 0.0045, stop a 3×ATR desde el open del formante.
        """
        st = StoreFalso()
        _servicio(store=st).escanear(ahora=_ahora(_velas()))

        fila = st.filas[0]
        cta_b = fila["breakdown"]["cta"]

        # Identidad con el motor: mismas velas, mismas cuentas.
        cerradas = _velas()[:-1]
        highs = [float(v["high"]) for v in cerradas]
        lows = [float(v["low"]) for v in cerradas]
        closes = [float(v["close"]) for v in cerradas]
        atrs = cta.atr(highs, lows, closes, n=14)
        upper, lower = cta.donchian(highs, lows, n=20)
        senales = cta.breakout_signals(closes, upper, lower)
        assert senales == [("long", 28)]

        entry = float(_velas()[-1]["open"])
        esperado = cta.chandelier("long", entry, float(atrs[28]), 3.0)

        assert fila["entry"] == pytest.approx(entry)
        assert fila["sl"] == pytest.approx(esperado, abs=1e-12)
        assert cta_b["atr"] == pytest.approx(0.0045, abs=1e-9)
        assert cta_b["sl_distance"] == pytest.approx(0.0135, abs=1e-9)
        assert fila["sl"] == pytest.approx(1.0502 - 3 * 0.0045, abs=1e-9)
        # Y lo que la fila declara sobre sí misma.
        assert cta_b["direction"] == "long"
        assert cta_b["atr_n"] == 14
        assert cta_b["don_n"] == 20
        assert cta_b["mult"] == 3.0
        assert cta_b["bars_closed"] == 29
        assert cta_b["magic"] == 8882027
        assert cta_b["fill"] == "next_open"
        assert cta_b["signal_bar"] == datetime.fromtimestamp(
            T0 + 28 * DIA, tz=timezone.utc).isoformat()

    def test_un_short_escribe_sell_con_la_direccion_cruda_en_el_breakdown(self):
        """La fila vive en BUY/SELL (`setup_log`, `risk_engine`); el motor, en raw."""
        st = StoreFalso()
        mkt = MarketFalso(velas=_velas(direccion="short"))
        _servicio(market=mkt, store=st).escanear(ahora=_ahora(mkt._velas))

        fila = st.filas[0]
        assert fila["direction"] == "SELL"
        assert fila["breakdown"]["cta"]["direction"] == "short"
        assert fila["sl"] == pytest.approx(0.9498 + 3 * 0.0065, abs=1e-9)

    def test_pide_velas_d1_con_las_barras_del_perfil(self):
        mkt = MarketFalso()
        _servicio(market=mkt).escanear(ahora=_ahora(mkt._velas))

        assert mkt.pedidos == [("EURUSD", "D1", 120)]

    def test_fin_de_semana_sin_formante_el_fill_es_el_close(self):
        """Sin formante (fin de semana) el último precio conocido es el de cierre.

        La alerta no se pierde por no haber vela siguiente: el mercado está cerrado
        y quien entre lo hará en la apertura; la fila lleva el close conocido.
        """
        velas = _velas(con_formante=False)
        st = StoreFalso()
        _servicio(store=st, market=MarketFalso(velas=velas)).escanear(
            ahora=_ahora(velas, horas=30))

        assert len(st.filas) == 1
        assert st.filas[0]["entry"] == pytest.approx(1.05)
        assert st.filas[0]["breakdown"]["cta"]["fill"] == "close"

    def test_a_las_25_horas_exactas_la_barra_ya_cuenta_como_cerrada(self):
        """El límite es `>= time + 25 h`: decide tarde, pero decide."""
        velas = _velas()
        st = StoreFalso()
        _servicio(store=st).escanear(ahora=_ahora(velas, horas=25))

        assert st.filas[0]["breakdown"]["cta"]["fill"] == "close"

    def test_con_24_horas_y_media_sigue_habiendo_formante(self):
        velas = _velas()
        st = StoreFalso()
        _servicio(store=st).escanear(ahora=_ahora(velas, horas=24.5))

        assert st.filas[0]["breakdown"]["cta"]["fill"] == "next_open"

    def test_una_senal_vieja_no_alerta(self):
        """La señal está en la ÚLTIMA cerrada; una de ayer ya tuvo su fill.

        Se arma la ruptura en la barra 28 y se pone una barra plana DESPUÉS: el
        canal ya no se rompe en la última, y perseguir la vieja sería entrar a un
        precio que ya no existe.
        """
        velas = _velas()
        cerradas = list(velas[:-1])
        base29 = 1.0 + 0.0005 * 29
        cerradas.append({"time": T0 + 29 * DIA, "open": base29,
                         "high": base29 + 0.001, "low": base29 - 0.001,
                         "close": base29 + 0.0004, "volume": 1})
        formante = dict(velas[-1], time=T0 + 30 * DIA)
        serie = cerradas + [formante]

        st = StoreFalso()
        _, cuerpo = _escanea(store=st, velas=serie)

        assert cuerpo["results"][0]["status"] == "sin_senal"
        assert cuerpo["events"] == []
        assert cuerpo["audit_logged"] == 0
        assert st.filas == []

    def test_el_formante_no_decide_ni_con_un_close_enorme(self):
        """La barra en formación cierra POR ENCIMA del canal (1.0505 > 1.015).

        Si el servicio la contara como cerrada, abriría una alerta con un fill que
        aún no existe. No la cuenta: la partición la separa antes de mirar señales.
        """
        st = StoreFalso()
        _, cuerpo = _escanea(store=st, velas=_velas(breakout=False))

        assert cuerpo["results"][0]["status"] == "sin_senal"
        assert st.filas == []

    def test_pocas_barras_cerradas_es_sin_datos(self):
        """Sin `don_n + 1` cerradas no hay canal que romper, y eso se dice."""
        velas = _velas(n_cerradas=5)
        _, cuerpo = _escanea(store=StoreFalso(), velas=velas)

        assert cuerpo["results"][0]["status"] == "sin_datos"
        assert cuerpo["results"][0]["bars_closed"] == 5
        assert "don_n + 1" in cuerpo["results"][0]["motivo"]
        assert cuerpo["events"] == []

    def test_un_simbolo_roto_no_tumba_el_ciclo(self):
        """El símbolo sano se evalúa igual: el roto es un `error`, no el scan."""
        perfil = dict(PERFIL_CTA, symbols=["EURUSD", "ROTO"])
        mkt = MarketFalso(fallo_simbolo={
            "ROTO": RuntimeError("sin datos publicados")})
        st = StoreFalso()
        _, cuerpo = _escanea(
            market=mkt, store=st, perfil=lambda: perfil,
            ahora=_ahora(mkt._velas))

        assert [r["status"] for r in cuerpo["results"]] == ["alerta", "error"]
        assert cuerpo["errors"] == [
            {"symbol": "ROTO", "timeframe": "D1",
             "error": "sin datos publicados"}]
        assert len(st.filas) == 1

    def test_sin_mercado_cableado_es_error_no_excepcion(self):
        servicio = CtaAlertService(
            market=None, store=StoreFalso(),
            perfil=lambda: dict(PERFIL_CTA), mapa=lambda: dict(MAPA_CTA))

        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["results"][0]["status"] == "error"
        assert cuerpo["results"][0]["error"] == "mercado no cableado"
        assert cuerpo["riesgo"] == {"error": "mercado no cableado"}
        assert cuerpo["riesgo_error"] == "mercado no cableado"
        assert cuerpo["events"] == []

    def test_un_mercado_que_revienta_es_error_del_simbolo(self):
        mkt = MarketFalso(fallo=RuntimeError("terminal apagado"))

        _, cuerpo = _escanea(market=mkt, store=StoreFalso())

        assert cuerpo["results"][0]["status"] == "error"
        assert cuerpo["errors"][0]["error"] == "terminal apagado"


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_dos_escaneos_sobre_la_misma_barra_escriben_una_fila(self):
        """La dedup es del SERVICIO, no de la ruta: sobrevive a dos ciclos."""
        st = StoreFalso()
        servicio = _servicio(store=st)
        ahora = _ahora(_velas())

        primero = servicio.escanear(ahora=ahora)
        segundo = servicio.escanear(ahora=ahora)

        assert len(st.filas) == 1
        assert primero["audit_logged"] == 1
        assert segundo["events"] == []
        assert segundo["dedup"] == 1
        assert segundo["results"][0]["status"] == "dedup"
        assert segundo["audit_logged"] == 0

    def test_un_reinicio_reescribe_la_fila(self):
        """Ruido aceptado y documentado (D-077): la dedup vive en memoria.

        No se usa `setup_state`, cuyo TTL es de minutos: esto es D1 y una dedup
        persistente podría ocultar una señal NUEVA por parecerse a una vieja.
        """
        st = StoreFalso()
        ahora = _ahora(_velas())

        _servicio(store=st).escanear(ahora=ahora)
        _servicio(store=st).escanear(ahora=ahora)

        assert len(st.filas) == 2

    def test_una_barra_nueva_es_otra_alerta(self):
        """La clave de dedup es (símbolo, TIEMPO de la barra de señal)."""
        st = StoreFalso()
        velas29, velas31 = _velas(29), _velas(31)
        mkt = MarketFalso(velas=velas29)
        servicio = _servicio(market=mkt, store=st)

        servicio.escanear(ahora=_ahora(velas29))
        mkt._velas = velas31  # el mercado trae una serie con la barra 29 ya cerrada
        servicio.escanear(ahora=_ahora(velas31))

        assert len(st.filas) == 2
        senales = [f["breakdown"]["cta"]["signal_bar"] for f in st.filas]
        assert len(set(senales)) == 2


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------


class TestAuditoria:
    def test_si_el_disco_falla_se_dice_y_no_se_reintenta(self):
        """Dedup guardada ANTES de la fila: el fallo no se repite para siempre.

        Reintentar en cada ciclo de un D1 escribiría el mismo error eternamente; el
        evento lo dice con `auditado: false` para que "no hubo señales" y "hubo y se
        perdió" no sean la misma cosa.
        """
        st = StoreFalso(log_raises=True)
        servicio = _servicio(store=st)
        ahora = _ahora(_velas())

        primero = servicio.escanear(ahora=ahora)
        segundo = servicio.escanear(ahora=ahora)

        assert primero["audit_logged"] == 0
        assert primero["audit_fallidos"] == 1
        assert primero["events"][0]["auditado"] is False
        assert primero["results"][0]["status"] == "alerta"
        assert segundo["dedup"] == 1
        assert segundo["audit_fallidos"] == 0
        assert st.filas == []

    def test_sin_store_la_alerta_se_detecta_y_lo_dice(self):
        servicio = CtaAlertService(
            market=MarketFalso(), store=None,
            perfil=lambda: dict(PERFIL_CTA), mapa=lambda: dict(MAPA_CTA))

        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["results"][0]["status"] == "alerta"
        assert cuerpo["events"][0]["auditado"] is False
        assert cuerpo["audit_fallidos"] == 1

    def test_el_riesgo_del_dia_va_en_la_fila(self):
        riesgo = {"dd_pct": 1.5, "operations_today": 2, "blocked": False}
        st = StoreFalso()
        _escanea(market=MarketFalso(riesgo=riesgo), store=st)

        assert st.filas[0]["risk_state"] == riesgo
        assert "risk_state_error" not in st.filas[0]["breakdown"]

    def test_un_riesgo_que_revienta_no_tumba_la_alerta(self):
        """La fila se escribe igual y el fallo queda en el breakdown."""
        st = StoreFalso()
        _, cuerpo = _escanea(
            market=MarketFalso(riesgo=RuntimeError("cuenta no responde")),
            store=st)

        assert cuerpo["results"][0]["status"] == "alerta"
        assert cuerpo["riesgo_error"] == "cuenta no responde"
        assert cuerpo["riesgo"] == {"error": "cuenta no responde"}
        assert st.filas[0]["risk_state"] == {}
        assert st.filas[0]["breakdown"]["risk_state_error"] == "cuenta no responde"


# ---------------------------------------------------------------------------
# Cero órdenes (estructural, no una bandera)
# ---------------------------------------------------------------------------


def _imports_y_atributos(ruta: Path) -> tuple:
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    imports: List[str] = []
    atributos: List[str] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            imports.extend(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            imports.append(nodo.module)
        elif isinstance(nodo, ast.Attribute):
            atributos.append(nodo.attr)
    return imports, atributos


class TestCeroOrdenes:
    """Ni la ruta más obvia lleva a una orden: ausencia de código, no de flags."""

    RUTAS = (
        Path(cta_mod.__file__),
        Path(cta_mod.__file__).parents[1] / "routes" / "cta.py",
    )

    def test_ni_el_servicio_ni_la_ruta_importan_ejecucion(self):
        for ruta in self.RUTAS:
            imports, _ = _imports_y_atributos(ruta)
            for nombre in imports:
                assert "execution" not in nombre.lower(), (
                    "{0} importa {1}".format(ruta.name, nombre))

    def test_nadie_llama_order_send(self):
        for ruta in self.RUTAS:
            _, atributos = _imports_y_atributos(ruta)
            assert "order_send" not in atributos, (
                "{0} llama order_send".format(ruta.name))

    def test_el_constructor_no_acepta_un_puerto_de_ejecucion(self):
        params = inspect.signature(CtaAlertService.__init__).parameters

        assert set(params) == {"self", "market", "store", "perfil", "mapa"}

    def test_no_hay_metodo_de_ejecucion_ni_ruta_auto_execute(self):
        """El watcher tiene auto-execute (responde 501); aquí ni la pregunta existe."""
        servicio = _servicio()
        from api.routes.cta import router

        assert not hasattr(servicio, "ejecutar")
        assert not hasattr(servicio, "_execution")
        assert [ruta.path for ruta in router.routes] == [
            "/api/cta/status", "/api/cta/scan"]

    def test_el_escaneo_publica_auto_ejecute_falso_y_dry_run_verdadero(self):
        _, cuerpo = _escanea(store=StoreFalso())

        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["auto_ejecute_disponible"] is False
        assert cuerpo["dry_run"] is True
        assert cta_mod.AUTO_EJECUCION_DISPONIBLE is False
