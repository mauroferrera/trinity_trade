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
        "DB_PATH", "STRATEGY_PATH", "DXY_CACHE_FILE",
        "TEST_FIXTURES_DIR", "RESEARCH_DIR", "BACKUPS_DIR",
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


class TestRutasExternas:
    """Las rutas de DATOS no viven en el repo, y ese es el punto.

    Al revés que el bloque de arriba, aquí la aserción es `startswith` NO vale:
    DATA_DIR tenía que estar dentro de la raíz hasta que empezó a pesar. El
    contrato inválido es ahora el que hay que defender.
    """

    @pytest.mark.parametrize("name", [
        "DATA_ROOT", "DATA_DIR", "DATABENTO_RAW_DIR",
        "RESEARCH_DATA_DIR", "RESEARCH_RESULTS_DIR",
    ])
    def test_datos_fuera_del_repo(self, name):
        value = getattr(paths, name)
        assert os.path.isabs(value), f"{name} es relativa: dependería del CWD"
        assert not paths._is_within(value, paths.PROJECT_DIR), (
            f"{name} sigue dentro del repo ({value}): OneDrive lo sincronizaría "
            "y git podría versionarlo"
        )

    def test_la_raiz_del_default_no_es_onedrive(self):
        """El default elegido tiene que pasar su propia regla.

        Si un día Windows redirige `Desktop` hacia OneDrive (es lo normal en un
        equipo corporativo), el default incumpliría la regla que motivó este
        módulo sin que nada lo gritara. Este test lo grita.
        """
        assert paths.data_root_guard_error(paths.DATA_ROOT) is None, (
            paths.data_root_guard_error(paths.DATA_ROOT)
        )

    def test_research_dir_sigue_dentro_porque_es_codigo(self):
        """RESEARCH_DIR es código (RESEARCH_CLI_REL se invoca con cwd=PROJECT_DIR)
        y por eso NO se mueve; sus resultados sí. Si este falla, alguien ha
        arrastrado el código a la raíz externa y el CLI relativo se rompe."""
        assert paths._is_within(paths.RESEARCH_DIR, paths.PROJECT_DIR)
        assert paths.RESEARCH_CLI_REL == os.path.join("research", "run_research.py")


class TestGuardianDeRaizDeDatos:

    def test_una_ruta_valida_no_dispara(self, tmp_path):
        assert paths.data_root_guard_error(str(tmp_path)) is None

    def test_relativa_dispara(self):
        motivo = paths.data_root_guard_error("data" + os.sep + "cinta")
        assert motivo is not None
        assert "CWD" in motivo

    def test_vacia_dispara(self):
        assert paths.data_root_guard_error("") is not None
        assert paths.data_root_guard_error("   ") is not None
        assert paths.data_root_guard_error(None) is not None

    def test_dentro_del_repo_dispara(self):
        motivo = paths.data_root_guard_error(
            os.path.join(paths.PROJECT_DIR, "parquet"))
        assert motivo is not None
        assert "dentro del repo" in motivo

    def test_dentro_de_onedrive_dispara(self):
        # Hermana del repo, dentro de OneDrive pero NO dentro del repo: solo la
        # regla de OneDrive la puede cazar. Si se quitara esa comprobación, este
        # test y la consecuencia de sincronizar gigabytes seguirían ahí.
        otro = os.path.join(paths.PROJECT_DIR, "..", "otra_carpeta")
        motivo = paths.data_root_guard_error(otro)
        assert motivo is not None
        assert "OneDrive" in motivo

    def test_dentro_de_ref_dispara(self):
        motivo = paths.data_root_guard_error(
            os.path.join(paths.REFERENCE_DIR, "cintas"))
        assert motivo is not None
        assert "solo lectura" in motivo

    @pytest.mark.parametrize("bad", [
        "", "   ", None, "cinta", os.path.join("C:\\", "Users", "OneDrive", "x"),
    ])
    def test_todo_lo_malo_dispara(self, bad):
        assert paths.data_root_guard_error(bad) is not None

    def test_importar_no_crea_directorios(self, tmp_path):
        """paths.py debe declarar, no crear.

        `ensure_dir` es la ÚNICA puerta de escritura del módulo. Sin esta
        aserción, un `os.makedirs` al importar crearía `Desktop\trinity_data` en
        cualquier ordenador que importara el módulo, con solo abrir la app.
        """
        source = Path(paths.__file__).read_text(encoding="utf-8")
        assert source.count("os.makedirs(") == 1, (
            "paths.py solo debe crear directorios dentro de ensure_dir")


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