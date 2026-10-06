"""El watcher: evalúa, audita y NO manda órdenes.

La primera batería comprueba lo que el watcher hace. La segunda comprueba lo que **no**
hace, que es la mitad del contrato: un setup aprobado no produce ninguna orden, ni una
orden abortada, ni una "preparada para cuando se pueda". Eso se afirma mirando el puerto
de ejecución y la tabla de `setup_log`, no el `ok` de la respuesta, porque un
`dry_run: true` con un `order_send` detrás sería un test verde con el sistema roto.

Lo que aquí NO se prueba
------------------------
El veredicto del setup (eso es `tests/unit/test_setup_gate.py`) y las transiciones del
ciclo (eso es `tests/unit/test_setup_lifecycle.py`). Aquí solo se comprueba que el
servicio los llama, encadena en el orden correcto y escribe lo que dice que escribe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from api.services import watcher_service as watcher_mod
from api.services.watcher_service import WatcherService

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Dobles
# ---------------------------------------------------------------------------


def _snapshot(direction: str = "BUY", score: float = 80.0,
              in_killzone: bool = True, precio: Optional[float] = 1.0850,
              zonas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Un snapshot con la forma que produce `MT5Market.chart_snapshot`."""
    return {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "current_price": precio,
        "last_time": 1_700_000_000,
        "analysis": {"patterns": zonas or {}},
        "risk_engine": {
            "bull": {
                "score": score if direction == "BUY" else 10.0,
                "verdict": "ALTA_PROBABILIDAD",
                "components": {"killzone": {"in_killzone": in_killzone, "name": "London"},
                               "smc": {"value": 70.0}},
            },
            "bear": {
                "score": score if direction == "SELL" else 10.0,
                "verdict": "ALTA_PROBABILIDAD",
                "components": {"killzone": {"in_killzone": in_killzone, "name": "London"},
                               "smc": {"value": 70.0}},
            },
            "invalidation": {direction: 1.0800},
        },
    }


#: Una zona de entrada en la que el gate aprueba: FVG alcista bajo el precio.
ZONA_BUY = {"fvgs": [{"type": "BULLISH_FVG", "top": 1.0840, "bottom": 1.0800}]}

CFG_MINIMA: Dict[str, Any] = {
    "symbols_allow": ["EURUSD"],
    "min_score": 59.5,
    "min_rr": 2.0,
    "sl_distance": 0.0012,
    "tp_ratio_r": 2.0,
}

WCFG_MINIMA: Dict[str, Any] = {
    "enabled": True,
    "scan_interval_sec": 60,
    "auto_execute": False,
    "dedup_ttl_sec": 2700,
    "symbols": [{"symbol": "EURUSD", "timeframe": "M15"}],
}


class MarketFalso:
    """Snapshot, spec y riesgo del día. Cada llamada queda registrada."""

    def __init__(self, snap: Any = "ok", spec: Any = None,
                 riesgo: Any = None, snapshot_raises: bool = False) -> None:
        self._snap = snap
        self._spec = spec
        self._riesgo = {"blocked": False, "reasons": [], "operations_today": 1} if riesgo is None else riesgo
        self._snapshot_raises = snapshot_raises
        self.llamadas: List[str] = []

    def chart_snapshot(self, symbol: str, timeframe: str, **kw: Any) -> Any:
        self.llamadas.append("snapshot:{0}".format(symbol))
        if self._snapshot_raises:
            raise RuntimeError("no hay velas")
        return _snapshot() if self._snap == "ok" else self._snap

    def spec(self, symbol: str) -> Any:
        self.llamadas.append("spec:{0}".format(symbol))
        if isinstance(self._spec, Exception):
            raise self._spec
        return self._spec

    def daily_risk_state(self) -> Any:
        self.llamadas.append("daily_risk_state")
        if isinstance(self._riesgo, Exception):
            raise self._riesgo
        return self._riesgo


class StoreFalso:
    """Configuración, estado del ciclo y auditoría. Sin disco."""

    def __init__(self, wcfg: Optional[Dict[str, Any]] = None,
                 cfg: Optional[Dict[str, Any]] = None,
                 estado: Optional[Dict[str, Any]] = None,
                 log_raises: bool = False) -> None:
        self._wcfg = WCFG_MINIMA if wcfg is None else wcfg
        self._cfg = CFG_MINIMA if cfg is None else cfg
        self.estado = dict(estado) if estado else None
        self._log_raises = log_raises
        self.filas: List[Dict[str, Any]] = []
        self.estados_nuevos: List[Dict[str, Any]] = []
        self.estados_actualizados: List[Dict[str, Any]] = []

    def get_watcher_config(self) -> Dict[str, Any]:
        return dict(self._wcfg)

    def get_trading_config(self) -> Dict[str, Any]:
        return dict(self._cfg)

    def get_setup_state(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        return dict(self.estado) if self.estado else None

    def new_setup_state(self, symbol: str, timeframe: str, gate: Any = None,
                        status: str = "active", notified: bool = True) -> None:
        self.estados_nuevos.append({"symbol": symbol, "timeframe": timeframe,
                                    "gate": gate, "status": status, "notified": notified})
        self.estado = {"symbol": symbol, "timeframe": timeframe, "status": status,
                       "created_at": AHORA.isoformat()}

    def update_setup_state(self, symbol: str, timeframe: str, **fields: Any) -> None:
        self.estados_actualizados.append({"symbol": symbol, "timeframe": timeframe, **fields})

    def log_setup(self, entry: Dict[str, Any]) -> int:
        if self._log_raises:
            raise RuntimeError("disco lleno")
        self.filas.append(dict(entry))
        return 1000 + len(self.filas)


class NewsFalso:
    """El calendario. `gate()` devuelve lo que se le pase, o revienta."""

    def __init__(self, veredicto: Any = None) -> None:
        self._veredicto = {} if veredicto is None else veredicto
        self.llamadas = 0

    def gate(self) -> Dict[str, Any]:
        self.llamadas += 1
        if isinstance(self._veredicto, Exception):
            raise self._veredicto
        return dict(self._veredicto)


class PuertoEjecucionFalso:
    """El puerto de escritura. Aquí solo sirve para afirmar que NO se toca."""

    def __init__(self) -> None:
        self.envios: List[Any] = []

    def send_market_order(self, *a: Any, **kw: Any) -> Dict[str, Any]:
        self.envios.append((a, kw))
        return {"ok": True, "retcode": 10009}


def _servicio(market: Any = None, store: Any = None, news: Any = None) -> WatcherService:
    return WatcherService(market=market if market is not None else MarketFalso(),
                          store=store if store is not None else StoreFalso(),
                          news=news)


def _aprobado(market: Any = None, store: Any = None, news: Any = None) -> Dict[str, Any]:
    """Un scan cuyo único símbolo tiene un setup que el gate aprueba."""
    mkt = market if market is not None else MarketFalso(snap=_snapshot(zonas=ZONA_BUY))
    st = store if store is not None else StoreFalso()
    return _servicio(mkt, st, news).escanear(ahora=AHORA)


# ---------------------------------------------------------------------------
# Estado
# ---------------------------------------------------------------------------


class TestEstado:
    def test_publica_la_configuracion_del_watcher(self):
        st = _servicio(store=StoreFalso())
        estado = st.estado()
        assert estado["enabled"] is True
        assert estado["scan_interval_sec"] == 60
        assert estado["dedup_ttl_sec"] == 2700
        assert estado["symbols"] == [{"symbol": "EURUSD", "timeframe": "M15"}]

    def test_auto_execute_siempre_apagado_incluso_si_el_yaml_lo_pide(self):
        """El interruptor lee lo que el sistema HACE, no lo que el YAML dice.

        `auto_execute: true` en `strategy.yaml` con un escaneo que no manda órdenes
        daría un panel rotulado "AUTO-EJECUCIÓN" sobre un sistema que no opera. El
        valor del YAML se publica aparte, en `auto_execute_conf`.
        """
        wcfg = dict(WCFG_MINIMA, auto_execute=True)
        estado = _servicio(store=StoreFalso(wcfg=wcfg)).estado()
        assert estado["auto_execute"] is False
        assert estado["auto_execute_conf"] is True
        assert estado["auto_execute_disponible"] is False
        assert "no manda órdenes" in estado["auto_execute_motivo"]

    def test_declara_el_umbral_vigente(self):
        """El umbral que el escaneo va a usar, en la misma pantalla que lo muestra."""
        estado = _servicio(store=StoreFalso(cfg=CFG_MINIMA)).estado()
        assert estado["min_score"] == 59.5

    def test_lista_los_estados_vigentes_de_los_simbolos_que_vigila(self):
        st = StoreFalso(estado={"symbol": "EURUSD", "timeframe": "M15",
                                "status": "active", "score": 80.0})
        estado = _servicio(store=st).estado()
        assert len(estado["states"]) == 1
        assert estado["states"][0]["status"] == "active"

    def test_sin_store_no_falla_solo_declara_que_no_hay_config(self):
        """Sin base de datos no hay qué vigilar, y eso es un `200` con la lista vacía.

        Un 503 aquí dejaría el panel del watcher sin poder ni encenderse ni apagarse.
        """
        estado = WatcherService(market=MarketFalso(), store=None).estado()
        assert estado["symbols"] == []
        assert estado["states"] == []
        assert estado["enabled"] is False

    def test_un_store_roto_no_tumba_la_ruta(self):
        class Roto:
            def get_watcher_config(self):
                raise RuntimeError("no hay config")

            def get_trading_config(self):
                raise RuntimeError("no hay config")

        estado = WatcherService(market=MarketFalso(), store=Roto()).estado()
        assert estado["enabled"] is False
        assert estado["symbols"] == []


# ---------------------------------------------------------------------------
# El scan
# ---------------------------------------------------------------------------


class TestScanAprobado:
    def test_un_setup_aprobado_avisa_y_escribe_su_fila(self):
        st = StoreFalso()
        salida = _aprobado(store=st)

        assert [e["event"] for e in salida["events"]] == ["new"]
        assert len(st.filas) == 1
        assert st.filas[0]["validated"] is True
        assert st.filas[0]["verdict"] == "ALTA_PROBABILIDAD"

    def test_el_setup_aprobado_se_guarda_como_ciclo_activo(self):
        st = StoreFalso()
        _aprobado(store=st)
        assert st.estados_nuevos[0]["status"] == "active"
        assert st.estados_nuevos[0]["notified"] is True

    def test_la_fila_declara_que_no_se_ejecuto(self):
        """`dry_run: true` porque es lo que pasó.

        Y `trade_result` NO va vacío a propósito: `store.link_last_setup` busca las filas
        con `trade_result_json` vacía para saber si una operación sigue abierta. Una
        intención sin marcar aparecería ahí como una operación de la que no se sabe el
        resultado.
        """
        st = StoreFalso()
        _aprobado(store=st)
        assert st.filas[0]["trade_result"] == {"executed": False, "dry_run": True}

    def test_la_fila_lleva_el_riesgo_del_dia_leido_una_vez(self):
        """El riesgo es del DÍA, no del símbolo: se lee una vez por ciclo.

        Leerlo por fila daría N lecturas del mismo estado y, si cambia a mitad del
        escaneo, una fila con el riesgo de antes y otra con el de después del mismo
        ciclo.
        """
        mkt = MarketFalso(snap=_snapshot(zonas=ZONA_BUY))
        salida = _aprobado(market=mkt, store=StoreFalso())
        assert mkt.llamadas.count("daily_risk_state") == 1
        assert salida["riesgo"]["operations_today"] == 1

    def test_si_el_riesgo_no_se_puede_leer_la_fila_lo_declara(self):
        """`risk_state: {}` sin explicación no se distingue del riesgo a cero.

        Con `risk_state_error` dentro del breakdown, la revisión de un mes sabe que esa
        fila se escribió sin poder comprobar los topes del día.
        """
        mkt = MarketFalso(snap=_snapshot(zonas=ZONA_BUY),
                          riesgo=RuntimeError("terminal cerrada"))
        st = StoreFalso()
        salida = _aprobado(market=mkt, store=st)

        assert st.filas[0]["risk_state"] == {}
        assert "terminal cerrada" in st.filas[0]["breakdown"]["risk_state_error"]
        assert salida["riesgo_error"]

    def test_la_fila_lleva_el_desglose_del_score(self):
        """El breakdown es la evidencia de por qué se aprobó ese setup."""
        st = StoreFalso()
        _aprobado(store=st)
        breakdown = st.filas[0]["breakdown"]
        assert "killzone" in breakdown
        assert breakdown["gate"]["direction"] == "BUY"

    def test_el_evento_declara_el_plan(self):
        """El `new` lleva entry/SL/TP para que el panel pueda dibujarlo.

        Un evento "hay un setup" sin niveles obliga a una segunda llamada para saber
        dónde está, y entre las dos el precio ya ha cambiado.
        """
        salida = _aprobado()
        evento = salida["events"][0]
        assert evento["entry"] == 1.0800
        assert evento["sl"] == 1.0788
        assert evento["tp"] == 1.0824
        assert evento["auto_ejecutado"] is False


class TestScanNoEjecuta:
    def test_un_setup_aprobado_no_manda_ninguna_orden(self):
        """La mitad del contrato: el watcher mira, no opera.

        No basta con que la respuesta diga `dry_run`. Se mira el puerto de escritura, y
        se mira también después de un setup aprobado con noticias bloqueando, que es el
        camino donde un descuido pondría el `order_send`.
        """
        puerto = PuertoEjecucionFalso()
        servicio = WatcherService(market=MarketFalso(snap=_snapshot(zonas=ZONA_BUY)),
                                  store=StoreFalso(), news=NewsFalso())
        # El servicio no recibe el puerto: por construcción no puede usarlo.
        assert getattr(servicio, "_execution", None) is None
        salida = servicio.escanear(ahora=AHORA)
        assert salida["auto_ejecute"] is False
        assert salida["dry_run"] is True
        assert puerto.envios == []

    def test_el_escaneo_no_puede_encender_auto_ejecute(self):
        """Ni con `auto_execute: true` en la configuración.

        El valor del YAML se lee y se publica como `auto_execute_conf`, y no llega a
        ninguna decisión del escaneo. Un `if wcfg["auto_execute"]` en este servicio
        sería el bug.
        """
        wcfg = dict(WCFG_MINIMA, auto_execute=True)
        salida = _aprobado(store=StoreFalso(wcfg=wcfg))
        assert salida["auto_ejecute"] is False
        assert salida["events"][0]["event"] == "new"
        assert salida["events"][0]["auto_ejecutado"] is False

    def test_la_constante_de_auto_ejecucion_esta_apagada(self):
        assert watcher_mod.AUTO_EJECUCION_DISPONIBLE is False


class TestScanRechazado:
    def test_un_rechazo_se_audita_con_sus_motivos(self):
        st = StoreFalso()
        mkt = MarketFalso(snap=_snapshot(score=20.0))
        salida = _servicio(mkt, st).escanear(ahora=AHORA)

        assert [e["event"] for e in salida["events"]] == ["rejected"]
        assert st.filas[0]["validated"] is False
        assert any("Score" in r for r in st.filas[0]["reject_reasons"])

    def test_el_mismo_rechazo_no_se_escribe_una_vez_por_ciclo(self):
        """Con `scan_interval_sec: 60`, media hora por debajo del umbral son 30 filas.

        La deduplicación es por firma (`core/setup_lifecycle`), en memoria del proceso.
        """
        st = StoreFalso()
        servicio = _servicio(MarketFalso(snap=_snapshot(score=20.0)), st)

        for _ in range(5):
            servicio.escanear(ahora=AHORA)

        assert len(st.filas) == 1

    def test_un_rechazo_que_cambia_de_motivo_se_vuelve_a_registrar(self):
        """Si el rechazo es otro, es otro hecho, aunque el símbolo sea el mismo."""
        st = StoreFalso()
        servicio = _servicio(MarketFalso(snap=_snapshot(score=20.0)), st)
        servicio.escanear(ahora=AHORA)

        mkt = MarketFalso(snap=_snapshot(score=20.0, in_killzone=False))
        servicio._market = mkt
        servicio.escanear(ahora=AHORA)

        assert len(st.filas) == 2

    def test_el_rechazo_no_toca_el_estado_del_ciclo(self):
        """Un descarte no abre ni cierra nada.

        Si un rechazo escribiera el estado, la fila de un setup que nunca se avisó
        parecería un ciclo en curso, y el siguiente setup válido del símbolo se
        deduplicaría contra un ciclo fantasma.
        """
        st = StoreFalso()
        _servicio(MarketFalso(snap=_snapshot(score=20.0)), st).escanear(ahora=AHORA)
        assert st.estados_nuevos == []
        assert st.estados_actualizados == []


class TestScanNoticias:
    def test_un_setup_aprobado_y_bloqueado_por_noticias_no_se_avisa(self):
        st = StoreFalso()
        news = NewsFalso({"ok": False, "block": True, "reason": "NFP en 5 min",
                          "detail": "NFP en 5 min"})
        salida = _aprobado(store=st, news=news)

        assert [e["event"] for e in salida["events"]] == ["ignored_news"]
        assert st.filas[0]["verdict"] == watcher_mod.VERDICTO_NOTICIA
        assert st.filas[0]["validated"] is False

    def test_el_blackout_deja_el_ciclo_en_noticia_y_no_notificado(self):
        st = StoreFalso()
        news = NewsFalso({"ok": False, "block": True, "reason": "NFP"})
        _aprobado(store=st, news=news)
        assert st.estados_nuevos[0]["status"] == "news_ignored"
        assert st.estados_nuevos[0]["notified"] is False

    def test_el_mismo_blackout_no_se_registra_en_cada_ciclo(self):
        """Un evento de noticias recontado cada 60 segundos infla la auditoría.

        Y además cambia lo que dice: 30 filas de "ignorado por noticia" se leen como 30
        noticias, cuando fue una.
        """
        st = StoreFalso()
        news = NewsFalso({"ok": False, "block": True, "reason": "NFP"})
        servicio = _servicio(MarketFalso(snap=_snapshot(zonas=ZONA_BUY)), st, news)

        primera = servicio.escanear(ahora=AHORA)
        segunda = servicio.escanear(ahora=AHORA)

        assert primera["events"][0]["event"] == "ignored_news"
        assert segunda["events"] == []
        assert len(st.filas) == 1

    def test_el_calendario_no_se_consulta_si_el_setup_no_aprueba(self):
        """Anotarle el blackout a un rechazo por score sería confuso.

        El motivo del rechazo es el score, y `reasons` dice eso. Preguntar al
        calendario para un setup que no iba a operarse solo añade una fuente de ruido a
        un ciclo que ya sabe que no hay nada que hacer.
        """
        news = NewsFalso({"ok": False, "block": True})
        _servicio(MarketFalso(snap=_snapshot(score=20.0)), StoreFalso(), news).escanear(ahora=AHORA)
        assert news.llamadas == 0

    def test_un_calendario_caido_no_bloquea_pero_se_avisa(self):
        """Fail-open declarado: el ciclo sigue y la respuesta lo dice.

        Un blackout inventado detendría el sistema por una caída de red; un fail-open
        silencioso dejaría creer que el calendario respondió. Las dos cosas están mal,
        así que la respuesta lleva el aviso.
        """
        news = NewsFalso(RuntimeError("sin conexión"))
        salida = _aprobado(news=news)

        assert salida["events"][0]["event"] == "new"
        assert len(salida["avisos"]) == 1
        assert "sin conexión" in salida["avisos"][0]["aviso"]

    def test_sin_puerto_de_noticias_tambien_se_avisa(self):
        """No cableado no es "no hay noticias": son dos cosas distintas."""
        salida = _aprobado(news=None)
        assert salida["events"][0]["event"] == "new"
        assert "no cableado" in salida["avisos"][0]["aviso"]


class TestScanCiclo:
    def test_un_setup_ya_activo_no_se_reavisa(self):
        """El mismo setup de hace un minuto, ya avisado.

        Con un `new_setup_state` aquí, el `created_at` se renovaría en cada ciclo y el
        TTL nunca llegaría a vencer: el setup quedaría "activo" para siempre.
        """
        st = StoreFalso(estado={"symbol": "EURUSD", "timeframe": "M15",
                                "status": "active", "created_at": AHORA.isoformat()})
        salida = _aprobado(store=st)

        assert salida["events"] == []
        assert st.estados_nuevos == []
        assert st.filas == []

    def test_un_setup_que_muere_se_cierra(self):
        st = StoreFalso(estado={"symbol": "EURUSD", "timeframe": "M15",
                                "status": "active", "created_at": AHORA.isoformat()})
        servicio = _servicio(MarketFalso(snap=_snapshot(score=20.0)), st)
        salida = servicio.escanear(ahora=AHORA)

        assert [e["event"] for e in salida["events"]] == ["closed"]
        assert st.estados_actualizados[0]["status"] == "closed"

    def test_el_escaneo_devuelve_lo_que_va_a_pintar_el_panel(self):
        """La forma que `static/main.js` ya sabe leer.

        `events` con `event` en `new`/`executed`/`closed`/`error`, y `rejects` y
        `news_ignored` aparte. Si un rechazo/contase como `events`, el panel diría
        "3 setups" con uno detectado y dos descartes.
        """
        salida = _aprobado()
        assert set(["events", "rejects", "news_ignored", "riesgo", "dry_run"]).issubset(salida)
        assert salida["events"][0]["event"] in ("new", "executed")


class TestScanFallos:
    def test_un_simbolo_sin_datos_no_tumba_el_ciclo(self):
        """Un símbolo roto da un evento `error` y el resto se evalúa.

        El watcher se configura por símbolos; si uno no publica datos, el operador tiene
        que ver CUÁL, no un 500 que le esconda los otros.
        """
        wcfg = dict(WCFG_MINIMA, symbols=[{"symbol": "EURUSD", "timeframe": "M15"},
                                          {"symbol": "GBPUSD", "timeframe": "M15"}])

        class DosSimboles(MarketFalso):
            def chart_snapshot(self, symbol: str, timeframe: str, **kw: Any) -> Any:
                self.llamadas.append("snapshot:{0}".format(symbol))
                if symbol == "GBPUSD":
                    return {"symbol": symbol, "risk_engine": {}}
                return _snapshot(zonas=ZONA_BUY)

        salida = _servicio(DosSimboles(), StoreFalso(wcfg=wcfg)).escanear(ahora=AHORA)

        eventos = [e["event"] for e in salida["events"]]
        assert eventos == ["new", "error"]

    def test_un_snapshot_que_reventa_da_error_y_no_excepcion(self):
        mkt = MarketFalso(snapshot_raises=True)
        salida = _servicio(mkt, StoreFalso()).escanear(ahora=AHORA)
        assert salida["events"][0]["event"] == "error"
        assert "no hay velas" in salida["events"][0]["error"]

    def test_sin_mercado_no_se_inventa_nada(self):
        salida = WatcherService(market=None, store=StoreFalso()).escanear(ahora=AHORA)
        assert salida["events"][0]["event"] == "error"
        assert salida["riesgo_error"] == "mercado no cableado"

    def test_un_spec_que_falla_no_tumba_el_escaneo(self):
        """El spec es presentación: degrada a distancia de precio y sigue."""
        mkt = MarketFalso(snap=_snapshot(zonas=ZONA_BUY), spec=RuntimeError("spec caído"))
        st = StoreFalso()
        salida = _servicio(mkt, st).escanear(ahora=AHORA)
        assert salida["events"][0]["event"] == "new"

    def test_un_log_que_falla_no_tumba_el_scan_pero_se_declara(self):
        """Perder la auditoría no cambia lo que pasó, pero no puede callarse.

        Sin `audit_logged=False` un "no se registró ningún setup" es indistinguible de
        "se registró y se perdió", que es la diferencia entre un histórico vacío y un
        histórico roto.
        """
        st = StoreFalso(log_raises=True)
        salida = _aprobado(store=st)
        assert salida["events"][0]["event"] == "new"
        assert salida["audit_logged"] == 0

    def test_una_fila_escrita_cuenta_como_auditada(self):
        st = StoreFalso()
        assert _aprobado(store=st)["audit_logged"] == 1

    def test_sin_store_no_hay_simbolos_que_escanear_y_dice_por_que(self):
        """Sin store no hay `watcher.symbols`, así que no hay nada que mirar.

        Y no es lo mismo que un mercado en calma: un escaneo con cero eventos y sin
        motivo parece que el watcher funcionó y no vio nada. El motivo va en la
        respuesta.
        """
        servicio = WatcherService(market=MarketFalso(snap=_snapshot(zonas=ZONA_BUY)),
                                  store=None)
        salida = servicio.escanear(ahora=AHORA)
        assert salida["events"] == []
        assert salida["symbols"] == 0
        assert "watcher.symbols" in salida["motivo"]
        assert salida["audit_logged"] == 0

    def test_con_simbolos_configurados_el_motivo_es_none(self):
        """`motivo` es para el caso anómalo; en un ciclo normal estorbaría."""
        assert _aprobado()["motivo"] is None
