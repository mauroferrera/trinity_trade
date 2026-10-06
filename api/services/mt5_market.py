"""`MarketPort` sobre el adaptador de MT5: la implementación que la Fase 5 declaró.

Qué es este módulo
------------------
`agent/ports.py` declara `MarketPort` (14 métodos) y la Fase 5 lo probó con dobles.
Aquí está el cableado real: el mismo contrato, satisfecho por el adaptador de la
Fase 3 y por `core/` para lo que es cálculo.

Por qué NO vive en `api/routes/`
-------------------------------
Los endpoints de REF hacían el trabajo dentro del handler: `build_chart_snapshot`
(210 líneas) estaba en `app.py` al lado de las rutas, y las rutas la llamaban. Un
snapshot son tres viajes a MT5 y un score; si eso vive en el handler, no hay forma
de probarlo sin levantar FastAPI, y el test acaba comprobando que el JSON tiene
llaves. Aquí el snapshot es un método que se llama con dos strings.

Las tres diferencias con REF que importan
-----------------------------------------
1. **Una sola puerta a MT5.** REF tenía `patterns_service`, `cvd_service` y
   `research/data.py` con tres ciclos de vida (D-017). Aquí todo lo que necesita
   el terminal pasa por `Session`, la del proceso.

2. **La config se lee ANIDADA.** REF hacía `json.loads(cfg.get("risk_weights"))` y
   `cfg.get("killzones")`, claves planas que NO existen en `strategy.yaml` (que es
   `score.weights` y una lista `killzones`). El snapshot salía con los pesos por
   defecto del risk engine y nadie lo notaba, porque un score sin pesos
  personalizados parece un score. Aquí se leen `cfg["score"]["weights"]` y
   `cfg["killzones"]`, y si no están se DICE (`config_warnings`) en vez de dejar
   que el default parezca una decisión.

3. **El risk engine no se traga sus errores.** REF envolvía el bloque entero del
   score en `except Exception: pass`: si el motor fallaba, el snapshot salía sin
   `risk_engine` y el panel se pintaba sin nota de riesgo, con la misma pinta que
   uno que sí la calculó. Aquí el fallo viaja en `risk_engine.error`.

Lo que este módulo NO hace
--------------------------
- **No manda órdenes.** `send_market_order` y la puerta de ejecución son otro
  servicio (y otro fichero) porque escribir en la cuenta es otra cosa que leer.
- **No inventa specs.** Si el bróker no publica el símbolo, `spec()` es `None` y
  quien llama decide; un `tick_value=0` produce un cálculo de riesgo plausible y
  equivocado, que es peor que un error.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from adapters.base_adapter import TIMEFRAMES, AdapterError, SymbolNotFound
from adapters.forex import mt5_forex
from agent.ports import AgentPortError
from core import clock, risk_engine, setup_gate, smc_engine, strategy, trade_history
from core.orderflow_engine import build_cvd_series, pick_cvd_source
from macro_ingestor.base_ingestor import neutro

from .broker_clock import BrokerClock

DEFAULT_SYMBOL = "EURUSD"
DEFAULT_TIMEFRAME = "M15"
DEFAULT_BARS = 300


def simbolo(valor: Optional[str]) -> str:
    """El símbolo de una query, normalizado: en mayúsculas y sin espacios.

    Vive aquí y no en cada router porque la normalización tiene que ser LA MISMA en
    todas partes. Con dos copias, la que se quede sin `.strip()` acepta " eurusd " y
    devuelve un `SymbolNotFound` con el motivo equivocado, y el síntoma parece del
    bróker.
    """
    return str(valor or DEFAULT_SYMBOL).strip().upper()

#: TTL de la caché de velas+patrones. REF usaba 10 s. Es el mismo número con la
#: misma justificación: el score de una vela M15 no cambia en 10 s, y cada
#: `chart_snapshot` sin caché son tres viajes a la terminal por petición.
CACHE_TTL_S = 10.0

#: Cuántas velas recientes lleva el snapshot. REF mandaba las 5 últimas; el resto
#: de la serie viaja por `/api/candles`, que es donde el gráfico la pide entera.
RECENT_CANDLES = 5

#: Ventana de la serie CVD live que se considera "reciente" (30 velas de M15).
CVD_LIVE_WINDOW_S = 30 * 900

#: Cuántas velas entran en el CVD sintético. Suficientes para la pendiente que mira
#: `core.risk_engine.cvd_component`; más velas solo hacen el payload más grande.
CVD_SYNTH_BARS = 60

#: Tope de caracteres del `indicator_export` en el snapshot. El detalle completo lo
#: pida el agente con `mt5_export_read`.
EXPORT_MAX_CHARS = 2000


def neutro_smr(motivo: str) -> Dict[str, Any]:
    """El veredicto SMR de "no se ha confirmado", con el motivo.

    Se construye aqui y no en `macro_ingestor` porque hay dos motivos distintos que
    acaban igual (fuente apagada, sin velas) y ninguno de los dos es del ingestor: son
    de quien mira. La forma es la que espera el frontend y la que lee
    `core.risk_engine` (`bull`/`bear` con `confirmed`).
    """
    return {
        "confirmed": False,
        "reason": motivo,
        "bull": {"confirmed": False},
        "bear": {"confirmed": False},
    }


def _topes(cfg: Dict[str, Any], riesgo: Dict[str, Any], clave: str) -> Any:
    """Un tope de riesgo, leído por las dos rutas: anidada y plana.

    Se delega en `core.strategy._primer_valor`, que es el resolver que ya acepta
    las dos formas que existen en el repositorio, en vez de repetir aquí una
    tercera variante. La plana es la que produce `build_flat` y la que consume el
    store; la anidada es el `strategy.yaml` tal cual. Aceptar solo una deja la
    otra en `None` sin decir nada, y un tope en `None` se lee como "no hay tope
    puesto" cuando sí lo hay.
    """
    if clave in riesgo and riesgo.get(clave) is not None:
        return riesgo.get(clave)
    return strategy._primer_valor(cfg, (clave,), ("risk", clave))


class MT5Market:
    """El mercado y la cuenta, leídos por MT5 a través de la sesión del proceso.

    Los collaborators son inyectables porque las tres respuestas que un test tiene
    que poder fijar son "el bróker dice que sí", "el bróker dice que no" y "no hay
    bróker": con un doble de `Session` las tres se prueban sin terminal.
    """

    def __init__(
        self,
        adapter: Optional[Any] = None,
        session: Optional[Any] = None,
        store: Optional[Any] = None,
        reloj: Optional[BrokerClock] = None,
        orderflow: Optional[Any] = None,
        exports: Optional[Any] = None,
        now: Optional[Callable[[], float]] = None,
        cache_ttl_s: float = CACHE_TTL_S,
    ) -> None:
        self._adapter = adapter if adapter is not None else mt5_forex.adapter()
        self._session = session if session is not None else self._adapter.session
        self._store = store
        self._reloj = reloj if reloj is not None else BrokerClock()
        self._orderflow = orderflow
        # Opcional a propósito: el snapshot tiene que salir aunque no haya puerto de
        # exportaciones (es un extra del diagnóstico, no un requisito del snapshot).
        self._exports = exports
        self._now = now if now is not None else clock.epoch
        self._ttl = float(cache_ttl_s)
        self._cache: Dict[Tuple[str, str, int], Tuple[float, Dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    # -- acceso a las piezas (para los tests y para el cableado) ----------------

    @property
    def adapter(self) -> Any:
        return self._adapter

    @property
    def session(self) -> Any:
        return self._session

    @property
    def reloj(self) -> BrokerClock:
        return self._reloj

    def set_orderflow(self, orderflow: Any) -> None:
        """Engancha el acumulador de cinta cuando ya existe.

        Se puede poner después de construir el mercado porque el feed y el mercado
        se encienden en momentos distintos: el primero cuando el usuario lo
        enciende, el segundo al arrancar.
        """
        self._orderflow = orderflow

    # -- cuenta y posiciones ----------------------------------------------------

    def account_info(self) -> Dict[str, Any]:
        """Balance, equity, flotante, margen y nivel de margen.

        Lanza si la terminal no responde en vez de devolver `{}`: un dict vacío
        parece "no hay cuenta" y quien decide necesita que suene.
        """

        def _get(mt5) -> Optional[Dict[str, Any]]:
            info = mt5.account_info()
            if info is None:
                return None
            return {
                "login": info.login,
                "name": info.name,
                "server": info.server,
                "company": info.company,
                "currency": info.currency,
                "leverage": info.leverage,
                "balance": info.balance,
                "equity": info.equity,
                "profit": info.profit,
                "margin": info.margin,
                "margin_free": info.margin_free,
                "margin_level": info.margin_level,
            }

        datos = self._session.call(_get)
        if datos is None:
            raise AdapterError("MT5 no devolvió información de la cuenta.")
        return datos

    def positions(self) -> List[Dict[str, Any]]:
        """Posiciones abiertas. Lista vacía es una respuesta legítima."""

        def _get(mt5) -> List[Dict[str, Any]]:
            posiciones = mt5.positions_get() or []
            return [
                {
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "price_current": p.price_current,
                    "profit": p.profit,
                    "swap": p.swap,
                }
                for p in posiciones
            ]

        return self._session.call(_get)

    def position(self, ticket: int) -> Optional[Dict[str, Any]]:
        """UNA posición por ticket, o `None`.

        `positions_get(ticket=...)` es una llamada distinta a `positions()`: filtrar
        la lista en Python está bien para 3 posiciones y mal para 300, y la bitácora
        busca por ticket en cada guardado.
        """

        def _get(mt5) -> Optional[Dict[str, Any]]:
            found = mt5.positions_get(ticket=int(ticket)) or []
            if not found:
                return None
            p = found[0]
            return {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume,
                "price_open": p.price_open,
                "sl": p.sl,
                "tp": p.tp,
                "price_current": p.price_current,
                "profit": p.profit,
                "swap": p.swap,
                "time": getattr(p, "time", None),
            }

        return self._session.call(_get)

    def history(self, days: int = 7) -> List[Dict[str, Any]]:
        """Operaciones cerradas de los últimos `days` días, consolidada por posición.

        La consolidación es `core.trade_history` (pura, ya probada): una posición
        abierta y cerrada son dos deals y una sola operación, y sin esto el
        post-mortem cuenta cada lado como una operación distinta.

        La ventana la da el reloj del BROKER, no `now - N días`: la medianoche del
        broker es la frontera que resetea el día de trading, y un corte en UTC
        trunca el día en curso justo cuando se opera.
        """

        def _get(mt5) -> List[Dict[str, Any]]:
            frm, to = self._reloj.trading_day_window(days)
            deals = mt5.history_deals_get(frm, to) or []
            return self._filas_de_deals(deals)

        return self._session.call(_get)

    def _filas_de_deals(self, deals: Any) -> List[Dict[str, Any]]:
        """Deals de MT5 a filas de historial. Puro: no toca el terminal."""
        filas: List[Dict[str, Any]] = []
        marca = self._store.utc_stamp if self._store is not None else clock.now_iso
        for position_id, rec in trade_history.consolidate_deals(deals).items():
            direction = trade_history.direction_of(rec)
            if direction is None:
                continue
            entrada, salida = rec["entry"], rec["exit"]
            lead = entrada or salida
            # MT5 habla en epoch y `clock.to_utc` solo acepta datetimes: pasarle un
            # entero revienta con `AttributeError` en el endpoint, tres capas más
            # allá de donde entró el dato.
            cierre_utc = clock.from_epoch(salida.time if salida is not None else entrada.time)
            entrada_utc = clock.from_epoch(entrada.time) if entrada is not None else None
            precio_cierre = salida.price if salida is not None else entrada.price
            filas.append(
                {
                    "ticket": lead.ticket,
                    "position_id": position_id,
                    "time": marca(cierre_utc),
                    "time_open": marca(entrada_utc) if entrada_utc is not None else None,
                    "symbol": lead.symbol,
                    "type": direction,
                    "volume": entrada.volume if entrada is not None else salida.volume,
                    "price_open": entrada.price if entrada is not None else salida.price,
                    "price_close": precio_cierre,
                    "price": precio_cierre,
                    "profit": rec["profit"],
                    "commission": rec["commission"],
                    "swap": rec["swap"],
                    # Motivo de cierre TAL CUAL lo dio la terminal. Sin esto no se
                    # puede separar "lo paró el SL" de "lo cerró el operador", que
                    # es la diferencia entre un stop que funciona y uno que se mueve
                    # a mano. Se deja CRUDO a propósito: lo etiqueta el post-mortem,
                    # que resuelve el nombre contra el módulo real de MT5.
                    "exit_reason": getattr(salida, "reason", None) if salida is not None else None,
                }
            )
        filas.sort(key=lambda f: str(f["time"]), reverse=True)
        return filas

    # -- precio y velas ---------------------------------------------------------

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Bid/Ask del tick. `None` si el bróker no publica el símbolo.

        `None` y no un dict con error: "no hay precio" y "el bróker está caído" son
        hechos distintos y quien llama los trata distinto (D-030 en el agente).
        """
        simbolo = self._normaliza(symbol)
        datos = self._adapter.price(simbolo)
        if datos is None:
            return None
        return {"symbol": simbolo, **datos}

    def candles(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME, bars: int = DEFAULT_BARS) -> List[Dict[str, Any]]:
        """Velas normalizadas (Fase 3), de más antigua a más reciente."""
        return self._adapter.ohlc(self._normaliza(symbol), self._tf(timeframe), bars)

    def spec(self, symbol: str) -> Optional[Any]:
        """El `SymbolSpec` del bróker, o `None`. Nunca inventado."""
        return self._adapter.spec(self._normaliza(symbol))

    def point(self, symbol: str) -> Optional[float]:
        """`SYMBOL_POINT` del símbolo, o `None` si no se puede leer.

        Envuelto en su propia función porque el spread del snapshot es una mejora
        y no puede hacer que el snapshot falle: sin terminal no hay punto, y sin
        punto no hay spread, pero el resto del snapshot sigue siendo válido.
        """
        try:
            spec = self.spec(symbol)
        except Exception:  # noqa: BLE001 - el punto es un extra, no el dato
            return None
        return float(spec.point) if spec is not None and spec.point else None

    # -- patrones ---------------------------------------------------------------

    def pattern_data(
        self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME, bars: int = DEFAULT_BARS
    ) -> Optional[Dict[str, Any]]:
        """`{"candles", "pdh", "pdl", "analysis"}` del símbolo, con caché de 10 s.

        `pdh`/`pdl` salen de las D1 ANTERIORES a la vela de hoy: el máximo y el
        mínimo del día en curso cambian con cada tick, y un PDH que se mueve con el
        precio no es un nivel, es una sombra. Por eso el puerto pide 3 diarias y
        descarta la última (ref `patterns_service._fetch`).

        `None` cuando el bróker no tiene velas del símbolo; el que llama decide si
        eso es un 404 o un 503.
        """
        simbolo = self._normaliza(symbol)
        tf = self._tf(timeframe)
        n = max(10, min(int(bars), 1000))
        clave = (simbolo, tf, n)
        ahora = self._now()
        with self._cache_lock:
            hit = self._cache.get(clave)
            if hit is not None and (ahora - hit[0]) < self._ttl:
                return hit[1]

        intraday, daily = self._adapter.ohlc_with_daily(simbolo, tf, n)
        if not intraday:
            return None
        pdh = pdl = None
        passadas = list(daily or [])[:-1]
        if passadas:
            pdh = max(float(c["high"]) for c in passadas)
            pdl = min(float(c["low"]) for c in passadas)
        data = {
            "candles": intraday,
            "pdh": pdh,
            "pdl": pdl,
            "analysis": smc_engine.analyze(intraday, pdh, pdl, symbol=simbolo, timeframe=tf),
        }
        with self._cache_lock:
            self._cache[clave] = (ahora, data)
        return data

    def clear_cache(self, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> None:
        """Vacía la caché (entera, o por símbolo/timeframe)."""
        with self._cache_lock:
            if symbol is None and timeframe is None:
                self._cache.clear()
                return
            for clave in list(self._cache):
                if (symbol is None or clave[0] == symbol.upper()) and (
                    timeframe is None or clave[1] == timeframe.upper()
                ):
                    self._cache.pop(clave, None)

    def classify_entry(self, symbol: str, entry_price: float, entry_time: int,
                       timeframe: str = DEFAULT_TIMEFRAME) -> Dict[str, Any]:
        """Qué zona del SMC cubre la entrada, para la bitácora."""
        data = self.pattern_data(symbol, timeframe)
        if data is None:
            return {"poi_type": None, "liquidity_swept": None}
        return smc_engine.classify_entry(
            data["candles"], data["pdh"], data["pdl"], float(entry_price), int(entry_time)
        )

    # -- order flow -------------------------------------------------------------

    def orderflow_snapshot(self) -> Dict[str, Any]:
        """CVD, delta y volumen de la cinta.

        Sin feed encendido se devuelve un `neutro(...)` con el motivo, no un `{}`:
        un CVD vacío se lee como "no hay presión vendedora" cuando significa
        "nadie está mirando".
        """
        if self._orderflow is None:
            return neutro("orderflow", "el feed de cinta no está encendido")
        return self._orderflow.snapshot()

    def orderflow_alerts(self) -> List[Dict[str, Any]]:
        """Alertas de absorción y picos institucionales. Vacía si no hay feed.

        Se leen del `snapshot()` y no de `engine.alerts`: `alerts` es un `deque`
        con su propio lock, y leerlo desde otro hilo sin el lock del motor puede
        devolver una lista a medio construir. El `snapshot()` sí está protegido.
        """
        if self._orderflow is None:
            return []
        snap = self._orderflow.snapshot() or {}
        return list(snap.get("alerts") or [])

    # -- snapshot ---------------------------------------------------------------

    def chart_snapshot(
        self,
        symbol: str = DEFAULT_SYMBOL,
        timeframe: str = DEFAULT_TIMEFRAME,
        bars: int = DEFAULT_BARS,
        force_killzone: bool = False,
    ) -> Dict[str, Any]:
        """El snapshot completo: precio, PDH/PDL, velas, SMC, CVD y score.

        El `built_at` se toma AL ENTRADA y no al final a propósito: la edad que se
        mida después tiene que incluir el tiempo que tardó el snapshot en armarse,
        que es donde se consultan las velas, el COT y el CVD. Un sello puesto al
        final daría por fresco un snapshot cuyo primer dato ya era viejo, que es
        justo lo que la auditoría necesita detectar.

        `force_killzone` es el gancho del modo test: fija el reloj del score dentro
        de la primera ventana configurada para que `/api/chart/setup-eval?killzone=1`
        pueda pintar las banderas de entrada a cualquier hora. Solo mueve el
        componente killzone del score —no `built_at`, ni el régimen, ni la
        invalidez— y sale declarado en `risk_engine.killzone_forced`: el snapshot es
        el que viaja a la fila de auditoría, y un score evaluado fuera de su horario
        que no lo dice es un score que se lee como si fuera real.
        """
        simbolo = self._normaliza(symbol)
        tf = self._tf(timeframe)
        built_at = clock.now_iso()

        data = self.pattern_data(simbolo, tf, bars)
        if data is None:
            raise SymbolNotFound(
                "Sin velas de '{0}' en el bróker (¿está en Market Watch?)".format(simbolo)
            )
        quote = self.price(simbolo)
        actual = (quote or {}).get("bid")
        pdh, pdl = data.get("pdh"), data.get("pdl")
        velas = data.get("candles") or []

        snap: Dict[str, Any] = {
            "symbol": simbolo,
            "timeframe": tf,
            "time": clock.fmt_utc(),
            "broker_time": self._reloj.fmt_broker(),
            "trading_day": self._reloj.trading_day(),
            "built_at": built_at,
            # De dónde salen los datos. La ruta de ejecución real ya garantiza
            # `live`, pero la fila de auditoría tiene que poder atribuirlo después
            # del hecho, y un campo que no se rellena no puede atribuir nada.
            "feed": "live",
            "current_price": actual,
            "PDH": pdh,
            "PDL": pdl,
            "last_time": velas[-1]["time"] if velas else None,
            "distance_to_PDL_pips": self._pips(actual, pdl),
            "distance_to_PDH_pips": self._pips(pdh, actual),
            "recent_candles": [
                {
                    # UTC: las velas son epochs UTC y formatearlas en hora local
                    # mezclaba dos relojes en la misma respuesta.
                    "time": clock.fmt_hm(c["time"]),
                    "close": c["close"],
                    "high": c["high"],
                    "low": c["low"],
                    "volume": c.get("volume"),
                }
                for c in velas[-RECENT_CANDLES:]
            ],
            "analysis": data.get("analysis"),
            "quote": quote,
            "point": self.point(simbolo),
        }
        snap["spread_points"] = self._spread_points(quote, snap["point"])

        cvd, cvd_source, cvd_warning = self._cvd_para_score(velas)
        if cvd is not None:
            snap["cvd"] = cvd
            snap["cvd_source"] = cvd_source
            if cvd_warning:
                snap["cvd_warning"] = cvd_warning

        cot = self._cot_report(simbolo)
        if cot is not None:
            snap["cot_macro_analysis"] = cot

        smr = self._smr(simbolo, tf, velas)
        if smr is not None:
            snap["smr"] = smr

        export = self._export(simbolo)
        if export:
            snap["indicator_export"] = export

        snap["risk_engine"] = self._risk_engine(
            simbolo, tf, data, cot, snap.get("cvd"), smr, force_killzone=force_killzone
        )
        return snap

    def _cvd_para_score(self, velas: List[Dict[str, Any]]) -> Tuple[Optional[List[Dict[str, Any]]], str, Optional[str]]:
        """`(puntos, fuente, aviso)` del CVD para el snapshot y el score.

        La fuente live es la serie del motor de cinta (que solo existe con el feed
        encendido) y la sintética se acumula del **tick volume de MT5**. El selector
        es `core.orderflow_engine.pick_cvd_source`, puro, y lo decide por cantidad de
        puntos: con la live conectada pero con menos de 5 puntos recientes se usa la
        sintética y se DICE, porque un CVD que cambia de fuente sin avisar es un CVD
        con dos contabilidades.

        El aviso de la sintética es explícito porque el tick volume no es volumen
        real: es un proxy relativo, y sin esa frase el modelo lo lee como tamaño de
        órdenes.
        """
        live: Optional[List[Dict[str, Any]]] = None
        if self._orderflow is not None:
            try:
                live = self._orderflow.cvd_series(since=self._now() - CVD_LIVE_WINDOW_S)
            except Exception:  # noqa: BLE001 - sin feed, la sintética es el plan B
                live = None
        sintetica = build_cvd_series(velas[-CVD_SYNTH_BARS:]) if velas else []
        puntos, fuente, aviso = pick_cvd_source(
            live,
            sintetica,
            min_live_points=5,
            live_label="live (cinta del feed)",
            synthetic_label="synthetic (tick volume MT5)",
        )
        if fuente and fuente.startswith("synthetic") and aviso is None:
            aviso = (
                "CVD sintético: acumulado de tick volume de MT5, no volumen real de "
                "mercado. Tómese como proxy relativo."
            )
        return puntos, fuente, aviso

    def smr(self, simbolo: str, timeframe: str, velas: List[Dict[str, Any]]) -> Dict[str, Any]:
        """El veredicto SMR de la serie DADA, en la forma que espera el frontend.

        Es `_smr` con un suelo: cuando la fuente esta apagada o no hay velas, en vez
        de `None` devuelve el veredicto neutro con su motivo. Motivo del suelo: quien
        llama es una ruta que tiene que devolver un veredicto, y `None` en el JSON
        seria "el SMR no existe" en vez de "el SMR no se ha confirmado"; el score ya
        trata `None` como neutro, pero el grafico no.

        Las velas las lee el llamante y se las pasa: el ingestor no vuelve a
        preguntar al broker (ver `_smr`).
        """
        veredicto = self._smr(simbolo, timeframe, velas)
        if veredicto is not None:
            return veredicto
        cfg, _ = self._config()
        if not (cfg.get("data_sources") or {}).get("smr_dxy", True):
            return neutro_smr("fuente smr_dxy apagada en la config")
        return neutro_smr("sin velas de {0} en {1} para alinear el DXY".format(simbolo, timeframe))

    def _smr(self, simbolo: str, tf: str, velas: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """El veredicto SMR/DXY, o `None` si la fuente está apagada o no hay velas.

        Se pide a `macro_ingestor` con las velas YA leídas del bróker: el ingestor no
        vuelve a consultar MT5, porque un ingestor que depende del bróker no puede
        degradar cuando el bróker se cae. Sin velas, no hay veredicto y el
        componente SMR del score vale 0 (neutro), que es lo que dice el motor.
        """
        cfg, _ = self._config()
        fuentes = cfg.get("data_sources") or {}
        if not fuentes.get("smr_dxy", True) or not velas:
            return None
        try:
            from macro_ingestor.forex import dxy_service  # noqa: PLC0415 - perezoso: red

            lectura = dxy_service.poll(symbol=simbolo, timeframe=tf, candles=velas)
        except Exception:  # noqa: BLE001 - el SMR es un extra: si no está, es neutro
            return None
        payload = lectura.get("payload") or {}
        return payload or None

    def _export(self, simbolo: str) -> Optional[str]:
        """La exportación del indicador, recortada. `None` si no hay.

        El recorte lleva su propia marca: un análisis del indicador cortado a los
        2000 caracteres sin decirlo produce un diagnóstico sobre media historia.
        """
        if self._exports is None:
            return None
        try:
            ultimo = self._exports.latest_export(symbol=simbolo, mode="Analyze Chart")
        except Exception:  # noqa: BLE001 - sin terminal no hay exportaciones
            return None
        if not ultimo:
            return None
        contenido = str(ultimo.get("content") or "")
        if len(contenido) > EXPORT_MAX_CHARS:
            return contenido[:EXPORT_MAX_CHARS] + "\n...(resumen - usa mt5_export_read para el detalle)"
        return contenido

    def _pips(self, a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None or b is None:
            return None
        return round((a - b) * 10000, 1)

    def _spread_points(self, quote: Optional[Dict[str, Any]], point: Optional[float]) -> Optional[float]:
        """El spread en PUNTOS, no en pips.

        La conversión a pips depende de los decimales del símbolo y esa regla vive
        en un solo sitio (`SymbolSpec.pip_size`), así que aquí no se reimplementa.
        """
        if not quote or not point:
            return None
        bid, ask = quote.get("bid"), quote.get("ask")
        if bid is None or ask is None:
            return None
        return round((float(ask) - float(bid)) / float(point), 1)

    def _cot_report(self, symbol: str) -> Optional[Dict[str, Any]]:
        """El COT solo se consulta para EURUSD: el reporte de la CFTC es del 6E.

        Es el mismo límite que en `core.risk_engine` (`cot` pesa 0 y solo se
        publica para EURUSD). Aplicarlo a otro par daría un sesgo del dólar
        leído como sesgo de ese par, que es un número con aspecto de señal.
        """
        if symbol != "EURUSD" or self._store is None:
            return None
        try:
            from macro_ingestor.forex import cot_service  # noqa: PLC0415 - perezoso: la red no se toca al importar

            return cot_service.build_report(self._store.list_cot_reports())
        except Exception:  # noqa: BLE001 - el COT es un extra: su ausencia no tumba el snapshot
            return None

    def _config(self) -> Tuple[Dict[str, Any], List[str]]:
        """`(config, avisos)` de la config PLANA, con lo que falte declarado.

        `store.get_trading_config()` devuelve `core.strategy.build_flat`: un dict de
        claves planas, donde `risk_weights` y `killzones` vienen como JSON **string**.
        Leerlos como si fueran un mapa anidado es lo que rompía el score entero
        (`killzone_score` iteraba un string y reventaba con `AttributeError`, así que
        el snapshot salía con `risk_engine.error` y el panel se quedaba sin veredicto
        sin motivo aparente). Los resolvers son `strategy.risk_weights_for` y
        `strategy.killzones_for`, y quien avisa de lo que falta es `_risk_engine`.
        """
        cfg: Dict[str, Any] = {}
        avisos: List[str] = []
        if self._store is not None:
            try:
                cfg = self._store.get_trading_config() or {}
            except Exception as exc:  # noqa: BLE001 - sin config el snapshot sale, sin pesos
                avisos.append("config no disponible: {0}".format(exc))
        return cfg, avisos

    def _risk_engine(
        self,
        simbolo: str,
        tf: str,
        data: Dict[str, Any],
        cot: Optional[Dict[str, Any]],
        cvd: Optional[List[Dict[str, Any]]],
        smr: Optional[Dict[str, Any]] = None,
        force_killzone: bool = False,
    ) -> Dict[str, Any]:
        """El score de las dos direcciones, con el error declarado si lo hay.

        REF envolvía esto en `except Exception: pass`. El resultado era un snapshot
        sin `risk_engine` que el panel pintaba igual que uno con score: el fallo
        más caro de los tres, porque no falla, se ve MAL.

        `smr` entra como `inputs["smr"]` porque es ahí donde lo lee
        `risk_engine.setup_score`. Dejarlo fuera no era un detalle: el componente
        `smr_dxy` habría puntuado 0 siempre, y el score salía BAJO sin motivo
        visible, que es como se pierde la confianza en un número.

        `force_killzone` fija el reloj del score dentro de la primera ventana
        configurada, para que el modo test pueda pintar las banderas de entrada sin
        esperar a las 07:00 UTC. Solo mueve el componente killzone: `built_at`, el
        régimen y la invalidez siguen siendo los del reloj real. Se resuelve AQUÍ y no
        en `chart_snapshot` porque las ventanas salen de la misma lectura de config que
        el score, y leerla dos veces podía dar dos ventanas distintas si alguien
        guardara el YAML entre medias.
        """
        cfg, avisos = self._config()
        weights = strategy.risk_weights_for(cfg)
        if weights is None:
            avisos.append(
                "score.weights ilegible o ausente en la config: se mide con los "
                "pesos de fábrica de risk_engine"
            )
        killzones = strategy.killzones_for(cfg)
        if killzones is None:
            avisos.append(
                "killzones ilegibles o ausentes en la config: se mide con las "
                "ventanas de fábrica de risk_engine"
            )
        now = setup_gate.killzone_forced_now(killzones) if force_killzone else None
        patterns = (data.get("analysis") or {}).get("patterns")
        velas = data.get("candles") or []
        entradas = {
            "cot": cot,
            "cvd": cvd,
            "patterns": patterns,
            "candles": velas,
            "smr": smr,
        }
        try:
            bull = risk_engine.setup_score(
                {**entradas, "direction": "BUY"}, weights=weights, killzones=killzones,
                now=now,
            )
            bear = risk_engine.setup_score(
                {**entradas, "direction": "SELL"}, weights=weights, killzones=killzones,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - el score no tumba el snapshot: se dice que falta
            return {"error": "risk engine no disponible: {0}".format(exc), "warnings": avisos}
        salida: Dict[str, Any] = {
            "bull": bull,
            "bear": bear,
            "killzone": bull["components"]["killzone"],
            "regime": bull["regime"],
            "invalidation": {
                "BUY": risk_engine.invalidation_level(patterns, "BUY"),
                "SELL": risk_engine.invalidation_level(patterns, "SELL"),
            },
        }
        if now is not None:
            # Solo se marca cuando la ventana se ha forzado: `/api/risk/setup` publica
            # este dict tal cual, y un `killzone_forced` siempre a True haría que el
            # panel dejara de fiarse del horario.
            salida["killzone_forced"] = True
        if avisos:
            salida["warnings"] = avisos
        return salida

    # -- estado de riesgo del día ------------------------------------------------

    def daily_risk_state(self) -> Dict[str, Any]:
        """DD, operaciones de hoy y topes.

        Puede traer `{"error": ...}`: quien decide (el gate de entrada, el watcher)
        tiene que LEER ese campo, no capturar la excepción, porque un estado de
        riesgo ausente y un estado de riesgo sin riesgo no se distinguen de otro
        modo en el prompt.
        """
        try:
            cuenta = self.account_info()
        except Exception as exc:  # noqa: BLE001 - se devuelve el error en el dict, no se propaga
            return {"error": str(exc)}
        cfg = {}
        if self._store is not None:
            try:
                cfg = self._store.get_trading_config() or {}
            except Exception:  # noqa: BLE001 - sin config no hay topes que aplicar
                cfg = {}
        # Los topes se buscan por las DOS rutas, plana y anidada, con el mismo
        # resolver que usa `core/`. El store devuelve la forma plana
        # (`{"risk_pct": 0.5, ...}`) y leer solo `cfg["risk"]` daba `None` en los
        # cinco: un panel de riesgo con todos los topes en `null` mientras el gate
        # de entrada sí los tenía, que es la peor forma de discrepancia: parece
        # que no hay límites puestos cuando los hay.
        riesgo = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
        hoy = self._reloj.trading_day()
        try:
            operaciones = len(self.history(1))
        except Exception as exc:  # noqa: BLE001 - idem: se dice que no se pudo leer
            return {"error": "no se pudo leer el historial: {0}".format(exc),
                    "balance": cuenta.get("balance")}
        estado: Dict[str, Any] = {
            "trading_day": hoy,
            "balance": cuenta.get("balance"),
            "equity": cuenta.get("equity"),
            "float_profit": cuenta.get("profit"),
            "margin_level": cuenta.get("margin_level"),
            "trades_today": operaciones,
            "max_trades_day": _topes(cfg, riesgo, "max_trades_day"),
            "risk_pct": _topes(cfg, riesgo, "risk_pct"),
            "reduced_risk_pct": _topes(cfg, riesgo, "reduced_risk_pct"),
            "loss_limit_fixed": _topes(cfg, riesgo, "loss_limit_fixed"),
            "loss_limit_pct": _topes(cfg, riesgo, "loss_limit_pct"),
        }
        fijo = estado["loss_limit_fixed"]
        balance = cuenta.get("balance")
        if fijo and balance:
            inicio, guardado = self._balance_inicio(hoy, float(balance))
            estado["pnl_day"] = round(float(balance) - inicio, 2)
            estado["balance_source"] = "guardado" if guardado else "estimado"
            estado["loss_limit_reached"] = estado["pnl_day"] <= -abs(float(fijo))
        # `_topes` y no `riesgo.get`: el tope de operaciones se leía del dict
        # anidado, que en la config real está vacío, así que
        # `max_trades_reached` NO aparecía nunca aunque `max_trades_day` sí se
        # mostrara en la misma respuesta. Un panel que enseña el tope y no dice
        # si se ha alcanzado es exactamente el fallo que motiva `_topes`.
        max_trades = estado["max_trades_day"]
        if max_trades:
            estado["max_trades_reached"] = operaciones >= int(max_trades)

        # El veredicto que necesita el ejecutor. `loss_limit_reached` y
        # `max_trades_reached` son HECHOS; `blocked` es la decisión de no operar.
        # Se calculan los tres aquí y no en el servicio de ejecución porque un
        # segundo consumidor de estos flags los interpretaría distinto: es el
        # mismo patrón de divergencia que `max_trades_reached`, una decisión
        # repartida entre quien mide y quien la usa.
        razones: List[str] = []
        if estado.get("loss_limit_reached"):
            razones.append(
                "Límite de pérdida del día alcanzado: {0} {1} contra un tope de {2}.".format(
                    estado.get("pnl_day"), cuenta.get("currency") or "de la cuenta", fijo))
        pct = estado.get("loss_limit_pct")
        pnl = estado.get("pnl_day")
        if pct and pnl is not None and not estado.get("loss_limit_reached"):
            inicio, _ = self._balance_inicio(hoy, float(balance or 0.0))
            if inicio > 0 and abs(float(pnl)) >= inicio * float(pct) / 100.0:
                estado["loss_limit_reached"] = True
                razones.append(
                    "Límite de pérdida porcentual del día alcanzado: {0:.2f}% sobre "
                    "un balance de inicio de {1:.2f} (tope {2}%).".format(
                        abs(float(pnl)) / inicio * 100.0, inicio, float(pct)))
        if estado.get("max_trades_reached"):
            razones.append(
                "Máximo de operaciones del día alcanzado: {0} de {1}.".format(
                    operaciones, max_trades))
        estado["trades_counted"] = operaciones
        estado["blocked"] = bool(razones)
        estado["reasons"] = razones
        return estado

    def _balance_inicio(self, trading_day: str, por_defecto: float) -> Tuple[float, bool]:
        """`(balance_inicio, estaba_guardado)`.

        Sin una tabla de HWM por día no hay forma de saber el balance con el que
        empezó el día: se usa el balance actual y se marca `estimado`. Un DD
        calculado contra el balance del momento es un DD que cambia según el
        balance, que es la forma más fácil de no tener un DD.
        """
        if self._store is None:
            return por_defecto, False
        try:
            state = self._store.get_prop_state() or {}
        except Exception:  # noqa: BLE001 - sin estado guardado se marca estimado
            state = {}
        inicio = state.get("day_start_balance")
        if inicio is None or str(state.get("trading_day") or "") != str(trading_day):
            return por_defecto, False
        return float(inicio), True

    # -- bitácora ---------------------------------------------------------------

    def enrich_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Rellena `poi_type`/`liquidity_swept` y los precios del trade.

        Nunca lanza. Es una mejora de conveniencia, no de corrección: si el bróker
        no está o no hay posición, la entrada se guarda tal cual la escribió el
        usuario. Quien llama decide si eso vale.
        """
        if entry.get("poi_type") and entry.get("liquidity_swept"):
            return entry
        ticket = entry.get("ticket")
        if not ticket:
            return entry
        try:
            pos = self.position(int(ticket))
        except Exception:  # noqa: BLE001 - sin bróker se guarda igual
            return entry
        if pos is None:
            return entry
        symbol = pos.get("symbol") or entry.get("symbol") or DEFAULT_SYMBOL
        try:
            clasificacion = self.classify_entry(
                symbol, float(pos.get("price_open") or 0.0), int(pos.get("time") or 0)
            )
        except Exception:  # noqa: BLE001 - ídem
            clasificacion = {}
        if not entry.get("poi_type"):
            entry["poi_type"] = clasificacion.get("poi_type")
        if not entry.get("liquidity_swept"):
            entry["liquidity_swept"] = clasificacion.get("liquidity_swept")
        for campo in ("entry_price", "sl_price", "tp_price"):
            if not entry.get(campo):
                entry[campo] = pos.get(
                    {"entry_price": "price_open", "sl_price": "sl", "tp_price": "tp"}[campo]
                )
        if not entry.get("time_open"):
            entry["time_open"] = pos.get("time")
        return entry

    # -- reloj ------------------------------------------------------------------

    def broker_time(self) -> str:
        """Hora de PARED del broker, ya formateada.

        Vive aquí y no en `core.clock` porque necesita la config del broker: el
        reloj UTC canónico es pura, el del broker no (D-009).
        """
        return self._reloj.fmt_broker()

    def trading_day(self) -> str:
        """El día del servidor del broker, que es el que cuentan los topes."""
        return self._reloj.trading_day()

    def clock_state(self) -> Dict[str, Any]:
        """El estado completo del reloj (lo que lee `/api/clock`)."""
        return self._reloj.state()

    # -- utilidades -------------------------------------------------------------

    @staticmethod
    def _normaliza(symbol: Optional[str]) -> str:
        simbolo = str(symbol or "").strip().upper()
        if not simbolo:
            raise AgentPortError("falta el símbolo")
        return simbolo

    @staticmethod
    def _tf(timeframe: Optional[str]) -> str:
        """El timeframe en corto. Un timeframe inválido es un error del llamador."""
        tf = str(timeframe or DEFAULT_TIMEFRAME).strip().upper()
        if tf not in TIMEFRAMES:
            raise ValueError(
                "Timeframe '{0}' no válido. Usa: {1}".format(tf, ", ".join(sorted(TIMEFRAMES)))
            )
        return tf

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "<MT5Market adapter={0} orderflow={1}>".format(
            getattr(self._adapter, "name", "?"), self._orderflow is not None
        )


__all__ = ["CACHE_TTL_S", "DEFAULT_BARS", "DEFAULT_SYMBOL", "DEFAULT_TIMEFRAME", "MT5Market", "simbolo"]

