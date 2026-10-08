"""Ejecución: decide si esta orden puede salir, y si puede, la manda.

Este módulo es la CONSECUENCIA de mandar dinero, así que es el sitio donde se
exige que cada puerta se pueda leer. La regla del fichero es una sola:

    **Una orden no se manda sin que las cuatro cosas que la justifican estén
    comprobadas en el MISMO instante: que el símbolo está permitido, que el riesgo
    del día lo admite, que no hay noticia encima, y que la orden que se va a
    enviar es la que el gate aprobó.**

Lo último es el punto donde fallan los sistemas. Es habitual calcular bien el
lote, pasar el riesgo, y enviar con un precio o un stop que ya no son los
validados; el resultado es una orden viva con una tesis que nadie ha mirado. Por
eso aquí los niveles se planifican DESPUÉS de leer la cotización, se validan, y se
envían tal cual: el `price` que sale de `order_send` es el que se ha medido.

**El orden de las puertas no es arbitrario** y cambiarlo cambia la seguridad:

1. Símbolo y acción: una petición mal formada no debe tocar el bróker.
2. `symbols_allow`: la lista blanca del usuario, antes de leer su cuenta.
3. Riesgo del día: los topes son del DÍA, y este es el primer momento en que se
   sabe que son los de hoy.
4. Noticias: un evento de alto impacto invalida el análisis entero, así que va
   después del riesgo pero antes de calcular nada de la orden.
5. Niveles, lote, `validate_entry` y `execution_quality`: la geometría de la orden.
6. `log_setup` y SOLO ENTONCES `order_send`.

**Por qué `log_setup` va antes del envío.** Una orden que sale y no se puede
explicar es indistinguible de una orden que nunca se intentó. Si el proceso
muere entre el envío y el registro, la fila sigue ahí con `validated=1` y el
resultado vacío, que es el estado que dice "se intentó y no consta". Al revés
—registrar después— deja operaciones sin fila, y una operación sin fila no
sabe ni si ocurrió.

**Por qué las noticias fallan ABIERTO.** Un calendario que no contesta y una
ventana con una noticia de alto impacto son cosas distintas, y bloquear por la
primera deja el sistema parado por un fallo de feed. Se opera, pero queda escrito
que se operó sin poder comprobarlo (`verdict=IGNORED_NEWS`): la ausencia de
veredicto es un dato que hay que poder ver, no un aprobado silencioso.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from core import execution_quality, lot_calculator, risk_engine, strategy
from core.clock import now_iso

#: Timeframe con el que se registra el setup cuando quien llama no lo dice.
DEFAULT_TIMEFRAME = "M15"

#: Motivo con el que se registra una orden bloqueada por el calendario sin veredicto.
VERDICT_IGNORED_NEWS = "IGNORED_NEWS"


class ExecutionService:
    """Puerta única de salida de órdenes. Requiere token en la capa HTTP.

    No es idempotente ni pretende serlo: mandar la misma orden dos veces abre dos
    posiciones. La protección es de quien llama (el token, y `auto_execute` apagado
    en el watcher), no una deduplicación inventada aquí que daría una falsa
    sensación de seguridad.
    """

    def __init__(self, market: Any, store: Any = None, news: Any = None,
                 execution: Any = None, reloj: Any = None) -> None:
        self._market = market
        self._store = store
        self._news = news
        self._execution = execution
        self._reloj = reloj

    # -- configuración ----------------------------------------------------------

    def _cfg(self) -> Dict[str, Any]:
        """Configuración de trading, plana o anidada. Nunca lanza.

        Sin store no hay configuración y por tanto no hay lista blanca: se opera
        con `symbols_allow` vacío, que en `_permite` significa "todo permitido".
        Es lo contrario de lo prudente, y por eso `_permite` avisa de esa
        situación en vez de dejarla pasar en silencio: sin configuración, la
        ejecución no debería ni estar montada.
        """
        if self._store is None:
            return {}
        try:
            return self._store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - sin config no hay topes que aplicar
            return {}

    @staticmethod
    def _permitidos(cfg: Dict[str, Any]) -> List[str]:
        """Símbolos permitidos, en mayúsculas, de la lista del usuario."""
        raw = strategy._primer_valor(cfg, ("symbols_allow",), ("risk", "symbols_allow"))
        if raw is None:
            return []
        if isinstance(raw, str):
            return [p.strip().upper() for p in raw.replace(",", " ").split() if p.strip()]
        if isinstance(raw, (list, tuple, set)):
            return [str(s).strip().upper() for s in raw if str(s).strip()]
        return []

    @staticmethod
    def _permite(cfg: Dict[str, Any], simbolo: str) -> Tuple[bool, Optional[str]]:
        """`(permitido, motivo)`.

        Una lista blanca VACÍA no bloquea: en un despliegue que no la ha
        configurado, bloquear todo dejaría el sistema sin poder hacer nada y
        nadie podría ni probarlo. El coste de esa elección —que una lista vacía
        no protege de nada— se declara en `motivo`, y la ruta lo publica.
        """
        permitidos = ExecutionService._permitidos(cfg)
        if not permitidos:
            return True, ("symbols_allow está vacío: se permiten todos los "
                          "símbolos. Configura la lista blanca para poder "
                          "operar de verdad.")
        if simbolo in permitidos:
            return True, None
        return False, ("{0} no está en symbols_allow ({1}).".format(
            simbolo, ", ".join(permitidos)))

    # -- auditoría --------------------------------------------------------------

    def _registra(self, entry: Dict[str, Any]) -> Optional[int]:
        """`store.log_setup` sin dejar que un fallo de auditoría tumbe la orden.

        Devolver `None` es un resultado honesto: la operación puede continuar
        (la auditoría no es un requisito de ejecución) pero la respuesta lo dice
        en `audit_logged=False`, porque una fila que no se escribió es
        información que el operador necesita aunque no le bloquee.
        """
        if self._store is None:
            return None
        try:
            return self._store.log_setup(entry)
        except Exception:  # noqa: BLE001 - la auditoría nunca tumba la ejecución
            return None

    def _graba_intentos(self, setup_id: Optional[int], resultado: Dict[str, Any]) -> None:
        """Los intentos de llenado junto a la decisión que los provocó.

        Importa el orden de los `type_filling`: si se probó FOK, falló, y se
        acabó mandando con IOC, la fila tiene que contar eso. Con un solo
        `retcode` final, el 10030 del FOK desaparecería y el rechazo parecería
        de un modo de llenado cualquiera.
        """
        if not setup_id or self._store is None:
            return
        intentos = resultado.get("attempts") or []
        if not intentos:
            return
        try:
            self._store.update_trade_result(setup_id, {
                "attempts": intentos,
                "filling": resultado.get("filling"),
                "retcode": resultado.get("retcode"),
                "deal": resultado.get("deal"),
                "order": resultado.get("order"),
                "fill_price": resultado.get("fill_price"),
                "status": resultado.get("status"),
                "error": resultado.get("error"),
                "ok": bool(resultado.get("ok")),
                "logged_at": now_iso(),
            })
        except Exception:  # noqa: BLE001 - perder el log no cambia lo que pasó
            return

    # -- riesgo -----------------------------------------------------------------

    def _riesgo_del_dia(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """`(estado, error)`. Un error NO abre la puerta.

        Si no se puede leer el estado de riesgo no se sabe si los topes están
        sobre ellos, y operar "por si acaso" al no poder comprobar es justamente el
        comportamiento que hace que un fallo de lectura se convierta en una
        posición sin control.
        """
        try:
            estado = self._market.daily_risk_state() or {}
        except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
            return {}, "No se pudo leer el estado de riesgo del día: {0}".format(exc)
        if estado.get("error"):
            return estado, "No se pudo leer el estado de riesgo del día: {0}".format(
                estado.get("error"))
        return estado, None

    def _gate_noticias(self) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """`(bloquea, veredicto, aviso_de_fail_open)`.

        Bloquea con `block` a `True` aunque el veredicto venga `fail_open`: si el
        calendario ha dicho que hay noticia de alto impacto, manda su respuesta. El
        fail-open de esta función es el otro caso —que no haya respuesta— y en ese
        caso se opera avisando, que es la decisión que se declara en el módulo.
        """
        if self._news is None:
            return False, {}, None
        try:
            veredicto = self._news.gate() or {}
        except Exception as exc:  # noqa: BLE001 - un feed caído no bloquea
            return False, {"error": str(exc)}, "No se pudo consultar el calendario: {0}".format(exc)
        if veredicto.get("block"):
            return True, veredicto, None
        if veredicto.get("fail_open"):
            return False, veredicto, (
                "El calendario no devolvió veredicto y la puerta de noticias se ha "
                "abierto: se opera sin poder comprobar si hay evento de alto impacto.")
        return False, veredicto, None

    # -- geometría y lote -------------------------------------------------------

    @staticmethod
    def _precio_de_entrada(action: str, precio: Dict[str, Any]) -> Optional[float]:
        """El precio al que se entra: el que se PAGA.

        Una BUY se paga al ask y una SELL al bid. Entrar al bid en una compra es
        un precio que el bróker nunca ha visto, y la diferencia es exactamente el
        spread del que el gate no se enteraría.
        """
        ask = precio.get("ask")
        bid = precio.get("bid")
        bruto = ask if action == "BUY" else bid
        try:
            v = float(bruto)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    @staticmethod
    def _lot_spec(spec: Any) -> lot_calculator.LotSpec:
        """El `LotSpec` del símbolo, leído del spec del bróker.

        Vive en una función propia porque el dimensionado y la comprobación del
        presupuesto tienen que leer EXACTAMENTE los mismos campos. Si cada uno
        armara el suyo, un campo olvidado en uno de los dos cambiaría el lote o
        el riesgo sin que nada fallara: las dos cifras seguirían siendo
        redondeadas y con aspecto de razonables.
        """
        return lot_calculator.LotSpec(
            lot_size=float(getattr(spec, "contract_size", 0.0) or 0.0),
            tick_value=float(getattr(spec, "tick_value", 0.0) or 0.0),
            tick_size=float(getattr(spec, "tick_size", 0.0) or 0.0)
            or float(getattr(spec, "point", 0.0) or 0.0),
            min_lot=float(getattr(spec, "volume_min", 0.01) or 0.01),
            lot_step=float(getattr(spec, "volume_step", 0.01) or 0.01),
        )

    def _lote_por_riesgo(self, balance: Optional[float], risk_pct: float,
                         sl_distance: float, spec: Any,
                         simbolo: str) -> Tuple[Optional[float], Optional[str]]:
        """`(lote, error)` cuando quien llama no pasa volumen.

        El riesgo se traduce con `core.lot_calculator`, que es la MISMA cuenta que
        usa la pantalla de lote: si se calculara aquí a mano, la operación real
        podría tener un riesgo distinto del que el operador vio en la pantalla.
        """
        if not balance or balance <= 0:
            return None, "No se pudo leer el balance para dimensionar el lote."
        lot_spec = self._lot_spec(spec)
        riesgo = float(balance) * float(risk_pct) / 100.0
        try:
            salida = lot_calculator.standard_forex_lots(riesgo, sl_distance, lot_spec)
        except ValueError as exc:
            return None, ("No se pudo dimensionar el lote para {0} con el SL de {1}: "
                          "{2}".format(simbolo, sl_distance, exc))
        lote = float(salida.get("lots") or 0.0)
        if lote <= 0:
            return None, ("El lote calculado para {0} es 0: con un SL de {1} y un "
                          "riesgo del {2}% no alcanza ni el mínimo del bróker "
                          "({3}).".format(simbolo, sl_distance, risk_pct,
                                          salida.get("clamped_to_min")))
        return lote, None

    # -- ejecución --------------------------------------------------------------

    def execute_market_trade(self, symbol: str, action: str = "BUY",
                             volume: Optional[float] = None,
                             sl_distance: Optional[float] = None,
                             tp_distance: Optional[float] = None,
                             no_tp: bool = False,
                             magic: Optional[int] = None, comment: Optional[str] = None,
                             deviation: Optional[int] = None,
                             score: Optional[float] = None,
                             verdict: Optional[str] = None,
                             invalidate_level: Optional[float] = None,
                             planned_entry: Optional[float] = None,
                             components: Optional[Dict[str, Any]] = None,
                             context: Optional[Dict[str, Any]] = None,
                             timeframe: str = DEFAULT_TIMEFRAME,
                             source: str = "") -> Tuple[Dict[str, Any], int]:
        """`(cuerpo, status)`. El status lo elige el servicio, no la ruta.

        Que sea aquí y no en el handler es lo que hace que el watcher y la API
        tengan EXACTAMENTE las mismas puertas. Si cada ruta.Validara por su cuenta,
        el auto-arranque acabaría siendo el camino con menos filtros, que es como
        se pierde dinero sin que nadie lo decida.

        `no_tp=True` manda la orden SIN objetivo (`tp=None`): es la entrada del
        CTA Swing D1 (F5), cuya salida la decide el trailing chandelier
        (`core/exit_policy.py`) y no un target congelado que la convalidación de
        F2 nunca evaluó. Con `no_tp` se salta solo el cálculo del TP: el R:R de
        `validate_entry` ni se plantea sin target, y el resto de puertas
        (lista blanca, riesgo del día, noticias, calidad) siguen iguales.
        """
        simbolo = str(symbol or "").strip().upper()
        acc = str(action or "").strip().upper()

        # -- 1. Petición bien formada ------------------------------------------
        if not simbolo:
            return {"error": "Falta el símbolo."}, 400
        if acc not in ("BUY", "SELL"):
            return {"error": "Acción inválida: solo BUY o SELL (recibido '{0}').".format(action)}, 400

        cfg = self._cfg()
        avisos: List[str] = []

        # -- 2. Lista blanca ----------------------------------------------------
        permitido, nota = self._permite(cfg, simbolo)
        if nota:
            avisos.append(nota)
        if not permitido:
            return {"error": nota, "symbol": simbolo, "status": "SYMBOL_NOT_ALLOWED"}, 403

        # -- 3. Riesgo del día -------------------------------------------------
        riesgo, error_riesgo = self._riesgo_del_dia()
        if error_riesgo:
            return {"error": error_riesgo, "symbol": simbolo, "status": "RISK_STATE_UNKNOWN"}, 503
        if riesgo.get("blocked"):
            return {"error": "Operación bloqueada por los topes del día: "
                             + "; ".join(riesgo.get("reasons") or ["riesgo bloqueado"]),
                    "symbol": simbolo, "status": "BLOCKED_BY_RISK",
                    "risk_state": riesgo}, 403

        # -- 4. Noticias --------------------------------------------------------
        bloqueado, veredicto_noticias, fail_open = self._gate_noticias()
        if bloqueado:
            return {"error": "Operar bloqueado por calendario económico: "
                             + str(veredicto_noticias.get("reason") or veredicto_noticias.get("detail")
                                   or "evento de alto impacto"),
                    "symbol": simbolo, "status": "BLOCKED_BY_NEWS_GATE",
                    "news": veredicto_noticias}, 403
        if fail_open:
            avisos.append(fail_open)

        # -- 5a. Spec y cotización ---------------------------------------------
        try:
            spec = self._market.spec(simbolo)
        except Exception as exc:  # noqa: BLE001 - spec ilegible = no se opera
            return {"error": "No se pudo leer la especificación de {0}: {1}".format(simbolo, exc),
                    "symbol": simbolo, "status": "SPEC_UNAVAILABLE"}, 503
        if spec is None:
            return {"error": "El bróker no publica el símbolo {0}.".format(simbolo),
                    "symbol": simbolo, "status": "SYMBOL_NOT_FOUND"}, 404

        try:
            precio = self._market.price(simbolo) or {}
        except Exception as exc:  # noqa: BLE001 - sin precio no hay orden a mercado
            return {"error": "No se pudo cotizar {0}: {1}".format(simbolo, exc),
                    "symbol": simbolo, "status": "PRICE_UNAVAILABLE"}, 503
        entrada = self._precio_de_entrada(acc, precio)
        if entrada is None:
            return {"error": "Cotización incompleta para {0}: no se manda una orden "
                             "a mercado sin precio.".format(simbolo),
                    "symbol": simbolo, "status": "PRICE_UNAVAILABLE"}, 503

        # -- 5b. Distancias ----------------------------------------------------
        if sl_distance is None:
            sl_distance, origen_sl = strategy.sl_distance_for(cfg, simbolo, spec)
            if sl_distance is None:
                clave = "sl_distance_by_symbol.{0}".format(simbolo) if simbolo else "sl_distance"
                return {"error": "No hay distancia de SL configurada para '{0}' "
                                 "(strategy.yaml: sl_distance o "
                                 "sl_distance_by_symbol): no se inventa un stop.".format(clave),
                        "symbol": simbolo, "status": "NO_SL_DISTANCE"}, 400
        else:
            origen_sl = "request"
        sl_distance = float(sl_distance)

        # Niveles a partir del precio REAL de entrada, no de un precio guardado.
        signo = 1.0 if acc == "BUY" else -1.0
        sl = entrada - signo * sl_distance
        if no_tp:
            if tp_distance is not None:
                avisos.append("no_tp=True: se ignora tp_distance y la orden sale "
                              "sin objetivo (la salida la decide la política de "
                              "salida de la estrategia).")
            tp_distance = None
            tp = None
        else:
            if tp_distance is None:
                tp_distance = sl_distance * float(cfg.get("tp_ratio_r", 2.0))
            tp_distance = float(tp_distance)
            tp = entrada + signo * tp_distance

        # -- 5c. Lote -----------------------------------------------------------
        eff = risk_engine.effective_risk_pct(score, verdict, cfg)
        risk_pct = float(eff.get("risk_pct") or 0.0)
        balance = None
        try:
            cuenta = self._market.account_info() or {}
            balance = cuenta.get("balance")
        except Exception:  # noqa: BLE001 - sin balance solo importa si hay que dimensionar
            balance = None

        lote = volume
        lote_nota: Optional[str] = None
        if lote is None or float(lote) <= 0:
            lote, error_lote = self._lote_por_riesgo(balance, risk_pct, sl_distance,
                                                      spec, simbolo)
            if error_lote:
                return {"error": error_lote, "symbol": simbolo,
                        "status": "LOT_UNAVAILABLE"}, 400
            # La misma cuenta que `_lote_por_riesgo`, escrita aquí para que la nota
            # diga de dónde sale el lote. Sin repetirla, el "= {2}" tendría que
            # inventar una cifra que nadie calculó, y una nota de dimensionado que
            # no cuadra es peor que no tener nota: el operador la leería como
            # comprobación y no lo es.
            riesgo_cash = float(balance) * risk_pct / 100.0
            lote_nota = ("Lote dimensionado por riesgo ({0}% de {1} = {2:.2f} con un "
                         "SL de {3:.5f}): {4} lotes.".format(
                             risk_pct, balance, riesgo_cash, sl_distance, lote))
        lote = float(lote)

        # -- 5d. Riesgo por lote, preguntado al bróker -------------------------
        #
        # `fuente_riesgo` se lleva EXPLÍCITO en vez de deducirse de si la cifra
        # existe. Deducirlo —"si hay número, es del bróker"— es como este bloque
        # llegó a etiquetar de `broker` una estimación hecha con el tick value: las
        # dos tienen número, así que el número no dice de dónde salió. Un campo que
        # miente sobre la procedencia del riesgo es peor que un campo ausente,
        # porque alguien lo leerá como una medición del bróker.
        perdida_por_lote = None
        fuente_riesgo: Optional[str] = None
        if self._execution is not None:
            try:
                perdida_por_lote = self._execution.profit_per_lot(simbolo, lote, entrada, sl)
                if perdida_por_lote is not None:
                    fuente_riesgo = "broker"
            except Exception:  # noqa: BLE001 - sin esta cifra se sigue, y se dice
                perdida_por_lote = None
                fuente_riesgo = None
        if perdida_por_lote is None and getattr(spec, "tick_value", 0.0):
            # Reserva: la MISMA fórmula con la que se dimensionó el lote.
            #
            # Si el bróker no contesta, la cifra sale del spec con la cuenta de
            # `lot_calculator.risk_per_unit`, que es la que usó `_lote_por_riesgo`
            # para decidir el tamaño. Que sea la misma no es un detalle: el lote
            # se dimensiona con una cuenta y se contrasta contra el presupuesto con
            # otra, y si difieren en un factor el fallo no da error, da un rechazo
            # con el motivo equivocado —o, peor, una aprobación con un riesgo que
            # nadie midió.
            #
            # El `tick_value` de MT5 ya viene por UN lote, así que aquí NO se
            # multiplica por `contract_size`: hacerlo inflaba el riesgo del lote en
            # 100.000x y rechazaba toda operación como "riesgo excede el
            # presupuesto" con cifras de millones.
            try:
                perdida_por_lote = lot_calculator.risk_per_unit(
                    abs(entrada - sl), self._lot_spec(spec))
            except ValueError as exc:
                # Un SL más fino que un tick no tiene riesgo calculable: no se
                # estima un 0, que es el número que pasa todos los budgets.
                perdida_por_lote = None
                avisos.append("No se pudo calcular la pérdida por lote con el spec "
                              "del bróker ({0}); el presupuesto de riesgo NO se ha "
                              "podido comprobar.".format(exc))
            if perdida_por_lote is not None:
                fuente_riesgo = "estimacion"
                avisos.append("Pérdida por lote ESTIMADA con el tick value del "
                              "spec (order_calc_profit no devolvió valor): "
                              "{0:.2f} por lote.".format(perdida_por_lote))

        # -- 5e. validate_entry: R:R, estructura, TTL y presupuesto ------------
        val = risk_engine.validate_entry(
            entry=entrada, sl=sl, target=tp, direction=acc,
            current_price=entrada, invalidate_level=invalidate_level,
            cfg=cfg, balance=balance, risk_pct=risk_pct,
            loss_per_lot=perdida_por_lote, lot=lote,
        )
        cal = execution_quality.evaluate(
            entry=entrada, sl=sl, tp=tp, direction=acc,
            point=float(getattr(spec, "point", 0.0) or 0.0),
            planned_entry=planned_entry, sl_distance=sl_distance,
            invalidate_level=invalidate_level,
            bid=precio.get("bid"), ask=precio.get("ask"), cfg=cfg,
        )

        # `approved` NO es permiso de ejecución: es el veredicto de un gate.
        # La orden sale solo si los DOS lo dicen, y el motivo de cada uno se
        # enseña por separado para que se sepa cuál falló.
        aprobado = bool(val.get("approved")) and bool(cal.get("approved"))
        motivos = list(val.get("reasons") or []) + list(cal.get("reasons") or [])

        fila: Dict[str, Any] = {
            "symbol": simbolo,
            "timeframe": str(timeframe or DEFAULT_TIMEFRAME).upper(),
            "direction": acc,
            "verdict": str(verdict or "") if not fail_open else VERDICT_IGNORED_NEWS,
            "score": float(score or 0.0),
            "breakdown": dict(components or {}),
            "entry": entrada,
            "sl": sl,
            "target": tp,
            "invalidate_level": invalidate_level,
            "validated": 1 if aprobado else 0,
            "reject_reasons": motivos,
            "risk_state": riesgo,
            "trade_result": {},
            "context": dict(context or {}),
            "source": str(source or ""),
        }

        if not aprobado:
            setup_id = self._registra(fila)
            return {"error": "La orden no pasa el gate de entrada: "
                             + "; ".join(motivos or ["rechazada sin motivo declarado"]),
                    "symbol": simbolo, "status": "GATE_REJECTED",
                    "validated": False,
                    "reasons": motivos,
                    "risk_validation": val,
                    "execution_quality": cal,
                    "setup_id": setup_id,
                    "audit_logged": setup_id is not None}, 400

        # -- 6. Auditoría ANTES del envío -------------------------------------
        fila["trade_result"] = {"planned_entry": entrada, "planned_sl": sl,
                                "planned_tp": tp, "volume": lote}
        setup_id = self._registra(fila)

        # -- 7. Envío -----------------------------------------------------------
        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "symbol": simbolo, "status": "NO_EXECUTION_PORT"}, 503

        magic_efectivo = int(magic if magic is not None else cfg.get("magic", 0) or 0)
        comentario = str(comment if comment is not None
                         else (cfg.get("comment") or "")) or "web"
        resultado = self._execution.send_market_order(
            simbolo, acc, lote, sl=sl, tp=tp, magic=magic_efectivo,
            comment=comentario,
            deviation=int(deviation if deviation is not None
                          else (cfg.get("deviation_points") or 20)),
        )
        self._graba_intentos(setup_id, resultado)

        cuerpo: Dict[str, Any] = {
            "ok": bool(resultado.get("ok")),
            "symbol": simbolo,
            "action": acc,
            "volume": resultado.get("volume", lote),
            "requested_volume": lote,
            "entry": entrada,
            "sl": sl,
            "tp": tp,
            "fill_price": resultado.get("fill_price"),
            "deal": resultado.get("deal"),
            "order": resultado.get("order"),
            "retcode": resultado.get("retcode"),
            "filling": resultado.get("filling"),
            "status": resultado.get("status"),
            "setup_id": setup_id,
            "audit_logged": setup_id is not None,
            "risk": {"risk_pct": risk_pct, "reason": eff.get("reason"),
                     "sl_distance": sl_distance, "sl_distance_source": origen_sl,
                     "tp_distance": tp_distance,
                     "loss_per_lot": perdida_por_lote,
                     "loss_per_lot_source": fuente_riesgo},
            # En el rechazo se necesita saber cuál de los dos falló; en el éxito
            # sirve para comprobar que la orden que salió pasó la puerta que se
            # cree que pasó, y para leer los `unknown` sin ir a la base de datos.
            "risk_validation": val,
            "execution_quality": {k: cal.get(k) for k in
                                  ("verdict", "approved", "reasons", "unknown",
                                   "spread_points", "spread_share_of_sl",
                                   "slippage_share_of_sl",
                                   "invalidation_share_of_sl", "rr_actual", "rr_planned")},
            "attempts": resultado.get("attempts") or [],
            "news_gate": {"fail_open": bool(fail_open), "aviso": fail_open} if fail_open else None,
        }
        if avisos:
            cuerpo["warnings"] = avisos
        if lote_nota:
            cuerpo.setdefault("warnings", []).append(lote_nota)
        if resultado.get("volume_note"):
            cuerpo.setdefault("warnings", []).append(resultado["volume_note"])
        if not resultado.get("ok"):
            cuerpo["error"] = resultado.get("error")

        status = 200 if resultado.get("ok") else _status_de_ejecucion(resultado.get("status"))
        return cuerpo, status

    # -- cierre -----------------------------------------------------------------

    def close_position(self, ticket: Optional[int] = None,
                       symbol: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
        """Cierra la posición por ticket (o por símbolo si no hay ticket).

        El cierre NO pasa por `symbols_allow`: una lista blanca de símbolos para
        ABRIR no puede impedir cerrar lo que ya está abierto. Bloquear un cierre
        por un símbolo que ya no está en la lista dejaría la posición viva sin
        ninguna manera de salir de ella, y el único remedio sería editar la config
        con la posición abierta.
        """
        simbolo = str(symbol or "").strip().upper()
        if not ticket and not simbolo:
            return {"error": "Falta el ticket de la posición a cerrar."}, 400

        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "status": "NO_EXECUTION_PORT"}, 503

        cfg = self._cfg()
        resultado = self._execution.close_position(
            ticket=int(ticket) if ticket else None,
            symbol=simbolo or None,
            magic=int(cfg.get("magic", 0) or 0),
            deviation=int(cfg.get("deviation_points") or 20),
        )
        if resultado.get("ok"):
            return {"ok": True, **resultado}, 200

        estado = resultado.get("status")
        if estado == "no_position":
            return {"error": resultado.get("error"), "status": estado,
                    "ticket": ticket, "symbol": simbolo or None}, 404
        if estado == "sin_terminal":
            return {"error": resultado.get("error"), "status": estado}, 503
        if estado == "symbol_not_found":
            return {"error": resultado.get("error"), "status": estado}, 404
        return {"error": resultado.get("error"), "status": estado,
                "ticket": resultado.get("ticket"), "symbol": resultado.get("symbol"),
                "retcode": resultado.get("retcode"), "filling": resultado.get("filling"),
                "attempts": resultado.get("attempts") or []}, 400

    # -- modificación de stops ---------------------------------------------------

    def modify_stop(self, ticket: Optional[int], sl: Optional[float] = None,
                    tp: Optional[float] = None) -> Tuple[Dict[str, Any], int]:
        """`(cuerpo, status)` moviendo el stop (y/o el TP) de una posición ABierta.

        **No pasa por las puertas de apertura, y no es un hueco.** La lista
        blanca, el riesgo del día y las noticias responden a la pregunta "¿puedo
        ABRIR riesgo?"; mover el stop de una posición que ya existe no abre nada:
        el riesgo de esa posición ya está comprometido y mover su stop hacia
        atrás solo puede EMPEORARLO — que es exactamente lo que la política de
        salida (`core/exit_policy.py`) impide en el cálculo previo. Bloquear un
        trailing por una noticia de alto impacto sería dejar el stop donde está
        justo cuando más se necesita. Las garantías son, por tanto, DOS y están
        antes de esta llamada: el ratchet "nunca afloja" del cálculo, y aquí el
        ticket y los niveles tal cual llegan.

        Un `sl=None` (o `tp=None`) significa "no tocar ese nivel": el bróker
        conserva el que ya tiene. Quien quita un nivel pasa `0.0`, que es la
        forma en que MT5 lo entiende.
        """
        if not ticket:
            return {"error": "Falta el ticket de la posición a modificar."}, 400
        if sl is None and tp is None:
            return {"error": "No hay nada que modificar: pasa sl y/o tp "
                             "(None deja el nivel como está)."}, 400
        if self._execution is None:
            return {"error": "No hay puerto de ejecución cableado: esta instalación "
                             "puede analizar pero no operar.",
                    "status": "NO_EXECUTION_PORT"}, 503

        fn = getattr(self._execution, "modify_position", None)
        if not callable(fn):
            return {"error": "El puerto de ejecución no sabe modificar posiciones "
                             "(no implementa modify_position).",
                    "status": "NO_EXECUTION_PORT"}, 503
        try:
            resultado = fn(int(ticket), sl=sl, tp=tp)
        except Exception as exc:  # noqa: BLE001 - un error de puente se reporta, no se propaga
            return {"error": "No se pudo modificar la posición {0}: {1}".format(ticket, exc),
                    "status": "MODIFY_FAILED", "ticket": int(ticket)}, 503

        if resultado.get("ok"):
            return {"ok": True, **resultado}, 200

        estado = str(resultado.get("status") or "rejected")
        status_http = {"no_position": 404, "symbol_not_found": 404,
                       "sin_terminal": 503, "sin_respuesta": 503}.get(estado, 400)
        cuerpo = {"error": resultado.get("error")
                  or "El bróker no aceptó la modificación.", "status": estado,
                  "ticket": resultado.get("ticket", int(ticket)),
                  "symbol": resultado.get("symbol"),
                  "retcode": resultado.get("retcode"),
                  "attempts": resultado.get("attempts") or []}
        return cuerpo, status_http

    # -- posiciones --------------------------------------------------------------

    def positions(self, magic: Optional[int] = None
                  ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """`(filas, error)`: posiciones abiertas, filtradas por `magic` si se pasa.

        `([], None)` es "no hay posiciones (o ninguna de este magic)", que NO es
        un error: es el caso normal de un trailing sin nada que mover. El error,
        cuando existe, distingue las dos formas de no poder contestar —sin puerto
        cableado (la instalación analiza pero no opera) y un puerto que no sabe
        listar posiciones—, porque el que llama decide diferente según cuál sea.
        """
        if self._execution is None:
            return None, ("No hay puerto de ejecución cableado: esta instalación "
                          "puede analizar pero no operar.")
        fn = getattr(self._execution, "positions", None)
        if not callable(fn):
            return None, "El puerto de ejecución no sabe listar posiciones (no implementa positions())."
        try:
            filas = fn()
        except Exception as exc:  # noqa: BLE001 - el motivo viaja en `error`, no revienta
            return None, "No se pudieron leer las posiciones abiertas: {0}".format(exc)
        if not isinstance(filas, (list, tuple)):
            return None, "El puerto de ejecución devolvió las posiciones en un formato desconocido."
        salidas = [dict(f) for f in filas]
        if magic is not None:
            objetivo = int(magic)
            salidas = [f for f in salidas if int(f.get("magic") or 0) == objetivo]
        return salidas, None


def _status_de_ejecucion(estado: Optional[str]) -> int:
    """Traduce el estado del bróker a un status HTTP con sentido.

    No se colapsa todo a 400: un `sin_terminal` es 503 (reintentable) y un
    símbolo inexistente es 404. Un 400 para "no hay terminal" hace que un cliente
    que reintenta solo ante 5xx no reintente nunca, y la orden se pierde en el
    primer corte de conexión.
    """
    return {
        "sin_terminal": 503,
        "symbol_not_found": 404,
        "sin_precio": 503,
        "volume_invalido": 400,
        "rejected": 400,
    }.get(str(estado or ""), 400)


def execution_from(runtime_rt: Any) -> ExecutionService:
    """Construye el servicio con las piezas del runtime ya montadas."""
    return ExecutionService(
        market=runtime_rt.market,
        store=runtime_rt.store,
        news=runtime_rt.news,
        execution=getattr(runtime_rt, "execution", None),
        reloj=getattr(runtime_rt, "reloj", None),
    )


__all__ = [
    "DEFAULT_TIMEFRAME",
    "ExecutionService",
    "VERDICT_IGNORED_NEWS",
    "execution_from",
]
