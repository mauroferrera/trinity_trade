"""Capa HTTP (FastAPI).

`app.py` solo debe encargarse de bootstrap, middlewares, mounts y registro de
routers. Los endpoints en `routes/` se mantienen delgados: la logica de
negocio vive en `core/` y `macro_ingestor/`.
"""