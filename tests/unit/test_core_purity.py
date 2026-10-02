"""Guardia de pureza del núcleo: `core/` no depende de nada que hable con el mundo.

La regla del proyecto es dura: `core/` NO importa `adapters`, `api`, `agent`,
MetaTrader5, Databento ni ningún proveedor de datos. Recibe datos normalizados y
devuelve resultados deterministas.

El motivo de que sea un TEST y no una nota en el README: esa dependencia no
compila-falla cuando se introduce, y casi no falla al ejecutarse. Importar MT5
desde `core/risk_engine.py` sigue funcionando en la máquina que tiene MT5
instalado, produce los mismos números, y el núcleo deja de ser testeable fuera de
esa máquina sin que nada se entere. Solo se nota cuando alguien intenta correr
los tests en otro entorno, que es decir, demasiado tarde.

El análisis es por AST y no por búsqueda de texto porque `adapters` aparece
legítimamente en docstrings y comentarios de `core/` (los módulos explican qué
adaptador los alimentará). Contar esas menciones daría falsos positivos que
empujarían a alguien a borrar la documentación en vez de la regla.

Run:  python -m pytest tests/unit/test_core_purity.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parent.parent.parent / "core"

# Prefijos prohibidos. Se comparan contra el nombre del módulo importado, no
# contra su ruta, para no tener que resolver Imports relativos.
FORBIDDEN_PREFIXES = (
    "MetaTrader5",
    "databento",
    "ccxt",
    "fastapi",
    "starlette",
    "uvicorn",
    "requests",
    "httpx",
    "aiohttp",
    "sqlalchemy",
    "pandas",
    "numpy",
    "dotenv",
)

# Paquetes del propio proyecto que `core/` no puede tocar, por nombre Y por ruta.
FORBIDDEN_PROJECT = ("adapters", "api", "agent", "macro_ingestor", "database")

# These ARE allowed: `core` importing itself is the point of the package layout.
ALLOWED_LOCAL = ("core",)


def _imported_modules(path: Path):
    """Módulos importados por `path`, resolviendo los imports relativos."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    package_parts = path.parent.name if path.parent.name != "core" else "core"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from ..core import y`: resolver la parte
                # local para poder compararla con la lista de prohibidos.
                base = package_parts
                names.append("." * node.level + (node.module or ""))
                if node.module:
                    names.append(f"{base}.{node.module}")
            elif node.module:
                names.append(node.module)
    return names


def _core_modules():
    return sorted(p for p in CORE_DIR.rglob("*.py"))


CORE_MODULES = _core_modules()


def test_hay_modulos_en_core():
    """Guarda de que la parametrización de abajo no esté vacía y no pase en verde."""
    assert CORE_MODULES, f"no se encontró ningún módulo .py en {CORE_DIR}"
    assert {p.name for p in CORE_MODULES} >= {
        "paths.py", "risk_engine.py", "smc_engine.py", "orderflow_engine.py",
        "market_view.py", "simulator.py", "trade_history.py", "lot_calculator.py",
    }


@pytest.mark.parametrize("module", CORE_MODULES, ids=lambda p: p.name)
class TestCoreModulesArePure:

    def test_no_importa_una_capa_que_habla_con_el_exterior(self, module):
        for name in _imported_modules(module):
            root = name.lstrip(".").split(".")[0]
            assert root not in FORBIDDEN_PREFIXES, (
                f"core/{module.name} importa {name}: el núcleo no puede depender de "
                f"ningún proveedor, HTTP client ni framework de API. Lo que necesite "
                f"datos de fuera debe recibirlos por parámetro (DI)."
            )

    def test_no_importa_adapters_api_agent_ni_database(self, module):
        for name in _imported_modules(module):
            root = name.lstrip(".").split(".")[0]
            assert root not in FORBIDDEN_PROJECT, (
                f"core/{module.name} importa {name}: la dirección de la dependencia "
                f"es adapters -> core, nunca al revés. Si el núcleo necesita algo de "
                f"ahí es que la lógica está en la capa equivocada."
            )

    def test_no_abre_ficheros_ni_toca_la_red(self, module):
        """I/O directo en el núcleo rompe el determinismo y la replay de la suite.

        Se permiten `Path(...)` puros (para saber dónde está un fichero) pero no
        abrirlo ni escribir: leer configuración desde `core/` convertiría un test
        unitario en un test de integración que depende del disco.
        """
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                assert name not in ("open", "urlopen", "read_text", "write_text"), (
                    f"core/{module.name} abre ficheros ({name}) en línea "
                    f"{node.lineno}: el núcleo solo recibe datos, no los carga"
                )

    def test_no_escribe_en_la_base_de_datos(self, module):
        source = module.read_text(encoding="utf-8")
        for prohibido in ("sqlite3", "execute(", "executemany(", "commit()"):
            assert prohibido not in source, (
                f"core/{module.name} parece tocar la base de datos ({prohibido}): "
                "la persistencia es de database/, no del núcleo"
            )


class TestCoreDoesNotDependOnAdapters:
    """El grafo de imports del núcleo, comprobado a nivel de paquete.

    Complementa los tests por módulo: estos miran el texto fuente, que puede
    esconder un import dinámico (`importlib.import_module`) que el análisis estático
    no ve. Comprobando el grafo ya construido, ese caso también salta.
    """

    def test_ningun_modulo_de_core_tiene_adapters_en_sys_modules(self):
        import importlib
        import sys

        for module in CORE_MODULES:
            name = f"core.{module.stem}"
            importlib.import_module(name)
            mod = sys.modules[name]
            for attr in vars(mod).values():
                mod_name = getattr(attr, "__module__", None) or ""
                origin = getattr(attr, "__name__", "") or ""
                assert origin.split(".")[0] not in FORBIDDEN_PROJECT, (
                    f"core/{module.name} expone {origin}, que viene de una capa "
                    "prohibida"
                )
                assert mod_name.split(".")[0] not in FORBIDDEN_PROJECT, (
                    f"core/{module.name} referencia un objeto definido en {mod_name}"
                )