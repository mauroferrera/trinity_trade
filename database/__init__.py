"""Persistencia.

`DB_PATH` es unico y absoluto, resuelto siempre desde `core.paths` (nunca relativo
al CWD). El esquema vive en `models.py`; los UPSERT en `store.py`.
"""