"""El watcher: qué se está vigilando y qué setups hay ahora mismo.

Dos operaciones, y solo dos
---------------------------
`estado()` y `escanear()`. El escaneo evalúa, **audita** y avisa; no manda órdenes. Es
una decisión explícita (D-063): la ruta que ejecuta es `POST /api/trade/market`, con su
token y sus manos, y el watcher se queda en mirar y en dejar constancia de lo que miró.
Auto-ejecutar se declara en `strategy.yaml` y se publica en el estado, pero no hay
código que la respete porque no hay código que la ejecute.

Por qué el watcher NO tiene su propio camino a la orden
-------------------------------------------------------
En REF el escaneo tenía un `executor` inyectado y, con `auto_execute` a true, mandaba
la orden dentro del mismo ciclo que detectaba el setup. Eso convierte un
`auto_execute: true` en una decisión de arranque: el sistema empieza a operar solo desde
que el proceso existe, sin nadie que pulse nada. Con las puertas de `ExecutionService`
(última blanca, riesgo del día, noticias, `validate_entry`, calidad de ejecución) ese
camino seguiría siendo seguro en lo que decide, pero no en lo que cuesta equivocarse:
un proceso que arranca y opera no tiene a nadie mirando cuando opera.

Cuando el auto-arranque entre, entra por `ExecutionService.execute_market_trade` y no
por un `order_send` propio. Lo que se)|^2trae de REF a esta altura es la dedup del
ciclo, el log de la noticia y el log de los descartes.

Lo que este servicio NO hace
-----------------------------
- No ejecuta. `AUTO_EJECUCION_DISPONIBLE` es `False` y no hay forma de que un `scan` lo
  convierta en `True` con un parámetro.
- No lanza el bucle. No hay `asyncio` aquí: escanear es una operación que el operador
  pide y una función que se puede probar. Un bucle en segundo plano dentro de un servicio
  HTTP es una cosa que sigue viva cuando el request ya terminó.
- No inventa el estado de riesgo. Si `daily_risk_state` falla, la fila se escribe con
  `risk_state: {}` y el ciclo lo dice en `riesgo.error`: una fila de auditoría con el
  riesgo vacío se puede distinguir de una con el riesgo leído, que es justo lo que hace
  falta al leerla después.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core import setup_gate, setup_lifecycle, strategy

log = logging.getLogger(__name__)

#: ¿Hay auto-ejecución? No. Es la respuesta a la pregunta "¿puede este escaneo mandar
#: una orden?" y la respuesta es que no, siempre, hasta que se escriba el código que la
#: mande. Publicarlo en el estado, en vez de callarlo, es lo que permite que el frontend
#: muestre el interruptor apagado y explica por qué.
AUTO_EJECUCION_DISPONIBLE = False

#: Motivo único de por qué no. Va en el estado y en la respuesta de la ruta de
#: auto-ejecución, con la misma frase en los dos sitios: si divergen, el operador lee uno
#: y se decide por el otro.
MOTIVO_SIN_AUTO_EJECUCION = (
    "El watcher no manda órdenes: evalúa setups y los audita. La ejecución es "
    "POST /api/trade/market, que pasa por ExecutionService con sus puertas "
    "(lista blanca, riesgo del día, noticias, validate_entry y calidad de ejecución). "
    "Auto-ejecutar requiere escribir antes ese camino, no solo encender una bandera."
)

#: Veredicto con el que se registra un setup que cae en la ventana de noticias. Es una
#: constante porque `setup_log` se consulta por este texto y porque el `status` del ciclo
#: (`news_ignored`) y este veredicto tienen que contarse juntos.
VERDICTO_NOTICIA = "IGNORED_NEWS"

#: Timeframe por defecto cuando la configuración del watcher no lo dice. M15 es el
#: timeframe del gate de REF y el que trae `watcher.symbols` por defecto.
TIMEFRAME_DEFECTO = "M15"


class WatcherService:
    """Estado y escaneo del watcher. No ejecuta órdenes.

    Los colaboradores son opcionales e inyectables, igual que en `ExecutionService`: un
    test hace `WatcherService(market=Falso(), store=Falso(), news=Falso())` y ya tiene
    el servicio entero. Sin `store` no hay ni configuración ni auditoría, y el escaneo
    lo dice en vez de fingir que no pasa nada.
    """

    def __init__(self, market: Any = None, store: Any = None, news: Any = None) -> None:
        self._market = market
        self._store = store
        self._news = news
        #: Firmas de rechazo ya registradas, por símbolo/timeframe. Vive en la
        #: instancia y NO en la base de datos: es memoria de este proceso para no
        #: escribir la misma fila 4 veces por minuto. Si el proceso se reinicia se
        #: vuelve a escribir una fila, que es un ruido aceptable; lo que no es
        #: aceptable es una deduplicación persistente que oculte un rechazo nuevo porque
        #: se parece a uno viejo.
        self._rejects: Dict[Tuple[str, str], Tuple[Any, ...]] = {}

    # -- configuración ----------------------------------------------------------

    def _cfg_watcher(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.get_watcher_config() or {}
        except Exception as exc:  # noqa: BLE001 - sin config no hay ciclo que escanear
            log.warning("no se pudo leer la configuración del watcher: %s", exc)
            return {}

    def _cfg_trading(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        try:
            return self._store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - el umbral cae al default de strategy.yaml
            return {}

    # -- puertos de lectura ------------------------------------------------------

    def _estado_previo(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        if self._store is None:
            return None
        try:
            return self._store.get_setup_state(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001 - sin estado previo se trata como ciclo nuevo
            log.warning("no se pudo leer el estado previo de %s %s: %s", symbol, timeframe, exc)
            return None

    def _snapshot(self, symbol: str, timeframe: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """`(snapshot, error)`.

        El error es un `str` porque se propaga como evento y no como excepción: un símbolo
        sin datos en este escaneo no es un fallo del escaneo entero. Si un símbolo del
        watcher no tiene velas, los demás siguen evaluándose y el operador ve cuál fue.
        """
        if self._market is None:
            return None, "mercado no cableado"
        try:
            snap = self._market.chart_snapshot(symbol, timeframe)
        except Exception as exc:  # noqa: BLE001 - un símbolo no tumba el ciclo
            return None, str(exc)
        if not isinstance(snap, dict) or not (snap.get("risk_engine") or {}):
            return None, "sin risk engine en el snapshot (no hay velas o no se pudo calcular)"
        return snap, None

    def _spec(self, symbol: str) -> Any:
        """El `SymbolSpec` del símbolo, o `None`. Nunca inventado.

        Igual que en `api/routes/setup.py`: `None` degrada los motivos a distancia de
        precio. Un spec inventado daría unidades equivocadas en el mercado donde más
        caro sale equivocarse.
        """
        if self._market is None:
            return None
        try:
            return self._market.spec(symbol)
        except Exception:  # noqa: BLE001 - el spec es presentación, no veredicto
            return None

    def _riesgo_del_dia(self) -> Dict[str, Any]:
        """El estado de riesgo del día, o `{}` con `error` dentro.

        Vacío y no inventado: sin él, la fila de auditoría no distingue "el día iba
        bien" de "no se pudo leer el día", y esas dos filas se leen igual en la revisión
        de un mes.
        """
        if self._market is None:
            return {"error": "mercado no cableado"}
        try:
            estado = self._market.daily_risk_state() or {}
        except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
            return {"error": str(exc)}
        if not isinstance(estado, dict):
            return {"error": "daily_risk_state no devolvió un dict"}
        return dict(estado)

    def _gate_noticias(self) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """`(bloquea, veredicto, aviso_de_fail_open)`.

        Misma política que `ExecutionService._gate_noticias`, y por el mismo motivo: un
        `block: True` manda aunque el veredicto venga `fail_open`, y un feed que no
        responde deja pasar el ciclo **diciéndolo**. El watcher no bloquea al mercado —
        bloquea su propia alerta—, así que aquí el fail-open no arriesga dinero; aun así
        no puede ser silencioso, porque un `verdict="IGNORED_NEWS"` con motivo "feed
        caído" es una afirmación falsa sobre el calendario.
        """
        if self._news is None:
            return False, {}, "puerto de noticias no cableado: el blackout no se ha comprobado"
        try:
            veredicto = self._news.gate() or {}
        except Exception as exc:  # noqa: BLE001 - un feed caído no bloquea
            return False, {"error": str(exc)}, (
                "No se pudo consultar el calendario: {0}".format(exc))
        if veredicto.get("block"):
            return True, veredicto, None
        if veredicto.get("fail_open"):
            return False, veredicto, (
                "El calendario no devolvió veredicto: el escaneo ha seguido sin poder "
                "comprobar si hay evento de alto impacto.")
        return False, veredicto, None

    # -- auditoría --------------------------------------------------------------

    def _registra(self, entry: Dict[str, Any]) -> Optional[int]:
        """`store.log_setup` sin dejar que un fallo de auditoría tumbe el escaneo.

        `None` es un resultado honesto: el escaneo puede continuar (auditar no es
        vigente) pero el ciclo lo dice en `audit_logged=False`. Perder la fila de un
        setup detectado es un agujero en la auditoría, y esconderlo dejaría un "no se
        registró ningún setup" indistinguible de "se registró y se perdió".
        """
        if self._store is None:
            return None
        try:
            return self._store.log_setup(entry)
        except Exception as exc:  # noqa: BLE001 - perder el log no cambia lo que pasó
            log.warning("no se pudo registrar el setup en setup_log: %s", exc)
            return None

    def _nuevo_estado(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                      status: str, notificado: bool) -> None:
        if self._store is None:
            return
        try:
            self._store.new_setup_state(symbol, timeframe, gate=gate,
                                        status=status, notified=notificado)
        except Exception as exc:  # noqa: BLE001 - el estado es dedup, no la auditoría
            log.warning("no se pudo guardar el estado de %s %s: %s", symbol, timeframe, exc)

    def _cierra_estado(self, symbol: str, timeframe: str, status: str) -> None:
        if self._store is None:
            return
        try:
            self._store.update_setup_state(symbol, timeframe, status=status)
        except Exception as exc:  # noqa: BLE001 - idem
            log.warning("no se pudo actualizar el estado de %s %s: %s", symbol, timeframe, exc)

    # -- estado -----------------------------------------------------------------

    def estado(self) -> Dict[str, Any]:
        """Configuración del watcher y estado vigente de cada símbolo que vigila.

        La forma es la que el JS de REF ya sabe pintar (`static/main.js`
        `renderWatcherStatus`): `enabled`, `scan_interval_sec`, `auto_execute`,
        `dedup_ttl_sec`, `symbols` y `states`.

        `auto_execute` sale SIEMPRE a `False` aunque `strategy.yaml` diga `true`: es la
        lectura del interruptor, y el interruptor controla algo que no existe. Publicar
        aquí el valor del YAML haría que el panel dijera "AUTO-EJECUCIÓN" con el código
        que no puede ejecutar. `auto_execute_conf` sí publica el valor del YAML, que es
        un dato sobre la configuración y no sobre lo que va a pasar.
        """
        wcfg = self._cfg_watcher()
        simbolos = wcfg.get("symbols") or []
        estados: List[Optional[Dict[str, Any]]] = []
        for s in simbolos:
            symbol = str((s or {}).get("symbol") or "").upper()
            timeframe = str((s or {}).get("timeframe") or TIMEFRAME_DEFECTO).upper()
            estados.append(self._estado_previo(symbol, timeframe))
        return {
            "enabled": bool(wcfg.get("enabled")),
            "scan_interval_sec": wcfg.get("scan_interval_sec"),
            "dedup_ttl_sec": wcfg.get("dedup_ttl_sec", setup_lifecycle.DEDUP_TTL_DEFECTO),
            "symbols": simbolos,
            "states": estados,
            "min_score": strategy.min_score_for(self._cfg_trading()),
            # Lo que el YAML pide...
            "auto_execute_conf": bool(wcfg.get("auto_execute")),
            # ...y lo que el sistema hace, que no es lo mismo.
            "auto_execute": False,
            "auto_execute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "auto_execute_motivo": None if AUTO_EJECUCION_DISPONIBLE else MOTIVO_SIN_AUTO_EJECUCION,
            "dry_run": True,
        }

    # -- escaneo ----------------------------------------------------------------

    def escanear(self, ahora: Optional[datetime] = None) -> Dict[str, Any]:
        """Un ciclo completo: evalúa, audita y devuelve los eventos.

        Por símbolo, en orden de la configuración:

        1. Snapshot. Si no hay datos, evento `error` con el motivo y se sigue con el
           siguiente símbolo: un símbolo roto no es un escaneo roto.
        2. Gate (`core.setup_gate`). Sin veredicto (`None`) no hay nada que decidir y no
           se escribe nada.
        3. Noticias, solo si el gate aprobó. Un setup que no llega a aprobarse no puede
           estar bloqueado por una noticia: el motivo del rechazo es el score, y
           anotarle el blackout encima sería confuso.
        4. Transición (`core.setup_lifecycle`), y según ella: `new`, `closed` o nada.

        Devuelve `events` para lo que el JS cuenta, `rejects` y `news_ignored` para lo
        que se registró, y `riesgo` con lo que se pudo leer del día. Los descartes y las
        noticias van en su propia lista y no en `events` porque no son "eventos del
        setup": son filas de auditoría, y mezclarlos es lo que hace que un panel diga
        "3 setups" con un setup detectado y dos descartes.

        `avisos` recoge los fail-open: un calendario que no responde tiene que salir en
        la respuesta de este ciclo, no solo dentro de la fila de la base de datos. Un
        fail-open que solo se puede ver en la auditoría es un fail-open que nadie ve.

        `audit_logged` cuenta los setups APROBADOS que dejaron fila y `audit_fallidos`
        las filas perdidas (descartes o noticias que se deduplicaron aunque su fila no
        saliera). Van separados porque el primero responde a "¿se registró lo que
        encontré?" y el segundo a "¿se perdió algo de lo que vi?".
        """
        momento = ahora or datetime.now().astimezone()
        wcfg = self._cfg_watcher()
        tcfg = self._cfg_trading()
        ttl = float(wcfg.get("dedup_ttl_sec") or setup_lifecycle.DEDUP_TTL_DEFECTO)
        riesgo = self._riesgo_del_dia()
        riesgo_error = riesgo.get("error")
        riesgo_log = {} if riesgo_error else riesgo

        eventos: List[Dict[str, Any]] = []
        rechazos: List[Dict[str, Any]] = []
        noticias: List[Dict[str, Any]] = []
        fallos: List[Dict[str, Any]] = []
        avisos: List[Dict[str, Any]] = []
        auditados = 0
        #: Filas que NO se pudieron escribir, en un ciclo que sí decidió algo. Van
        #: aparte de `audit_logged` porque aquel cuenta setups aprobados y estos son
        #: descartes y noticias: sumarlos todos daría un número que no corresponde a
        #: ninguna lista. Y van declarados porque el estado y la dedup ya se guardaron:
        #: lo que no se escribió no se reintentará, y sin este contador el histórico
        #: tendría huecos que nadie podría distinguir de "no se miró".
        huecos = 0

        for entrada_cfg in wcfg.get("symbols") or []:
            symbol = str((entrada_cfg or {}).get("symbol") or "").strip().upper()
            timeframe = str((entrada_cfg or {}).get("timeframe") or TIMEFRAME_DEFECTO).upper()
            if not symbol:
                continue
            base = {"symbol": symbol, "timeframe": timeframe}

            snap, error = self._snapshot(symbol, timeframe)
            if snap is None:
                eventos.append({**base, "event": "error", "error": error})
                fallos.append({**base, "error": error})
                continue

            gate = setup_gate.evaluate_gate(snap, tcfg, spec=self._spec(symbol))
            if gate is None:
                eventos.append({
                    **base, "event": "error",
                    "error": "el snapshot no trae scores: no hay veredicto que evaluar",
                })
                fallos.append({**base, "error": "sin scores"})
                continue

            componentes = setup_lifecycle.componentes_del_score(snap, gate["direction"])
            fila_base = {
                **base,
                "direction": gate["direction"],
                "score": gate["score"],
                "verdict": gate["verdict"],
                "killzone": gate["killzone_name"],
                "entry": gate["entry"],
                "sl": gate["sl"],
                "tp": gate["tp"],
                "invalidate_level": gate["invalidate_level"],
            }
            previo = self._estado_previo(symbol, timeframe)

            # Noticias: solo si el setup ya es operable. Un rechazo por score anotado
            # como bloqueo por noticia se leería como "el mercado estaba cerrado por
            # datos", que no es lo que pasó.
            bloqueo, veredicto_noticia, aviso = (False, {}, None)
            if gate["approved"]:
                bloqueo, veredicto_noticia, aviso = self._gate_noticias()
                if aviso:
                    avisos.append({**base, "aviso": aviso})

            if gate["approved"] and bloqueo:
                registrado = self._noticia_una_vez(
                    symbol, timeframe, gate, previo, veredicto_noticia, riesgo_log,
                    componentes, riesgo_error,
                )
                if registrado is not None:
                    noticias.append(registrado)
                    if not registrado["auditado"]:
                        huecos += 1
                    eventos.append({
                        **fila_base, "event": "ignored_news",
                        "reason": str(veredicto_noticia.get("detail") or veredicto_noticia.get("reason") or ""),
                        "news": veredicto_noticia.get("event"),
                    })
                continue

            transicion = setup_lifecycle.decide_transition(
                previo, bool(gate["approved"]), ahora=momento, ttl_sec=ttl)

            if transicion == "new":
                self._nuevo_estado(symbol, timeframe, gate,
                                    status=setup_lifecycle.ESTADO_ACTIVO, notificado=True)
                if self._audita_aprobado(
                        symbol, timeframe, gate, componentes, riesgo_log,
                        riesgo_error) is not None:
                    auditados += 1
                eventos.append({
                    **fila_base, "event": "new",
                    "reason": "; ".join(gate["reasons"]),
                    "notificado": True,
                    "auto_ejecutado": False,
                })
            elif transicion == "closed":
                self._cierra_estado(symbol, timeframe, "closed")
                eventos.append({
                    **fila_base, "event": "closed",
                    "reason": "el setup ya no cumple el gate o expiró su TTL ({0:.0f} s)".format(ttl),
                })
            elif not gate["approved"] and transicion is None:
                descartado = self._audita_rechazo(
                    symbol, timeframe, gate, componentes, riesgo_log, riesgo_error)
                if descartado is not None:
                    rechazos.append(descartado)
                    if not descartado["auditado"]:
                        huecos += 1
                    eventos.append({
                        **fila_base, "event": "rejected",
                        "reasons": list(gate["reasons"]),
                    })

        return {
            "events": eventos,
            "rejects": rechazos,
            "news_ignored": noticias,
            "avisos": avisos,
            "riesgo": riesgo,
            "riesgo_error": riesgo_error,
            "audit_logged": auditados,
            "audit_fallidos": huecos,
            "auto_ejecute": False,
            "auto_ejecute_disponible": AUTO_EJECUCION_DISPONIBLE,
            "dry_run": True,
            "symbols": len(wcfg.get("symbols") or []),
            # Un escaneo sin símbolos configurados devuelve cero eventos y parecería un
            # mercado en calma. El motivo va explícito para que "no había nada que
            # mirar" no se lea como "no había nada".
            "motivo": (None if (wcfg.get("symbols") or []) else
                       "watcher.symbols está vacío: no hay nada que escanear."),
            "ttl_sec": ttl,
            "scan_at": momento.isoformat(),
        }

    # -- escritura de filas -----------------------------------------------------

    @staticmethod
    def _breakdown(gate: Dict[str, Any], componentes: Dict[str, Any],
                   riesgo_error: Optional[str], **extra: Any) -> Dict[str, Any]:
        """El `breakdown_json` de la fila: el gate, los componentes y lo que explica
        el resto.

        `risk_state_error` va dentro cuando el estado de riesgo del día no se pudo leer.
        Sin él, la fila tiene `risk_state: {}` y no se distingue de una escrita con el
        riesgo a cero; con él, la revisión de un mes sabe que esa fila se escribió sin
        poder comprobar los topes del día en vez de con los topes cumplidos.
        """
        salida: Dict[str, Any] = {"gate": gate, **componentes, **extra}
        if riesgo_error:
            salida["risk_state_error"] = riesgo_error
        return salida

    def _audita_aprobado(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                         componentes: Dict[str, Any], riesgo: Dict[str, Any],
                         riesgo_error: Optional[str]) -> Optional[int]:
        """Fila de un setup APROBADO, sin ejecutar.

        `trade_result` lleva `{"executed": False, "dry_run": True}`. No es un detalle de
        forma: `store.link_last_setup` busca las filas con `trade_result_json` vacía, y
        una fila de setup detectado que apareciera más tarde ahí se leería como una
        operación abierta de la que todavía no se sabe el resultado. Marcarla aquí es lo
        que hace que la fila sea una **intención auditada**, que es lo que fue.
        """
        return self._registra({
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "verdict": gate["verdict"],
            "score": gate["score"],
            "breakdown": self._breakdown(gate, componentes, riesgo_error),
            "entry": gate["entry"],
            "sl": gate["sl"],
            "target": gate["tp"],
            "invalidate_level": gate.get("invalidate_level"),
            "validated": True,
            "reject_reasons": [],
            "risk_state": riesgo,
            "trade_result": {"executed": False, "dry_run": True},
            "context": {},
            "source": "watcher",
        })

    def _audita_rechazo(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                        componentes: Dict[str, Any], riesgo: Dict[str, Any],
                        riesgo_error: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fila de un setup RECHAZADO, una sola vez por firma.

        Deduplica por `{símbolo, timeframe}` y firma (`core.setup_lifecycle`), en
        memoria de este proceso. Sin esto, un setup que lleva media hora por debajo del
        umbral escribe una fila cada `scan_interval_sec`, y el histórico deja de
        servir para nada.

        Devuelve el evento si se escribió, `None` si ya estaba registrado. Un `None` no
        es un fallo: es un rechazo que ya se sabe.
        """
        clave = (symbol.upper(), timeframe.upper())
        firma = setup_lifecycle.firma_de_rechazo(gate)
        if self._rejects.get(clave) == firma:
            return None
        self._rejects[clave] = firma

        veredicto = gate.get("verdict") or "RECHAZADO"
        id_fila = self._registra({
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "verdict": veredicto,
            "score": gate["score"],
            "breakdown": self._breakdown(gate, componentes, riesgo_error),
            "entry": gate.get("entry"),
            "sl": gate.get("sl"),
            "target": gate.get("tp"),
            "invalidate_level": gate.get("invalidate_level"),
            "validated": False,
            "reject_reasons": list(gate.get("reasons") or []),
            "risk_state": riesgo,
            "trade_result": {},
            "context": {},
            "source": "watcher",
        })
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "score": gate["score"],
            "verdict": veredicto,
            "reasons": list(gate.get("reasons") or []),
            # `auditado=False` con la firma ya guardada: la fila se perdió y este
            # descarte no se reintentará. Se declara para que el ciclo lo diga, y no
            # para que se note al mes, cuando ya no queda memoria del ciclo.
            "auditado": id_fila is not None,
        }

    def _noticia_una_vez(self, symbol: str, timeframe: str, gate: Dict[str, Any],
                         previo: Optional[Dict[str, Any]], veredicto: Dict[str, Any],
                         riesgo: Dict[str, Any], componentes: Dict[str, Any],
                         riesgo_error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Un setup aprobado que cae en la ventana de noticias, registrado UNA vez.

        La deduplicación es la del propio estado: si el ciclo ya está en
        `news_ignored`, el blackout ya se registró y reescribir la fila sería contar el
        mismo evento cada 60 segundos. El estado se queda en `news_ignored` hasta que
        la señal muera (lo decide `core.setup_lifecycle`) —y mientras siga viva no
        revive, porque un evento de noticias que reaparece cada ciclo es un evento que
        ya se auditó.

        Devuelve el evento si se registró, `None` si ya estaba.
        """
        estado_previo = str((previo or {}).get("status") or "").lower()
        if estado_previo == setup_lifecycle.ESTADO_NOTICIA:
            return None
        self._nuevo_estado(symbol, timeframe, gate,
                            status=setup_lifecycle.ESTADO_NOTICIA, notificado=False)
        id_fila = self._registra({
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "verdict": VERDICTO_NOTICIA,
            "score": gate["score"],
            "breakdown": self._breakdown(gate, componentes, riesgo_error, news=veredicto),
            "entry": gate.get("entry"),
            "sl": gate.get("sl"),
            "target": gate.get("tp"),
            "invalidate_level": gate.get("invalidate_level"),
            "validated": False,
            "reject_reasons": ["BLOCKED_BY_NEWS_GATE: {0}".format(
                veredicto.get("detail") or veredicto.get("reason") or "sin detalle")],
            "risk_state": riesgo,
            "trade_result": {},
            "context": {},
            "source": "watcher",
        })
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": gate["direction"],
            "score": gate["score"],
            "reason": str(veredicto.get("detail") or veredicto.get("reason") or ""),
            "event": veredicto.get("event"),
            # El estado ya quedó en `news_ignored`, así que la fila no se reintentará
            # aunque se haya perdido. Ídem que en los descartes: se declara aquí.
            "auditado": id_fila is not None,
        }


__all__ = [
    "AUTO_EJECUCION_DISPONIBLE",
    "MOTIVO_SIN_AUTO_EJECUCION",
    "TIMEFRAME_DEFECTO",
    "VERDICTO_NOTICIA",
    "WatcherService",
]
