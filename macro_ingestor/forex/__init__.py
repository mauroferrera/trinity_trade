"""Ingestores macro de Forex/CME.

Fuentes: CFTC COT, índice DXY y calendario de noticias (NFP/FOMC/ECB).

No se reexportan los submódulos aquí a propósito. `from macro_ingestor.forex import
cot_service` obliga a que quien importa diga de dónde viene el dato, y evita que un
`import *` monte la caché de los tres feeds sin querer. Además, importar el paquete
no debe tener efectos: la red solo ocurre dentro de `poll()`.
"""

__all__: list = []