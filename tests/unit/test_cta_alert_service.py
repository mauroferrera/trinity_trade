"""El CTA Swing D1: evalúa, audita y —con el YAML mandando— ejecuta (F4 D-077 · F5 D-078).

Espejo de `test_watcher_service.py` con las tres diferencias que definen F4/F5:

1. **Decide en D1 sobre barras CERRADAS.** La señal es el breakout de la última
   barra cerrada (`research/cta.py`, convalidado en F2) y la barra EN FORMACIÓN no
   decide nunca: un close por encima del canal en el formante no abre alerta. A las
   25 h esa misma barra sí cuenta, porque la regla decide tarde, nunca temprano.
2. **La fila ES el motor convalidado.** El stop sale de `cta.chandelier` con el ATR
   que `cta.atr` calcula sobre las mismas velas: si la fila no coincide con lo que
   dice `research/cta.py`, la alerta no es el sistema que se convalidó en F2.
3. **La ejecución es opt-in y estructural (F5).** El servicio solo ejecuta si
   `auto_execute` está encendido en `config/strategy_cta.yaml` Y hay puerto de
   ejecución inyectado; nunca importa `api.services.execution` ni llama `order_send`
   (AST), y la fila de la orden la escribe `ExecutionService` con sus puertas. Se
   afirma mirando imports, atributos y firmas con AST, no con un `dry_run: true` que
   un `order_send` podría acompañar en silencio.

Y una pieza de F5 aparte: `gestionar_salidas()` mueve los stops con la política
convalidada (`core/exit_policy.py` → `research.cta.update_trail`), ratcheteado con el
extremo de la última barra CERRADA. `sin_sl` y `sin_cambio` significan "no se tocó
nada": sin llamada al bróker y sin fila nueva.

Las velas sintéticas están calculadas a mano y verificadas contra el motor: 29
barras cerradas con rangos de 0.002 (los primeros `tr` son todos 0.002, así que
`atr = (0.002*13 + 0.037)/14 = 0.0045` en la barra de ruptura long, y
`(0.002*13 + 0.065)/14 = 0.0065` en la short) y el cierre de la última por encima
de todo el canal previo.

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
    ejecucion: Any = None,
    perfil: Any = None,
    mapa: Any = None,
) -> CtaAlertService:
    """`ejecucion=None` es "sin puerto": el servicio entero sigue en alerta."""
    return CtaAlertService(
        market=MarketFalso() if market is None else market,
        store=StoreFalso() if store is None else store,
        ejecucion=ejecucion,
        perfil=(lambda: dict(PERFIL_CTA)) if perfil is None else perfil,
        mapa=(lambda: dict(MAPA_CTA)) if mapa is None else mapa,
    )


def _escanea(
    market: Any = None,
    store: Any = None,
    ejecucion: Any = None,
    perfil: Any = None,
    mapa: Any = None,
    velas: Optional[List[Dict[str, Any]]] = None,
    ahora: Optional[datetime] = None,
) -> Any:
    """`(servicio, cuerpo_del_scan)` con las velas y el reloj ya casados."""
    if market is None:
        market = MarketFalso(velas=_velas() if velas is None else velas)
    servicio = _servicio(market=market, store=store, ejecucion=ejecucion,
                         perfil=perfil, mapa=mapa)
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

    def test_auto_execute_apagado_por_defecto_con_su_motivo(self):
        """De fábrica el YAML no trae la bandera: el CTA entra en alerta.

        `auto_execute_disponible` SÍ es `True` (el MECANISMO existe desde F5);
        lo que apaga el ciclo es la config, y `auto_execute_motivo` lo dice.
        Sin esa distinción, "apagado" y "no puede estar encendido" serían
        indistinguibles, y un panel no podría explicar por qué.
        """
        estado = _servicio().estado()

        assert estado["auto_execute_conf"] is False
        assert estado["auto_execute"] is False
        assert estado["auto_execute_disponible"] is True
        assert estado["dry_run"] is True
        assert "auto_execute" in estado["auto_execute_motivo"]
        assert "apagado" in estado["auto_execute_motivo"]
        assert "ExecutionService" in estado["auto_execute_motivo"]
        assert estado["exit_policy"] == "chandelier"

    def test_auto_execute_encendido_cuando_el_yaml_lo_pide_y_hay_puerto(self):
        """Tres condiciones a la vez —YAML, perfil habilitado y puerto—; con las
        tres, `estado()` lee el interruptor encendido."""
        perfil = dict(PERFIL_CTA, auto_execute=True)

        estado = _servicio(ejecucion=object(), perfil=lambda: perfil).estado()

        assert estado["auto_execute_conf"] is True
        assert estado["auto_execute"] is True
        assert estado["auto_execute_motivo"] is None
        assert estado["dry_run"] is False

    def test_el_yaml_lo_pide_sin_puerto_lo_dice_el_motivo(self):
        """Interruptor encendido sin cable: un "apagado" sin explicación sería
        un `false` mentiroso, y `estado()` existe para explicar falses."""
        perfil = dict(PERFIL_CTA, auto_execute=True)

        estado = _servicio(perfil=lambda: perfil).estado()

        assert estado["auto_execute_conf"] is True
        assert estado["auto_execute"] is False
        assert estado["dry_run"] is True
        assert estado["auto_execute_motivo"] == cta_mod.MOTIVO_SIN_PUERTO_DE_EJECUCION

    def test_un_perfil_deshabilitado_gana_sobre_el_yaml_encendido(self):
        """Sin perfil válido no hay magic ni motor, así que el interruptor no
        puede encender: el motivo del perfil es el motivo de la ausencia de orden."""
        perfil = dict(PERFIL_CTA, auto_execute=True, enabled=False)

        estado = _servicio(ejecucion=object(), perfil=lambda: perfil).estado()

        assert estado["auto_execute"] is False
        assert estado["auto_execute_motivo"] == "perfil.enabled es false"

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
        assert cuerpo["auto_ejecute_disponible"] is True
        assert cuerpo["auto_ejecutadas"] == 0
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
# Estructura de la ejecución (F5): puerto inyectado, cero imports, trailing D1
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


class EjecucionFalsa:
    """El PUERTO inyectado (una `ExecutionService` simulada): registra y responde.

    La firma de `execute_market_trade` es EXACTAMENTE la real: si el servicio
    llama con un kwarg mal nombrado, el test revienta con `TypeError` aquí, que es
    donde tiene que reventar. `positions`/`modify_stop` imitan el contrato real
    (`(filas, error)` y `(cuerpo, status)`).
    """

    def __init__(
        self,
        respuesta: Any = None,
        status: int = 200,
        filas: Optional[List[Dict[str, Any]]] = None,
        error_posiciones: Optional[str] = None,
        sin_listado: bool = False,
        modifica_respuesta: Any = None,
        modifica_status: int = 200,
        falla: Optional[BaseException] = None,
    ) -> None:
        self._respuesta = (respuesta if respuesta is not None
                           else {"ok": True, "setup_id": 42, "audit_logged": True})
        self._status = status
        self._filas = [] if filas is None else filas
        self._error_posiciones = error_posiciones
        self._sin_listado = sin_listado
        self._modifica_respuesta = (modifica_respuesta if modifica_respuesta is not None
                                    else {"ok": True})
        self._modifica_status = modifica_status
        self._falla = falla
        self.llamadas: List[Dict[str, Any]] = []
        self.posiciones_pedidas: List[Optional[int]] = []
        self.modificaciones: List[Dict[str, Any]] = []

    def execute_market_trade(
        self, symbol: str, action: str = "BUY", volume: Optional[float] = None,
        sl_distance: Optional[float] = None, tp_distance: Optional[float] = None,
        no_tp: bool = False, magic: Optional[int] = None,
        comment: Optional[str] = None, deviation: Optional[int] = None,
        score: Optional[float] = None, verdict: Optional[str] = None,
        invalidate_level: Optional[float] = None,
        planned_entry: Optional[float] = None,
        components: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        timeframe: str = "M15", source: str = "",
    ) -> tuple:
        if self._falla is not None:
            raise self._falla
        self.llamadas.append({
            "symbol": symbol, "action": action, "volume": volume,
            "sl_distance": sl_distance, "no_tp": no_tp, "magic": magic,
            "comment": comment, "score": score, "verdict": verdict,
            "planned_entry": planned_entry, "components": components,
            "context": context, "timeframe": timeframe, "source": source,
        })
        return dict(self._respuesta), self._status

    def positions(self, magic: Optional[int] = None
                  ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        self.posiciones_pedidas.append(magic)
        if self._sin_listado:
            return None, self._error_posiciones
        return list(self._filas), self._error_posiciones

    def modify_stop(self, ticket: Optional[int], sl: Optional[float] = None,
                    tp: Optional[float] = None) -> tuple:
        self.modificaciones.append({"ticket": ticket, "sl": sl, "tp": tp})
        return dict(self._modifica_respuesta), self._modifica_status


class TestCeroOrdenes:
    """Ni la ruta más obvia lleva a una orden: ausencia de código, no de flags.

    F5 añadió un PUERTO inyectado (`ejecucion`), no un import: el servicio sigue
    sin conocer `api.services.execution` ni `order_send`, y el único camino a una
    orden es `execute_market_trade` de ese puerto — que es `ExecutionService`,
    con sus cuatro puertas delante. La garantía se afirma con AST, no con un
    `dry_run: true`.
    """

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

    def test_el_constructor_acepta_un_puerto_inyectado_no_un_import(self):
        params = inspect.signature(CtaAlertService.__init__).parameters

        assert set(params) == {"self", "market", "store", "ejecucion",
                               "perfil", "mapa"}

    def test_el_servicio_expone_estado_escanear_y_gestionar_salidas(self):
        """La superficie pública de F5: evaluar, auditar y mover stops. La orden
        en sí NO es un método público: sale dentro del escaneo, o no sale."""
        servicio = _servicio()

        publicos = sorted(n for n in dir(servicio) if not n.startswith("_"))

        assert publicos == ["escanear", "estado", "gestionar_salidas"]

    def test_no_hay_ruta_auto_execute(self):
        """El watcher tiene auto-execute (responde 501); aquí el YAML es el
        interruptor, y una ruta que encendiera lo que la config apaga sería
        configuración en el sitio que menos se revisa. El trailing es una ruta
        de salidas, no de activación."""
        from api.routes.cta import router

        rutas = [ruta.path for ruta in router.routes]
        assert rutas == ["/api/cta/status", "/api/cta/scan", "/api/cta/trail"]
        assert not any("auto" in r for r in rutas)

    def test_el_escaneo_publica_auto_ejecute_falso_y_dry_run_verdadero(self):
        _, cuerpo = _escanea(store=StoreFalso())

        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["auto_ejecute_disponible"] is True
        assert cuerpo["auto_ejecutadas"] == 0
        assert cuerpo["dry_run"] is True
        assert cta_mod.AUTO_EJECUCION_DISPONIBLE is True


class TestEjecutando:
    """F5: la orden sale por el puerto inyectado, y SOLO si el YAML lo pide."""

    PERFIL_ENCENDIDO = dict(PERFIL_CTA, auto_execute=True)

    def test_con_el_yaml_apagado_ni_con_puerto_hay_intentos(self):
        """Las tres condiciones van juntas: la bandera ausente apaga aunque haya
        puerto, y la alerta F4 —fila, dry_run, modo— sigue existiendo intacta."""
        ej = EjecucionFalsa()
        st = StoreFalso()
        servicio = _servicio(store=st, ejecucion=ej, perfil=lambda: dict(PERFIL_CTA))
        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["auto_ejecutadas"] == 0
        assert cuerpo["dry_run"] is True
        assert ej.llamadas == []

        assert len(st.filas) == 1
        assert st.filas[0]["trade_result"] == {
            "executed": False, "dry_run": True, "mode": "alert"}
        assert cuerpo["events"][0]["auto_ejecutado"] is False

    def test_con_el_yaml_encendido_y_puerto_la_orden_sale_con_los_datos_del_cta(self):
        ej = EjecucionFalsa()
        st = StoreFalso()
        servicio = _servicio(store=st, ejecucion=ej,
                             perfil=lambda: self.PERFIL_ENCENDIDO)
        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["auto_ejecute"] is True
        assert cuerpo["dry_run"] is False
        assert cuerpo["auto_ejecutadas"] == 1
        assert len(ej.llamadas) == 1
        # La fila de ESTA orden la escribe ExecutionService, no este servicio.
        assert st.filas == []

        llamada = ej.llamadas[0]
        assert llamada["symbol"] == "EURUSD"
        assert llamada["action"] == "BUY"
        assert llamada["no_tp"] is True            # sin target: la salida es el trailing
        assert llamada["volume"] is None           # el sizing lo decide ExecutionService
        assert llamada["magic"] == 8882027
        assert llamada["verdict"] == cta_mod.VERDICTO_CTA
        assert llamada["score"] == 0.0
        assert llamada["source"] == cta_mod.SOURCE_CTA
        assert llamada["timeframe"] == "D1"
        assert llamada["comment"] == "Trinity CTA D1"
        assert llamada["planned_entry"] == pytest.approx(1.0502)   # fill next-open
        assert llamada["sl_distance"] == pytest.approx(0.0135, abs=1e-9)
        assert llamada["components"]["cta"]["direction"] == "long"
        assert llamada["context"] == {
            "magic": 8882027, "profile": "cta", "comment": "Trinity CTA D1"}

        evento = cuerpo["events"][0]
        assert evento["auto_ejecutado"] is True
        assert evento["ejecucion_status"] == 200
        assert evento["auditado"] is True          # setup_id de ExecutionService
        assert cuerpo["results"][0]["ejecutado"] is True

    def test_el_rechazo_de_un_gate_no_se_reintenta_en_la_misma_barra(self):
        """La dedup se marca ANTES del intento (docstring del módulo): un gate que
        rechaza deja la señal vista, y no se pelea con el mismo rechazo hasta que
        cambie la barra de señal."""
        ej = EjecucionFalsa(
            respuesta={"error": "Operación bloqueada por los topes del día: tope",
                       "status": "BLOCKED_BY_RISK"}, status=403)
        servicio = _servicio(ejecucion=ej, perfil=lambda: self.PERFIL_ENCENDIDO)
        reloj = _ahora(_velas())

        primero = servicio.escanear(ahora=reloj)
        segundo = servicio.escanear(ahora=reloj)

        evento = primero["events"][0]
        assert evento["auto_ejecutado"] is False
        assert evento["ejecucion_status"] == 403
        assert evento["auditado"] is False        # gate antes de auditar: ni fila
        assert "topes" in (evento["ejecucion_motivo"] or "")
        assert len(ej.llamadas) == 1

        assert segundo["dedup"] == 1
        assert segundo["events"] == []
        assert len(ej.llamadas) == 1

    def test_el_yaml_lo_pide_sin_puerto_la_alerta_no_se_pierde_y_lo_dice(self):
        st = StoreFalso()
        servicio = _servicio(store=st, perfil=lambda: self.PERFIL_ENCENDIDO)
        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["auto_ejecute"] is False
        assert cuerpo["dry_run"] is True
        assert len(st.filas) == 1               # la alerta F4 sigue existiendo
        evento = cuerpo["events"][0]
        assert evento["auto_ejecutado"] is False
        assert evento["ejecucion_motivo"] == cta_mod.MOTIVO_SIN_PUERTO_DE_EJECUCION

    def test_un_puerto_que_revienta_no_tumba_el_scan(self):
        ej = EjecucionFalsa(falla=RuntimeError("cable suelto"))
        servicio = _servicio(ejecucion=ej, perfil=lambda: self.PERFIL_ENCENDIDO)
        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        assert cuerpo["errors"] == []
        assert cuerpo["results"][0]["status"] == "alerta"
        assert cuerpo["results"][0]["ejecutado"] is False
        assert cuerpo["events"][0]["ejecucion_motivo"] == "cable suelto"

    def test_el_puerto_real_con_riesgo_bloqueado_no_se_salta_la_puerta(self):
        """La garantía de F5 es de COMPOSICIÓN: el servicio solo conoce el puerto,
        y el puerto (un `ExecutionService` real) corre sus cuatro puertas."""
        from api.services.execution import ExecutionService

        st = StoreFalso()
        real = ExecutionService(
            market=MarketFalso(riesgo={"blocked": True, "reasons": ["tope del día"]}),
            store=st)
        servicio = _servicio(store=st, ejecucion=real,
                             perfil=lambda: self.PERFIL_ENCENDIDO)
        cuerpo = servicio.escanear(ahora=_ahora(_velas()))

        evento = cuerpo["events"][0]
        assert evento["auto_ejecutado"] is False
        assert evento["ejecucion_status"] == 403
        assert evento["auditado"] is False
        assert "topes" in (evento["ejecucion_motivo"] or "")
        assert st.filas == []


class TestGestionarSalidas:
    """F5: el trailing D1 mueve stops con la política convalidada — o no mueve.

    Con las velas sintéticas (29 cerradas, sin formante en la partición),
    `atr[28]` = 0.0045 (long) / 0.0065 (short) y `ref` = extremo de la última
    cerrada (high 1.05 / low 0.95), `mult` 3 → candidato = ref ∓ 3·ATR. El
    ratchet nunca afloja, así que `sin_cambio` no llama al bróker.
    """

    def _sirve(self, filas: List[Dict[str, Any]], **kwargs) -> tuple:
        ej = EjecucionFalsa(filas=filas)
        servicio = _servicio(ejecucion=ej, **kwargs)
        return servicio, ej

    def _gestiona(self, servicio, direccion: str = "long") -> Dict[str, Any]:
        """Pasa con el reloj clavado en la serie sintética.

        Sin `ahora`, `datetime.now()` (lejos en el futuro) daría por cerrada la
        vela formante y `ref` pasaría a ser su extremo. Con 12 h desde la última,
        la partición deja 29 cerradas, como describe el docstring de la clase.
        """
        return servicio.gestionar_salidas(
            ahora=_ahora(_velas(direccion=direccion)))

    def test_no_hay_puerto_lo_dice_y_no_revienta(self):
        cuerpo = _servicio().gestionar_salidas()

        assert cuerpo["error"] is not None
        assert "no hay puerto de ejecución cableado" in cuerpo["error"]
        assert cuerpo["positions"] == 0
        assert cuerpo["results"] == []

    def test_un_perfil_deshabilitado_no_toca_el_bróker(self):
        ej = EjecucionFalsa(filas=[{"ticket": 1, "symbol": "EURUSD",
                                    "type": "BUY", "sl": 1.03}])
        cuerpo = _servicio(
            ejecucion=ej, perfil=lambda: dict(PERFIL_CTA, enabled=False)
        ).gestionar_salidas()

        assert cuerpo["enabled"] is False
        assert cuerpo["motivo"] == "perfil.enabled es false"
        assert cuerpo["error"] is None
        assert ej.posiciones_pedidas == []
        assert ej.modificaciones == []

    def test_sin_posiciones_es_correcto_y_vacio(self):
        """`([], None)` del puerto no es un error: es el caso normal de un pase."""
        servicio, ej = self._sirve([])
        cuerpo = servicio.gestionar_salidas()

        assert cuerpo["positions"] == 0
        assert cuerpo["results"] == []
        assert cuerpo["modificados"] == 0
        assert cuerpo["errores"] == 0
        assert cuerpo["error"] is None
        # El puerto se consulta filtrando por el magic del CTA.
        assert ej.posiciones_pedidas == [8882027]

    def test_un_trailing_que_mejora_modifica_el_stop(self):
        st = StoreFalso()
        servicio, ej = self._sirve(
            [{"ticket": 7, "symbol": "EURUSD", "type": "BUY", "sl": 1.03}],
            store=st)
        cuerpo = self._gestiona(servicio)

        assert cuerpo["enabled"] is True
        assert cuerpo["exit_policy"] == "chandelier"   # core/exit_policy.policy_for("cta")
        assert cuerpo["positions"] == 1
        assert cuerpo["modificados"] == 1
        assert cuerpo["sin_cambio"] == 0
        assert cuerpo["errores"] == 0
        r = cuerpo["results"][0]
        assert r["status"] == "modificado"
        assert r["ticket"] == 7
        assert r["symbol"] == "EURUSD"
        assert r["sl"] == pytest.approx(1.03)
        # 1.05 (high de la última cerrada) − 3×0.0045: el formante (high 1.051) NO
        # decide, igual que en el scan.
        assert r["sl_nuevo"] == pytest.approx(1.05 - 3 * 0.0045, abs=1e-9)
        assert ej.modificaciones == [
            {"ticket": 7, "sl": pytest.approx(1.05 - 3 * 0.0045, abs=1e-9),
             "tp": None}]
        # El trailing no escribe filas nuevas ni consulta riesgo/lista blanca.
        assert st.filas == []
        assert "riesgo" not in cuerpo

    def test_sin_cambio_no_llama_al_bróker(self):
        """Sl fresco del chandelier: el candidato no lo mejora y NO se manda nada.
        Ese es exactamente el ratchet convalidado en F2."""
        servicio, ej = self._sirve(
            [{"ticket": 7, "symbol": "EURUSD", "type": "BUY",
              "sl": 1.0502 - 3 * 0.0045}])
        cuerpo = self._gestiona(servicio)

        assert cuerpo["sin_cambio"] == 1
        assert cuerpo["modificados"] == 0
        assert cuerpo["results"][0]["status"] == "sin_cambio"
        assert cuerpo["results"][0]["sl"] == pytest.approx(1.0502 - 3 * 0.0045)
        assert ej.modificaciones == []

    def test_el_short_ratchea_con_el_low(self):
        velas = _velas(direccion="short")
        servicio, ej = self._sirve(
            [{"ticket": 9, "symbol": "EURUSD", "type": "SELL", "sl": 0.975}],
            market=MarketFalso(velas=velas))
        cuerpo = self._gestiona(servicio, direccion="short")

        assert cuerpo["modificados"] == 1
        assert cuerpo["sin_cambio"] == 0
        # 0.95 (low de la última cerrada) + 3×0.0065.
        assert cuerpo["results"][0]["sl_nuevo"] == pytest.approx(
            0.95 + 3 * 0.0065, abs=1e-9)
        assert ej.modificaciones[0]["ticket"] == 9

    def test_sin_sl_no_inventa_uno(self):
        """Una posición sin stop no recibe uno inventado aquí: `sin_sl` sin tocar
        el bróker, y el motivo lo dice."""
        servicio, ej = self._sirve(
            [{"ticket": 7, "symbol": "EURUSD", "type": "BUY", "sl": 0}])
        cuerpo = servicio.gestionar_salidas()

        assert cuerpo["sin_sl"] == 1
        assert cuerpo["results"][0]["status"] == "sin_sl"
        assert "no se inventa" in (cuerpo["results"][0]["motivo"] or "")
        assert ej.modificaciones == []

    def test_una_fila_malformada_no_tumba_el_pase(self):
        servicio, ej = self._sirve(
            [{"symbol": "EURUSD", "type": "BUY", "sl": 1.03},
             {"ticket": 8, "symbol": "EURUSD", "type": "BUY", "sl": 1.03}])
        cuerpo = servicio.gestionar_salidas()

        assert cuerpo["errores"] == 1
        assert cuerpo["modificados"] == 1
        assert cuerpo["results"][0]["status"] == "error"
        assert "ticket" in cuerpo["results"][0]["motivo"]
        assert cuerpo["results"][1]["status"] == "modificado"

    def test_un_simbolo_sin_datos_no_tumba_el_pase(self):
        servicio, ej = self._sirve(
            [{"ticket": 1, "symbol": "GBPUSD", "type": "BUY", "sl": 1.03},
             {"ticket": 2, "symbol": "EURUSD", "type": "BUY", "sl": 1.03}],
            market=MarketFalso(velas=_velas(),
                               fallo_simbolo={"GBPUSD": RuntimeError("sin datos")}))
        cuerpo = servicio.gestionar_salidas()

        assert cuerpo["errores"] == 1
        assert cuerpo["modificados"] == 1
        assert cuerpo["results"][0]["status"] == "sin_datos"
        assert "sin datos" in (cuerpo["results"][0]["motivo"] or "")
        assert cuerpo["results"][1]["status"] == "modificado"

    def test_una_direccion_desconocida_es_error(self):
        servicio, ej = self._sirve(
            [{"ticket": 7, "symbol": "EURUSD", "type": "HOLD", "sl": 1.03}])
        cuerpo = servicio.gestionar_salidas()

        assert cuerpo["errores"] == 1
        assert cuerpo["results"][0]["status"] == "error"
        assert "dirección" in cuerpo["results"][0]["motivo"]
        assert ej.modificaciones == []

    def test_el_broker_que_rechaza_queda_en_la_respuesta(self):
        ej = EjecucionFalsa(
            filas=[{"ticket": 7, "symbol": "EURUSD", "type": "BUY", "sl": 1.03}],
            modifica_respuesta={"error": "rechazada por el bróker",
                                "status": "rejected"},
            modifica_status=400)
        cuerpo = _servicio(ejecucion=ej).gestionar_salidas()

        assert cuerpo["errores"] == 1
        assert cuerpo["modificados"] == 0
        r = cuerpo["results"][0]
        assert r["status"] == "error"
        assert r["http"] == 400
        assert r["motivo"] == "rechazada por el bróker"
        assert r["broker_status"] == "rejected"

    def test_un_puerto_que_no_sabe_listar_lo_dice(self):
        ej = EjecucionFalsa(sin_listado=True,
                            error_posiciones="el puerto no lista posiciones")
        cuerpo = _servicio(ejecucion=ej).gestionar_salidas()

        assert cuerpo["error"] == "el puerto no lista posiciones"
        assert cuerpo["positions"] == 0
        assert cuerpo["results"] == []

    def test_pide_el_d1_con_las_barras_del_perfil(self):
        mkt = MarketFalso()
        servicio, ej = self._sirve(
            [{"ticket": 7, "symbol": "EURUSD", "type": "BUY", "sl": 1.03}],
            market=mkt)
        servicio.gestionar_salidas()

        assert mkt.pedidos == [("EURUSD", "D1", 120)]
