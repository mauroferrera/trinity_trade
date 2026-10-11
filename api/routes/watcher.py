"""El watcher por HTTP: qué se vigila, un escaneo y por qué no hay auto-ejecución.

Cuatro cosas
------------
- `GET  /api/watcher/status`: configuración y estado vigente. Lectura.
- `POST /api/watcher/scan`: un ciclo de evaluación y auditoría. **No manda órdenes.**
- `POST /api/watcher/auto-execute`: `501`. Existe para que el interruptor del panel
  tenga una respuesta honesta en vez de un `404` que parece un bug, y dice por qué.
- Y el bucle en segundo plano (F6, D-081):
  `GET /api/watcher/loop/status` · `POST /api/watcher/loop/start` ·
  `POST /api/watcher/loop/stop`. Opt-in: por defecto la app no arranca ninguna tarea;
  se enciende con `watcher.background_loop: true` en el YAML o con `start` desde aquí.

Por qué el 501 y no un 200 con `{"ok": false}`
---------------------------------------------
El `static/main.js` de REF hace `res.ok ? res.json() : reject(...)` y, con un 200,
enseñaría "Watcher auto-ejecutar ACTIVADO" después de encender un interruptor que no
encendió nada. El estado del interruptor tiene que venir del `GET /status`, que
publica `auto_execute: false` siempre. Un 501 con el motivo en el cuerpo es la única
respuesta que un panel no puede convertir en una mentira.

Las tres rutas del escaneo son delgadas: no hay score, ni zonas, ni dedup aquí. Eso es
`core/setup_gate.py`, `core/setup_lifecycle.py` y `api/services/watcher_service.py`. Esta
capa solo traduce, cablea puertos y elige el código de salida. Las rutas del bucle son
todavía más delgadas: `Runtime.watcher_loop()` ya lleva el `escanear` enchufado, y aquí
solo se traduce un botón en `arrancar()`/`parar()` y un panel en `estado()`.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import deps

router = APIRouter(tags=["watcher"])


class AutoExecuteBody(BaseModel):
    """Lo que el interruptor del panel manda.

    `enabled` es obligatorio y no tiene valor por defecto a propósito. Un interruptor
    que se enciende sin decir a qué se refiere es un interruptor que alguien activó sin
    querer; que el cuerpo no valide es un `422` y el checkbox se queda como estaba.
    """

    enabled: bool


def _servicio(rt: Any) -> Any:
    """El `WatcherService` de esta app, no uno nuevo por petición.

    El servicio deduplica los descartes en memoria (firma de rechazo por
    símbolo/timeframe). Si la ruta lo construyese en cada `POST /scan`, esa memoria
    moriría con el `return` y un setup por debajo del umbral escribiría una fila de
    `setup_log` por ciclo, para siempre, sin que nada fallara. La instancia vive en
    `Runtime.watcher_service()`.

    Estas rutas no piden `deps.store` ni `deps.market`: el servicio ya sabe qué hacer
    sin ellos —degradar y decirlo en la respuesta— y un `503` en `/status` no
    ayudaría a un panel que solo quiere saber si el watcher está configurado.
    """
    return rt.watcher_service()


@router.get("/api/watcher/status", response_model=None)
def watcher_status(rt: Any = Depends(deps.runtime)) -> Dict[str, Any]:
    """Qué está vigilando el watcher y en qué estado está cada símbolo.

    Incluye `auto_execute`, que sale **siempre a `false`** aunque `strategy.yaml` lo pida,
    junto con `auto_execute_conf` (lo que el YAML dice) y `auto_execute_disponible` (si
    hay código que lo haga). Los tres juntos son la diferencia entre "está apagado" y
    "no existe": un panel que solo publicara el valor del YAML rotularía
    "AUTO-EJECUCIÓN" sobre un sistema que no manda nada.

    No pide token: es una lectura. Y no pide base de datos: sin ella, `enabled` sale
    `False` con la lista de símbolos vacía, que es la verdad sobre un sistema que no
    puede vigilar nada.
    """
    return _servicio(rt).estado()


@router.post("/api/watcher/scan", response_model=None)
def watcher_scan(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Un ciclo: evalúa cada símbolo, audita y devuelve los eventos.

    No acepta parámetros a propósito. `auto_execute` no es un flag de esta ruta: la
    ejecución no se puede encender desde aquí, y una ruta que parece encenderla es
    justo lo que hay que evitar.

    Escribe en `setup_log` y en `setup_state`, así que **es una escritura y pide token**.
    Que no mande órdenes no la vuelve de lectura: escribe en la auditoría de operaciones y
    quien puede escribir en la auditoría de operaciones es quien puede operar.
    """
    return _servicio(rt).escanear()


@router.post("/api/watcher/auto-execute", response_model=None)
def watcher_auto_execute(
    body: AutoExecuteBody,
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """No está implementado, y el cuerpo lo dice.

    501 en los dos casos, encendido o apagado:

    - `enabled: false`: apagar algo que no se puede encender también es no-op. Un 200
      aquí sugeriría que el interruptor estaba de verdad en `true` y lo que hace este
      sistema es limpiar ese estado.
    - `enabled: true`: encenderlo exigiría el camino `ExecutionService` desde el escaneo
      (D-063). Encender una bandera sin ese camino solo produce un panel que miente.

    Se pide token antes de responder, y el orden importa: sin token esta ruta es `401`,
    no `501`. Decirle a cualquiera que la función no existe es documentación pública
    innecesaria; se lo dice a quien ya puede operar.
    """
    raise HTTPException(status_code=501, detail={
        "error": "auto_ejecutar no está implementado: el watcher no manda órdenes",
        "modo": "watcher solo evalúa y audita",
        "pide": body.enabled,
        "auto_execute": False,
        "dry_run": True,
        "por_que": (
            "La ejecución pasa por POST /api/trade/market -> ExecutionService, con "
            "lista blanca, riesgo del día, noticias, validate_entry y calidad de "
            "ejecución. Auto-ejecutar desde el escaneo exige escribir ese camino; "
            "encender la bandera sin él solo haría que el panel mienta."
        ),
    })


def _bucle(rt: Any) -> Any:
    """El `WatcherLoop` de esta app. El servicio dedup del bucle es el mismo que el
    de `POST /scan`: `scans` de `estado()` y una duplicación manual no comparten
    tarea ni nada, así que la idempotencia de `arrancar()`/`parar()` vive en el loop.
    """
    return rt.watcher_loop()


@router.get("/api/watcher/loop/status", response_model=None)
def watcher_loop_status(rt: Any = Depends(deps.runtime)) -> Dict[str, Any]:
    """Estado del bucle en segundo plano (F6, D-081): corre, cuándo y cuánto.

    Lectura sin token. `running` es la fuente de verdad de "¿corre ahora?"; `auto_start`
    dice si el YAML lo encendería (y `auto_start_key` qué key es), porque un bucle
    detenido por estar apagado de fábrica y uno detenido a mano se leen distinto en un
    panel. Sin base de datos ni mercado responde igual: `estado()` no necesita ninguno.
    """
    return _bucle(rt).estado()


@router.post("/api/watcher/loop/start", response_model=None)
async def watcher_loop_start(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Arranca el bucle ahora (si no corre ya). **No lo enciende para siempre.**

    La tarea del bucle vive en este proceso: apagada la app, se va. Que el bucle
    arranque junto con la app en el futuro es el flag `watcher.background_loop` del
    YAML, no esta ruta. `ya_corria` distingue el `start` repetido del primero.

    Escribe de verdad (crea una tarea), así que es una escritura y pide token.
    """
    return await _bucle(rt).arrancar()


@router.post("/api/watcher/loop/stop", response_model=None)
async def watcher_loop_stop(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Para el bucle (si corre). Idempotente: parar dos veces devuelve igual.

    Escritura (cancela la tarea): pide token.
    """
    return await _bucle(rt).parar()


__all__ = ["AutoExecuteBody", "router"]
