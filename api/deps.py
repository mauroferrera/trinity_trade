"""Dependencias de FastAPI: dónde están las cosas y quién puede llamarlas.

Dos familias, y la separación es deliberada:

- **Proveedores** (`runtime`, `store`, `market`, `ws`, `deps_agente`): leen el
  contenedor de `app.state` y devuelven la pieza. Ninguna función de este módulo
  importa `api.app`, así que un test de endpoint no necesita la app y un test de
  la app no necesita endpoints.
- **Autenticación** (`require_api_token`): compara el token por petición.

Por qué el token se lee por petición
------------------------------------
REF hacía esto en el arranque:

    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "...").split(",")

    def require_api_token(request: Request):
        expected = os.getenv("API_TOKEN")
        ...

El token se leía del entorno en cada llamada (bien), pero los orígenes CORS se
leían **al importar**, así que cambiarlos exigía reiniciar. Aquí los dos se leen
por petición. Y el token se compara con `secrets.compare_digest` en vez de `==`:
la comparación de cadenas de Python sale antes en cuanto encuentra una diferencia,
así que un `==` filtra el token prefix a prefix. No es una amenaza teórica en un
script local y lo es en cuanto la API escuche en la red.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request, WebSocket

from .runtime import Runtime, token_actual

#: Variable de entorno con la lista de orígenes permitidos, separada por comas.
ALLOWED_ORIGINS_ENV = "ALLOWED_ORIGINS"

#: Origenes de desarrollo por defecto. Son los dos que usa el frontend de REF.
DEFAULT_ORIGINS = ("http://localhost:8000", "http://localhost:3000")


def origins(origins: Optional[str] = None) -> list:
    """Los orígenes permitidos, leídos del entorno si no se pasan.

    Nunca devuelve `*`: con credenciales o sin ellas, un `*` abierto convierte
    cualquier página web en un cliente de la API con la sesión del usuario. La
    lista por defecto es la de desarrollo y se amplía por entorno, que es donde
    tiene que ampliarse: un servidor real no es desarrollo local.
    """
    crudo = origins if origins is not None else os.getenv(ALLOWED_ORIGINS_ENV)
    if not crudo:
        return list(DEFAULT_ORIGINS)
    return [o.strip() for o in str(crudo).split(",") if o.strip()]


# -- proveedores ---------------------------------------------------------------


def runtime(request: Request) -> Runtime:
    """El contenedor de la aplicación.

    Si no está, es que alguien montó un `APIRouter` sin la app: es un bug de
    cableado, no una petición mal formada, y por eso el error lo dice con su
    nombre en vez de devolver un 503 que se repite en producción.
    """
    rt = getattr(request.app.state, "runtime", None)
    if rt is None:
        raise HTTPException(
            status_code=503,
            detail="la aplicación no tiene runtime: falta startup() o el router está suelto",
        )
    return rt


def store(request: Request) -> Any:
    rt = runtime(request)
    if rt.store is None:
        raise HTTPException(status_code=503, detail="base de datos no cableada")
    return rt.store


def market(request: Request) -> Any:
    rt = runtime(request)
    if rt.market is None:
        raise HTTPException(status_code=503, detail="mercado no cableado")
    return rt.market


def news(request: Request) -> Any:
    rt = runtime(request)
    return rt.news


def exports(request: Request) -> Any:
    rt = runtime(request)
    return rt.exports


def ws_manager(request: Request) -> Any:
    return runtime(request).ws


def deps_agente(request: Request) -> Any:
    """Los `AgentDeps`, ya montados con los puertos de ESTA app."""
    return runtime(request).deps


def broker_clock(request: Request) -> Any:
    return runtime(request).reloj


# -- autenticación -------------------------------------------------------------


def require_api_token(request: Request) -> None:
    """Valida el token si hay uno configurado. 401 si no cuadra.

    Sin `API_TOKEN` en el entorno **no** se exige nada: es el modo de desarrollo
    local (el de REF), y ponerlo obligatorio rompe el arranque de un portátil sin
    servidor. Con token puesto, la ausencia de cabecera o el valor equivocado son
    la misma respuesta y el mismo código, porque la diferencia entre "no has
    mandado nada" y "has mandado algo malo" solo importa al atacante.
    """
    esperado = token_actual()
    if esperado is None:
        return
    recibido = request.headers.get("x-api-token") or ""
    if not recibido:
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            recibido = authorization[7:].strip()
    if not recibido or not secrets.compare_digest(recibido, esperado):
        raise HTTPException(status_code=401, detail="token inválido o ausente")


def ws_token(websocket: WebSocket) -> bool:
    """Valida el token en un WebSocket. `True` si vale.

    Los WebSockets no llevan cabeceras de autorización en el navegador, así que el
    token llega en la query o en un `Sec-WebSocket-Protocol`. Y hay que comprobar
    ANTES del `accept()`: después, el handshake ya está hecho y el cierre correcto
    es 1008 (policy violation), que el cliente distingue de un corte de red.
    """
    esperado = token_actual()
    if esperado is None:
        return True
    recibido = websocket.query_params.get("token") or ""
    if not recibido:
        protocolo = websocket.headers.get("sec-websocket-protocol") or ""
        if protocolo and protocolo.lower().startswith("bearer."):
            recibido = protocolo[7:].strip()
    return bool(recibido) and secrets.compare_digest(recibido, esperado)


def token_presente() -> bool:
    """¿Hay token configurado? Lo que `/api/health` publica en `auth`."""
    return token_actual() is not None


def resumen_config(runtime_rt: Runtime) -> Dict[str, Any]:
    """Lo que la UI necesita de la config para no hardcodear. Read-only."""
    cfg: Dict[str, Any] = {}
    if runtime_rt.store is not None:
        try:
            cfg = runtime_rt.store.get_trading_config() or {}
        except Exception:  # noqa: BLE001 - health/config no lanzan
            cfg = {}
    return {
        "symbols": cfg.get("symbols") or [],
        "prop": cfg.get("prop") or {},
        "execution": cfg.get("execution") or {},
    }


__all__ = [
    "ALLOWED_ORIGINS_ENV",
    "DEFAULT_ORIGINS",
    "broker_clock",
    "deps_agente",
    "exports",
    "market",
    "news",
    "origins",
    "require_api_token",
    "resumen_config",
    "runtime",
    "store",
    "token_presente",
    "ws_manager",
    "ws_token",
]