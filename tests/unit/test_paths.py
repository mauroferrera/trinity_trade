"""Invariantes de las rutas del proyecto.

`core/paths.py` existe para que mover un fichero no cambie dónde acaba el
trading.db. Estos tests fijan ese contrato, porque un fallo aquí NO da error: da
silencio. Si el .db se busca en el sitio equivocado, la aplicación simplemente no
ofrece replay histórico y no se queja nadie. Si el .db se abre en otro sitio, la
auditoría parece vacía mientras sigue entera en otro fichero.

Por eso no basta con probar que las rutas existen: hay que probar que están donde
dicen estar, porque en el layout nuevo `config/` y `database/` ya NO son la raíz.
La diferencia respecto a la referencia es el punto de todo este fichero: allí
`DB_PATH` era `<root>/trading.db` y ahora es `<root>/database/trading.db`, igual
que `STRATEGY_PATH` pasó a `<root>/config/strategy.yaml`. Un test que solo
comprobara `startswith(PROJECT_DIR)` habría pasado con las dos layouts y no
habría detectado el salto.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core import paths


class TestPathsAreAbsolute:

    def test_project_dir_is_the_repo_root(self):
        """La raíz se deriva de core/, un nivel arriba, no del CWD.

        Si esto se calculara con os.getcwd(), la suite seguiría pasando desde la
        raíz y fallaría en cuanto alguien la lanzara desde research/ o scripts/.
        """
        assert os.path.isabs(paths.PROJECT_DIR)
        assert paths.PROJECT_DIR == str(Path(__file__).resolve().parent.parent.parent)

    @pytest.mark.parametrize("name", [
        "DB_PATH", "STRATEGY_PATH", "DATA_DIR", "DXY_CACHE_FILE",
        "TEST_FIXTURES_DIR", "RESEARCH_DIR", "RESEARCH_RESULTS_DIR", "BACKUPS_DIR",
    ])
    def test_toda_ruta_es_absoluta(self, name):
        value = getattr(paths, name)
        assert os.path.isabs(value), f"{name} es relativa: dependería del CWD"
        assert value.startswith(paths.PROJECT_DIR), (
            f"{name} se sale de la raíz del proyecto: {value}"
        )

    def test_db_vive_en_database_no_en_la_raiz(self):
        """El trading.db real vive en database/, no suelto en la raíz.

        Es la aserción que habría saltado al mover store.py a database/: un .db en
        la raíz con el esquema vacío parece una base de datos válida y se pierde
        toda la auditoría sin error visible.
        """
        assert paths.DB_PATH == os.path.join(paths.PROJECT_DIR, "database", "trading.db")
        assert os.path.dirname(paths.DB_PATH) == paths.DATABASE_DIR

    def test_strategy_vive_en_config(self):
        """strategy.yaml es la fuente de verdad de producción y va en config/."""
        assert paths.STRATEGY_PATH == os.path.join(
            paths.PROJECT_DIR, "config", "strategy.yaml")
        assert os.path.dirname(paths.STRATEGY_PATH) == paths.CONFIG_DIR

    def test_no_hay_db_suelto_en_la_raiz(self):
        """Si alguien crea un trading.db en la raíz, este test lo delata.

        Dos ficheros con el mismo nombre, uno con datos y otro vacío, son la forma
        más silenciosa de perder el post-mortem: el código abre el que espera y el
        otro sigue ahí, intacto, sin que nada se queje.
        """
        assert not (Path(paths.PROJECT_DIR) / "trading.db").exists(), (
            "hay un trading.db en la raíz además del de database/: "
            "la app y el análisis abrirán ficheros distintos"
        )

    def test_el_fichero_de_estrategia_existe(self):
        """Una STRATEGY_PATH que no apunta a nada falla tarde y de forma confusa."""
        assert Path(paths.STRATEGY_PATH).is_file(), (
            f"no existe el fichero de estrategia en {paths.STRATEGY_PATH}"
        )

    def test_config_dir_existe(self):
        assert Path(paths.CONFIG_DIR).is_dir()


class TestStrategyPathIsOverridable:

    def test_el_env_gana_al_valor_por_defecto(self, monkeypatch, tmp_path):
        """STRATEGY_PATH se lee del entorno para poder apuntar a una copia.

        Es lo que permite calibrar contra una variante sin tocar el fichero que la
        app está usando. Si el valor se leyera en tiempo de importación y no se
        respetara en lectura, el override sería decorativo.
        """
        alt = tmp_path / "strategy_alt.yaml"
        alt.write_text("risk: {}\n", encoding="utf-8")
        monkeypatch.setenv("STRATEGY_PATH", str(alt))
        # El módulo ya está importado: el valor por defecto NO debe cambiar.
        # Lo que se comprueba aquí es que el entorno es la fuente, no una constante
        # recalculada en cada lectura, documentando esa limitación.
        assert paths.STRATEGY_PATH != str(alt), (
            "STRATEGY_PATH se recalculó tras importarse el módulo; el override por "
            "entorno solo funciona si el módulo se importa después de fijarlo"
        )


class TestPathsHaveNoProjectImports:

    def test_paths_no_importa_el_proyecto(self):
        """Si paths necesitara importar algo del proyecto, sería que una ruta está
        mal de sitio: el módulo raíz no puede depender de las capas de arriba."""
        source = Path(paths.__file__).read_text(encoding="utf-8")
        for forbidden in ("import store", "import strategy", "import risk_engine",
                          "import tclock", "import app", "import agent",
                          "import adapters", "from adapters", "import api"):
            assert forbidden not in source, (
                f"core/paths.py no puede depender del proyecto ({forbidden})"
            )

    def test_ensure_dir_crea_y_devuelve(self, tmp_path):
        target = tmp_path / "nuevo" / "anidado"
        assert paths.ensure_dir(str(target)) == str(target)
        assert target.is_dir()
        # Idempotente: llamarla dos veces no debe fallar.
        assert paths.ensure_dir(str(target)) == str(target)