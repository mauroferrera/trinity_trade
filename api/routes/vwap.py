"""El Mean Reversion a VWAP D1 por HTTP (F6, D-080): perfil, estado y escaneo.

Dos rutas, la primera de lectura y la segunda de escritura:

- `GET  /api/vwap/status`: perfil, motor, símbolos, régimen e interruptor de
  ejecución (que es SIEMPRE falso y publica el motivo).
- `POST /api/vwap/scan`: un ciclo de evaluación en D1. Audita en `setup_log` con
  magic 8882028 y perfil `vwap`. Solo alerta cuando `regime() == "rango"` y NUNCA
  manda una orden: este perfil no tiene puerto de ejecución (`AUTO_EJECUCION_
  DISPONIBLE`), y la explicación completa está en
  `api/services/vwap_alert_service.py`.

No hay `/api/vwap/auto-execute` ni `/api/vwap/trail`: no hay nada que encender ni
stops que mover. Las salidas son niveles fijos en la apertura (target = VWAP,
stop = k·ATR, `max_hold`) y no existe trailing que mantener.

Las rutas son delgadas: sin canales, sin VWAP, sin régiimen aquí. Eso es
`api/services/vwap_alert_service.py` (orquestación) y `research/mr.py` (aritmética
convalidada en F3). Esta capa solo traduce y cablea puertos.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import deps

router = APIRouter(tags=["vwap"])


def _servicio(rt: Any) -> Any:
    """El `VwapAlertService` de esta app, no uno nuevo por petición.

    El servicio deduplica las alertas en memoria por (símbolo, barra de señal). Si la
    ruta construyera uno nuevo en cada `POST /scan`, esa memoria moriría con el
    `return` y la misma barra D1 reescribiría su fila en cada llamada. La instancia
    vive en `Runtime.vwap_service()`.

    Estas rutas no piden `deps.store` ni `deps.market`: el servicio ya sabe degradar
    y decirlo en la respuesta, y un `503` en `/status` no ayudaría a un panel que
    solo quiere saber si el MR VWAP está configurado.
    """
    return rt.vwap_service()


@router.get("/api/vwap/status", response_model=None)
def vwap_status(rt: Any = Depends(deps.runtime)) -> Dict[str, Any]:
    """El perfil del MR VWAP, su motor, sus símbolos y las alertas emitidas.

    Incluye `auto_execute` (que es SIEMPRE falso: este perfil no sabe ejecutar) con
    `auto_execute_motivo` diciendo por qué: `estado()` es la verdad sobre si este
    ciclo manda órdenes o solo alerta, y en F6 la verdad es que solo alerta.

    No pide token: es una lectura. Sin base de datos, `enabled` sale a `false` con el
    motivo, que es la verdad sobre un perfil que no se pudo leer.
    """
    return _servicio(rt).estado()


@router.post("/api/vwap/scan", response_model=None)
def vwap_scan(
    rt: Any = Depends(deps.runtime),
    _auth: None = Depends(deps.require_api_token),
) -> Dict[str, Any]:
    """Un ciclo: evalúa el régimen y la última barra D1 cerrada de cada símbolo.

    No acepta parámetros: qué mirar lo decide el perfil (`config/strategy_vwap.yaml`),
    y una ruta que permitiera cambiarlo por petición sería configuración en el sitio
    que menos se revisa.

    Escribe en `setup_log`, así que **es una escritura y pide token**. Que hoy mande
    cero órdenes no la vuelve de lectura: quien puede escribir en la auditoría de
    operaciones es quien puede operar.
    """
    return _servicio(rt).escanear()


__all__ = ["router"]