"""El bucle en segundo plano del watcher (F6, D-081): escanea, audita y NO ejecuta.

Qué es
------
Un proceso que se queda en `escanear()` y `auditar` solos, sin nadie pulsando nada,
es exactamente el hueco de la fase: el watcher se puede disparar a mano desde el
panel, pero "estar a la escucha de verdad" es otra cosa. Este módulo es ese
bucle — una tarea asyncio OPT-IN que repite `WatcherService.escanear()` cada
`scan_interval_sec` y deja constancia de su propia vida (`estado()`), para que un
operador vea cuántos ciclos corrieron y si el último falló.

Tres decisiones (todas de D-081):

1. **Opt-in y por defecto APAGADO.** La tarea no se crea en el arranque a menos que
   `watcher.background_loop` esté a `true` en `strategy.yaml`. Un proceso que empieza
   a escanear solo en cuanto existe es un proceso que nadie les estaba mirando cuando
   arrancó a escanear: la regla de D-063 sobre la auto-ejecución tenía razón en no
   ser configurable, y la extensión natural (un bucle) tiene que ser encendible
   EXPLÍCITO —con la key en un fichero revisable— y no un efecto lateral de levantar
   la app. Cuando el flag está a `false`, el arranque no construye ni la tarea ni el
   `WatcherService`: `import Runtime` y `create_app()` no dejan ni rastro del bucle.
2. **NO ejecuta.** El bucle llama a `escanear()`, y ese método no puede mandar
   órdenes (D-063): `auto_execute` no existe como camino aquí. El bucle es el reloj
   del watcher, no un auto-operator.
3. **No decide por sí mismo.** No hay policy de reintento ni backoff en el bucle:
   un ciclo que revienta se registra (`last_error`) y el siguiente corre en su
   intervalo. Un bucle que decide cuándo saltarse un ciclo o cómo volver a intentarlo
   sería un segundo sistema de gobierno del riesgo al lado de `escanear()`.

El intervalo se relee del YAML en CADA ciclo, no al arrancar: cambiar
`scan_interval_sec` mientras la app corre se aplica al siguiente ciclo sin reiniciar,
que es lo que un fichero de config se supone que puede hacer.

Colaboradores inyectables (el mismo patrón que el resto de servicios): `escanear` es
un callable (`WatcherService.escanear`) y `config` un callable que devuelve la
sección `watcher` raw del YAML. Un test monta el bucle con dos `lambda` y no toca ni
`settings/` ni el `Runtime`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

#: La key del YAML (sección `watcher`) que enciende el bucle en el arranque. Default
#: `false`: ver el docstring del módulo sobre por qué es opt-in.
KEY_AUTOSTART = "background_loop"

_INTERVALO_DEFECTO = 60.0


def _config_fabrica() -> Dict[str, Any]:
    """La sección `watcher` CRUDA de `strategy.yaml`, con los defaults del bucle.

    Se lee la sección RAW (no la plana de `core.strategy`): el schema plano no
    conoce `background_loop` —es un interruptor de bucle, no una regla de trading— y
    `_parse_watcher` lo descarta. Un fallo de lectura deja `{}`: el bucle no puede
    arrancar solo sin config, y `estado()` lo publica.
    """
    from settings import strategy_source  # noqa: PLC0415 - tardío (settings/lee el YAML)

    try:
        seccion = dict(strategy_source.get_watcher_section() or {})
    except Exception as exc:  # noqa: BLE001 - sin config el bucle no se inventa una
        log.warning("no se pudo leer la sección watcher del YAML: %s", exc)
        return {}
    return {
        "enabled": bool(seccion.get("enabled", True)),
        "scan_interval_sec": seccion.get("scan_interval_sec", _INTERVALO_DEFECTO),
        KEY_AUTOSTART: bool(seccion.get(KEY_AUTOSTART, False)),
    }


def autoarranque_configurado() -> bool:
    """Si `watcher.background_loop` está a `true` ahora mismo (para el lifespan)."""
    return bool(_config_fabrica().get(KEY_AUTOSTART))


def _intervalo(config: Dict[str, Any]) -> float:
    """El intervalo del próximo ciclo, acotado a >= 1 s (un `sleep(0)` sería un
    spin loop contra el mercado)."""
    valor = float(config.get("scan_interval_sec") or _INTERVALO_DEFECTO)
    return valor if valor >= 1.0 else _INTERVALO_DEFECTO


class WatcherLoop:
    """La tarea asyncio que repite `escanear()`. Arranque y parada explícitos.

    La clase es la pieza probable del bucle: `arrancar()`/`parar()`/`estado()` y el
    ciclo en `_ciclo()`. No toca `asyncio` hasta que alguien llama a `arrancar()`
    (que necesitan un loop corriendo): construir un `WatcherLoop` —y por tanto
    `Runtime.watcher_loop()`— no crea ninguna tarea.

    `parar()` es idempotente y no lanza: apagar el bucle en el `shutdown` de la app
    no puede ser el motivo por el que la app no apague.
    """

    def __init__(
        self,
        escanear: Callable[[], Dict[str, Any]],
        config: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        if not callable(escanear):
            raise TypeError("escanear debe ser un callable (WatcherService.escanear)")
        self._escanear = escanear
        self._config = config if config is not None else _config_fabrica
        #: La tarea del ciclo, o `None`. La única fuente de verdad de "¿corre?": un
        #: `estado()` no debe adivinar el estado por el reloj ni por el contador.
        self._tarea: Optional[asyncio.Task] = None
        self._inicio: Optional[datetime] = None
        self._ultimo_scan: Optional[datetime] = None
        self._escaneos = 0
        self._ultimo_error: Optional[str] = None
        self._ultimos_eventos = 0
        self._cerrando = False

    # -- estado ------------------------------------------------------------------

    @property
    def esta_corriendo(self) -> bool:
        return self._tarea is not None and not self._tarea.done()

    def estado(self) -> Dict[str, Any]:
        """Qué pasó con el bucle y por qué no está (si no está). Lectura pura."""
        cfg = self._config() or {}
        return {
            "running": self.esta_corriendo,
            "started_at": self._inicio.isoformat() if self._inicio else None,
            "last_scan_at": self._ultimo_scan.isoformat() if self._ultimo_scan else None,
            "scans": self._escaneos,
            "last_events": self._ultimos_eventos,
            "last_error": self._ultimo_error,
            # Lo que el YAML pide: el bucle podría no correr porque el flag está
            # apagado (default) o porque alguien lo paró; los dos estados se leen
            # distinto en un panel.
            "auto_start": bool(cfg.get(KEY_AUTOSTART)),
            "auto_start_key": KEY_AUTOSTART,
            "scan_interval_sec": cfg.get("scan_interval_sec", _INTERVALO_DEFECTO),
            "enabled": bool(cfg.get("enabled", True)),
            "motivo": (
                None if self.esta_corriendo else
                "el bucle está detenido: `POST /api/watcher/loop/start` lo arranca, "
                "o `watcher.{0}: true` en strategy.yaml lo arranca con la app".format(
                    KEY_AUTOSTART)
            ),
        }

    # -- ciclo de vida -----------------------------------------------------------

    async def arrancar(self) -> Dict[str, Any]:
        """Crea la tarea del ciclo si no hay una corriendo. Idempotente.

        Devuelve el estado tras el arranque (`ya_corria` distingue la llamada
        repetida del primer arranque). Requiere un loop de asyncio: se llama desde
        una ruta `async` o desde `Runtime.startup`.
        """
        if self.esta_corriendo:
            return {**self.estado(), "ok": True, "ya_corria": True}
        if self._cerrando:
            return {**self.estado(), "ok": False,
                    "motivo": "el bucle se está cerrando: reinténtalo en un momento"}
        loop = asyncio.get_running_loop()
        self._tarea = loop.create_task(self._ciclo())
        self._inicio = datetime.now(timezone.utc)
        return {**self.estado(), "ok": True, "ya_corria": False}

    async def parar(self) -> Dict[str, Any]:
        """Cancela la tarea y espera a que se detenga. Idempotente y sin fallos."""
        tarea = self._tarea
        self._tarea = None
        self._cerrando = True
        if tarea is not None and not tarea.done():
            tarea.cancel()
            try:
                await tarea
            except asyncio.CancelledError:
                pass
        self._cerrando = False
        return {**self.estado(), "ok": True}

    # -- el ciclo ----------------------------------------------------------------

    async def _ciclo(self) -> None:
        """El bucle: escanea, registra el desenlace y duerme hasta el siguiente ciclo.

        Cada iteración relee la config (el intervalo puede cambiar sin reiniciar) y
        envuelve el escaneo en su propio `try`: un ciclo que revienta NO mata el
        bucle, se registra en `last_error` y el siguiente corre. El bucle no reintenta
        un ciclo fallido más allá de la espera normal: reintentar con backoff sería
        una política de decisión aquí, y eso es de `escanear()` (ver docstring).
        """
        while True:
            cfg = self._config() or {}
            try:
                resultado = self._escanear() or {}
                self._escaneos += 1
                self._ultimo_scan = datetime.now(timezone.utc)
                self._ultimo_error = None
                self._ultimos_eventos = len(resultado.get("events") or [])
            except Exception as exc:  # noqa: BLE001 - un ciclo roto no mata el bucle
                log.warning("escaneo en segundo plano revintió: %s", exc)
                self._ultimo_error = "{0}: {1}".format(type(exc).__name__, exc)
            await asyncio.sleep(_intervalo(cfg))


__all__ = ["KEY_AUTOSTART", "WatcherLoop", "autoarranque_configurado"]