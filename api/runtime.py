"""El contenedor de la aplicación: quién tiene qué, y quién enciende qué.

Por qué un `Runtime` y no globals
---------------------------------
REF tenía el estado del proceso repartido en variables de módulo: `_engine`,
`manager`, `DB_STATE`, `_main_loop`, `sim`, `_mt5_adapter`. Consecuencias concretas:

1. **Importar `app` encendía cosas.** `_cot_background_sync` lanzaba un `threading.Thread`
   en el arranque (línea 1610), y el manager de WebSockets guardaba el loop en un
   global mutable. Un test que importaba `app` para ver un endpoint ya tenía un
   hilo corriendo.
2. **Dos apps en el mismo proceso compartían estado.** Los tests de integración
   wanting dos instancias con puertos distintos no podían: la segunda leía el
   `manager` de la primera.
3. **No había forma de saber qué estaba cableado.** `import app` funcionaba igual
   con la base caída, sin MT5 y sin feeds, y el primer síntoma era un 500 en una
   ruta concreta.

Aquí el estado vive en un objeto que se crea en `create_app()` y se guarda en
`app.state.runtime`. Los endpoints lo leen con `deps.runtime(request)`. Las rutas
no importan `app`, los tests pueden construir un `Runtime` con dobles, y
`/api/health` puede responder qué hay conectado y qué no.

Sobre qué se enciende al arrancar
---------------------------------
**Nada que hable con la red.** `startup()`:

- engancha el loop de asyncio al gestor de WebSockets (para poder emitir desde un
  hilo), que es lo único que hace falta para que `emit()` funcione;
- comprueba la base de datos y la deja inicializada;
- **no** abre MT5, no descarga el COT, no arranca el feed de cinta ni el watcher.

Esas cosas se encienden cuando alguien las pide (feed, `/api/...`) y todas tienen
un estado visible. La razón es la que yaMotiva el resto del proyecto: un proceso
que abre un terminal y descarga recursos al importar no se puede testear, y un
proceso que enciende todo al arrancar esconde el fallo de arranque detrás de un
`502` en la primera petición.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from .services.broker_clock import BrokerClock
from .services.macro_news import MacroNews
from .services.mt5_exports import MT5Exports
from .services.mt5_market import MT5Market
from .websocket_manager import ConnectionManager

log = logging.getLogger("api.runtime")


class Runtime:
    """Las piezas de la aplicación y su ciclo de vida.

    Todos los colaboradores son opcionales e inyectables. `build()` es el único
    sitio que compone la configuración real; un test hace
    `Runtime(store=FakeStore(), market=FakeMarket())` y ya tiene el cableado.
    """

    __slots__ = (
        "store",
        "reloj",
        "market",
        "news",
        "exports",
        "orderflow",
        "ws",
        "symbols",
        "execution",
        "watcher",
        "cta",
        "_deps",
        "_store_backend",
    )

    def __init__(
        self,
        *,
        store: Optional[Any] = None,
        reloj: Optional[BrokerClock] = None,
        market: Optional[Any] = None,
        news: Optional[Any] = None,
        exports: Optional[Any] = None,
        orderflow: Optional[Any] = None,
        ws: Optional[ConnectionManager] = None,
        symbols: Optional[Any] = None,
        execution: Optional[Any] = None,
        watcher: Optional[Any] = None,
        cta: Optional[Any] = None,
    ) -> None:
        from agent.ports import AgentDeps

        self.store = store
        self.reloj = reloj if reloj is not None else BrokerClock()
        self.market = market
        self.news = news
        self.exports = exports
        self.orderflow = orderflow
        self.ws = ws if ws is not None else ConnectionManager()
        #: Proveedor de símbolos. Por defecto es el mercado, porque el mercado es
        #: quien los tiene; `symbols` existe como atributo aparte para poder listar
        #: símbolos sin bróker (tests y modo diagnóstico), no como una pieza más que
        #: haya que acordarse de cablear.
        self.symbols = symbols if symbols is not None else market
        #: Puerto de EJECUCIÓN (`order_send`). Viene aparte del mercado a propósito:
        #: el mercado LEE y este ESCRIBE. Una instalación puede tener el primero sin
        #: el segundo —analiza y no opera— y es una distinción que la UI declara,
        #: no un fallo que se descubra al pulsar un botón de compra.
        self.execution = execution
        #: Servicio del watcher. NO se construye en el arranque: se pide la primera
        #: vez que alguien lee `/api/watcher/status` o llama a `/api/watcher/scan`, y
        #: se comparte desde ese momento. Ver `watcher_service()`.
        self.watcher = watcher
        #: Servicio del CTA en alerta (F4, D-077). Mismo trato que el watcher: lazy y
        #: compartido, porque deduplica las alertas D1 en memoria. Ver `cta_service()`.
        self.cta = cta
        self._store_backend = "inyectado" if store is not None else None
        # El agente se construye una vez y se comparte: sus anclajes entre
        # llamadas (`AnalysisAnchors`) viven aquí, no en un global.
        self._deps = AgentDeps(
            market=market,
            store=store,
            news=news,
            exports=exports,
        )

    # -- construcción ------------------------------------------------------------

    @classmethod
    def build(cls, *, store: Optional[Any] = None, orderflow: Optional[Any] = None) -> "Runtime":
        """Compone el cableado real: `database.store` + MT5 + `macro_ingestor`.

        Los imports son perezosos a propósito (regla de `macro_ingestor` y
        `adapters`): importar `Runtime` no debe abrir la base ni mirar el terminal.
        """
        from database import store as store_mod

        from adapters.forex import mt5_execution, mt5_forex
        from core.orderflow_engine import OrderFlowEngine

        store = store if store is not None else store_mod
        engine = orderflow if orderflow is not None else OrderFlowEngine()
        adapter = mt5_forex.adapter()
        reloj = BrokerClock(
            cfg=store.get_trading_config,
            # El EA escribe en FILE_COMMON del terminal, y quien sabe dónde está es
            # el propio adaptador. Se pasa la sesión (D-017), no un `import
            # MetaTrader5` desde el reloj.
            files_dir=lambda: adapter.terminal_files_dir(),
        )
        exports = MT5Exports()
        market = MT5Market(
            adapter=adapter,
            store=store,
            reloj=reloj,
            orderflow=engine,
            exports=exports,
        )
        return cls(
            store=store,
            reloj=reloj,
            market=market,
            news=MacroNews(store=store),
            exports=exports,
            orderflow=engine,
            symbols=market,
            # Comparte la SESIÓN con el adaptador de lectura, no solo la clase: dos
            # sesiones contra el mismo terminal son dos puertas al mismo sitio, y la
            # que escribe mientras la otra lee es la forma de mandar con una
            # cotización que ya no es la que se validó.
            execution=mt5_execution.adapter(session=adapter.session),
        )

    @property
    def deps(self) -> Any:
        """Los `AgentDeps` del agente, construidos una vez y compartidos."""
        return self._deps

    def watcher_service(self) -> Any:
        """El `WatcherService` de ESTA app, construido una vez.

        Por qué vive aquí y no en la ruta: el watcher deduplica los descartes **en
        memoria**, con la firma de cada rechazo (ver `core/setup_lifecycle`). Si cada
        petición construyera su propio servicio, esa memoria dying con el `return`
        dejaría la dedup inservible y un setup por debajo del umbral escribiría una
        fila de `setup_log` cada `scan_interval_sec`, para siempre. Un test que usa un
        servicio a mano no lo detecta: reutiliza la instancia y la dedup parece
        funcionar.

        Se construye en el primer uso, no en el arranque: `import Runtime` no debe
        tocar la base ni el mercado, y el watcher no hace nada solo.

        Si alguien cambia el mercado con `set_market`, el servicio se descarta. El
        watcher guarda el mercado con el que se construyó, y escanear contra un
        mercado que ya no está conectado daría `error` en cada símbolo haciéndose el
        vivo; es mejor perder la memoria de dedup (un par de filas de más) que
        escanear el sitio equivocado.
        """
        if self.watcher is None:
            from .services.watcher_service import WatcherService

            self.watcher = WatcherService(
                market=self.market, store=self.store, news=self.news,
            )
        return self.watcher

    def cta_service(self) -> Any:
        """El `CtaAlertService` de ESTA app, construido una vez.

        Mismo motivo que `watcher_service()`: deduplica las alertas D1 **en memoria**
        por (símbolo, barra de señal). Si cada petición construyera su servicio, esa
        memoria moriría con el `return` y la misma barra reescribiría su fila de
        `setup_log` en cada llamada. Se construye en el primer uso, no en el arranque:
        `import Runtime` no toca la config, y el CTA no hace nada solo.

        Si alguien cambia el mercado con `set_market`, el servicio se descarta (igual
        que el watcher): escanear contra un mercado que ya no está conectado daría
        `error` en cada símbolo haciéndose el vivo; se prefiere perder la memoria de
        dedup —un par de filas de más— a mirar el sitio equivocado.
        """
        if self.cta is None:
            from .services.cta_alert_service import CtaAlertService

            self.cta = CtaAlertService(market=self.market, store=self.store)
        return self.cta

    def set_market(self, market: Any) -> None:
        """Cambia el mercado y actualiza los `AgentDeps` sin recrearlos.

        Los anclajes sobreviven: son estado del chat, no de la terminal. Un test que
        cambia el mercado a mitad de una conversación pierde los datos de mercado,
        que es lo que quería cambiar, y nada más.

        Los servicios se sueltan con el mercado: su dedup en memoria es de este
        proceso, y los que la guardan escanearían contra el mercado viejo.
        """
        self.market = market
        self.symbols = market
        self.watcher = None
        self.cta = None
        self._deps.market = market

    # -- ciclo de vida -----------------------------------------------------------

    async def startup(self) -> None:
        """Prepara lo que hace falta para servir, y nada más.

        - engancha el loop al gestor de WebSockets: `emit()` desde el hilo de MT5
          depende de esto, y sin loop un `emit` cuenta un drop en vez de fallar
          en silencio;
        - deja la base inicializada (una sola vez; `init_db` es idempotente).
        """
        import asyncio

        self.ws.attach_loop(asyncio.get_running_loop())
        if self.store is not None and self._store_backend is None:
            try:
                self.store.init_db()
                self._store_backend = "sqlite"
            except Exception as exc:  # noqa: BLE001 - arrancar sin DB es un hecho, no una razón para no arrancar
                self._store_backend = "error"
                log.warning("base de datos no disponible: %s", exc)
        log.info(
            "runtime listo: store=%s market=%s news=%s exports=%s orderflow=%s",
            self._store_backend,
            self.market is not None,
            self.news is not None,
            self.exports is not None,
            self.orderflow is not None,
        )

    async def shutdown(self) -> None:
        """Apaga lo que se encendió. Idempotente."""
        for pieza, nombre in ((self.orderflow, "orderflow"), (self.ws, "ws")):
            stop = getattr(pieza, "stop", None)
            if callable(stop):
                try:
                    result = stop()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:  # noqa: BLE001 - apagar no puede fallar el apagado
                    log.warning("apagando %s: %s", nombre, exc)
        market = self.market
        adapter = getattr(market, "adapter", None)
        session = getattr(adapter, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 - ídem
                log.warning("cerrando la sesión de MT5: %s", exc)

    # -- diagnóstico -------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Qué está vivo, qué no, y por qué. `/api/health` responde esto.

        Los estados son strings y no booleanos porque lo que un operador necesita
        leer no es si algo está "ok" sino si está `sin_bróker`, `sin_base` o
        `error`, y un `false` no dice cuál de las tres cosas es.
        """
        return {
            "ok": bool(self.store is not None),
            "store": self._estado_store(),
            "market": self._estado_market(),
            "news": self._estado_news(),
            "exports": self._estado_exports(),
            "orderflow": self._estado_orderflow(),
            "ws": self.ws.stats(),
            "broker_time": self._broker_time(),
        }

    def _estado_store(self) -> Dict[str, Any]:
        if self.store is None:
            return {"estado": "sin_puerto"}
        try:
            tablas = self.store.list_db_tables()
        except Exception as exc:  # noqa: BLE001 - el estado es "no responde", no un 500
            return {"estado": "error", "error": "{0}: {1}".format(type(exc).__name__, exc)}
        return {"estado": "ok", "tablas": len(tablas or [])}

    def _estado_market(self) -> Dict[str, Any]:
        if self.market is None:
            return {"estado": "sin_puerto"}
        session = getattr(self.market, "session", None)
        if session is None:
            session = getattr(getattr(self.market, "adapter", None), "session", None)
        return {
            "estado": "con_terminal" if _terminal_alcanzable(session) else "sin_terminal",
            "session": type(getattr(self.market, "adapter", None)).__name__,
        }

    def _estado_news(self) -> Dict[str, Any]:
        if self.news is None:
            return {"estado": "sin_puerto"}
        veredicto = self.news.gate()
        return {
            "estado": "ok",
            "block": veredicto.get("block"),
            "fail_open": bool(veredicto.get("fail_open")),
        }

    def _estado_exports(self) -> Dict[str, Any]:
        if self.exports is None:
            return {"estado": "sin_puerto"}
        return {"estado": "ok", "dir": self.exports.files_dir() or None}

    def _estado_orderflow(self) -> Dict[str, Any]:
        if self.orderflow is None:
            return {"estado": "sin_puerto"}
        snap = self.orderflow.snapshot() or {}
        return {
            "estado": "ok",
            "msgs_per_sec": snap.get("msgs_per_sec", 0.0),
            "cvd": snap.get("cvd"),
            "alertas": len(snap.get("alerts") or []),
        }

    def _broker_time(self) -> Dict[str, Any]:
        try:
            reloj = self.reloj.state()
            return {
                "broker": reloj.get("broker_now"),
                "trading_day": reloj.get("trading_day"),
                "tz": reloj.get("broker_tz"),
                # El EA clock se publica entero, incluido `stale`. Antes se leía
                # `.get("estado")` de un payload que no tiene esa clave y de un
                # `read_ea_clock()` que puede ser `None`: el campo salía siempre
                # `null` (o tumbaba el bloque entero con un AttributeError) y un
                # reloj rancio pasaba por verificado.
                "ea_clock": reloj.get("ea_clock"),
            }
        except Exception as exc:  # noqa: BLE001 - health nunca lanza
            return {"error": "{0}: {1}".format(type(exc).__name__, exc)}

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "<Runtime store={0} market={1} ws={2}>".format(
            self._store_backend,
            type(self.market).__name__ if self.market is not None else None,
            self.ws,
        )


def token_actual() -> Optional[str]:
    """`API_TOKEN` leído AHORA, o `None` si no hay token configurado.

    Se lee por llamada y no al importar para que un token rotado en el entorno se
    aplique sin reiniciar, y para que los tests puedan monkeypatchearlo. Sin token
    no hay autenticación (el modo por defecto de un desarrollo local), y con
    token la comparación es en tiempo constante.
    """
    valor = os.getenv("API_TOKEN", "").strip()
    return valor or None


def _bool_de_marca(objeto: Any, nombre: str) -> bool:
    """Lee una marca que puede ser atributo o método, sin adivinar cuál es.

    La sesión de MT5 expone `is_open` como `@property`, pero un doble de test (o un
    puerto nuevo) puede exponerlo como método. Llamarlo siempre como método rompía
    `/api/health` justo en la configuración real -con terminal abierta-, que es la
    única que importa: con el doble, `session` era `None` y la rama ni se ejecutaba.
    Un `health` que da 500 en producción y pasa en verde en tests es el peor sitio
    posible para una aserción de estado.

    La lectura va dentro del `try` porque una property que calcula algo real puede
    lanzar al ACCEDERLA, no solo al invocarla: `getattr` es parte de lo que puede
    fallar.
    """
    try:
        marca = getattr(objeto, nombre, False)
        if callable(marca):
            marca = marca()
    except Exception:  # noqa: BLE001 - un estado que no responde es "cerrado", no un 500
        return False
    return bool(marca)


def _terminal_alcanzable(session: Any) -> bool:
    """Si la terminal de MT5 responde AHORA, no solo si ya se conectó.

    La sesión es perezosa: `is_open` es `True` únicamente después de que una
    lectura abrió la terminal. Una app recién arrancada que aún no ha leído nada
    publicaba `sin_terminal` con el bróker conectado y el resto de rutas
    funcionando, que es la peor clase de mentira en un panel de estado: el
    operador ve rojo y no toca nada, cuando el único botón que necesita es abrir
    el chart.

    Así que se PREGUNTA: si ya está abierta se lee la marca, y si no, se hace el
    trabajo mínimo de conexión. El fallo no es un error de health: una terminal
    cerrada es un estado legítimo (`sin_terminal`), no una avería.
    """
    if session is None:
        return False
    if _bool_de_marca(session, "is_open"):
        return True
    call = getattr(session, "call", None)
    if not callable(call):
        return False
    try:
        return bool(call(lambda mt5: mt5 is not None))
    except Exception:  # noqa: BLE001 - idem: cerrada, rota o ausente = sin terminal
        return False


__all__ = ["Runtime", "token_actual"]