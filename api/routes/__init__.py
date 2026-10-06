"""Routers de la API: health, market, agent, journal, orderflow, macro, setup, trading y watcher.

Un router por dominio. Sin logica de negocio: solo validar, delegar y serializar.
Cada modulo se importa por su nombre desde `api/app.py` para que un `SyntaxError`
en uno no impida montar la app entera.

El contrato con el frontend heredado
-------------------------------------
`static/main.js` (copiado de REF sin reescribir) llama 46 rutas. Este paquete sirve
54 paths, de los cuales 46 son las que el JS pide: el resto son alias de REF y el
`/api/docs`. El inventario se comprueba con
`tests/integration/test_api_routes.py::TestContratoConElFrontend`, que recorre el
OpenAPI y falla si el JS pide algo que no existe o si una ruta se cae del inventario.

Las que FALTAN no son un descuido: son funcionalidad que en REF vivia en el
monolito y todavia no esta extraida. Inventario exacto a 2026-10-05:

| ruta que llama el JS            | de donde sale                        | bloqueada por                       |
|---------------------------------|-------------------------------------|------------------------------------|
| `/api/orderflow/feed`           | `OrderFlowEngine.start/stop`        | falta el feed real (6E)             |
| `/api/orderflow/fixtures`       | `mock_feed` de REF                  | depende del feed                    |
| `/api/stream/{sym}`             | SSE de ticks                        | falta decidir si vive o se elimina  |
| `/api/research/*`               | `research/` de REF (7 rutas)        | fase de research, posterior         |

Las tres primeras de ejecucion dejan de estar pendientes y las sirve
`api/routes/trading.py`: `trade/info/{sym}` con `market.spec`, y `trade/market` y
`positions/{t}/close` a traves de `api/services/execution.py` ->
`adapters/forex/mt5_execution.py`.

Las tres van por `ExecutionService`, que es el unico sitio con las puertas
(lista blanca, riesgo del dia, noticias, `validate_entry`, `execution_quality`), y
no por codigo propio en el router: una ruta que calcula por su cuenta lo que
`core.setup_gate` deberia calcular es la logica de negocio metida en FastAPI, que
es lo que este paquete no hace.

Nota para quien edite esta tabla: el test de contrato lee la PRIMERA columna de las
lineas que empiezan por `| `/api`, asi que las rutas ya servidas se listan fuera de
la tabla (arriba, en prosa) y no como filas suyas.

`/api/risk/setup` y `/api/chart/setup-eval` dejan de estar pendientes: las sirve
`api/routes/setup.py`, y el calculo vive en `core/setup_gate.py`, puro.

Las tres del watcher (`/api/watcher/status`, `/api/watcher/scan`,
`/api/watcher/auto-execute`) las sirve `api/routes/watcher.py`. Las dos primeras
funcionan y la tercera responde `501` a proposito: el watcher evalua setups y los
audita, y no manda ordenes (D-063). El calculo esta en `core/setup_lifecycle.py`
(transiciones y deduplicacion) y `core/setup_gate.py` (el veredicto), y la
orquestacion en `api/services/watcher_service.py`. Cuando el auto-arranque entre
entra por `ExecutionService`, no por codigo propio aqui.

Sobre el orden de las rutas literales
-------------------------------------
`/api/journal/{jid}` con `jid: int` se traga `/api/journal/overlay` y devuelve un
422 ("no se puede convertir 'overlay' en entero") en vez de un 404. Por eso
`overlay` se declara ANTES que la parametrizada: Starlette empareja por orden de
registro. Lo mismo con `/api/analysis/chartism/{symbol}` frente a `/{symbol}`.
"""

__all__ = ["agent", "health", "journal", "macro", "market", "orderflow", "setup", "trading", "watcher"]
