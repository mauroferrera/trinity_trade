"""Tests del gestor de conexiones WebSocket.

Por qué existen
---------------
`ConnectionManager` nació como un objeto con estado a nivel de módulo en la
referencia (`ws_manager = ConnectionManager()` en el import). Eso funciona con UN
proceso, y en cuanto hay dos (uvicorn --reload, o dos workers) cada uno tiene su
propia copia: un cliente conectado al worker A no recibe los mensajes que
publica el worker B, y el frontend se queda quieto sin que nada falle.

El cambio a estado por instancia (D-034) es lo que hay que demostrar aquí. La
prueba dura: dos managers NO se ven entre sí. Con el manager global, un test así
no se puede ni escribir.

Lo que se afirma
----------------
1. Aislamiento: dos instancias no comparten clientes.
2. Entrega: `send`/`broadcast` dicen si el mensaje llegó, y `broadcast` cuenta.
3. Muerte de una conexión: si el envío a un cliente caído falla, se da de baja y
   el resto de los clientes sigue recibiendo (un zombi no puede tumbar el bus).
4. `emit` es la puerta de los HILOS: sin bucle enganchado devuelve 0 y lo cuenta
   como perdido, en vez de tragarse la excepción como hacía REF.
5. `stats()` publica lo que `/api/health` promete.

Los tests son SÍNCRONOS con `asyncio.run(...)`, como el resto de la suite: no hay
`pytest-asyncio` en el entorno, y añadir un plugin para probar veinte líneas de
`await` no compensa.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict, List

import pytest

from api.websocket_manager import ConnectionManager


def run(coro: Any) -> Any:
    """Ejecuta la corrutina y devuelve su resultado."""
    return asyncio.run(coro)


class FakeWebSocket:
    """Doble de un `WebSocket` de FastAPI.

    `send_json` puede fallar a propósito: en producción un cliente que se acaba de
    desconectar lanza `RuntimeError` en el envío, y ese es justo el caso que el
    bus tiene que sobrevivir.
    """

    def __init__(self, falla: bool = False) -> None:
        self.aceptado = False
        self.enviados: List[Dict[str, Any]] = []
        self._falla = falla

    async def accept(self) -> None:
        self.aceptado = True

    async def send_json(self, data: Any) -> None:
        if self._falla:
            raise RuntimeError("WebSocket is disconnected")
        self.enviados.append(data)


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


class TestConexiones:
    def test_connect_acepta_y_registra(self, manager: ConnectionManager) -> None:
        ws = FakeWebSocket()

        run(manager.connect(ws))

        assert ws.aceptado, "connect() debe aceptar el socket, no solo contarlo"
        assert manager.clients == 1

    def test_dos_managers_no_se_ven(self, manager: ConnectionManager) -> None:
        """El test que la variable global de la referencia hace imposible."""
        otro = ConnectionManager()

        run(manager.connect(FakeWebSocket()))

        assert manager.clients == 1
        assert otro.clients == 0

    def test_disconnect_quita_el_cliente(self, manager: ConnectionManager) -> None:
        ws = FakeWebSocket()
        run(manager.connect(ws))

        manager.disconnect(ws)

        assert manager.clients == 0
        assert manager.drops == 1

    def test_disconnect_es_idempotente(self, manager: ConnectionManager) -> None:
        """El `finally` de la ruta lo llama siempre; debe poder repetir."""
        ws = FakeWebSocket()
        run(manager.connect(ws))

        manager.disconnect(ws)
        manager.disconnect(ws)

        assert manager.clients == 0
        assert manager.drops == 1, "sacar dos veces no es dos bajas"

    def test_disconnect_de_un_ajeno_no_toca_la_lista(self, manager: ConnectionManager) -> None:
        """Sacar a quien no está no puede vaciar a los que sí."""
        registrado = FakeWebSocket()
        run(manager.connect(registrado))

        manager.disconnect(FakeWebSocket())

        assert manager.clients == 1


class TestEnvio:
    def test_send_devuelve_true_cuando_llega(self, manager: ConnectionManager) -> None:
        ws = FakeWebSocket()

        ok = run(manager.send(ws, {"type": "price", "price": 1.1}))

        assert ok is True
        assert ws.enviados == [{"type": "price", "price": 1.1}]
        assert manager.enviados == 1

    def test_send_a_un_cliente_caido_lo_da_de_baja(self, manager: ConnectionManager) -> None:
        """Un socket que falla al escribir ya no está: se limpia solo.

        Sin la baja automática, cada publicación futura insiste contra un socket
        muerto y cada intento es un `RuntimeError` tragado. Es un goteo, no un fallo.
        """
        zombi = FakeWebSocket(falla=True)

        ok = run(manager.send(zombi, {"type": "price"}))

        assert ok is False
        assert manager.fallos == 1
        assert manager.clients == 0

    def test_send_no_propaga_la_excepcion_del_cliente(self, manager: ConnectionManager) -> None:
        """El fallo se devuelve como `False`, nunca como excepción.

        Si `send` levantara, el `RuntimeError` del cliente caído subiría por el
        endpoint que está emitiendo y lo convertiría en un 500.
        """
        ok = run(manager.send(FakeWebSocket(falla=True), {"type": "price"}))

        assert ok is False

    def test_broadcast_llega_a_todos(self, manager: ConnectionManager) -> None:
        a, b = FakeWebSocket(), FakeWebSocket()
        run(manager.connect(a))
        run(manager.connect(b))

        recibidos = run(manager.broadcast({"type": "price", "price": 1.085}))

        assert len(a.enviados) == len(b.enviados) == 1
        assert a.enviados[0]["price"] == 1.085
        assert recibidos == 2

    def test_un_cliente_caido_no_tumba_al_resto(self, manager: ConnectionManager) -> None:
        """El bus sobrevive a un zombi y sigue contando al que sí recibió."""
        zombi = FakeWebSocket(falla=True)
        sano = FakeWebSocket()
        run(manager.connect(zombi))
        run(manager.connect(sano))

        recibidos = run(manager.broadcast({"type": "price", "price": 1.2}))

        assert sano.enviados, "el cliente sano debe recibir igualmente"
        assert recibidos == 1
        assert manager.clients == 1, "el zombi se da de baja al primer fallo"

    def test_broadcast_sin_clientes_devuelve_cero(self, manager: ConnectionManager) -> None:
        """Sin clientes NO es un error: es el estado normal entre ráfagas."""
        assert run(manager.broadcast({"type": "price"})) == 0

    def test_broadcast_deja_pasar_estructuras_anidadas(self, manager: ConnectionManager) -> None:
        """CVD, alertas y config anidada viajan por el mismo canal.

        Si el bus serializara a texto, estos payloads dejarían de ser parseables en
        el frontend; por eso se prueba con algo anidado de verdad.
        """
        ws = FakeWebSocket()
        run(manager.connect(ws))
        payload = {
            "type": "cvd",
            "points": [{"time": 1, "value": -3.5}, {"time": 2, "value": 4.0}],
            "meta": {"source": "of_eurusd_m5", "tf": "M5"},
        }

        run(manager.broadcast(payload))

        assert ws.enviados[0]["meta"]["source"] == "of_eurusd_m5"

    def test_mensaje_vacio_tambien_se_entrega(self, manager: ConnectionManager) -> None:
        """`{}` es un payload válido (p. ej. un keepalive)."""
        ws = FakeWebSocket()
        run(manager.connect(ws))

        run(manager.broadcast({}))

        assert ws.enviados == [{}]


class TestEmitDesdeHilos:
    def test_sin_bucle_devuelve_cero_y_lo_cuenta(self, manager: ConnectionManager) -> None:
        """El caso que REF se tragaba con `except RuntimeError: pass`.

        Sin bucle enganchado no hay a quién repartir. Devolver 0 y subir `drops` lo
        hace visible en `/api/health` en vez de dejarlo como un silencio.
        """
        ws = FakeWebSocket()
        run(manager.connect(ws))

        assert manager.emit({"type": "price"}) == 0
        assert manager.drops == 1
        assert ws.enviados == []

    def test_desde_el_propio_hilo_del_bucle_encola(self, manager: ConnectionManager) -> None:
        """`await manager.broadcast(...)` DENTRO del bucle no puede esperar.

        Si `emit` hiciera el reparto aquí, el bucle se bloquearía esperando una
        corrutina que solo puede correr cuando él mismo vuelva: deadlock.
        """
        ws = FakeWebSocket()

        async def _main() -> None:
            manager.attach_loop(asyncio.get_running_loop())
            await manager.connect(ws)
            manager.emit({"type": "price", "price": 1.3})
            # Se da margen al task creado para que llegue a ejecutarse.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if ws.enviados:
                    break

        run(_main())

        assert ws.enviados == [{"type": "price", "price": 1.3}]
        assert manager.clients == 1

    def test_desde_otro_hilo_reparte(self, manager: ConnectionManager) -> None:
        """El camino real del listener de cinta y del watcher: un hilo, no un await."""
        ws = FakeWebSocket()

        async def _main() -> None:
            manager.attach_loop(asyncio.get_running_loop())
            await manager.connect(ws)

            def desde_hilo() -> None:
                manager.emit({"type": "cvd", "points": []})

            hilo = threading.Thread(target=desde_hilo)
            hilo.start()
            # El hilo termina; se espera a que su corrutina scheduled se ejecute.
            for _ in range(50):
                await asyncio.sleep(0.01)
                if ws.enviados:
                    break
            hilo.join(timeout=2)

        run(_main())

        assert ws.enviados == [{"type": "cvd", "points": []}]
        assert manager.enviados == 1

    def test_bucle_cerrado_cuenta_caida(self, manager: ConnectionManager) -> None:
        """Apagar el bucle sin desengancharlo es un estado real (shutdown)."""
        loop = asyncio.new_event_loop()
        loop.close()
        manager.attach_loop(loop)

        assert manager.emit({"type": "price"}) == 0
        assert manager.drops == 1

    def test_bucle_cerrado_detectado_por_attach_explicito(self) -> None:
        """`attach_loop(None)` es cómo se desengancha: el estado vuelve a 0."""
        manager = ConnectionManager()
        manager.attach_loop(None)

        assert manager.emit({"type": "price"}) == 0


class TestObservabilidad:
    def test_stats_publica_lo_prometido(self, manager: ConnectionManager) -> None:
        ws = FakeWebSocket()
        run(manager.connect(ws))

        run(manager.broadcast({"type": "price"}))

        st = manager.stats()
        assert st["clientes"] == 1
        assert st["enviados"] == 1
        assert st["drops"] == 0
        assert st["fallos"] == 0
        assert st["loop"] is False, "sin attach_loop el bus no puede emitir a nadie"

    def test_stats_refleja_un_cliente_que_falla(self, manager: ConnectionManager) -> None:
        run(manager.connect(FakeWebSocket(falla=True)))

        run(manager.broadcast({"type": "price"}))

        st = manager.stats()
        assert st["fallos"] == 1
        assert st["clientes"] == 0

    def test_stats_con_bucle_vivo(self, manager: ConnectionManager) -> None:
        async def _main() -> None:
            manager.attach_loop(asyncio.get_running_loop())
            await asyncio.sleep(0)

        run(_main())

        # El bucle de `run` queda cerrado al salir, así que se cuenta como caído.
        assert manager.stats()["loop"] is False

    def test_repr_no_revienta(self, manager: ConnectionManager) -> None:
        run(manager.connect(FakeWebSocket()))

        assert "clientes=1" in repr(manager)

    def test_slots_evitan_ampliar_el_contrato(self, manager: ConnectionManager) -> None:
        """`__slots__` está para que un typo en producción sea un error de texto.

        Sin él, `manager.enviado = 3` (con una `s` de más) crearía un atributo
        nuevo, el contador real no subiría y el fallo sería invisible.
        """
        assert not hasattr(manager, "__dict__")