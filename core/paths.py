"""Fuente única de verdad de las rutas del proyecto.

Antes de existir este módulo, cada fichero que necesitaba una ruta la calculaba por
su cuenta con `os.path.dirname(os.path.abspath(__file__))`. Eso funcionaba solo
mientras todos los módulos vivieran en la raíz. Mover `store.py` dentro de
`database/` habría cambiado `PROJECT_DIR` a `.../database/` y el `trading.db` de
real se habría quedado atrás, en la raíz, sin su base de datos encima.

Ese es el fallo que este módulo existe para impedir: no da error, da silencio. Si
el anclaje se computa en un solo sitio, mover un fichero deja de ser un riesgo.

Todas las rutas son ABSOLUTAS y se resuelven contra la raíz del repositorio,
nunca contra el CWD del proceso. Con una ruta relativa, lanzar la app o la suite
desde otro directorio abre (o crea de cero, con el esquema completo) un SEGUNDO
`trading.db`: toda la auditoría parece haberse evaporado mientras sigue entera, en
otro fichero.

Diferencia respecto a la referencia: aquí `config/` y `database/` son
subdirectorios, así que las rutas se componen a partir de la raíz en lugar de
apuntar a ficheros sueltos de la raíz. Los `__file__` de `core/paths.py` y
`store.py` ya no comparten padre, que es justo el motivo del módulo.

Este módulo no importa nada del proyecto. Si alguna vez necesita hacerlo, es la
señal de que una ruta está mal de sitio.
"""

from __future__ import annotations

import os

# Raíz del repositorio: el directorio que contiene ESTE fichero, un nivel arriba
# (core/ -> raíz). Fijado por estructura, no por la convención del CWD.
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Configuración -----------------------------------------------------------

# strategy.yaml manda sobre DEFAULT_WEIGHTS en risk_engine. Si se mueve el YAML
# hay que moverlo aquí, no en los tres sitios que lo leen.
CONFIG_DIR = os.path.join(PROJECT_DIR, "config")

STRATEGY_PATH = os.environ.get(
    "STRATEGY_PATH", os.path.join(CONFIG_DIR, "strategy.yaml")
)

# --- Persistencia ------------------------------------------------------------

# El activo más valioso del proyecto: de aquí salen el post-mortem, los win-rate
# por componente y la calibración. Ver el docstring de arriba sobre por qué es
# absoluta y por qué se computa en un solo sitio.
DATABASE_DIR = os.path.join(PROJECT_DIR, "database")

DB_PATH = os.path.join(DATABASE_DIR, "trading.db")

# --- Datos locales -----------------------------------------------------------

DATA_DIR = os.path.join(PROJECT_DIR, "data")

# Caché de DXY en disco. Vive en la raíz (y no en data/) porque es un artefacto
# de una sola máquina, no un dataset.
DXY_CACHE_FILE = os.path.join(PROJECT_DIR, ".dxy_cache.json")

# --- Testing -----------------------------------------------------------------

# Fixtures de control para la auditoría visual (históricos y extremos). No son
# datos de producción: los consume solo /api/orderflow/fixtures.
TEST_FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests", "fixtures")

# --- Research ----------------------------------------------------------------

# Pipeline offline. Deliberadamente ajeno al runtime: se ejecuta como SUBPROCESO
# para no ensuciar las dependencias de api/agent/watcher.
RESEARCH_DIR = os.path.join(PROJECT_DIR, "research")
RESEARCH_RESULTS_DIR = os.path.join(RESEARCH_DIR, "results")

# El CLI se pasa como RELATIVO porque se invoca con cwd=PROJECT_DIR. Mantenerlo
# relativo es lo que hace que el cwd y la ruta no puedan desincronizarse.
RESEARCH_CLI_REL = os.path.join("research", "run_research.py")

# --- Backups -----------------------------------------------------------------

# Snapshots de strategy.yaml antes de aplicar una sugerencia del research.
BACKUPS_DIR = os.path.join(PROJECT_DIR, "backups")


def ensure_dir(path: str) -> str:
    """Crea el directorio si falta y lo devuelve. Para rutas de escritura."""
    os.makedirs(path, exist_ok=True)
    return path