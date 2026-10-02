"""Adaptadores de datos por mercado.

Cada adaptador implementa la interfaz definida en `base_adapter.BaseAdapter`,
encapsula la conexion, la concurrencia (locks) y la normalizacion de datos del
proveedor hacia el formato que consume `core/`.
"""