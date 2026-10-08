"""El CTA Swing D1 (F4 alerta, D-077 · F5 ejecución opt-in, D-078): evalúa, audita
y —solo si el YAML lo pide— ejecuta por `ExecutionService`.

Qué hace
--------
Tres operaciones:

- `estado()` y `escanear()`, iguales en forma a las del watcher: el escaneo mira los
  símbolos del perfil propio (`config/strategy_cta.yaml`), evalúa el breakout con el
  motor CONVALIDADO en F2 (`research/cta.py`) sobre SOLO las barras D1 cerradas, y si
  la última cerrada rompió el canal, escribe una fila en `setup_log` con el magic
  8882027 y el perfil `cta`.
- `gestionar_salidas()`: el trailing chandelier de las posiciones del magic CTA
  (`core/exit_policy.py`), la otra mitad de F5. Sin esa, el stop inicial que pone la
  apertura sería el último stop que el sistema pone jamás, y la regla convalidada en
  F2 (ratchet "nunca afloja" barra a barra) existiría solo en el backtest.

Cómo se activa la ejecución (F5, D-078)
---------------------------------------
El interruptor es `auto_execute` en `config/strategy_cta.yaml` y viene **apagado**:
el CTA entra en vivo solo si alguien lo enciende a sabiendas. Para que la orden salga
hacen falta TRES cosas a la vez —el perfil habilitado, `auto_execute: true` y un
puerto de ejecución inyectado en el constructor (`Runtime.cta_service()` monta
`execution_from(self)`)—, y cualquiera de las tres ausente deja el sistema en alerta
con el motivo publicado en `estado()`. No hay ruta `/api/cta/auto-execute`: el YAML
es el interruptor, y una ruta que encendiera lo que la config apaga sería
configuración en el sitio que menos se revisa.

La orden pasa ENTERA por `ExecutionService.execute_market_trade` con sus puertas
(lista blanca, riesgo del día, noticias, `validate_entry`, `execution_quality`) y con
`no_tp=True`: el CTA no tiene target (su salida la decide el trailing), y
`CTA_BREAKOUT` no es `ALTA_PROBABILIDAD`, así que el sizing sale por
`reduced_risk_pct`. En ese camino la fila de auditoría la escribe el propio
`ExecutionService` —con sus gates y su `validated`—, no este servicio; aquí solo se
cuenta el resultado.

Qué NO cambió (y por qué es estructural, no una bandera)
--------------------------------------------------------
Este módulo no importa `api.services.execution` ni `MetaTrader5`, y en ningún punto
llama `order_send`: no conoce el bróker, conoce el PUERTO inyectado, igual que
`market` y `store`. Sin puerto inyectado no hay orden posible y el servicio lo dice
(`auto_execute_motivo`); con puerto, el único camino a una orden es el que ya existía
con sus cuatro puertas. El trailing va aparte y a propósito: `gestionar_salidas()`
no consulta lista blanca ni noticias (mover un stop NO abre riesgo: las tres responden
a "¿puedo abrir?"; un trailing bloqueado por una noticia dejaría el stop viejo justo
cuando más se necesita), y no escribe fila de `setup_log` (la fila de esa posición
existe desde su apertura).

Por qué este servicio y no el watcher
-------------------------------------
El watcher evalúa en M15 con cadencia de minutos; el CTA decide en D1 con cadencia de
días. Meterlo en el ciclo del watcher acoplaría dos ritmos que nada comparte (y haría
que un escaneo M15 reevaluara un breakout D1 sin que la barra cambiara). Son servicios
hermanos con el mismo contrato —leer, decidir, auditar, y (F5) ejecutar solo si el
YAML lo pide— y cadencias distintas (D-077, fork 3).

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
y esto es D1. En F5 la dedup se marca ANTES del intento de ejecución: una orden
rechazada por un gate no se reintenta en cada ciclo de un D1 —la barra de señal no
cambia, y reintentar sería pelearse con el mismo rechazo hasta el martes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import exit_policy, strategy_map
from research import cta

log = logging.getLogger(__name__)

#: ¿Puede este scan mandar una orden? Sí — pero solo si el YAML lo pide y hay puerto
#: de ejecución inyectado (F5, D-078). Publicarlo (igual que en el watcher) es lo que
#: permite que un panel muestre el interruptor y explique POR QUÉ en vez de no mostrar
#: nada: la constante dice si el MECANISMO existe, `estado()` dice si ESTÁ ENCENDIDO.
AUTO_EJECUCION_DISPONIBLE = True

#: Motivo de `auto_execute_motivo` cuando el YAML lo tiene apagado (de fábrica).
MOTIVO_SIN_AUTO_EJECUCION = (
    "auto_execute está apagado en config/strategy_cta.yaml: el CTA entra en vivo "
    "solo si el YAML lo pide (F5, D-078). Encendido, la orden sale por "
    "ExecutionService con sus puertas (lista blanca, riesgo del día, noticias, "
    "validate_entry, execution_quality)."
)

#: Motivo cuando el YAML lo pide pero nadie inyectó el puerto: el interruptor está
#: encendido y el que explica la ausencia de órdenes es el cable, no la config.
MOTIVO_SIN_PUERTO_DE_EJECUCION = (
    "auto_execute está encendido en config/strategy_cta.yaml pero el servicio no "
    "tiene puerto de ejecución inyectado (Runtime.cta_service() monta "
    "execution_from(self)): se queda en alerta."
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
    """Alerta D1 del CTA Swing D1: evalúa, audita y —con el YAML mandando— ejecuta.

    Tres operaciones: `estado()`, `escanear()` y `gestionar_salidas()` (el trailing
    D1 de las posiciones propias). La ejecución NO es un cuarto camino: es la misma
    llamada a `ExecutionService.execute_market_trade` que cualquier otra estrategia,
    con sus puertas, y solo si `auto_execute` está encendido en el YAML y hay puerto
    inyectado (ver el docstring del módulo).

    Los colaboradores son opcionales e inyectables, igual que en `WatcherService`:
    un test hace `CtaAlertService(market=Falso(), store=Falso(), ejecucion=Falso(),
    perfil=lambda: {...}, mapa=lambda: {...})` y ya tiene el servicio entero. Los
    callables de config existen para que los tests no dependan de `config/` ni de
    `settings/` (y para que el servicio no crezca un seam en `database/store.py` que
    nadie más usa). `ejecucion` es el PUERTO ya construido —una `ExecutionService`—:
    este módulo no la importa, igual que no importa MT5.
    """

    def __init__(
        self,
        market: Any = None,
        store: Any = None,
        ejecucion: Any = None,
        perfil: Optional[Callable[[], Dict[str, Any]]] = None,
        mapa: Optional[Callable[[], Dict[int, str]]] = None,
    ) -> None:
        self._market = market
        self._store = store
        #: El PUERTO de ejecución (una `ExecutionService`), inyectado por
        #: `Runtime.cta_service()`. `None` = esta instalación analiza pero no opera:
        #: el servicio entero funciona en alerta y `estado()` lo publica con motivo.
        self._ejecucion = ejecucion
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

        `auto_execute_conf` publica lo que pide el YAML (como en el watcher);
        `auto_execute` es el interruptor EFECTIVO: `true` solo si el perfil está
        habilitado, el YAML lo pide y hay puerto inyectado. Cualquiera de las tres
        ausente deja `auto_execute` a `false` y `auto_execute_motivo` diciendo
        cuál falta — sin eso, un panel vería "encendido" y no entendería por qué
        no sale ninguna orden. `dry_run` acompaña al efectivo: es la lectura de
        "¿este ciclo manda órdenes?".
        """
        habilitado, motivo, perfil = self._config()
        conf = bool(perfil.get("auto_execute"))
        efectivo = bool(habilitado and conf and self._ejecucion is not None)
        if efectivo:
            motivo_auto = None
        elif not habilitado:
            motivo_auto = motivo
        elif not conf:
            motivo_auto = MOTIVO_SIN_AUTO_EJECUCION
        else:
            motivo_auto = MOTIVO_SIN_PUERTO_DE_EJECUCION
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
            "auto_execute_conf": conf,
            "auto_execute": efectivo,
            "auto_execute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_execute_motivo": motivo_auto,
            "dry_run": not efectivo,
            # La política de salida que aplica `gestionar_salidas()`: el mismo
            # motor convalidado en F2 (`core/exit_policy.py`).
            "exit_policy": exit_policy.policy_for(PERFIL_ESPERADO),
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
        4. Dedup por (símbolo, barra de señal), ANTES de cualquier intento.
        5. Si `auto_execute` es efectivo, la orden sale por
           `ExecutionService.execute_market_trade` (`no_tp=True`, el trailing es la
           salida); si no, fila de alerta como en F4. En los dos caminos la fila
           (cuando la hay) está en `setup_log`; en el de ejecución la escribe
           `ExecutionService` con sus gates y su `validated`.

        `results` lleva el desenlace de cada símbolo (`alerta`, `dedup`, `sin_senal`,
        `sin_datos`, `sin_atr`, `error`; con ejecución, `ejecutado` dice si la orden
        salió), `events` solo las alertas escritas (o con el intento fallido) y
        `errors` los símbolos que no se pudieron mirar. Van separados porque son tres
        preguntas distintas: ¿qué encontré?, ¿qué escribí?, ¿qué no pude mirar?.
        """
        momento = ahora or datetime.now().astimezone()
        habilitado, motivo, perfil = self._config()
        conf_auto = bool(perfil.get("auto_execute"))
        auto = bool(habilitado and conf_auto and self._ejecucion is not None)
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
        ejecutadas = 0
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
                    magic, comment, riesgo_log, riesgo_error,
                    auto=auto, auto_conf=conf_auto)
                results.append({**base, **resultado})
                if evento is not None:
                    if evento.get("event") == "dedup":
                        dedups += 1
                    else:
                        events.append(evento)
                        if evento.get("auto_ejecutado"):
                            ejecutadas += 1
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
            "auto_ejecute": auto,
            "auto_ejecute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_ejecutadas": ejecutadas,
            "dry_run": not auto,
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
        auto: bool = False,
        auto_conf: bool = False,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """`(resultado, evento)`. El evento es `None` si no hay alerta que contar.

        `auto` es el interruptor efectivo del ciclo (YAML + puerto); `auto_conf` es
        solo lo que pide el YAML, para poder decir POR QUÉ no se intentó cuando la
        config lo pedía y el cable faltaba.
        """
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

        # La dedup se guarda ANTES de la fila y ANTES del intento de ejecución,
        # igual que `_audita_rechazo` del watcher: si el disco falla O el gate
        # rechaza, no se reintenta en cada ciclo de un D1 (la barra de señal no
        # cambia y perseguir el mismo rechazo hasta el martes sería ruido). El
        # evento lo dice con `auditado`/`ejecucion_motivo`.
        self._alertas[clave] = bar_time

        if auto:
            # La fila la escribe ExecutionService (con sus gates y su validated);
            # aquí solo se traduce `(cuerpo, status)` en evento y resultado.
            try:
                cuerpo, status = self._ejecucion.execute_market_trade(
                    symbol=clave, action=direction, volume=None,
                    sl_distance=abs(entry - sl),
                    # Sin objetivo: la salida la decide el trailing D1, y un target
                    # congelado aquí sería una regla que F2 no convalidó.
                    no_tp=True,
                    magic=magic, comment=comment or None,
                    # 0.0 y no `None`: la columna score es numérica; el CTA no tiene
                    # gate de score (lo tiene el watcher).
                    score=0.0, verdict=VERDICTO_CTA,
                    invalidate_level=None,
                    # El fill planificado es el next-open de la señal: si el precio
                    # se aleja de él, el gate de deriva lo dice (y queda auditable).
                    planned_entry=entry,
                    components=breakdown,
                    context={"magic": magic, "profile": PERFIL_ESPERADO,
                             "comment": comment},
                    timeframe=timeframe, source=SOURCE_CTA,
                )
            except Exception as exc:  # noqa: BLE001 - un puerto roto no tumba el scan
                log.warning("execute_market_trade revintió para %s: %s", clave, exc)
                cuerpo, status = {"error": str(exc)}, 500
            ok = status == 200 and bool(cuerpo.get("ok"))
            setup_id = cuerpo.get("setup_id")
            evento = {
                "symbol": clave,
                "timeframe": timeframe,
                "event": "alerta",
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "fill": fill,
                "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                "auto_ejecutado": ok,
                "ejecucion_status": status,
                # La fila de ESTE intento la escribe ExecutionService, no este
                # servicio: `auditado` es que setup_id existe, venga de donde venga.
                "auditado": setup_id is not None,
            }
            if not ok:
                evento["ejecucion_motivo"] = (
                    cuerpo.get("error") or cuerpo.get("status")
                    or "rechazada sin motivo declarado")
            return (
                {**base_status, "status": "alerta", "ejecutado": ok,
                 "auditado": setup_id is not None},
                evento,
            )

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

        # La dedup ya está puesta (arriba): si el disco falla, la fila se pierde y
        # NO se reintenta, porque reintentar en cada ciclo de un D1 escribiría el
        # mismo fallo para siempre.
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
        if auto_conf and self._ejecucion is None:
            # El YAML lo pedía y no hubo intento: que el motivo esté en el EVENTO
            # (además de en `estado()`) es lo que impide leer "no se intentó" como
            # "no había señal".
            evento["ejecucion_motivo"] = MOTIVO_SIN_PUERTO_DE_EJECUCION
        return {**base_status, "status": "alerta", "auditado": id_fila is not None}, evento

    # -- salidas: trailing D1 ----------------------------------------------------

    def gestionar_salidas(self, ahora: Optional[datetime] = None) -> Dict[str, Any]:
        """Un pase de trailing chandelier sobre las posiciones del magic CTA.

        La otra mitad de F5: el stop que pone la apertura no puede ser el último
        stop que el sistema pone jamás, o la regla convalidada en F2 (ratchet "nunca
        afloja", barra a barra) existiría solo en el backtest. Por posición:

        1. `ExecutionService.positions(magic)` — solo las del CTA. Sin posiciones
           el pase es correcto y vacío (`[], None` no es error).
        2. Con las barras D1 del símbolo, SOLO las cerradas (misma regla que el
           scan), el ATR de la última y su extremo (high en BUY, low en SELL) como
           `ref`: exactamente la convención del backtest (`research/cta.py`).
        3. `exit_policy.trailing_stop(...)` devuelve el candidato o `None` — el
           ratchet no afloja, y en ese caso NO se llama al bróker.
        4. Si hay candidato, `ExecutionService.modify_stop`. Aquí no hay lista
           blanca, riesgo del día ni noticias: mover un stop no ABRÍ riesgo
           (ver `modify_stop`), y un trailing bloqueado dejaría el stop viejo
           justo cuando más se necesita. Tampoco se escribe fila en `setup_log`:
           la fila de esa posición existe desde su apertura.

        Nunca lanza: cualquier fallo (puerto, fila rota, un símbolo sin datos) sale
        como `status`/`error` en la respuesta, porque esto se dispara desde una ruta
        y el bucle del operador tiene que poder leer el resultado aunque algo fallen.
        """
        momento = ahora or datetime.now().astimezone()
        habilitado, motivo, perfil = self._config()
        base: Dict[str, Any] = {
            "trail_at": momento.isoformat(),
            "enabled": habilitado,
            "motivo": motivo,
            "profile": PERFIL_ESPERADO,
            "magic": perfil.get("magic"),
            "exit_policy": exit_policy.policy_for(PERFIL_ESPERADO),
            "positions": 0,
            "results": [],
            "modificados": 0,
            "sin_cambio": 0,
            "sin_sl": 0,
            "errores": 0,
            "error": None,
        }
        if not habilitado:
            # Sin perfil válido no hay magic confiable ni motor: mover el stop de
            # QUIÉN con qué parámetros sería adivinar.
            return base
        if self._ejecucion is None:
            return {**base, "error": "no hay puerto de ejecución cableado: no hay "
                                     "nada con lo que mover los stops"}
        try:
            filas, error = self._ejecucion.positions(magic=int(perfil["magic"]))
        except Exception as exc:  # noqa: BLE001 - un puerto roto se reporta, no revienta
            return {**base, "error": str(exc)}
        if filas is None:
            return {**base, "error": error}

        engine = self._engine(perfil)
        results: List[Dict[str, Any]] = []
        modificados = sin_cambio = sin_sl = errores = 0
        for pos in filas:
            try:
                r = self._trail_de_posicion(dict(pos), engine, momento)
            except Exception as exc:  # noqa: BLE001 - una fila rota no tumba el pase
                r = {"ticket": (pos or {}).get("ticket"),
                     "symbol": (pos or {}).get("symbol"),
                     "status": "error", "motivo": str(exc)}
            results.append(r)
            est = r.get("status")
            if est == "modificado":
                modificados += 1
            elif est == "sin_cambio":
                sin_cambio += 1
            elif est == "sin_sl":
                sin_sl += 1
            else:
                # `error`, `sin_datos`, `sin_atr`: para ESTE pase no se pudo mover
                # el stop. El motivo de cada uno está en su fila.
                errores += 1
        return {**base, "positions": len(filas), "results": results,
                "modificados": modificados, "sin_cambio": sin_cambio,
                "sin_sl": sin_sl, "errores": errores}

    def _trail_de_posicion(self, pos: Dict[str, Any], engine: Dict[str, Any],
                           momento: datetime) -> Dict[str, Any]:
        """El desenlace de UNA posición: `modificado`, `sin_cambio`, `sin_sl`,
        `sin_datos`, `sin_atr` o `error`.

        `sin_sl` es deliberado: una posición sin stop (el bróker permite abrir sin
        SL) no recibe uno inventado aquí — inventar el nivel de una salida que la
        convalidación no evaluó sería una regla nueva disfrazada de mantenimiento.
        """
        ticket = pos.get("ticket")
        base: Dict[str, Any] = {"ticket": ticket, "symbol": pos.get("symbol")}
        if not ticket:
            return {**base, "status": "error", "motivo": "la fila no trae ticket"}
        tipo = str(pos.get("type") or "").upper()
        if tipo not in ("BUY", "SELL"):
            return {**base, "status": "error",
                    "motivo": "dirección desconocida: {0!r}".format(pos.get("type"))}
        try:
            sl = float(pos.get("sl") or 0.0)
        except (TypeError, ValueError):
            sl = 0.0
        if sl <= 0.0:
            return {**base, "status": "sin_sl",
                    "motivo": "la posición no tiene stop que mover (no se inventa uno)"}

        symbol = str(pos.get("symbol") or "")
        velas, error = self._velas(symbol, TIMEFRAME_CONVALIDADO, BARS_DEFECTO)
        if velas is None:
            return {**base, "status": "sin_datos", "motivo": error}
        cerradas, _formante = self._particion(velas, momento)
        if len(cerradas) < engine["atr_n"]:
            return {**base, "status": "sin_datos",
                    "motivo": "{0} barras cerradas, hace falta {1} (atr_n)".format(
                        len(cerradas), engine["atr_n"])}

        atrs = cta.atr([float(v["high"]) for v in cerradas],
                       [float(v["low"]) for v in cerradas],
                       [float(v["close"]) for v in cerradas], n=engine["atr_n"])
        atr_value = atrs[-1]
        if atr_value is None:
            return {**base, "status": "sin_atr",
                    "motivo": "el ATR de la última barra cerrada no es calculable"}

        # ref = extremo de la ÚLTIMA barra cerrada, la convención exacta con la que
        # el backtest recorría el stop barra a barra (research/cta.py L177-179).
        ultima = cerradas[-1]
        ref = float(ultima["high"]) if tipo == "BUY" else float(ultima["low"])
        nuevo = exit_policy.trailing_stop(tipo, sl, ref, float(atr_value),
                                          engine["mult"])
        if nuevo is None:
            # El ratchet no mejora: sin llamada al bróker, sin fila, sin ruido.
            return {**base, "status": "sin_cambio", "sl": sl}

        cuerpo, status = self._ejecucion.modify_stop(ticket, sl=nuevo)
        if status == 200:
            return {**base, "status": "modificado", "sl": sl, "sl_nuevo": nuevo}
        return {**base, "status": "error", "sl": sl, "sl_nuevo": nuevo,
                "motivo": cuerpo.get("error"), "http": status,
                "broker_status": cuerpo.get("status")}


__all__ = [
    "AUTO_EJECUCION_DISPONIBLE",
    "BARS_DEFECTO",
    "MOTIVO_SIN_AUTO_EJECUCION",
    "MOTIVO_SIN_PUERTO_DE_EJECUCION",
    "SOURCE_CTA",
    "TIMEFRAME_CONVALIDADO",
    "VERDICTO_CTA",
    "CtaAlertService",
]
