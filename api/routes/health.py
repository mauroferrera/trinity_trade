"""Rutas de estado: ¿qué está vivo, con qué config y a qué hora?

Las tres cosas que un operador mira antes que nada, y las tres contestan con
estados nombrados (`ok`, `sin_terminal`, `sin_puerto`, `error`) en vez de
booleanos: `market: false` no dice si el bróker está apagado o si la base está
caída, y esas dos cosas tienen una remediación distinta.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from .. import deps
from ..runtime import Runtime

router = APIRouter(tags=["estado"])


@router.get("/api/health", response_model=None)
def health(rt: Runtime = Depends(deps.runtime)) -> Dict[str, Any]:
    """El estado del proceso y de sus cables. Nunca devuelve 500.

    Es la ruta que un monitor consulta cada 30 s. Si `health` puede lanzar, el
    monitor solo distingue "la API está caída" de "la API está caída por lo que
    está intentando decirme", y para eso tiene el log.
    """
    estado = rt.health()
    estado["auth"] = "token" if deps.token_presente() else "sin_token"
    return estado


@router.get("/api/config/summary", response_model=None)
def config_summary(rt: Runtime = Depends(deps.runtime)) -> Dict[str, Any]:
    """Config de trading que la UI necesita, y el resumen de `store`.

    Son DOS cosas y por eso dos claves. `symbols`/`prop`/`execution` son un recorte
    de la config anidada, con los valores tal cual, para que el frontend no tenga
    que hardcodear ni parsear el YAML entero. `summary` es el resumen de reglas que
    ya calculaba `store.get_config_summary()`, con sus claves planas de siempre.

    `summary` viene con `error` y no revienta si la config no está disponible: es un
    dato para pintar un panel, y una pantalla en blanco por un import pendiente
    (`strategy`) esconde el único dato que el operador necesita ver.
    """
    store = rt.store
    if store is None:
        return {"error": "base de datos no cableada", **deps.resumen_config(rt)}
    try:
        resumen = store.get_config_summary()
    except Exception as exc:  # noqa: BLE001 - la config no es crítica para pintar la UI
        return {"error": "{0}: {1}".format(type(exc).__name__, exc), **deps.resumen_config(rt)}
    return {"summary": resumen, **deps.resumen_config(rt)}


@router.get("/api/clock", response_model=None)
def clock(rt: Runtime = Depends(deps.runtime)) -> Dict[str, Any]:
    """Hora del broker, día de trading y estado del EA que publica `ILOF_clock.json`.

    El EA es la fuente de verdad de la hora del servidor cuando el terminal está
    abierto; el reloj de pared del broker se usa cuando no hay EA. `estado` dice
    cuál de las dos está mandando, porque un reloj de dos posibles sin decir
    cuál está mandando es un reloj con dos respuestas.
    """
    return rt.reloj.state()


__all__ = ["router"]