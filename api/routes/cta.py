"""El CTA Swing D1 por HTTP: su perfil, su estado y un escaneo. No manda órdenes.

Dos rutas, espejo de las del watcher (D-077):

- `GET  /api/cta/status`: perfil, símbolos, motor y alertas ya emitidas. Lectura.
- `POST /api/cta/scan`: un ciclo de evaluación y auditoría en D1. **No manda órdenes.**

Por qué no hay ruta de auto-ejecución aquí
------------------------------------------
El watcher tiene `/api/watcher/auto-execute` para que el interruptor de REF encuentre
una respuesta honesta (`501`) en vez de un `404`. Este servicio no tiene interruptor:
nació en F4 sin camino de ejecución (D-077) y F5 decidirá cómo se activa la ejecución
multi-estrategia —por `ExecutionService`, con sus puertas—. Una ruta que simule un
interruptor para un sistema que aún no existe sería una pregunta sin respuesta.

Las rutas son delgadas: sin canales, sin ATR, ni dedup aquí. Eso es
`api/services/cta_alert_service.py` (orquestación) y `research/cta.py` (aritmética
convalidada en F2). Esta capa solo traduce y cablea puertos.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import deps

router = APIRouter(tags=["cta"])


def _servicio(rt: Any) -> Any:
    """El `CtaAlertService` de esta app, no uno nuevo por petición.

    El servicio deduplica las alertas en memoria por (símbolo, barra de señal). Si la
    ruta construyera uno nuevo en cada `POST /scan`, esa memoria moriría con el
    `return` y la misma barra D1 reescribiría su fila en cada llamada. La instancia
    vive en `Runtime.cta_service()`.

    Estas rutas no piden `deps.store` ni `deps.market`: el servicio ya sabe degradar
    y decirlo en la respuesta, y un `503` en `/status` no ayudaría a un panel que
    solo quiere saber si el CTA está configurado.
    """
    return rt.cta_service()


@router.get("/api/cta/status", response_model=None)
def cta_status(rt: Any = Depends(deps.runtime)) -> Dict[str, Any]:
    """El perfil del CTA, su motor, sus símbolos y las alertas emitidas.

    Incluye `auto_execute`, que sale **siempre a `false`**, con su motivo: es la
    lectura de un interruptor que controla algo que no existe hoy (las cero órdenes
    son estructurales, D-077).

    No pide token: es una lectura. Sin base de datos, `enabled` sale a `false` con el
    motivo, que es la verdad sobre un perfil que no se pudo leer.
    """
    return _servicio(rt).estado()


@router.post("/api/cta/scan", response_model=None)
def cta_scan(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Un ciclo: evalúa la última barra D1 cerrada de cada símbolo y audita.

    No acepta parámetros: qué mirar lo decide el perfil (`config/strategy_cta.yaml`),
    y una ruta que permitiera cambiarlo por petición sería configuración en el sitio
    que menos se revisa.

    Escribe en `setup_log`, así que **es una escritura y pide token**. Que no mande
    órdenes no la vuelve de lectura: quien puede escribir en la auditoría de
    operaciones es quien puede operar.
    """
    return _servicio(rt).escanear()


__all__ = ["router"]
