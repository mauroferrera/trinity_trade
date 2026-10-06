"""La aplicación FastAPI. Bootstrap, ciclo de vida, middleware, montaje y routers.

Este fichero hace exactamente cuatro cosas, y ninguna más:

1. `create_app()`: construye el contenedor, engancha los routers, instala CORS,
   los handlers de error y los estáticos.
2. El `lifespan`: `runtime.startup()` al entrar, `runtime.shutdown()` al salir.
3. Los middlewares: uno de tiempo de respuesta y nada más.
4. El `app` de módulo, para `uvicorn api.app:app`.

Lo que NO está aquí y por qué
----------------------------
- **Lógica de negocio.** Ni SMC, ni score, ni footprint, ni lectura de ficheros de
  MT5. Eso son `core/` (puro) y `api/services/`.
- **Routers.** Viven en `api/routes/`, y cada uno se importa por su módulo para que
  un fallo de uno no impida montar la app entera (un `import` de un router con un
  `SyntaxError` tumba el proceso en el arranque, que es cuando menos se puede
  diagnosticar).
- **Estado mutable.** No hay variables globales de estado: el contenedor vive en
  `app.state.runtime`. La excepción es `app` misma, que es lo que Uvicorn espera.
- **Acceso a la base de datos.** No se abre aquí. `runtime.startup()` la inicializa
  y falla hacia atrás con un warning, porque una API sin base de datos puede servir
  `/api/health` (que es justo lo que se necesita para saber que la base no está).

Sobre el frontend
-----------------
Los estáticos se montan desde `static/` (los ficheros de `REF/static/` copiados tal
cual, sin reescribir ni un byte). `/` sirve `index.html`. Reestructurar `main.js`
(195 KB) y `style.css` (52 KB) es un trabajo con su propia fase: mezclado aquí
haría imposible revisar el portado de la API.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import errors
from .deps import ALLOWED_ORIGINS_ENV, origins
from .routes import agent, health, journal, macro, market, orderflow, setup, trading, watcher
from .runtime import Runtime

#: Título y versión. La versión va en el título porque es la forma más barata de
#: saber qué build tiene abierto un dashboard cuando alguien reporta un fallo.
API_TITLE = "Trinity MultiMarket Quant Engine"
API_VERSION = "0.6.0"

log = logging.getLogger("api")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
INDEX_FILE = "index.html"

#: Peticiones por encima de esto se avisan en el log. Es un umbral de vigilancia, no un
#: un limite: el limite de peticiones del agente es otro (429 del proveedor, con su
#: backoff) y este no debe confundirse con él.
LOG_SLOW_MS = 1000.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Enciende y apaga el contenedor. Lo único que vive fuera de una petición."""
    runtime = app.state.runtime
    await runtime.startup()
    try:
        yield
    finally:
        await runtime.shutdown()


def create_app(runtime: Optional[Runtime] = None) -> FastAPI:
    """La aplicación. Con `runtime` para tests; sin él, el cableado real.

    Que el contenedor sea un argumento es lo que permite que un test levante una app
    entera con dobles sin monkeypatchear nada global: `create_app(Runtime(store=Fake()))`.
    """
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.runtime = runtime if runtime is not None else Runtime.build()

    errors.install(app)
    _instala_cors(app)
    _instala_tiempos(app)

    for modulo in (health, market, agent, journal, orderflow, macro, setup, trading, watcher):
        app.include_router(modulo.router)

    _monta_estaticos(app)
    return app


def _instala_cors(app: FastAPI) -> None:
    """CORS con la lista de orígenes del entorno.

    REF leía `ALLOWED_ORIGINS` **al importar**, así que cambiarla exigía reiniciar y
    el síntoma era "cambié el origen y el navegador sigue dando CORS". Aquí la lista
    se lee en `create_app()` —que es "al arrancar", pero explícito y con un punto de
    entrada único— y se puede re-leer sin reiniciar llamando a `refrescar_cors(app)`.

    El default son dos `localhost`, no `*`: con credenciales, un `*` abierto
    convierte cualquier página web en un cliente de la API con la sesión del
    usuario. Ampliar es cosa del despliegue, por entorno.
    """
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Process-Time-Ms"],
    )


def refrescar_cors(app: FastAPI) -> list:
    """Vuelve a leer `ALLOWED_ORIGINS` y lo aplica. Devuelve los orígenes puestos.

    Existe porque `CORSMiddleware` congela su lista al construirse, y el síntoma de
    no tener esto es el de REF: cambiar la variable de entorno no hace nada hasta
    que alguien reinicia el servidor y se lo atribuye al navegador.
    """
    actuales = origins()
    for mw in app.user_middleware:
        if getattr(mw, "cls", None) is not None and mw.cls.__name__ == "CORSMiddleware":
            mw.kwargs["allow_origins"] = actuales
    return actuales


def _instala_tiempos(app: FastAPI) -> None:
    """Un middleware de tiempo. Uno solo, y solo mide."""

    @app.middleware("http")
    async def _cronometro(request: Request, call_next: Any) -> Any:
        inicio = time.perf_counter()
        respuesta = await call_next(request)
        ms = (time.perf_counter() - inicio) * 1000.0
        respuesta.headers["X-Process-Time-Ms"] = "{0:.1f}".format(ms)
        if ms > LOG_SLOW_MS:
            log.warning("petición lenta: %s %s en %.0f ms", request.method, request.url.path, ms)
        return respuesta


def _monta_estaticos(app: FastAPI) -> None:
    """Sirve `static/` y `/`. Si no existe el directorio, la API sigue funcionando.

    Un frontend ausente es un problema de despliegue; que la API no arranque por eso
    es un problema de diseño. Se avisa y se sigue.
    """
    if not os.path.isdir(STATIC_DIR):
        log.warning("sin estáticos en %s: la API sirve pero el dashboard no", STATIC_DIR)

        @app.get("/", response_model=None)
        def _sin_estaticos() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "el frontend no está desplegado",
                    "detalle": "falta el directorio static/ (ver AGENT_GUIDELINES.md, fase 6)",
                    "docs": "/api/docs",
                },
                status_code=503,
            )

        return

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_model=None, include_in_schema=False)
    def index() -> Any:
        ruta = os.path.join(STATIC_DIR, INDEX_FILE)
        if not os.path.isfile(ruta):
            return JSONResponse({"error": "falta static/index.html"}, status_code=503)
        return FileResponse(ruta)


#: El objeto que Uvicorn carga: `uvicorn api.app:app`.
app = create_app()


__all__ = ["API_TITLE", "API_VERSION", "ALLOWED_ORIGINS_ENV", "STATIC_DIR", "app", "create_app", "lifespan"]