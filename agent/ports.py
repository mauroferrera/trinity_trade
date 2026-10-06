"""Lo que el agente necesita del exterior, declarado como puerto.

Por qué existe este módulo
--------------------------
`REF/agent.py` no tenía puertos: sus herramientas hacían `import app` **dentro** de
cada handler (`agent.py:318`) y llamaban a funciones del monolito de 5.185 líneas.
De ahí salían tres cosas sin nombre:

1. **El agente no se podía probar sin el API.** Una herramienta que solo necesita la
   base de datos (`trade_query`, `journal_append`) pasaba igual por `app`, así que
   para testearla había que levantar el monolito entero.
2. **La capa de explicabilidad quedó soldada al mercado.** Todo lo que el agente
   sabía del mundo le llegaba en forma de "lo que devuelva `app`", y `app` es
   MT5-Forex. Añadir B3 exigía tocar el agente.
3. **El import era un efecto secundario.** `import agent` abría el terminal: la
   Fase 3 acabó centralizando la sesión de MT5 (D-017) y cualquier capa que
   importara el agente entraba por la misma puerta.

Aquí el agente **declara** lo que necesita y quien lo tiene lo **inyecta**. Los
puertos son `Protocol` porque no hay herencia que forzar: `database.store` (un
módulo con funciones sueltas) satisface `StorePort` sin adaptadores de por medio, y
un doble de test satisface lo mismo. La comparación es estructural, y que un doble
de test cumpla el contrato se verifica con un test, no con una clase base que
obligaría a `store` a heredar de algo.

Qué NO es un puerto
------------------
- **El reloj UTC.** `core.clock` es pura y no depende de nadie: se usa directamente.
- **La persistencia.** `database.store` es el dueño de la base y se inyecta como
  puerto, pero el agente no la reimplementa.
- **El score.** `core.risk_engine` es pura y se llama directamente desde las
  plantillas de prompt; el agente no decide (regla 19 de `AGENT_GUIDELINES.md`).
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Vocabulario de estados de una herramienta
# ---------------------------------------------------------------------------
#
# REF tenía `ok` / `failed` / `offline`, y `offline` significaba dos cosas
# distintas según quién mirase: para `price` era "el símbolo no está en Market
# Watch" y para `set_chart_alert` era "MT5 no respondió". Con un solo nombre, el
# modelo recibía el mismo rótulo para un problema de configuración y para una
# caída del bróker, y en la traza no hay forma de distinguirlos.
#
# Aquí son tres y cada uno tiene una reacción distinta:
#
#   ok           -> hay dato. Se persiste y se enseña.
#   failed       -> el puerto existe y reventó al producir la respuesta. Es un
#                   error nuestro o del dato; se lee el `error`.
#   unavailable  -> el puerto NO está cableado, o el proveedor está apagado. No
#                   hay nada que reintentar: hay algo que terminar de construir.

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"

#: Motivo para una herramienta que no tiene con quién trabajar. Viaja en el `error`
#: para que el log diga "falta MarketPort" y no "error desconocido".
PORT_MISSING = "puerto no cableado: {name}"


class AgentPortError(RuntimeError):
    """Fallo de un puerto del agente (distinto de los de `adapters` y `macro_ingestor`).

    No hereda de `adapters.base_adapter.AdapterError` ni de
    `macro_ingestor.base_ingestor.IngestorError` por la misma razón que D-022: cada
    capa tiene su reacción. Aquí el fallo es "la capa de datos que el agente
    necesita no pudo hablar", y la reacción es devolver `unavailable`/`failed` al
    modelo para que lo diga en voz alta en vez de inventarse un número.
    """


# ---------------------------------------------------------------------------
# Mercado y broker
# ---------------------------------------------------------------------------


@runtime_checkable
class MarketPort(Protocol):
    """Precio, cuenta y contexto de mercado. Lo implementa la capa API (Fase 6).

    Los nombres son los de REF (`app.get_account`, `app.patterns_service...`) para
    que el portado sea legible: quien conoce el monolito sabe de dónde sale cada
    cosa. Lo que cambia es que aquí son un contrato, no una incidental de un
    `import`.
    """

    def account_info(self) -> Dict[str, Any]:
        """Balance, equity, flotante, margen y nivel de margen de la cuenta."""
        ...

    def positions(self) -> List[Dict[str, Any]]:
        """Posiciones abiertas (símbolo, lado, volumen, precio, SL/TP, flotante)."""
        ...

    def history(self, days: int) -> List[Dict[str, Any]]:
        """Operaciones cerradas de los últimos `days` días."""
        ...

    def price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Bid/Ask en vivo. `None` si el símbolo no existe en el proveedor.

        `None` y no un dict con error: "no hay precio" y "el bróker está caído" son
        hechos distintos y el llamador los trata distinto.
        """
        ...

    def pattern_data(self, symbol: str, timeframe: str) -> Optional[Dict[str, Any]]:
        """`{"candles": [...], "analysis": {"patterns": {...}}}` del símbolo.

        `None` si el proveedor no tiene el símbolo. Las velas van con `time` en
        SEGUNDOS epoch, la forma normalizada de la Fase 3.
        """
        ...

    def chart_snapshot(self, symbol: str, timeframe: str) -> Dict[str, Any]:
        """Snapshot completo: precio, PDH/PDL, velas, SMC, CVD y score del risk engine."""
        ...

    def orderflow_snapshot(self) -> Dict[str, Any]:
        """CVD, delta, volumen y alertas del motor de order flow."""
        ...

    def orderflow_alerts(self) -> List[Dict[str, Any]]:
        """Alertas de absorción y picos institucionales."""
        ...

    def daily_risk_state(self) -> Dict[str, Any]:
        """Estado diario: DD, operaciones de hoy, topes y motivos de bloqueo.

        Puede traer `{"error": ...}` cuando el proveedor no responde: en REF era un
        campo de texto dentro del dict y se mantiene, porque quien decide necesita
        leerlo en vez de capturarlo.
        """
        ...

    def enrich_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Completa una entrada de bitácora con lo que sí se sepa (precio, SL, TP).

        Es una mejora de convenience, no de corrección: si el proveedor falla, la
        entrada se guarda tal cual. Quien llama decide si eso vale.
        """
        ...

    def broker_time(self) -> str:
        """Hora de PARED del broker, ya formateada.

        Vive en el puerto y no en `core.clock` porque necesita el broker (D-009): el
        reloj UTC canónico es pura, el del broker no.
        """
        ...

    def trading_day(self) -> str:
        """Día de trading (el del servidor del broker), que es el que cuentan los topes."""
        ...


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------


@runtime_checkable
class StorePort(Protocol):
    """Lo que el agente necesita de la base. `database.store` lo satisface tal cual."""

    def get_role(self, role_id: str) -> Optional[Dict[str, Any]]:
        ...

    def get_settings(self) -> Dict[str, Any]:
        ...

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        tool_call_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> None:
        ...

    def get_messages(self, conversation_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        ...

    def add_journal_entry(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def list_journal(
        self,
        symbol: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        ...

    def list_trades(
        self,
        symbol: Optional[str] = None,
        action: Optional[str] = None,
        days: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        ...

    def get_drawings(self, symbol: str, timeframe: str = "M15") -> List[Dict[str, Any]]:
        ...

    def add_chart_alert(
        self,
        symbol: str,
        price: float,
        label: Optional[str] = None,
        side: Optional[str] = None,
        conditions: Optional[List[Dict[str, Any]]] = None,
        timeframe: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...

    def get_agent_topics(self) -> Dict[str, List[str]]:
        """Palabras clave por tema, del `agent.topics` de `strategy.yaml`."""
        ...

    def get_trading_config(self) -> Dict[str, Any]:
        """Configuración de trading (riesgo, score, killzones, prop firm)."""
        ...


# ---------------------------------------------------------------------------
# Noticias y exportaciones del indicador
# ---------------------------------------------------------------------------


@runtime_checkable
class NewsPort(Protocol):
    """Calendario económico. Lo implementa `macro_ingestor` (Fase 4)."""

    def news(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """Última y próxima noticia relevante.

        Devuelve una lectura normalizada de `macro_ingestor` (`stale`, `reason`, ...):
        un calendario que no se pudo leer tiene que poder decirlo, no devolver `[]`.
        """
        ...


@runtime_checkable
class ExportPort(Protocol):
    """Exportaciones `.txt` que el indicador "AI Chart Assistant" escribe en MT5.

    Es I/O de ficheros del BROKER, así que no puede vivir en el agente. El puerto
    existe para dejar el hueco declarado: hasta que la Fase 6 lo cablee,
    `mt5_export_read` responde `unavailable` en vez de fingir que leyó un fichero.
    """

    def list_exports(
        self,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        ...

    def read_export(self, filename: str) -> Dict[str, Any]:
        ...

    def latest_export(
        self,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        ...

    def extract_export_path(self, text: str) -> Optional[str]:
        """Nombre de fichero si el usuario pegó una exportación en el chat."""
        ...


# ---------------------------------------------------------------------------
# Anclaje de análisis entre llamadas
# ---------------------------------------------------------------------------


class AnalysisAnchors:
    """Último análisis de patrones por (conversación, símbolo, timeframe).

    Existe para una herramienta: `chart_annotate` recibe del modelo unas referencias
    (`start_time` de un FVG, `time` de un sweep) y las tiene que validar contra
    datos REALES. REF guardaba ese análisis en un dict de módulo, `_LAST_ANALYSIS`,
    con tres problemas que este módulo cierra:

    1. **Era global del proceso.** La conversación A anchoring sobre EURUSD M15
       servía como validación para la conversación B: el modelo de B podía "anotar"
       patrones que el usuario de B no tenía en pantalla. Aquí la clave incluye la
       conversación.
    2. **No caducaba.** Un análisis de hace una hora seguía validando referencias.
       Las refs existen mientras el patrón siga en la ventana de velas que las generó;
       pasadas las velas, son coordenadas de otro gráfico.
    3. **No crecía.** Cada (símbolo, timeframe) consultados añadía una entrada para
       siempre. Aquí hay tope por conversación y eviction por antigüedad.

    El reloj es inyectable (`now`) porque un caducado que solo se puede comprobar
    esperando no es un caducado, es una intención.
    """

    #: Antigüedad máxima de un análisis para seguir anclando referencias. Cinco
    #: minutos es un intervalo corto a propósito: pasadas las velas que generaron el
    #: patrón, sus coordenadas describen otro gráfico, y una anotación correcta sobre
    #: datos viejos es peor que no anotar.
    TTL_S = 300.0

    #: Anchuras máximas por conversación. Un usuario que recorre 20 símbolos no
    #: necesita 20 anclas vivas para que la siguiente anotación sea válida.
    MAX_POR_CONVERSACION = 8

    __slots__ = ("_datos", "_now", "_ttl_s", "_max", "_lock")

    def __init__(
        self,
        now: Optional[Callable[[], float]] = None,
        ttl_s: float = TTL_S,
        max_por_conversacion: int = MAX_POR_CONVERSACION,
    ) -> None:
        from core import clock

        self._datos: Dict[str, List[Any]] = {}
        self._now = now if now is not None else clock.epoch
        self._ttl_s = float(ttl_s)
        self._max = int(max_por_conversacion)
        self._lock = threading.Lock()

    @staticmethod
    def _clave(conversation_id: str, symbol: str, timeframe: str) -> str:
        return "{0}|{1}|{2}".format(conversation_id, symbol, timeframe)

    def put(
        self,
        conversation_id: str,
        symbol: str,
        timeframe: str,
        analysis: Dict[str, Any],
        candles: Optional[List[Dict[str, Any]]] = None,
    ) -> float:
        """Guarda el análisis y devuelve el instante (epoch) en que se guardó.

        Devolver el sello es lo que permite que un test compruebe el caducado sin
        tener que leer la hora del reloj: se compara contra `age_s()`.
        """
        stamps = list(candles or [])
        ventana = {
            "analysis": analysis,
            "last_time": stamps[-1].get("time") if stamps else None,
            "candle_start": stamps[0].get("time") if stamps else None,
            "candle_lo": min((c.get("low") for c in stamps if c.get("low") is not None), default=None),
            "candle_hi": max((c.get("high") for c in stamps if c.get("high") is not None), default=None),
        }
        clave = self._clave(conversation_id, symbol, timeframe)
        guardado = self._now()
        with self._lock:
            entradas = self._datos.setdefault(conversation_id, [])
            for i, (k, _, _) in enumerate(entradas):
                if k == clave:
                    entradas[i] = (clave, ventana, guardado)
                    break
            else:
                entradas.append((clave, ventana, guardado))
                entradas.sort(key=lambda e: e[2])
                while len(entradas) > self._max:
                    entradas.pop(0)
        return guardado

    def get(
        self, conversation_id: str, symbol: str, timeframe: str
    ) -> Optional[Dict[str, Any]]:
        """El análisis vigente, o `None` si no hay, caducó o es de otra conversación.

        Devuelve una COPIA de la ventana. El llamante la usa para leer patrones y no
        la puede mutar: el anclaje sobrevive a una anotación aunque al usuario le dé
        por recortar coordenadas.
        """
        clave = self._clave(conversation_id, symbol, timeframe)
        ahora = self._now()
        with self._lock:
            for k, ventana, guardado in self._datos.get(conversation_id, ()):
                if k != clave:
                    continue
                if (ahora - guardado) > self._ttl_s:
                    self._olvidar(conversation_id, clave)
                    return None
                copia = dict(ventana)
                copia["stored_at"] = guardado
                return copia
        return None

    def age_s(self, conversation_id: str, symbol: str, timeframe: str) -> Optional[float]:
        """Antigüedad del anclaje en segundos, o `None` si no hay (o caducó)."""
        clave = self._clave(conversation_id, symbol, timeframe)
        ahora = self._now()
        with self._lock:
            for k, _, guardado in self._datos.get(conversation_id, ()):
                if k == clave:
                    edad = ahora - guardado
                    return edad if edad <= self._ttl_s else None
        return None

    def forget(self, conversation_id: str) -> None:
        """Olvida todo lo anclado en una conversación. Al borrar un chat, o en tests."""
        with self._lock:
            self._datos.pop(conversation_id, None)

    def _olvidar(self, conversation_id: str, clave: str) -> None:
        entradas = self._datos.get(conversation_id)
        if not entradas:
            return
        self._datos[conversation_id] = [e for e in entradas if e[0] != clave]

    def __len__(self) -> int:  # pragma: no cover - ayuda de depuración
        with self._lock:
            return sum(len(v) for v in self._datos.values())


# ---------------------------------------------------------------------------
# El paquete de puertos
# ---------------------------------------------------------------------------


class AgentDeps:
    """Los puertos que tiene el agente, más el estado que comparte entre llamadas.

    Un objeto y no una función con veinte argumentos: `laya_bridge` y `tools` la
    comparten, y un test construye una con dobles de una línea.

    `now` y `sleep` son inyectables porque los dos esperas del bucle del agente (el
    backoff por límite de peticiones y el TTL de las alertas) son cosas que hay que
    poder probar sin dormir de verdad.
    """

    __slots__ = ("market", "store", "news", "exports", "anchors", "now", "sleep")

    def __init__(
        self,
        market: Optional[MarketPort] = None,
        store: Optional[StorePort] = None,
        news: Optional[NewsPort] = None,
        exports: Optional[ExportPort] = None,
        anchors: Optional[AnalysisAnchors] = None,
        now: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Any]] = None,
    ) -> None:
        from core import clock

        self.market = market
        self.store = store
        self.news = news
        self.exports = exports
        self.anchors = anchors if anchors is not None else AnalysisAnchors(now=now)
        self.now = now if now is not None else clock.epoch
        self.sleep = sleep

    def puerto_ausente(self, nombre: str) -> str:
        """Motivo para una herramienta que no tiene con quién trabajar."""
        return PORT_MISSING.format(name=nombre)

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        cables = [
            nombre
            for nombre in ("market", "store", "news", "exports")
            if getattr(self, nombre) is not None
        ]
        return "<AgentDeps cables={0}>".format(", ".join(cables) or "ninguno")


__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_UNAVAILABLE",
    "AgentDeps",
    "AgentPortError",
    "AnalysisAnchors",
    "ExportPort",
    "MarketPort",
    "NewsPort",
    "StorePort",
]
