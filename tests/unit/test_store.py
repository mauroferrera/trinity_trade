"""La capa de persistencia, probada sin la pila que REF arrastraba.

REF/store.py importaba `tclock` (que importa MetaTrader5) y `strategy` (Fase 6).
Eso hacia que la capa de DB no se pudiera probar sin la de estrategia: no habia
forma de ejercitar el CRUD sin levantar el YAML y el reloj de broker. Aqui la
config entra por un seam, asi que estos tests meteen un `FakeConfigSource` y
trabajan contra una base temporal.

Lo que se comprueba, en orden de importancia:

  1. Que el seam funciona de verdad: importar `store` no carga `strategy`, ni
     `MetaTrader5`, ni `yaml`. Si esto se rompe, la promesa de `core/` (D-009) se
     queda a medias y nadie se entera hasta que falta MetaTrader5 en un servidor.
  2. Que `DB_PATH` tiene UNA sola fuente de verdad. REF lo recomponia en
     `store.py` mientras `core.paths` tenia su propia constante, y solo
     coincidian por casualidad; con las rutas nuevas habrian apuntado a ficheros
     distintos.
  3. Round-trip de las funciones que importan al post-mortem. El resto del CRUD
     (roles, conversaciones, drawings) se cubre en `test_store_crud.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import clock
from core import paths as core_paths
from database import store

PROJECT_DIR = Path(core_paths.PROJECT_DIR)


def _yaml_real() -> dict:
    """El `config/strategy.yaml` que se reparte, leído del disco."""
    import yaml

    with open(core_paths.STRATEGY_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConfigSource:
    """Implementa `ConfigSource` sin YAML ni strategy."""

    def __init__(self, cfg=None, mapa=None):
        self.cfg = dict(cfg or {})
        self.mapa = dict(mapa or {})
        self.saved = []

    def get_config(self) -> dict:
        return dict(self.cfg)

    def save_config(self, cfg: dict) -> None:
        self.saved.append(dict(cfg))
        self.cfg = dict(cfg)

    def data_sources(self) -> dict:
        return self.cfg.get("data_sources") or {}

    def agent_topics(self) -> dict:
        return self.cfg.get("agent_topics") or {}

    def watcher_config(self) -> dict:
        return self.cfg.get("watcher_config") or {}

    def strategy_map(self) -> dict:
        return dict(self.mapa)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Base temporal con el esquema completo, y el seam con una config falsa.

    Se redirige `store.DB_PATH` (atributo de módulo) y NO `core.paths.DB_PATH`:
    así el canario de `conftest.py`, que vigila la ruta real, sigue vigilando de
    verdad, y este test escribe donde le dejan.
    """
    target = tmp_path / "test.db"
    monkeypatch.setattr(store, "DB_PATH", str(target))
    monkeypatch.setattr(store, "PROJECT_DIR", str(tmp_path))
    store.set_config_source(FakeConfigSource({"agent_risk_policy": "riesgo alto"}))
    store.init_db()
    yield target
    store.set_config_source(None)


# ---------------------------------------------------------------------------
# El seam de configuracion
# ---------------------------------------------------------------------------


class TestSeamDeConfiguracion:
    def test_importar_store_no_carga_la_fase_6(self):
        """La promesa: `database/` es importable sin strategy/MT5/yaml."""
        code = (
            "import sys\n"
            "import database.store\n"
            "print(','.join(sorted(m for m in sys.modules\n"
            "    if m in ('strategy','core.strategy','settings.strategy_source',"
            "'MetaTrader5','yaml'))))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60, cwd=str(PROJECT_DIR),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "", (
            "importar database.store arrastro modulos de otras fases: %s"
            % proc.stdout.strip()
        )

    def test_el_default_azosa_el_yaml_en_vez_de_importarlo_al_arrancar(self):
        """El default sigue siendo el YAML de producción, pero a través de un import
        tardío: la Fase 6 solo entra si alguien pide la config."""
        assert isinstance(store._LazyStrategyConfig(), store.ConfigSource)
        assert store._config_source is not None

    def test_el_default_lee_el_yaml_real(self):
        """Con la Fase 6 ya exists, el seam por defecto materializa el YAML de verdad.

        Antes esto no podía existir: sin `strategy` el seam solo servía para inyectar
        un fake, así que la ruta de producción (leer `config/strategy.yaml`) no la
        cubría ningún test.
        """
        source = store._LazyStrategyConfig()
        cfg = source.get_config()
        doc = _yaml_real()
        assert cfg["min_score"] == doc["score"]["min_score"]
        assert cfg["magic"] == doc["execution"]["magic"]
        assert source.data_sources()["orderflow"] is False
        assert {s["symbol"] for s in source.watcher_config()["symbols"]} == {
            s["symbol"] for s in doc["watcher"]["symbols"]
        }

    def test_sin_yaml_el_error_dice_como_resolverlo(self, tmp_path, monkeypatch):
        """Un YAML ausente es `ConfigUnavailable` con un mensaje accionable, no un
        `FileNotFoundError` a mitad de una escritura.

        El seam traduce `StrategyConfigError` (que es del módulo que lee el fichero)
        a su propia excepción: quien habla con `store` necesita un solo tipo que
        capturar y un solo significado, sea cual sea la capa que falló.
        """
        from settings import strategy_source

        monkeypatch.setattr(strategy_source, "STRATEGY_PATH", str(tmp_path / "no-existe.yaml"))
        strategy_source.invalidate()
        source = store._LazyStrategyConfig()
        try:
            with pytest.raises(store.ConfigUnavailable) as exc:
                source.get_config()
            msg = str(exc.value)
            assert "no-existe.yaml" in msg
            assert "obligatorio" in msg
        finally:
            strategy_source.invalidate()

    def test_set_config_source_devuelve_el_anterior_y_se_puede_restaurar(self):
        first = FakeConfigSource({"a": 1})
        second = FakeConfigSource({"a": 2})
        prev = store.set_config_source(first)
        try:
            assert store.get_config_source() is first
            prev2 = store.set_config_source(second)
            assert prev2 is first
        finally:
            store.set_config_source(None)
        assert prev is not None

    def test_el_default_lee_el_mapa_real(self):
        """Igual que el YAML de strategy: el default tardío materializa el mapa real.

        El mapa tiene una sola entrada (8882026 -> "default") en F1: sin ella, no
        habría nada que leyera el fichero en producción ni en tests.
        """
        source = store._LazyStrategyConfig()

        assert source.strategy_map() == {8882026: "default"}

    def test_get_strategy_map_delega_en_el_seam(self, db):
        store.get_config_source().mapa = {8882026: "default", 9999001: "cta"}

        assert store.get_strategy_map() == {8882026: "default", 9999001: "cta"}

    def test_las_funciones_de_config_usan_el_seam(self, db):
        fake = store.get_config_source()
        fake.cfg = {
            "min_score": 61,
            "risk_weights": '{"smc": 40, "dxy": 15}',
            "killzones": '[{"name": "LON", "start": "07:00", "end": "10:00"}]',
            "data_sources": {"cme_6e": "databento"},
            "prop_enabled": 1,
        }
        summary = store.get_config_summary()
        assert summary["min_score"] == 61
        assert summary["risk_weights"] == {"smc": 40, "dxy": 15}
        assert summary["killzones"] == [
            {"name": "LON", "start": "07:00", "end": "10:00"}
        ]
        assert summary["prop_enabled"] is True

    def test_set_trading_config_delega_en_guardar(self, db):
        store.set_trading_config({"min_score": 99})
        assert store.get_config_source().saved == [{"min_score": 99}]

    def test_json_roto_en_config_no_tira_la_ui(self, db):
        """`risk_weights`/`killzones` vienen como JSON string del YAML. Uno roto
        no puede devolver 500 en /api/config: se degrada a vacio."""
        store.get_config_source().cfg = {
            "risk_weights": "{no es json",
            "killzones": "tampoco",
        }
        summary = store.get_config_summary()
        assert summary["risk_weights"] == {}
        assert summary["killzones"] == []


# ---------------------------------------------------------------------------
# El resumen contra el YAML REAL que se reparte
# ---------------------------------------------------------------------------


class TestResumenContraElYamlReal:
    """`get_config_summary` contra `config/strategy.yaml`, no contra un invento.

    El bug que esto cubre: la función leía `cfg["min_score"]`, `cfg["risk_pct"]` y
    compañía, que no existen en el YAML (que anida por secciones). Con la config de
    verdad puesta devolvía `min_score: None`, `risk_weights: {}` y `killzones: []`, y
    la UI pintaba "sin reglas" con el sistema entero configurado. Un test con un
    dict plano NO lo habría detectado: el dict plano es justo la forma que la
    función equivocada esperaba.
    """

    @staticmethod
    def _yaml_real() -> Dict[str, Any]:
        return _yaml_real()

    def test_el_resumen_refleja_el_yaml_anidado(self, db):
        store.get_config_source().cfg = self._yaml_real()

        summary = store.get_config_summary()

        assert summary["min_score"] == 59.5
        assert summary["min_rr"] == 2.0
        assert summary["setup_ttl_minutes"] == 40.0
        assert summary["risk_pct"] == 0.5
        assert summary["loss_limit_fixed"] == 400.0
        assert summary["loss_limit_pct"] == 1.6
        assert summary["max_trades_day"] == 3
        assert summary["news_buffer_min"] == 15
        assert summary["prop_enabled"] is False
        assert summary["prop_max_dd_daily_pct"] == 2.0
        assert summary["prop_max_dd_total_pct"] == 6.0
        assert summary["prop_max_profit_day_pct"] == 1.5
        assert summary["prop_consistency_days"] == 4

    def test_los_pesos_y_las_killzones_salen_de_sus_secciones(self, db):
        store.get_config_source().cfg = self._yaml_real()

        summary = store.get_config_summary()

        assert summary["risk_weights"] == {
            "cot": 0.0,
            "cvd_of": 20.0,
            "smc": 50.0,
            "killzone": 0.0,
            "smr_dxy": 15.0,
        }
        assert [kz["name"] for kz in summary["killzones"]] == ["Londres", "Nueva York"]
        assert summary["killzones"][0] == {
            "name": "Londres",
            "start": "07:00",
            "end": "10:00",
        }

    def test_data_sources_llega_entero(self, db):
        store.get_config_source().cfg = self._yaml_real()

        fuentes = store.get_config_summary()["data_sources"]

        assert fuentes["orderflow"] is False
        assert fuentes["news"] is True

    def test_la_config_plana_antigua_sigue_sirviendo(self, db):
        """Una config guardada por la UI de antes es plana. Leerla no cuesta nada."""
        store.get_config_source().cfg = {
            "min_score": 61,
            "risk_pct": 0.75,
            "prop_enabled": 1,
            "prop_max_dd_daily_pct": 2.5,
        }

        summary = store.get_config_summary()

        assert summary["min_score"] == 61
        assert summary["risk_pct"] == 0.75
        assert summary["prop_enabled"] is True
        assert summary["prop_max_dd_daily_pct"] == 2.5

    def test_anidado_gana_al_plano(self, db):
        """Con las dos formas presentes manda el YAML: es el que se carga hoy."""
        store.get_config_source().cfg = {
            "score": {"min_score": 59.5},
            "min_score": 12.0,
        }

        assert store.get_config_summary()["min_score"] == 59.5

    def test_una_seccion_que_falta_no_rompe(self, db):
        store.get_config_source().cfg = {"score": {"min_score": 55.0}}

        summary = store.get_config_summary()

        assert summary["min_score"] == 55.0
        assert summary["risk_pct"] is None
        assert summary["killzones"] == []
        assert summary["prop_enabled"] is False

    def test_todas_las_claves_del_resumen_existen_en_la_tabla(self, db):
        """Si alguien añade una clave al resumen y no a `RESUMEN_CLAVES`, sale plana."""
        store.get_config_source().cfg = self._yaml_real()

        resumen = store.get_config_summary()

        assert set(resumen) == {clave for clave, _, _ in store.RESUMEN_CLAVES}


# ---------------------------------------------------------------------------
# DB_PATH: una sola fuente de verdad
# ---------------------------------------------------------------------------


class TestDbPathUnica:
    def test_store_delega_en_core_paths(self):
        """REF lo recomponia en store.py; aqui sale de core.paths. Dos
        constantes con el mismo nombre y rutas distintas es como se acaba
        escribiendo la mitad de las filas en otro fichero, en silencio."""
        assert store.DB_PATH == core_paths.DB_PATH
        assert store.PROJECT_DIR == core_paths.PROJECT_DIR

    def test_apunta_a_database_no_a_la_raiz(self):
        assert Path(store.DB_PATH).parent.name == "database"
        assert Path(store.DB_PATH).name == "trading.db"
        assert Path(store.DB_PATH).parent.parent == PROJECT_DIR

    def test_es_absoluta(self):
        assert Path(store.DB_PATH).is_absolute()

    def test_no_se_recompone_en_el_propio_modulo(self):
        """Guardia de regresión: si alguien vuelve a poner un os.path.join aquí,
        las dos constantes dejan de estar sincronizadas."""
        source = Path(store.__file__).read_text(encoding="utf-8")
        assert 'DB_PATH = os.path.join(' not in source
        assert 'DB_PATH = "trading.db"' not in source

    def test_importar_desde_otro_cwd_no_crea_una_segunda_db(self, tmp_path):
        """El bug original, reproducido: ruta relativa + subproceso con otro CWD
        abría un segundo trading.db y toda la auditoría parecía evaporada."""
        code = (
            "import sys\n"
            "import database.store as s\n"
            "print(s.DB_PATH)\n"
        )
        # El proyecto tiene que estar en sys.path para el subproceso; se pasa por
        # PYTHONPATH en vez de cambiar el CWD, que es justo lo que se quiere probar.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_DIR)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(tmp_path), capture_output=True, text=True, timeout=60, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == store.DB_PATH
        assert not (tmp_path / "trading.db").exists()


# ---------------------------------------------------------------------------
# init_db y sembrado
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_crea_el_esquema_completo(self, db):
        import sqlite3

        conn = sqlite3.connect(db)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        }
        conn.close()
        assert len(tables) == 14
        assert {"setup_log", "trades", "roles", "journal"} <= tables

    def test_sembra_el_rol_general_con_el_prompt_de_la_config(self, db):
        roles = store.list_roles(active_only=False)
        general = [r for r in roles if r["id"] == "general"]
        assert len(general) == 1
        # El prompt se arma con agent_risk_policy del FakeConfigSource.
        assert "riesgo alto" in general[0]["system_prompt"]

    def test_los_roles_legados_se_conservan_pero_se_inactivan(self, db):
        """No se borra nada del pasado: se marca. Una fila que no se puede
        explicar se conserva, y el audit la excluye."""
        store.upsert_role(
            "legacy", "Viejo", "prompt", ["x"], provider="openai", model="m"
        )
        store.init_db()
        active = {r["id"] for r in store.list_roles(active_only=True)}
        allr = {r["id"] for r in store.list_roles(active_only=False)}
        assert "legacy" in allr
        assert "legacy" not in active
        assert active == {"general"}

    def test_es_idempotente(self, db):
        """init_db() corre en cada arranque: la segunda vez no puede fallar."""
        store.init_db()
        store.init_db()
        assert len(store.list_roles(active_only=False)) >= 1

    def test_siembra_los_ajustes_por_defecto(self, db):
        settings = store.get_settings()
        assert settings["active_role_id"] == "general"
        assert settings["history_limit"] == "20"


# ---------------------------------------------------------------------------
# Migracion de datos: horas legacy -> UTC
# ---------------------------------------------------------------------------


class TestMigracionHorasLegacy:
    """`trades.time_close` venia de `datetime.fromtimestamp()` (naive, hora del SO)
    mientras `synced_at` de la MISMA fila era UTC. Se corrige una vez en init_db."""

    def _insert(self, db, ticket, time_close):
        import sqlite3

        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT OR REPLACE INTO trades (ticket, symbol, action, time_close, synced_at) "
            "VALUES (?, 'EURUSD', 'BUY', ?, '2026-09-01T00:00:00+00:00')",
            (ticket, time_close),
        )
        conn.commit()
        conn.close()

    def test_convierte_el_naive_a_utc(self, db):
        self._insert(db, 1, "2026-09-01 12:00:00")
        store.init_db()
        import sqlite3

        conn = sqlite3.connect(db)
        value = conn.execute(
            "SELECT time_close FROM trades WHERE ticket=1"
        ).fetchone()[0]
        conn.close()
        assert value.endswith("+00:00"), value

    def test_no_toca_lo_que_ya_era_utc(self, db):
        self._insert(db, 2, "2026-09-01 12:00:00+00:00")
        store.init_db()
        import sqlite3

        conn = sqlite3.connect(db)
        value = conn.execute(
            "SELECT time_close FROM trades WHERE ticket=2"
        ).fetchone()[0]
        conn.close()
        assert value == "2026-09-01 12:00:00+00:00"

    def test_es_idempotente(self, db):
        self._insert(db, 3, "2026-09-01 12:00:00")
        store.init_db()
        import sqlite3

        conn = sqlite3.connect(db)
        first = conn.execute(
            "SELECT time_close FROM trades WHERE ticket=3"
        ).fetchone()[0]
        conn.close()
        store.init_db()
        conn = sqlite3.connect(db)
        second = conn.execute(
            "SELECT time_close FROM trades WHERE ticket=3"
        ).fetchone()[0]
        conn.close()
        assert first == second

    def test_un_valor_ilegible_no_rompe_el_init(self, db):
        """Una fila corrupta no puede impedir que la base levante."""
        self._insert(db, 4, "no es una fecha")
        store.init_db()  # no debe lanzar
        import sqlite3

        conn = sqlite3.connect(db)
        value = conn.execute(
            "SELECT time_close FROM trades WHERE ticket=4"
        ).fetchone()[0]
        conn.close()
        assert value == "no es una fecha"  # se deja como estaba, no se inventa


# ---------------------------------------------------------------------------
# Reloj
# ---------------------------------------------------------------------------


class TestRelojDelegado:
    """El sello de auditoria viene de core.clock, no de tclock."""

    def test_now_iso_es_utc_aware_y_longitud_fija(self):
        """Longitud fija porque se ordena lexicograficamente en SQLite."""
        stamp = store.now_iso()
        assert stamp.endswith("+00:00")
        assert len(stamp) == len("2026-09-27T12:00:00+00:00")

    def test_utc_stamp_y_now_iso_difieren_en_formato_pero_no_en_instante(self):
        """REF escribe con DOS formatos a proposito y no es una contradiccion:

        - `now_iso()` (ISO con 'T') en `created_at`/`timestamp`/`synced_at`.
        - `utc_stamp()` (espacio) en `trades.time_close`, que es la columna que
          arrastra el legado en hora local.

        Ordenar esas dos columnas entre si por string daria un resultado
        arbitrario, asi que el filtro de ventana compara con `datetime(col)` y no
        de forma lexicografica. Eso es lo que hay que comprobar aqui, no que los
        dos sellos sean el mismo string.
        """
        a = store.now_iso()
        b = store.utc_stamp()
        assert a != b, "si coinciden, el legado de time_close dejo de distinguirse"
        # Mismo instante, dos representaciones.
        assert clock.parse_iso(a) == clock.parse_iso(b.replace(" ", "T"))
        assert clock.parse_iso(a).tzinfo is not None, "ambos deben ser aware"

    def test_el_filtro_de_ventana_devuelve_sql_y_params(self):
        sql, params = store.utc_filter("timestamp", 7)
        assert "AND datetime(timestamp) >= datetime(?)" in sql
        assert len(params) == 1
        assert params[0].endswith("+00:00")

    def test_el_reloj_no_esta_congelado(self):
        """Guardia: si alguien sustituye now_iso por una constante, todas las
        filas del audit comparten sello y el orden temporal deja de existir."""
        assert store.now_iso() != "2026-01-01T00:00:00+00:00"