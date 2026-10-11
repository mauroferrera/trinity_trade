"""El Mean Reversion a VWAP D1 (F6, D-080): evalúa SOLO el régimen de rango y audita.

Qué hace
--------
Es el servicio hermano de `api/services/cta_alert_service.py` para la pata de
compresión del sistema multi-estrategia (D-071). Una sola operación de escaneo:

- `estado()` y `escanear()`, iguales en forma a las del watcher y del CTA: el
  escaneo mira los símbolos del perfil propio (`config/strategy_vwap.yaml`), evalúa
  el motor CONVALIDADO en F3 (`research/mr.py`) sobre SOLO las barras D1 cerradas,
  y si la última cerrada cerró fuera de la banda (estirada), escribe una fila en
  `setup_log` con el magic 8882028 y el perfil `vwap`. La fila va con
  `trade_result: {"executed": False, "dry_run": True, "mode": "alert"}`.

Cómo se activa la ejecución (spoiler: no existe)
-------------------------------------------------
Este perfil NO ejecuta. `AUTO_EJECUCION_DISPONIBLE` es `False` y no hay puerto de
ejecución en el constructor: en F6 el MR VWAP es, por decisión, SOLO alerta. Sus
salidas son niveles fijos en la apertura (target = VWAP, stop = k·ATR) y un techo
temporal (`max_hold`); no hay trailing que mantener, así que no existe ni
`gestionar_salidas()` ni `Policy` de chandelier. Si la ejecución llega, será otra
fase con su convalidación en vivo delante, como le pasó al CTA entre F4 y F5.

El GATE de régimen (el interruptor de F3, D-079)
------------------------------------------------
Lo que le da identidad a esta estrategia es CUÁNDO opera: solo se revierte al
VWAP cuando el mercado está comprimido. `escanear()` evalúa `core.risk_engine.
regime()` sobre las barras cerradas —el MISMO slice que usó el backtest
(`research/backtest_mr.py` L154: `regime(candles[:bar_i + 1])`), nunca la barra
formante: la heurística no puede ver el futuro de la barra de señal— y solo alerta
si el resultado es `REGIME_RANGO`. Si el régimen es expansión o neutro, el
resultado del símbolo es `sin_regimen` con el detalle de por qué: la señal existe,
el gate la descarta, y que se descarte por régimen tiene que poder distinguirse de
que no hubiera señal. `regime_lookback` del engine (250) es la ventana de ese
percentil: `bars` pide 260 para que la clasificación tenga su historia entera.

La señal (misma convención que F3)
----------------------------------
- VWAP rolling N barras sobre precio típico (H+L+C)/3 ponderado por `tick_volume`;
  bandas ± k·ATR (ATR Wilder 14). Cierre por encima de la banda superior → SHORT
  (estirado, espera revertir al VWAP); por debajo de la inferior → LONG.
- La banda de la barra `i` se conoce al cerrar `i` (VWAP y ATR de `i` son datos de
  ESA barra ya cerrada, no hay lookahead): el fill va al open de `i+1`
  (next-open, la convención con la que se convalidó en F3).
- Target fijo = VWAP de la barra de señal. Si el open ya quedó del lado equivocado
  (`target <= entry` en LONG, `>= ` en SHORT), el setup no existe: no se "arregla"
  moviendo el target (`sin_target`, la misma regla que el backtest L163-167).
- Stop = fill ∓ `sl_k`·ATR. Hueco y SL-primero son reglas del simulador: la ALERTA
  no simula, solo fija los niveles; el `trade_result` va en `mode: "alert"`.

Anti-lookahead (la regla de barras cerradas, idéntica al CTA)
--------------------------------------------------------------
`market.candles(sym, "D1", n)` devuelve la barra EN FORMACIÓN como último elemento.
Regla conservadora (D-077): barra `i` cerrada si existe `i+1`, y la ÚLTIMA se da por
cerrada SOLO si `ahora >= time + 25 h` (24 h de sesión + 1 h de margen por el cambio
de hora: decide TARDE, nunca temprano).

Dedup en memoria por (símbolo, tiempo de la barra de señal): un reinicio del proceso
reescribe la fila (ruido aceptable y documentado); NO se usa `setup_state`, cuyo TTL
es de minutos y esto es D1.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from core import exit_policy, risk_engine, strategy_map
from research import cta, mr

log = logging.getLogger(__name__)

#: ¿Puede este scan mandar una orden? No, y no es un "aún": es la decisión de F6
#: (D-080). El MR VWAP es SOLO alerta; su ejecución, si llega, será otra fase con su
#: convalidación en vivo delante. Publicarlo en `estado()` es lo que permite que un
#: panel muestre por qué este perfil nunca manda órdenes.
AUTO_EJECUCION_DISPONIBLE = False

#: Motivo de `auto_execute_motivo`. Va en `estado()` y, si algún día pide el YAML
#: `auto_execute: true`, es la frase que explica por qué se ignora: la bandera controla
#: algo que este código no sabe hacer, y un interruptor que miente sobre lo que
#: controla es peor que un interruptor que siempre está apagado.
MOTIVO_SIN_AUTO_EJECUCION = (
    "El MR VWAP es SOLO alerta por decisión de F6 (D-080): evalúa, audita y nunca "
    "manda una orden. La ejecución, si llega, será otra fase con su convalidación "
    "en vivo delante; auto_ejecute en el YAML se ignora hasta que ese camino exista."
)

#: Veredicto de una alerta del MR VWAP. Es el texto con el que se consulta la
#: auditoría, así que es constante y no un f-string por ciclo.
VERDICTO_VWAP = "MR_VWAP_BAND"

#: `source` de las filas que escribe. El watcher usa "watcher" y el CTA "cta_alert";
#: esta es OTRA estrategia y la distinción es lo que permite filtrar por pata.
SOURCE_VWAP = "vwap_alert"

#: Perfil al que debe resolver el magic en `strategy_map.yaml`.
PERFIL_ESPERADO = "vwap"

#: Solo se evalúa en D1: es la temporalidad en la que el MR se convalidó (F3). Otra
#: temporalidad no es "el mismo sistema con otro timeframe", es otra estrategia.
TIMEFRAME_CONVALIDADO = "D1"

TIMEFRAME_DEFECTO = "D1"
BARS_DEFECTO = 260

#: Horas mínimas desde el open de la última barra para darla por cerrada. 24 h de
#: sesión + 1 h de margen por el cambio de hora (misma regla que el CTA).
MARGEN_CIERRE_S = 25 * 3600

#: Campos que cada vela tiene que traer. `volume` es el `tick_volume` con el que
#: pondera el VWAP: sin él la vela no sirve para ESTE motor, aunque sirva para el del
#: CTA (que no la usa).
_CAMPOS_VELA = ("time", "open", "high", "low", "close", "volume")

#: Campos del engine que un perfil COMPLETO tiene que traer. `max_hold` no decide en
#: este servicio (no hay posición que tener), pero exigirlo aquí es lo que evita que
#: un perfil truncado se presente como "habilitado" y falle en la fase que lo use.
_CAMPOS_ENGINE = ("atr_n", "vwap_n", "k_bands", "sl_k", "max_hold", "regime_lookback")


def _perfil_de_fabrica() -> Dict[str, Any]:
    """`config/strategy_vwap.yaml` vía su source. Import tardío: yaml solo si hace falta."""
    from settings import strategy_vwap_source  # noqa: PLC0415 - tardío (yaml)

    return strategy_vwap_source.load()


def _mapa_de_fabrica() -> Dict[int, str]:
    """`config/strategy_map.yaml` vía su source. Misma razón para el import tardío."""
    from settings import strategy_map_source  # noqa: PLC0415 - tardío (yaml)

    return strategy_map_source.load()


class VwapAlertService:
    """Alerta D1 del Mean Reversion a VWAP: evalúa el régimen rango y audita.

    Dos operaciones: `estado()` y `escanear()`, iguales en forma a las del watcher y
    del CTA. NO hay ejecución (ver el docstring del módulo): ni puerto inyectado ni
    camino a una orden en todo el servicio.

    Los colaboradores son opcionales e inyectables, igual que en `CtaAlertService`:
    un test hace `VwapAlertService(market=Falso(), store=Falso(), perfil=lambda: {...},
    mapa=lambda: {...})` y ya tiene el servicio entero. Los callables de config existen
    para que los tests no dependan de `config/` ni de `settings/`.
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
        #: de datos, como la dedup del CTA y del watcher: es dedup de este proceso. Un
        #: reinicio reescribe la fila (ruido aceptable); lo que no es aceptable es una
        #: dedup persistente que oculte una señal NUEVA porque se parece a una vieja.
        self._alertas: Dict[str, int] = {}

    # -- configuración ----------------------------------------------------------

    def _perfil_y_error(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """`(perfil, error_de_lectura)`. Un perfil ilegible no es `{}`: es un motivo."""
        try:
            return dict(self._perfil() or {}), None
        except Exception as exc:  # noqa: BLE001 - la config manda, y si no se lee se dice
            log.warning("no se pudo leer config/strategy_vwap.yaml: %s", exc)
            return {}, "no se pudo leer config/strategy_vwap.yaml: {0}".format(exc)

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
            return False, "no hay perfil: falta config/strategy_vwap.yaml (o está vacío)", perfil
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
                "espera '{2}': el MR VWAP no puede alertar con un magic ajeno".format(
                    magic, resuelto, PERFIL_ESPERADO)), perfil

        if str(perfil.get("timeframe") or TIMEFRAME_DEFECTO).upper() != TIMEFRAME_CONVALIDADO:
            return False, (
                "el MR VWAP está convalidado en {0} y el perfil pide {1!r}".format(
                    TIMEFRAME_CONVALIDADO, perfil.get("timeframe"))), perfil

        simbolos = [s for s in (perfil.get("symbols") or []) if str(s).strip()]
        if not simbolos:
            return False, "perfil.symbols está vacío: no hay nada que escanear", perfil

        engine = perfil.get("engine") or {}
        faltan = [k for k in _CAMPOS_ENGINE if not engine.get(k)]
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
        return {"atr_n": int(engine["atr_n"]), "vwap_n": int(engine["vwap_n"]),
                "k_bands": float(engine["k_bands"]), "sl_k": float(engine["sl_k"]),
                "max_hold": int(engine["max_hold"]),
                "regime_lookback": int(engine["regime_lookback"])}

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
                return None, "la vela no trae OHLC+volume completo ({0})".format(
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
        """El estado de riesgo del día, o `{}` con `error` dentro. Idéntico a los otros."""
        if self._market is None:
            return {"error": "mercado no cableado"}
        try:
            estado = self._market.daily_risk_state() or {}
        except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
            return {"error": str(exc)}
        if not isinstance(estado, dict):
            return {"error": "daily_risk_state no devolvió un dict"}
        return dict(estado)

    def _regimen(self, cerradas: List[Dict[str, Any]], lookback: int
                 ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """`(reg, error)`. `reg` es el dict de `risk_engine.regime`, sin clasificar.

        `None` en `reg` significa "no se pudo clasificar": la regla de F6 es que sin
        régimen no se alerta. Un fallo aquí (serie no positiva, sin historia, excepción)
        se trata igual por la misma razón: el interruptor abre SOLO al régimen de rango
        y una heurística rota no debe dejarlo abierto por defecto.
        """
        try:
            reg = risk_engine.regime(cerradas, lookback=lookback) or {}
        except Exception as exc:  # noqa: BLE001 - un gate roto no tumba el ciclo
            log.warning("regime() falló para el escaneo vwap: %s", exc)
            return None, "regime() revintió: {0}".format(exc)
        return reg, None

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

        `auto_execute_conf` publica lo que pide el YAML (que debería ser `false`);
        `auto_execute` es SIEMPRE `false`, porque este perfil no sabe ejecutar
        (AUTO_EJECUCION_DISPONIBLE): un panel que viera el interruptor encendido
        estaría leyendo una configuración que el código no respeta.
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
            # Lo que el YAML pide (debería ser false)...
            "auto_execute_conf": bool(perfil.get("auto_execute")),
            # ...y lo que el sistema hace: nada, porque no sabe hacer otra cosa.
            "auto_execute": False,
            "auto_execute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_execute_motivo": MOTIVO_SIN_AUTO_EJECUCION,
            "dry_run": True,
            # Salida de niveles fijos en la apertura (target=VWAP, stop=k·ATR,
            # max_hold): nadie mueve stops, `policy_for` es "static" y no existe
            # gestionar_salidas.
            "exit_policy": exit_policy.policy_for(PERFIL_ESPERADO),
        }

    # -- escaneo -----------------------------------------------------------------

    def escanear(self, ahora: Optional[datetime] = None) -> Dict[str, Any]:
        """Un ciclo completo: evalúa cada símbolo, audita y devuelve los eventos.

        Por símbolo, en orden del perfil:

        1. Velas. Sin datos → evento `error` con el motivo y se sigue.
        2. Partición cerradas/formante (regla de arriba). Insuficientes →
           `sin_datos`: sin las cerradas del warm-up no hay VWAP que comparar.
        3. Régimen (el interruptor F3): `regime()` sobre SOLO las cerradas. Distinto
           de rango → `sin_regimen` con el detalle, y NO se alerta: la señal existe,
           el gate la descarta, y que se descarte por régimen tiene que distinguirse
           de que no hubiera señal.
        4. Señal SOLO en la última cerrada (`research/mr.py`). Una señal vieja no es
           una alerta nueva: el fill habría sido el open de una barra que ya cerró.
        5. Dedup por (símbolo, barra de señal).

        `results` lleva el desenlace de cada símbolo (`alerta`, `dedup`, `sin_senal`,
        `sin_regimen`, `sin_target`, `sin_ind`, `sin_datos`, `error`), `events` solo
        las alertas escritas y `errors` los símbolos que no se pudieron mirar. Van
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
                min_cerradas = engine["vwap_n"]
                if len(cerradas) < min_cerradas:
                    motivo_simbolo = (
                        "{0} barras cerradas, hacen falta {1} (vwap_n)".format(
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
            "auto_ejecutadas": 0,
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
        """`(resultado, evento)`. El evento es `None` si no hay alerta que contar.

        El orden es el del docstring: régimen ANTES que señal, porque el régimen es
        el interruptor que DA licencia a la señal contrarian (F3).
        """
        base_status: Dict[str, Any] = {"bars_closed": len(cerradas)}
        ultima = len(cerradas) - 1
        highs = [float(v["high"]) for v in cerradas]
        lows = [float(v["low"]) for v in cerradas]
        closes = [float(v["close"]) for v in cerradas]
        volumes = [float(v.get("volume") or 0.0) for v in cerradas]

        # 1) El interruptor: solo rango da licencia a revertir. El slice SON las
        # cerradas (barra de señal inclusive), el MISMO slice del backtest de F3
        # (`regime(candles[: bar_i + 1])`): nunca la formante.
        reg, error_reg = self._regimen(cerradas, engine["regime_lookback"])
        if error_reg is not None:
            return {**base_status, "status": "sin_regimen", "motivo": error_reg}, None
        if reg is None or reg.get("regime") != risk_engine.REGIME_RANGO:
            detalle = "sin régimen" if reg is None else "régimen {0}".format(reg.get("regime"))
            return {**base_status, "status": "sin_regimen", "regime": detalle,
                    "detail": reg.get("detail") if reg else None}, None

        atrs = cta.atr(highs, lows, closes, n=engine["atr_n"])
        vwap_s = mr.vwap(highs, lows, closes, volumes, n=engine["vwap_n"])
        sup, inf = mr.bands(vwap_s, atrs, k=engine["k_bands"])
        senales = mr.mr_signals(closes, sup, inf)

        if not senales or senales[-1][1] != ultima:
            return {**base_status, "status": "sin_senal",
                    "regime": reg.get("regime")}, None

        direccion, i = senales[-1]
        atr_dec = atrs[i]
        target = vwap_s[i]
        if atr_dec is None or target is None:
            # Inalcanzable con la guarda de `vwap_n` cerradas y los defaults (ATR 14),
            # pero un perfil con warm-ups raros puede llegar aquí: decirlo es mejor que
            # un `float(None)` a mitad del scan.
            return {**base_status, "status": "sin_ind",
                    "regime": reg.get("regime")}, None

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

        # Fill: open de la barra formante (next-open, la convención de F3). Si la
        # última ya cerró (fin de semana), no hay formante y el precio de referencia
        # es el close de esa barra: el mercado está cerrado y quien quiera entrar lo
        # hará en la apertura; la alerta lleva el último precio conocido.
        if formante is not None:
            entry = float(formante["open"])
            fill = "next_open"
        else:
            entry = float(cerradas[-1]["close"])
            fill = "close"

        # El target es el VWAP de la señal (fijo, conocido al abrir). Si el open ya
        # quedó del lado equivocado, el setup no existe: no se "arregla" moviendo el
        # target (la misma regla del backtest, L163-167).
        if (direccion == "long" and target <= entry) or (
            direccion == "short" and target >= entry
        ):
            return {**base_status, "status": "sin_target",
                    "regime": reg.get("regime"),
                    "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat()}, None

        sl = (entry - engine["sl_k"] * float(atr_dec)) if direccion == "long" else (
            entry + engine["sl_k"] * float(atr_dec))
        direction = "BUY" if direccion == "long" else "SELL"

        # La dedup se guarda ANTES de la fila: si el disco falla, no se reintenta en
        # cada ciclo de un D1 (la barra de señal no cambia y perseguir el mismo fallo
        # hasta el martes sería ruido). El evento lo dice con `auditado`.
        self._alertas[clave] = bar_time

        breakdown: Dict[str, Any] = {
            "vwap": {
                # La dirección CRUDA del motor (long/short) vive aquí; la fila lleva
                # BUY/SELL porque `setup_log` se lee en esos términos (store, risk_engine).
                "direction": direccion,
                "atr": float(atr_dec),
                "atr_n": engine["atr_n"],
                "vwap_n": engine["vwap_n"],
                "k_bands": engine["k_bands"],
                "sl_k": engine["sl_k"],
                "max_hold": engine["max_hold"],
                "regime_lookback": engine["regime_lookback"],
                # El interruptor que abrió (o no alertó): la clasificación completa se
                # queda en la fila, con su percentil y su guardia de Hurst, para que la
                # auditoría distinga el gate del motor.
                "regime": reg.get("regime"),
                "vol_percentile": reg.get("vol_percentile"),
                "hurst": reg.get("hurst"),
                "hurst_class": reg.get("hurst_class"),
                "bars_closed": len(cerradas),
                "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                "fill": fill,
                "sl_distance": abs(entry - sl),
                "target_distance": abs(target - entry),
                "magic": magic,
            }
        }
        if riesgo_error:
            breakdown["risk_state_error"] = riesgo_error

        fila = {
            "symbol": clave,
            "timeframe": timeframe,
            "direction": direction,
            "verdict": VERDICTO_VWAP,
            # El MR VWAP no tiene gate de score (lo tiene el watcher). 0.0 y no `None`
            # porque la columna es numérica y un scoring inventado sería mentir.
            "score": 0.0,
            "breakdown": breakdown,
            "entry": entry,
            "sl": sl,
            # Target real = VWAP de la barra de señal: es la salida convalidada.
            "target": float(target),
            # Sin nivel de invalidación: es una regla que la convalidación F3 no evaluó.
            "invalidate_level": None,
            "validated": True,
            "reject_reasons": [],
            "risk_state": riesgo,
            # `mode: "alert"` distingue estas filas de las del watcher.
            "trade_result": {"executed": False, "dry_run": True, "mode": "alert"},
            "context": {"magic": magic, "profile": PERFIL_ESPERADO, "comment": comment},
            "source": SOURCE_VWAP,
        }

        id_fila = self._registra(fila)
        evento = {
            "symbol": clave,
            "timeframe": timeframe,
            "event": "alerta",
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "target": float(target),
            "fill": fill,
            "signal_bar": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
            "regime": reg.get("regime"),
            "vol_percentile": reg.get("vol_percentile"),
            "auto_ejecutado": False,
            "auditado": id_fila is not None,
        }
        return {**base_status, "status": "alerta", "auditado": id_fila is not None}, evento


__all__ = [
    "AUTO_EJECUCION_DISPONIBLE",
    "BARS_DEFECTO",
    "MOTIVO_SIN_AUTO_EJECUCION",
    "SOURCE_VWAP",
    "TIMEFRAME_CONVALIDADO",
    "VERDICTO_VWAP",
    "VwapAlertService",
]