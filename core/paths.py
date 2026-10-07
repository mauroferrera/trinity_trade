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

# strategy_map.yaml -> el magic con que sale una orden. Un fichero aparte de
# strategy.yaml porque es OTra clase de configuración: strategy.yaml es UN
# perfil, y el mapa es cómo se distingue un perfil de otro por magic (D-071).
STRATEGY_MAP_PATH = os.environ.get(
    "STRATEGY_MAP_PATH", os.path.join(CONFIG_DIR, "strategy_map.yaml")
)

# --- Persistencia ------------------------------------------------------------

# El activo más valioso del proyecto: de aquí salen el post-mortem, los win-rate
# por componente y la calibración. Ver el docstring de arriba sobre por qué es
# absoluta y por qué se computa en un solo sitio.
DATABASE_DIR = os.path.join(PROJECT_DIR, "database")

DB_PATH = os.path.join(DATABASE_DIR, "trading.db")

# --- Datos locales -----------------------------------------------------------

# Caché de DXY en disco. Vive en la raíz porque es un artefacto de una sola
# máquina, no un dataset: pesa unos pocos KB y no hay nada que sincronizar.
DXY_CACHE_FILE = os.path.join(PROJECT_DIR, ".dxy_cache.json")

# --- Datos externos (fuera del repo, fuera de OneDrive) -----------------------

# Los datos PESAN. La cinta de Databento, un histórico de velas o un dump de
# órdenes son megas que no son código, y el repo vive dentro de OneDrive
# (C:\Users\fmaur\OneDrive\Desktop\Trinity_proyect). Meter esos archivos aquí
# tiene dos consecuencias distintas, y ninguna se manifiesta como un error:
#
#   1. OneDrive los intenta sincronizar: gigabytes de red por día y cuota gastada.
#   2. Git se puede llevárselos. data/ está en .gitignore, pero eso es una lista
#      de nombres y no protege contra un directorio nuevo ni contra un `.gitignore`
#      mal copiado. Un fichero de 2 GB commitado "por error" se arrastra en cada
#      clone para siempre.
#
# Por eso la raíz es una VARIABLE, no una constante dentro del repo, y por eso
# existe `data_root_guard_error`: una regla que solo está escrita en un comentario
# depende de que alguien la lea; esta devuelve el motivo y quien descarga decide.
#
# El default está fuera de OneDrive y fuera del repo: C:\Users\fmaur\Desktop,
# que es donde ya vive la referencia de solo lectura. Verificado que es un
# directorio real (no una junction hacia OneDrive\Desktop).
DATA_ROOT = os.environ.get(
    "TRINITY_DATA_ROOT",
    os.path.join(os.path.expanduser("~"), "Desktop", "trinity_data"),
)

# El nombre con que el resto del código conoce este directorio. Apunta a la misma
# raíz que DATA_ROOT: aquí no hay dos cosas distintas, hay dos nombres, y el
# antiguo se conserva para no cambiar el significado de un identificador que ya
# está en el código.
DATA_DIR = DATA_ROOT

# Cinta de Databento en Parquet. Es el más pesado de todos y el que motivó la
# separación: un histórico real son gigabytes que no son código.
DATABENTO_RAW_DIR = os.path.join(DATA_ROOT, "databento", "raw")

# --- Testing -----------------------------------------------------------------

# Fixtures de control para la auditoría visual (históricos y extremos). No son
# datos de producción: los consume solo /api/orderflow/fixtures.
TEST_FIXTURES_DIR = os.path.join(PROJECT_DIR, "tests", "fixtures")

# --- Research ----------------------------------------------------------------

# Pipeline offline. Deliberadamente ajeno al runtime: se ejecuta como SUBPROCESO
# para no ensuciar las dependencias de api/agent/watcher.
#
# Este directorio se queda DENTRO del repo porque es CÓDIGO: RESEARCH_CLI_REL
# (más abajo) se invoca con cwd=PROJECT_DIR y una ruta relativa, así que el
# módulo tiene que seguir viviendo en la raíz. Lo que sale de ahí, no.
RESEARCH_DIR = os.path.join(PROJECT_DIR, "research")

# Resultados y datasets del pipeline: salen de la raíz externa por lo mismo que
# DATABENTO_RAW_DIR. Una corrida de research que guardara tablas dentro del repo
# acabaría sincronizándose con OneDrive sin que nada lo pidiera.
RESEARCH_DATA_DIR = os.path.join(DATA_ROOT, "research")
RESEARCH_RESULTS_DIR = os.path.join(RESEARCH_DATA_DIR, "results")

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


# --- Guardián de la raíz de datos ----------------------------------------------

# El código de referencia vive fuera del repo y es de SOLO LECTURA
# (.gitignore:2 y PROJECT_STATE.reference_code.writable). Escribir datos ahí
# corrompería la referencia con la que se comparan los ports.
REFERENCE_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "Trading")


def _is_within(path: str, parent: str) -> bool:
    r"""True si `path` cae dentro de `parent` (o es él mismo).

    No mira si existen: es una comprobación de FORMA, y tiene que poderse
    hacer ANTES de crear nada. `normcase` es lo que hace que en Windows
    `c:\users\...` y `C:\Users\...` cuenten como el mismo sitio; sin él, la
    regla se salta con cualquier diferencia de mayúsculas, que en Windows es
    lo más fácil del mundo. (Docstring crudo por eso mismo: un `\u` suelto
    aquí sería un escape Unicode y el módulo no importaría.)
    """
    try:
        p = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(parent))
    except (TypeError, ValueError, OSError):
        return False
    if p == b:
        return True
    return p.startswith(b if b.endswith(os.sep) else b + os.sep)


def _within_onedrive(path: str) -> bool:
    """True si algún componente del camino es un directorio de OneDrive.

    No se comprueba solo "OneDrive\\Desktop": el tenant añade su nombre
    ("OneDrive - Empresa"), y una sincronización corporativa crea más de uno.
    Con empezar por `onedrive` se cubren todas.
    """
    try:
        parts = os.path.normcase(os.path.abspath(path)).split(os.sep)
    except (TypeError, ValueError, OSError):
        return False
    return any(p.startswith("onedrive") for p in parts if p)


def data_root_guard_error(path: str) -> str | None:
    """Devuelve el motivo por el que `path` no sirve como raíz de datos, o None.

    Es el guardián del contrato de arriba, escrito como función y no como
    comentario: un comentario depende de que alguien lo lea, y esta función no.
    Quien descarga (research/fetch_databento.py) la llama y se niega a escribir
    si devuelve un motivo; el test fija que el default está limpio y que cada
    regla por separado dispara.

    Tres motivos, en orden de lo más caro a lo menos:

    - Relativa. Este módulo exige rutas absolutas (ver docstring): contra el CWD
      se cambia al lanzar la app desde otro sitio.
    - Dentro del repo. El repo está en OneDrive, así que esto ya está cubierto,
      pero se comprueba por separado porque un Parquet en el repo es un error de
      git y no solo uno de sincronización, y los dos se arreglan de otro modo.
    - Dentro de cualquier OneDrive. La razón de ser de esta raíz.
    - Dentro de REF. Solo lectura por decisión registrada.
    """
    if path is None or not str(path).strip():
        return "la raíz de datos está vacía"
    if not os.path.isabs(path):
        return f"es relativa: {path!r} dependería del CWD del proceso"
    if _is_within(path, PROJECT_DIR):
        return (
            f"dentro del repo ({path}): el repo está en OneDrive, así que la "
            "cinta subiría a la nube Y se podría versionar en git"
        )
    if _within_onedrive(path):
        return f"dentro de OneDrive ({path}): gigabytes sincronizados a diario"
    if _is_within(path, REFERENCE_DIR):
        return f"dentro de REF ({path}): es código de referencia de solo lectura"
    return None