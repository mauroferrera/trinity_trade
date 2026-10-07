"""El CTA Swing D1 en MODO ALERTA (F4, D-077): evalúa, audita y NO manda órdenes.

Qué hace
--------
Dos operaciones, igual que el watcher: `estado()` y `escanear()`. El escaneo mira los
símbolos del perfil propio (`config/strategy_cta.yaml`), evalúa el breakout con el
motor CONVALIDADO en F2 (`research/cta.py`) sobre SOLO las barras D1 cerradas, y si la
última cerrada rompió el canal, escribe una fila en `setup_log` con el magic 8882027 y
el perfil `cta`, y `trade_result {executed: false, dry_run: true, mode: "alert"}`.

Qué NO hace (y por qué es estructural, no una bandera)
-------------------------------------------------------
No toca `ExecutionService`, no importa `api.services.execution` y no existe ningún
camino de este servicio hacia `order_send`: las cero órdenes no las garantiza un
`auto_execute: false`, las garantiza la ausencia de código. Cuando F5 conecte la
ejecución multi-estrategia, entrará por `ExecutionService.execute_market_trade` con sus
puertas (lista blanca, riesgo del día, noticias, `validate_entry`), nunca por aquí.

Por qué este servicio y no el watcher
-------------------------------------
El watcher evalúa en M15 con cadencia de minutos; el CTA decide en D1 con cadencia de
días. Meterlo en el ciclo del watcher acoplaría dos ritmos que nada comparte (y haría
que un escaneo M15 reevaluara un breakout D1 sin que la barra cambiara). Son servicios
hermanos con el mismo contrato —leer, decidir, auditar, no ejecutar— y cadencias
distintas (D-077, fork 3).

La regla de barras cerradas (anti-lookahead)
--------------------------------------------
`market.candles(sym, "D1", n)` devuelve la barra EN FORMACIÓN como último elemento
(`copy_rates_from_pos(..., 0, n)`). Decidir con ella sería decidir con una barra que
aún se está dibujando. Regla conservadora (D-077):

- barra `i` está CERRADA si existe `i+1` (la mera existencia de la siguiente prueba
  que `i` ya no cambia);
- la ÚLTIMA se da por cerrada SOLO si `ahora >= time + 25 h` (margen de una hora por
  el cambio de hora: decide TARDE, nunca temprano). Así, en fin de semana, la barra
  del viernes acaba contando como cerrada aunque el bróker no publique formante.

La señal es el breakout de la ÚLTIMA barra cerrada y el fill el open de la barra
formante (next-open, la misma convención con la que se convalidó en F2). Dedup en
memoria por (símbolo, tiempo de la barra de señal): un reinicio del proceso reescribe
la fila, ruido aceptable y documentado; NO se usa `setup_state`, cuyo TTL es de minutos
y esto es D1.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import strategy_map
from research import cta

log = logging.getLogger(__name__)

#: ¿Puede este scan mandar una orden? No, y no hay parámetro que lo cambie. Publicarlo
#: (igual que en el watcher) es lo que permite que un panel muestre el interruptor
#: apagado y explique POR QUÉ en vez de no mostrar nada.
AUTO_EJECUCION_DISPONIBLE = False

MOTIVO_SIN_AUTO_EJECUCION = (
    "El CTA en alerta evalúa el breakout D1 y lo audita en setup_log con "
    "trade_result {executed: false, dry_run: true}. La ejecución multi-estrategia "
    "llega en F5 por ExecutionService, con sus puertas; no existe hoy ningún "
    "camino de este servicio a una orden."
)

#: Veredicto de una alerta del CTA. Es el texto con el que se consulta la auditoría,
#: así que es constante y no un f-string por ciclo.
VERDICTO_CTA = "CTA_BREAKOUT"

#: `source` de las filas que escribe. El watcher usa "watcher"; estas filas son de
#: OTRO servicio y la distinción es lo que permite filtrar la auditoría por estrategia.
SOURCE_CTA = "cta_alert"

#: Perfil al que debe resolver el magic en `strategy_map.yaml`.
PERFIL_ESPERADO = "cta"

#: Solo se evalúa en D1: es la temporalidad en la que el CTA se convalidó (F2). Otra
#: temporalidad no es "el mismo sistema con otro timeframe", es otra estrategia.
TIMEFRAME_CONVALIDADO = "D1"

TIMEFRAME_DEFECTO = "D1"
BARS_DEFECTO = 120

#: Horas mínimas desde el open de la última barra para darla por cerrada. 24 h de
#: sesión + 1 h de margen por el cambio de hora: con menos, una barra de lunes se
#: daría por cerrada el mismo lunes; con este margen solo se decide tarde.
MARGEN_CIERRE_S = 25 * 3600

#: Campos que cada vela tiene que traer para que el motor pueda correr.
_CAMPOS_VELA = ("time", "open", "high", "low", "close")


def _perfil_de_fabrica() -> Dict[str, Any]:
    """`config/strategy_cta.yaml` vía su source. Import tardío: yaml solo si hace falta."""
    from settings import strategy_cta_source  # noqa: PLC0415 - tardío (yaml)

    return strategy_cta_source.load()


def _mapa_de_fabrica() -> Dict[int, str]:
    """`config/strategy_map.yaml` vía su source. Misma razón para el import tardío."""
    from settings import strategy_map_source  # noqa: PLC0415 - tardío (yaml)

    return strategy_map_source.load()


class CtaAlertService:
    """Alerta D1 del CTA Swing D1. Evalúa y audita; nunca ejecuta.

    Los colaboradores son opcionales e inyectables, igual que en `WatcherService`:
    un test hace `CtaAlertService(market=Falso(), store=Falso(), perfil=lambda: {...},
    mapa=lambda: {...})` y ya tiene el servicio entero. Los callables de config
    existen para que los tests no dependan de `config/` ni de `settings/` (y para que
    el servicio no crezca un seam en `database/store.py` que nadie más usa).
    """

    def __init__(
        self,
        market: Any = None,
        store: Any = None,
        perfil: Optional[Callable[[], Dict[str, Any]]] = None,
        mapa: Optional[Callable[[], Dict[int, str]]] = None,
    ) -> None:
        self._market = market
        self._store = store
        self._perfil = perfil if perfil is not None else _perfil_de_fabrica
        self._mapa = mapa if mapa is not None else _mapa_de_fabrica
        #: Última barra de señal ya alertada, por símbolo. En memoria y NO en la base
        #: de datos, como `WatcherService._rejects`: es dedup de este proceso. Un
        #: reinicio reescribe la fila (ruido aceptable); lo que no es aceptable es una
        #: dedup persistente que oculte una señal NUEVA porque se parece a una vieja.
        self._alertas: Dict[str, int] = {}

    # -- configuración ----------------------------------------------------------

    def _perfil_y_error(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """`(perfil, error_de_lectura)`. Un perfil ilegible no es `{}`: es un motivo."""
        try:
            return dict(self._perfil() or {}), None
        except Exception as exc:  # noqa: BLE001 - la config manda, y si no se lee se dice
            log.warning("no se pudo leer config/strategy_cta.yaml: %s", exc)
            return {}, "no se pudo leer config/strategy_cta.yaml: {0}".format(exc)

    def _config(self) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """`(habilitado, motivo_si_no, perfil)`, validando TODO lo que decide el scan.

        La coherencia con `strategy_map.yaml` se comprueba AQUÍ y en cada scan: un
        magic que no está en el mapa resuelve "default" (`profile_for_magic`), y
        alertar con el magic de otra estrategia escribiría filas que la auditoría
        atribuiría a un perfil que nunca las mandó.
        """
        perfil, error = self._perfil_y_error()
        if error:
            return False, error, perfil
        if not perfil:
            return False, "no hay perfil: falta config/strategy_cta.yaml (o está vacío)", perfil
        if not perfil.get("enabled"):
            return False, "perfil.enabled es false", perfil

        magic = perfil.get("magic")
        if isinstance(magic, bool) or not isinstance(magic, int) or magic <= 0:
            return False, "perfil.magic no es un entero > 0 (recibido: {0!r})".format(
                magic), perfil

        try:
            mapa = dict(self._mapa() or {})
        except Exception as exc:  # noqa: BLE001 - sin mapa no se puede validar el magic
            log.warning("no se pudo leer strategy_map.yaml: %s", exc)
            return False, "no se pudo leer strategy_map.yaml: {0}".format(exc), perfil
        resuelto = strategy_map.profile_for_magic(magic, mapa)
        if resuelto != PERFIL_ESPERADO:
            return False, (
                "el magic {0} resuelve al perfil '{1}' en strategy_map.yaml y se "
                "espera '{2}': el CTA no puede alertar con un magic ajeno".format(
                    magic, resuelto, PERFIL_ESPERADO)), perfil

        if str(perfil.get("timeframe") or TIMEFRAME_DEFECTO).upper() != TIMEFRAME_CONVALIDADO:
            return False, (
                "el CTA está convalidado en {0} y el perfil pide {1!r}".format(
                    TIMEFRAME_CONVALIDADO, perfil.get("timeframe"))), perfil

        simbolos = [s for s in (perfil.get("symbols") or []) if str(s).strip()]
        if not simbolos:
            return False, "perfil.symbols está vacío: no hay nada que escanear", perfil

        engine = perfil.get("engine") or {}
        faltan = [k for k in ("atr_n", "don_n", "mult") if not engine.get(k)]
        if faltan:
            return False, "perfil.engine incompleto, falta {0}".format(
                ", ".join(faltan)), perfil
        return True, None, perfil

    @staticmethod
    def _simbolos(perfil: Dict[str, Any]) -> List[str]:
        return [str(s).strip().upper() for s in (perfil.get("symbols") or [])
                if str(s).strip()]

    @staticmethod
    def _engine(perfil: Dict[str, Any]) -> Dict[str, Any]:
        engine = perfil.get("engine") or {}
        return {"atr_n": int(engine["atr_n"]), "don_n": int(engine["don_n"]),
                "mult": float(engine["mult"])}

    # -- puertos de lectura ------------------------------------------------------

    def _velas(self, symbol: str, timeframe: str, bars: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """`(velas, error)`. Un símbolo roto no tumba el ciclo: se dice y se sigue."""
        if self._market is None:
            return None, "mercado no cableado"
        try:
            velas = self._market.candles(symbol, timeframe, bars)
        except Exception as exc:  # noqa: BLE001 - un símbolo sin datos no es el ciclo roto
            return None, str(exc)
        if not velas:
            return None, "sin velas"
        for v in velas:
            if any(k not in v for k in _CAMPOS_VELA):
                return None, "la vela no trae OHLC completo ({0})".format(
                    ",".join(_CAMPOS_VELA))
        return list(velas), None

    @staticmethod
    def _particion(velas: List[Dict[str, Any]], momento: datetime
                   ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """`(cerradas, formante)`. Ver la regla en el docstring del módulo.

        Se compara contra `momento` (el reloj del escaneo) y no contra el tiempo de la
        barra siguiente, porque no siempre hay barra siguiente: es exactamente lo que
        distingue "la de ayer cerró" de "hoy hay formante".
        """
        ultimo = velas[-1]
        if momento.timestamp() >= float(ultimo["time"]) + MARGEN_CIERRE_S:
            return velas, None
        return velas[:-1], ultimo

    def _riesgo_del_dia(self) -> Dict[str, Any]:
        """El estado de riesgo del día, o `{}` con `error` dentro. Idéntico al watcher."""
        if self._market is None:
            return {"error": "mercado no cableado"}
        try:
            estado = self._market.daily_risk_state() or {}
        except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
            return {"error": str(exc)}
        if not isinstance(estado, dict):
            return {"error": "daily_risk_state no devolvió un dict"}
        return dict(estado)

    # -- auditoría ---------------------------------------------------------------

    def _registra(self, entry: Dict[str, Any]) -> Optional[int]:
        """`store.log_setup` sin dejar que un fallo de auditoría tumbe el escaneo.

        `None` es honesto: el escaneo sigue (auditar no es vigente) pero la respuesta
        lo cuenta en `audit_fallidos`. Perder la fila de una alerta detectada es un
        agujero, y esconderlo dejaría "no hubo señales" indistinguible de "hubo y se
        perdió".
        """
        if self._store is None:
            return None
        try:
            return self._store.log_setup(entry)
        except Exception as exc:  # noqa: BLE001 - perder el log no cambia lo que pasó
            log.warning("no se pudo registrar la alerta en setup_log: %s", exc)
            return None

    # -- estado ------------------------------------------------------------------

    def estado(self) -> Dict[str, Any]:
        """Configuración del perfil y estado vigente. Lectura, sin token.

        `auto_execute` sale SIEMPRE a `False`: es la lectura de un interruptor que
        controla algo que no existe. `auto_execute_conf` no existe aquí porque no hay
        ni siquiera una bandera en el YAML: este servicio nació sin camino de
        ejecución (D-077), y publicar un "lo que el YAML pide" que nadie pidió
        confundiría más que aclarar.
        """
        habilitado, motivo, perfil = self._config()
        return {
            "enabled": habilitado,
            "motivo": motivo,
            "profile": PERFIL_ESPERADO,
            "magic": perfil.get("magic"),
            "comment": perfil.get("comment"),
            "timeframe": str(perfil.get("timeframe") or TIMEFRAME_DEFECTO).upper(),
            "bars": int(perfil.get("bars") or BARS_DEFECTO),
            "symbols": self._simbolos(perfil),
            "engine": dict(perfil.get("engine") or {}),
            # Tiempo de la última barra de señal alertada, por símbolo (iso). Es lo
            # que hace visible la dedup en memoria sin abrir la base de datos.
            "alerts": {s: datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                       for s, ts in sorted(self._alertas.items())},
            "auto_execute": False,
            "auto_execute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_execute_motivo": (None if AUTO_EJECUCION_DISPONIBLE
                                    else MOTIVO_SIN_AUTO_EJECUCION),
            "dry_run": True,
        }

    # -- escaneo -----------------------------------------------------------------

    def escanear(self, ahora: Optional[datetime] = None) -> Dict[str, Any]:
        """Un ciclo completo: evalúa cada símbolo, audita y devuelve los eventos.

        Por símbolo, en orden del perfil:

        1. Velas. Sin datos → evento `error` con el motivo y se sigue: un símbolo
           roto no es un escaneo roto.
        2. Partición cerradas/formante (regla de arriba). Insuficientes → resultado
           `sin_datos`: sin `don_n + 1` cerradas no hay canal que romper.
        3. Señal SOLO en la última cerrada (`research/cta.py`). Una señal vieja no
           es una alerta nueva: el fill habría sido el open de una barra que ya
           cerró, y perseguirla sería entrar a un precio que ya no existe.
        4. Dedup por (símbolo, barra de señal) y fila en `setup_log`.

        `results` lleva el desenlace de cada símbolo (`alerta`, `dedup`, `sin_senal`,
        `sin_datos`, `sin_atr`, `error`), `events` solo las alertas escritas (o con el
        intento fallido) y `errors` los símbolos que no se pudieron mirar. Van
        separados porque son tres preguntas distintas: ¿qué encontré?, ¿qué escribí?,
        ¿qué no pude mirar?.
        """
        momento = ahora or datetime.now().astimezone()
        habilitado, motivo, perfil = self._config()
        riesgo = self._riesgo_del_dia()
        riesgo_error = riesgo.get("error")
        riesgo_log = {} if riesgo_error else riesgo

        simbolos = self._simbolos(perfil) if habilitado else []
        events: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        auditados = 0
        perdidas = 0
        dedups = 0
        bars = int(perfil.get("bars") or BARS_DEFECTO)
        timeframe = str(perfil.get("timeframe") or TIMEFRAME_DEFECTO).upper()

        if habilitado:
            engine = self._engine(perfil)
            magic = int(perfil["magic"])
            comment = str(perfil.get("comment") or "")

            for symbol in simbolos:
                base = {"symbol": symbol, "timeframe": timeframe}
                velas, error = self._velas(symbol, timeframe, bars)
                if velas is None:
                    results.append({**base, "status": "error", "error": error})
                    errors.append({**base, "error": error})
                    continue

                cerradas, formante = self._particion(velas, momento)
                min_cerradas = engine["don_n"] + 1
                if len(cerradas) < min_cerradas:
                    motivo_simbolo = (
                        "{0} barras cerradas, hacen falta {1} (don_n + 1)".format(
                            len(cerradas), min_cerradas))
                    results.append({**base, "status": "sin_datos",
                                    "bars_closed": len(cerradas),
                                    "motivo": motivo_simbolo})
                    continue

                resultado, evento = self._evalua(
                    symbol, timeframe, cerradas, formante, engine,
                    magic, comment, riesgo_log, riesgo_error)
                results.append({**base, **resultado})
                if evento is not None:
                    if evento.get("event") == "dedup":
                        dedups += 1
                    else:
                        events.append(evento)
                        if evento.get("auditado"):
                            auditados += 1
                        else:
                            perdidas += 1

        return {
            "enabled": habilitado,
            "motivo": motivo,
            "scan_at": momento.isoformat(),
            "symbols": len(simbolos),
            "events": events,
            "results": results,
            "errors": errors,
            "dedup": dedups,
            "audit_logged": auditados,
            "audit_fallidos": perdidas,
            "riesgo": riesgo,
            "riesgo_error": riesgo_error,
            "auto_ejecute": False,
            "auto_ejecute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "dry_run": True,
        }

    # -- evaluación de un símbolo ------------------------------------------------

    def _evalua(
        self,
        symbol: str,
        timeframe: str,
        cerradas: List[Dict[str, Any]],
        formante: Optional[Dict[str, Any]],
        engine: Dict[str, Any],
        magic: int,
        comment: str,
        riesgo: Dict[str, Any],
        riesgo_error: Optional[str],
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """`(resultado, evento)`. El evento es `None` si no hay alerta que contar."""
        base_status: Dict[str, Any] = {"bars_closed": len(cerradas)}
        highs = [float(v["high"]) for v in cerradas]
        lows = [float(v["low"]) for v in cerradas]
        closes = [float(v["close"]) for v in cerradas]

        atrs = cta.atr(highs, lows, closes, n=engine["atr_n"])
        upper, lower = cta.donchian(highs, lows, n=engine["don_n"])
        senales = cta.breakout_signals(closes, upper, lower)
        ultima = len(cerradas) - 1

        if not senales or senales[-1][1] != ultima:
            return {**base_status, "status": "sin_senal"}, None

        direccion, i = senales[-1]
        atr_dec = atrs[i]
        if atr_dec is None:
            # Inalcanzable con la guarda de `don_n + 1` y los defaults (ATR 14), pero
            # un perfil con don_n pequeño y atr_n grande puede llegar aquí: decirlo
            # es mejor que un `float(None)` a mitad del scan.
            return {**base_status, "status": "sin_atr"}, None

        barra = cerradas[i]
        bar_time = int(barra["time"])
        clave = symbol.upper()
        if self._alertas.get(clave) == bar_time:
            return (
                {**base_status, "status": "dedup",
                 "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat()},
                {"symbol": clave, "timeframe": timeframe, "event": "dedup",
                 "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat()},
            )

        # Fill: open de la barra formante (next-open, la convención de F2). Si la
        # última ya cerró (fin de semana), no hay formante y el precio de referencia
        # es el close de esa barra: el mercado está cerrado y quien quiera entrar lo
        # hará en la apertura; la alerta lleva el último precio conocido.
        if formante is not None:
            entry = float(formante["open"])
            fill = "next_open"
        else:
            entry = float(cerradas[-1]["close"])
            fill = "close"

        sl = float(cta.chandelier(direccion, entry, float(atr_dec), engine["mult"]))
        direction = "BUY" if direccion == "long" else "SELL"

        breakdown: Dict[str, Any] = {
            "cta": {
                # La dirección CRUDA del motor (long/short) vive aquí; la fila lleva
                # BUY/SELL porque `setup_log` se lee en esos términos (store, risk_engine).
                "direction": direccion,
                "atr": float(atr_dec),
                "atr_n": engine["atr_n"],
                "don_n": engine["don_n"],
                "mult": engine["mult"],
                "bars_closed": len(cerradas),
                "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                "fill": fill,
                "sl_distance": abs(entry - sl),
                "magic": magic,
            }
        }
        if riesgo_error:
            breakdown["risk_state_error"] = riesgo_error

        fila = {
            "symbol": clave,
            "timeframe": timeframe,
            "direction": direction,
            "verdict": VERDICTO_CTA,
            # El CTA no tiene score (lo tiene el gate del watcher). 0.0 y no `None`
            # porque la columna es numérica y un scoring inventado sería mentir.
            "score": 0.0,
            "breakdown": breakdown,
            "entry": entry,
            "sl": sl,
            # Sin target y sin nivel de invalidación: el trailing D1 es F5
            # (`core/exit_policy.py`). Un target congelado aquí sería una regla que la
            # convalidación no evaluó.
            "target": None,
            "invalidate_level": None,
            "validated": True,
            "reject_reasons": [],
            "risk_state": riesgo,
            # `mode: "alert"` distingue estas filas de las del watcher y de las de F5.
            "trade_result": {"executed": False, "dry_run": True, "mode": "alert"},
            "context": {"magic": magic, "profile": PERFIL_ESPERADO, "comment": comment},
            "source": SOURCE_CTA,
        }

        # La dedup se guarda ANTES de la fila, igual que `_audita_rechazo` del
        # watcher: si el disco falla, la fila se pierde y NO se reintenta (el evento
        # lo dice con `auditado: false`), porque reintentar en cada ciclo de un D1
        # escribiría el mismo fallo para siempre.
        self._alertas[clave] = bar_time
        id_fila = self._registra(fila)
        evento = {
            "symbol": clave,
            "timeframe": timeframe,
            "event": "alerta",
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "fill": fill,
            "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
            "auto_ejecutado": False,
            "auditado": id_fila is not None,
        }
        return {**base_status, "status": "alerta", "auditado": id_fila is not None}, evento


__all__ = [
    "AUTO_EJECUCION_DISPONIBLE",
    "BARS_DEFECTO",
    "MOTIVO_SIN_AUTO_EJECUCION",
    "SOURCE_CTA",
    "TIMEFRAME_CONVALIDADO",
    "VERDICTO_CTA",
    "CtaAlertService",
]
