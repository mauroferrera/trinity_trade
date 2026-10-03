"""Interfaz común de los ingestores macro + lo que un ingestor debe cumplir.

Qué es un ingestor y por qué necesita su propia capa
---------------------------------------------------
`adapters/` trae el precio: velas y cinta. Un ingestor macro trae el ENTORNO: el
sesgo del COT, el DXY, el calendario de noticias, el Focus del BCB. Son datos que
no se pueden negociar, así que no pueden ser adaptadores de mercado, pero se
consumen igual: los lee `core.risk_engine` como un componente más del score.

Tres características que comparten TODOS y que en REF estaban reimplementadas tres
veces:

1. **Van a un tercero sin API key.** La CFTC publica en Socrata abierto, el DXY se
   puede leer de Yahoo y Forex Factory se puede leer por un proxy. Eso significa
   que pueden caerse sin que nadie avise, y que el scraper es frágil por
   definición: la API puede cambiar un martes.
2. **Se consultan en el camino de ejecución.** El gate de noticias se pregunta
   ANTES de cada orden. Un fetch por trade es un fetch por trade contra un
   servidor que devuelve 403 por rate-limit, y el resultado es que el bot deja de
   operar justo cuando la red falla, que es cuando más lo necesita.
3. **Degradar a neutro es lo correcto.** Un COT que no se pudo leer no es "COT
   alcista", es "no hay dato". El sesgo tiene que ser 0/NEUTRAL con el motivo
   escrito, nunca el último valor cacheado presentado como si fuera fresco.

La forma normalizada
--------------------
    {"source", "asof_ts", "payload", "stale", "reason"}

- `payload` es el dato del ingestor (un sesgo, una serie, una lista de eventos). No
  se estandariza porque "sesgo" y "serie de velas" no comparten forma; estandarizar
  la ENVOLTORIA es lo que permite que `risk_engine` trate a todos igual.
- `asof_ts` es epoch SEGUNDOS y es del DATO, no del fetch. Una serie del DXY tiene
  su propia fecha de último cierre; poner `time.time()` ahí haría que un dato de
  hace tres horas pareciera reciente.
- `stale` es un booleano y va explícito. En REF el DXY servía caché vencida sin
  decirlo, así que un consumidor no podía distinguir "el DXY va en 104" de "este 104
  es de ayer".
- `reason` es para el humano: por qué no hay dato, si no lo hay.

Por qué el CVD no aparece aquí
------------------------------
`core/orderflow_engine.build_cvd_series()` es puro y ya es su dueño. Un ingestor que
calcula el CVD duplicaría esa lógica en un sitio que además depende de red, que es
donde peor se testea.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Generic, Optional, Protocol, TypeVar
from typing import runtime_checkable

# ---------------------------------------------------------------------------
# Jerarquía de errores
# ---------------------------------------------------------------------------

T = TypeVar("T")


class IngestorError(RuntimeError):
    """Fallo de una fuente macro (CFTC caída, proxy caído, respuesta ilegible).

    NO hereda de `adapters.base_adapter.AdapterError` a propósito, por la misma
    razón que `DatabentoNoConfigurado`: son capas distintas con reacciones
    distintas. Un `AdapterError` significa "el bróker no responde"; un
    `IngestorError` significa "no sé qué dice el COT". El primero se le avisa al
    usuario, el segundo degrada un componente del score a neutro.
    """


class FeedUnavailable(IngestorError):
    """La fuente no está accesible ahora mismo.

    Distinto de `IngestorError` porque la reacción correcta es reintentar o servir
    caché, no avisar: la fuente vuelve sola y la noticia no espera.
    """


class FeedUnreadable(IngestorError):
    """La fuente respondió pero lo que respondió no se entiende.

    Es el fallo del scraper, y es el que más se va a dar: Forex Factory no ofrece
    API, así que se parsea markdown con expresiones regulares. Un cambio de formato
    rompe el parseo, no la red.
    """


# ---------------------------------------------------------------------------
# La forma normalizada
# ---------------------------------------------------------------------------


def reading(
    source: str,
    payload: Any = None,
    asof_ts: int = 0,
    stale: bool = False,
    reason: str = "",
) -> Dict[str, Any]:
    """Construye un `MacroReading` con la forma de la que habla este módulo.

    Los parámetros por defecto son los de "no hay dato" a propósito: se puede
    llamar con dos argumentos y obtener una lectura vacía pero bien formada. Una
    lectura sin `source` no se puede atribuir a nadie, y sin `stale` explícito el
    consumidor no puede distinguir fresco de cacheado.
    """
    return {
        "source": source,
        "asof_ts": int(asof_ts or 0),
        "payload": payload,
        "stale": bool(stale),
        "reason": str(reason or ""),
    }


def neutro(source: str, reason: str, asof_ts: int = 0) -> Dict[str, Any]:
    """Lectura explícitamente vacía, con el motivo.

    Existe como función porque "no hay dato" es un valor que se va a devolver
    mucho —cada vez que la red falla— y escribirlo a mano cada vez es donde se
    cuela el `{"payload": last_value, "reason": ""}` que disfraza un dato viejo de
    uno fresco.
    """
    return reading(source=source, payload=None, asof_ts=asof_ts, stale=False, reason=reason)


# ---------------------------------------------------------------------------
# Caché con TTL y respaldo de dato viejo
# ---------------------------------------------------------------------------


class TTLCache(Generic[T]):
    """Caché con TTL, reloj inyectable y respaldo de lo viejo cuando falla el fetch.

    Existe porque los tres ingestores de REF tenían tres versiones de esto y solo
    una hacía lo correcto:

    - `smr_service` tenía TTL en memoria Y copia en disco, y servía la copia
      vieja **sin decir que era vieja**.
    - `ff_calendar` tenía TTL en memoria con respaldo de lo viejo, y tampoco
      marcaba la procedencia.
    - `cot_service` no tenía caché: pegaba a la CFTC en cada `sync()`.

    Lo que unifica aquí:

    - **`now` inyectable.** Con `time.time()` de módulo no se puede testear el
      vencimiento del TTL sin dormir, y un test que no puede comprobar la expiración
      no comprueba el TTL.
    - **El respaldo se marca `stale=True`.** Servir lo viejo es correcto; servirlo
      sin marcarlo es mentir sobre la frescura del dato.
    - **El fetch ocurre FUERA del lock.** Con el lock-around-fetch, una fuente lenta
      bloquea a todos los consumidores durante el timeout entero.
    """

    def __init__(
        self,
        fetch: Callable[[], T],
        ttl: float,
        now: Callable[[], float] = time.time,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"ttl debe ser > 0, no {ttl!r}.")
        self._fetch = fetch
        self._ttl = float(ttl)
        self._now = now
        self._lock = threading.Lock()
        self._value: Optional[T] = None
        self._stored_at: float = 0.0

    def get(self) -> "CacheResult[T]":
        """Devuelve el valor fresco, el viejo, o el motivo del fallo.

        Nunca lanza por un fallo del fetch: el llamador decide si un `FeedUnavailable`
        se propaga o se degrada. Devolver la excepción DENTRO del resultado, en vez
        de lanzarla, es lo que permite que los tres consumidores puedan degradar de
        forma distinta sin repetir el try/except en cada uno.
        """
        with self._lock:
            if self._value is not None and (self._now() - self._stored_at) < self._ttl:
                return CacheResult(value=self._value, fetched_at=self._stored_at, stale=False)

        # Fuera del lock a propósito: el fetch es lento y es de UNO.
        try:
            value = self._fetch()
        except Exception as exc:  # noqa: BLE001 - el llamador decide qué hacer
            with self._lock:
                if self._value is not None:
                    return CacheResult(
                        value=self._value,
                        fetched_at=self._stored_at,
                        stale=True,
                        error=exc,
                    )
            return CacheResult(value=None, fetched_at=0.0, stale=False, error=exc)

        with self._lock:
            self._value = value
            self._stored_at = self._now()
        return CacheResult(value=value, fetched_at=self._stored_at, stale=False)

    def invalidate(self) -> None:
        """Olvida el valor. Para tests y para forzar un refresco tras una caída."""
        with self._lock:
            self._value = None
            self._stored_at = 0.0


class CacheResult(Generic[T]):
    """Lo que devuelve `TTLCache.get()`.

    `error` va DENTRO del resultado y no como excepción porque hay dos reacciones
    distintas al mismo fallo: el DXY degrada a neutro con su copia en disco, y el
    gate de noticias degrada a fail-open. Si el fallo fuera una excepción, cada
    llamador tendría que capturar la excepción para aplicar su propia política.
    """

    __slots__ = ("value", "fetched_at", "stale", "error")

    def __init__(
        self,
        value: Optional[T],
        fetched_at: float,
        stale: bool = False,
        error: Optional[BaseException] = None,
    ) -> None:
        self.value = value
        self.fetched_at = fetched_at
        self.stale = stale
        self.error = error

    @property
    def ok(self) -> bool:
        """Hay un valor utilizable, aunque sea viejo."""
        return self.value is not None

    @property
    def fresh(self) -> bool:
        """Hay un valor Y no es un respaldo."""
        return self.value is not None and not self.stale

    def reason(self) -> str:
        """Explicación en texto para el humano, vacía si no hay nada que explicar."""
        if self.error is None:
            return ""
        return f"{type(self.error).__name__}: {self.error}"

    def age_s(self, now: float) -> float:
        """Antigüedad del valor en segundos. `0.0` si no hay valor."""
        if self.value is None:
            return 0.0
        return max(0.0, now - self.fetched_at)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def http_text(
    url: str,
    timeout: float = 30.0,
    headers: Optional[Dict[str, str]] = None,
    opener: Optional[Callable[..., Any]] = None,
) -> str:
    """Descarga texto plano. Es el ÚNICO punto del proyecto que habla HTTP crudo.

    Centralizarlo no es por estilo: es para que los tests puedan inyectar un
    `opener` falso sin monkeypatchear `urllib.request.urlopen` en tres módulos a la
    vez, y para que la política de reintentos y de User-Agent tenga un solo sitio.

    `urllib` y no `requests` a propósito: `requests` es una dependencia de la Fase 3
    solo para esto y para `ff_calendar`, y urllib está en la biblioteca estándar. La
    noticia de que una fuente bloquee por User-Agent se resuelve con un header, no
    con otra dependencia.

    Lanza `FeedUnavailable` (no `urllib.error.URLError`) para que quien llama no
    tenga que saber qué librería se usó.
    """
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers=headers or {"User-Agent": "trinity-trade/0.1"})
    getter = opener if opener is not None else urllib.request.urlopen
    try:
        with getter(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - se traduce todo a la familia propia
        raise FeedUnavailable(f"{url}: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# El contrato
# ---------------------------------------------------------------------------


@runtime_checkable
class MacroIngestor(Protocol):
    """Lo mínimo que se le puede pedir a cualquier fuente macro.

    Un método, `poll()`. Es deliberadamente más pequeño que `MarketDataAdapter`:
    un ingestor no tiene `symbols()` ni `spec()`, y obligar a que los tenga
    produciría un `symbols()` inventado que nadie consulta.
    """

    name: str
    #: Los símbolos a los que esta fuente aporta sesgo. `()` = todos: es lo
    #: correcto para el DXY y lo que hace que un COT solo de EURUSD quede acotado.
    symbols: tuple

    def poll(self) -> Dict[str, Any]:
        """Un `MacroReading`. NUNCA lanza por un fallo de red.

        El contrato es "degrada a neutro con el motivo escrito". Quien necesite
        saber si la fuente está viva mira `reason`, no captura la excepción.
        """
        ...


__all__ = [
    "CacheResult",
    "FeedUnavailable",
    "FeedUnreadable",
    "IngestorError",
    "MacroIngestor",
    "TTLCache",
    "http_text",
    "neutro",
    "reading",
]