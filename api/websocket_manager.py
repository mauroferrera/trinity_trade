"""El registro de conexiones WebSocket del proceso.

Qué arregla respecto a REF
-------------------------
`REF/app.py` tenía el `ConnectionManager` (líneas 461-489) y, por separado, una
variable de módulo `_main_loop` que se rellenaba en el `@app.on_event("startup")`
y que `_on_record` usaba para lanzar el broadcast desde el hilo del listener:

    asyncio.run_coroutine_threadsafe(manager.broadcast(payload), loop)

Tres cosas que se pierden al tener el loop fuera del manager:

1. **El broadcast caía en silencio.** Sin loop (arranque sin `startup`, test, o
   apagado) el `RuntimeError` se tragaba con `except RuntimeError: pass` y el
   mensaje no llegaba a ningún cliente sin que nadie lo notara. Aquí el envío sin
   loop es un contador que sube, no una excepción muda.
2. **No había forma de saber si el WS servía de algo.** `len(manager.active)` era
   lo único observable. Aquí hay clientes, enviados, drops y fallos, y `/api/health`
   los publica.
3. **El manager no sabía de quién dependía.** La lista de conexiones era global
   del módulo, así que dos apps en el mismo proceso (tests) compartían clientes
   cruzados. Aquí el manager es un objeto y la app lo recibe.

El bucle se engancha con `attach_loop()` en el arranque y la lista de conexiones es
propiedad de la instancia, no del módulo. Un `ConnectionManager()` nuevo no tiene
nada: por eso un test no necesita limpiar nada.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List, Optional


class ConnectionManager:
    """Clientes conectados y difusión de mensajes.

    `send` nunca propaga el fallo de un cliente: un socket cerrado deja de estar en
    la lista y el resto de los clientes siguen recibiendo. Un `raise` aquí
    convertiría el fallo de una pestaña cerrada en un 500 del endpoint que está
    emitiendo, que es como un cliente se tira el broadcast de todos.
    """

    __slots__ = ("_active", "_lock", "_loop", "enviados", "drops", "fallos")

    def __init__(self) -> None:
        self._active: List[Any] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.enviados = 0
        self.drops = 0
        self.fallos = 0

    # -- ciclo de vida ----------------------------------------------------------

    def attach_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Engancha el bucle de eventos para poder emitir desde otros hilos."""
        self._loop = loop

    # -- conexiones -------------------------------------------------------------

    async def connect(self, ws: Any) -> None:
        await ws.accept()
        with self._lock:
            self._active.append(ws)

    def disconnect(self, ws: Any) -> None:
        with self._lock:
            if ws in self._active:
                self._active.remove(ws)
                self.drops += 1

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._active)

    # -- envío ------------------------------------------------------------------

    async def send(self, ws: Any, message: Dict[str, Any]) -> bool:
        """Envía a UN cliente. Devuelve si el mensaje llegó.

        `False` significa "ya no está": el socket se da de baja y el siguiente
        broadcast no lo intentará otra vez.
        """
        try:
            await ws.send_json(message)
        except Exception:  # noqa: BLE001 - un cliente roto no es un error del sistema
            self.fallos += 1
            self.disconnect(ws)
            return False
        self.enviados += 1
        return True

    async def broadcast(self, message: Dict[str, Any]) -> int:
        """Envía a todos los clientes. Devuelve cuántos lo recibieron."""
        with self._lock:
            targets = list(self._active)
        recibidos = 0
        for ws in targets:
            if await self.send(ws, message):
                recibidos += 1
        return recibidos

    def emit(self, message: Dict[str, Any]) -> int:
        """Difunde desde CUALQUIER hilo, sincronizado o no.

        Es la puerta que usan los hilos: el listener de cinta, el watcher y las
        tareas de fondo no son corrutinas y no pueden hacer `await`. En REF eso
        obligaba a que cada uno guardara el loop en una global de módulo; aquí el
        loop es del manager y `emit` hace el reparto:

        - con bucle enganchado y desde otro hilo: `run_coroutine_threadsafe`.
        - desde el propio hilo del bucle: se encola una tarea (el `await` directo
          bloquearía el bucle en un `broadcast` awaitado desde dentro).
        - sin bucle: el mensaje se cuenta como perdido y se devuelve 0.

        Que el caso "sin bucle" devuelva 0 en vez de reventar es deliberado: el
        emisor es un hilo de datos que no puede hacer otra cosa que continuar, y
        perder un tick de un cliente que aún no está conectado no es un fallo.
        """

        loop = self._loop
        if loop is None or loop.is_closed():
            with self._lock:
                self.drops += 1
            return 0
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self.broadcast(message))
            return 1
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(message), loop)
        except RuntimeError:
            with self._lock:
                self.drops += 1
            return 0
        return 1

    # -- observabilidad ---------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """Contadores para `/api/health`.

        Lo que se afirma es lo que se mide: cuántos clientes hay, cuántos mensajes
        les han llegado, cuántos se perdieron sin destinatario y cuántos fallaron al
        escribirse. No hay medidas de latencia porque no las hay.
        """
        return {
            "clientes": self.clients,
            "enviados": self.enviados,
            "drops": self.drops,
            "fallos": self.fallos,
            "loop": self._loop is not None and not self._loop.is_closed(),
        }

    def __repr__(self) -> str:  # pragma: no cover - ayuda de depuración
        return "<ConnectionManager clientes={0} enviados={1}>".format(
            self.clients, self.enviados
        )


__all__ = ["ConnectionManager"]
