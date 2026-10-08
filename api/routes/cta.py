"""El CTA Swing D1 por HTTP: perfil, estado, escaneo y trailing de salidas.

Tres rutas: la primera de lectura y las otras dos de escritura (D-077, D-078):

- `GET  /api/cta/status`: perfil, símbolos, motor y alertas ya emitidas. Lectura.
- `POST /api/cta/scan`: un ciclo de evaluación en D1. Audita en `setup_log` y,
  **solo si el YAML lo pide y hay puerto**, manda la orden por `ExecutionService`.
- `POST /api/cta/trail`: mueve los stops de las posiciones CTA con la política D1
  convalidada (`core/exit_policy.py`). Sin puerto dice por qué, sin revientar.

El escaneo cumple cero órdenes mientras `strategy_cta.yaml` no tenga
`auto_execute: true` (o el perfil esté deshabilitado, o no haya puerto): el
interruptor compone tres condiciones y `estado()` publica cuál falta. No hay ruta
de "auto-ejecutar" aquí como la del watcher porque el interruptor es del servicio,
no de la ruta: `/api/cta/status` ya lo devuelve, y un `POST` que lo cambiara
sería configuración en el sitio que menos se revisa.

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

    Incluye `auto_execute` (el interruptor EFECTIVO: YAML && perfil habilitado &&
    puerto) con `auto_execute_motivo` diciendo cuál falta cuando está apagado:
    `estado()` es la verdad sobre si este ciclo manda órdenes o solo alerta.

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

    Escribe en `setup_log` y **puede mandar una orden** (si `auto_execute` está
    encendido), así que **es una escritura y pide token**. Que hoy mande cero órdenes
    no la vuelve de lectura: quien puede escribir en la auditoría de operaciones es
    quien puede operar.
    """
    return _servicio(rt).escanear()


@router.post("/api/cta/trail", response_model=None)
def cta_trail(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Un pase de trailing: mueve los stops de las posiciones CTA con el chandelier.

    No acepta parámetros: el motor es el del perfil (`exit_policy`) y las posiciones
    se piden filtrando por el magic CTA. Un pase correcto con cero posiciones devuelve
    `positions: 0` sin error, que es el caso normal de un D1.

    Modifica stops en el bróker, así que **es una escritura y pide token**. Nunca
    lanza: cualquier fallo (sin puerto, un símbolo sin datos, un broker que rechaza)
    sale en el cuerpo con su motivo, por si el bucle del operador quiere leerlo.
    """
    return _servicio(rt).gestionar_salidas()


__all__ = ["router"]
