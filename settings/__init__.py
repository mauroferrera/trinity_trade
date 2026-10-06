"""Acceso a la configuración en ficheros.

Un solo paquete para lo que "vive en un fichero": leer `config/strategy.yaml`,
cachearlo y escribirlo. Existe como capa propia, y no dentro de `core/` ni de
`database/`, porque las dos están cerradas por contrato:

- `core/` es lógica pura y su test (`tests/unit/test_core_purity.py`) prohíbe
  `open()`: un núcleo que abre ficheros tiene tests que dependen del disco.
- `database/` es la capa que habla con SQLite y recibe la configuración por un seam
  inyectable (`store.set_config_source`), precisamente para no arrastrar la Fase 6
  al importar. Meter aquí la lectura del YAML sería meter en `store` una segunda
  fuente de verdad y volver a atar las dos.

Este paquete solo depende de `core` (y de `yaml`), así que no crea ciclos: lo
importan `database/store.py` de forma tardía, la API y los tests.
"""

from __future__ import annotations

__all__ = ["strategy_source"]