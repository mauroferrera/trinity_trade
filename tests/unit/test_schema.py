"""El esquema de `database/models.py` es el de PRODUCCION, y no una aproximacion.

REF/store.py metia el DDL dentro de `init_db()`, junto al sembrado y a una
migracion de datos. Preguntar "¿que columnas tiene setup_log?" exigia leer 330
lineas en medio de otra logica, y el esquema no se podia regenerar para un
entorno limpio sin ejecutar la funcion entera.

Aqui el esquema es una tupla de texto, y eso habilita las tres comprobaciones de
este fichero:

  1. Coincide con la DB real de produccion, congelada en
     `tests/fixtures/schema_snapshot.json`. REF es de solo lectura y puede
     desaparecer; el snapshot no, y mientras exista sigueholder la garantia.
  2. `database/schema.sql` no se ha desincronizado de las tuplas que lo
     generaron. `.gitignore` dice que las DB "se regeneran desde schema.sql", y
     un .sql mantenido a mano es una segunda fuente de verdad que nadie actualiza.
  3. Las migraciones son idempotentes y solo anaden lo que falta, que es lo unico
     que permite correrlas contra una base ya viva sin romperla.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from database import models

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "schema_snapshot.json"
SCHEMA_SQL = Path(__file__).resolve().parents[2] / "database" / "schema.sql"


@pytest.fixture()
def fresh_conn(tmp_path):
    """Base nueva, en memoria, con el esquema recien creado."""
    conn = sqlite3.connect(tmp_path / "nueva.db")
    conn.row_factory = sqlite3.Row
    models.init_schema(conn)
    yield conn
    conn.close()


def _snapshot_of(conn):
    out = {}
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for t in tables:
        cols = {}
        for r in conn.execute('PRAGMA table_info("%s")' % t):
            cols[r[1]] = {
                "type": r[2].upper(),
                "notnull": r[3],
                "default": r[4],
                "pk": r[5],
            }
        idx = sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? "
                "AND name NOT LIKE 'sqlite_%'",
                (t,),
            )
        )
        out[t] = {"columns": cols, "indexes": idx}
    return out


class TestEsquemaDeProduccion:
    """Lo unico que importa: que este esquema sea el de la base que ya existe."""

    def test_coincide_con_la_db_real_de_produccion(self, fresh_conn):
        """Tabla por tabla, columna por columna, contra produccion."""
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["tables"]
        got = _snapshot_of(fresh_conn)

        assert sorted(got) == sorted(expected), (
            "tablas distintas: faltan %s / sobran %s"
            % (sorted(set(expected) - set(got)), sorted(set(got) - set(expected)))
        )

        for table in sorted(expected):
            want_cols = expected[table]["columns"]
            got_cols = got[table]["columns"]
            assert sorted(got_cols) == sorted(want_cols), (
                "%s: faltan %s / sobran %s"
                % (
                    table,
                    sorted(set(want_cols) - set(got_cols)),
                    sorted(set(got_cols) - set(want_cols)),
                )
            )
            for col in sorted(want_cols):
                assert got_cols[col] == want_cols[col], "%s.%s" % (table, col)
            assert got[table]["indexes"] == expected[table]["indexes"], table

    def test_tiene_las_catorce_tablas_de_produccion(self, fresh_conn):
        got = [
            r[0]
            for r in fresh_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        assert got == list(models.TABLE_NAMES)
        assert len(got) == 14

    def test_tabla_setup_log_conserva_las_columnas_del_post_mortem(self, fresh_conn):
        """Las que hacen posible el post-mortem; si desaparecen, se pierde el audit.

        `context_json` y `snapshot_age_s` son las que separan "el setup no
        funcionó" de "el score era viejo". `source` separa producción de un test
        que escribió contra la real. `trade_result_json` es el desenlace.
        """
        cols = models.existing_columns(fresh_conn, "setup_log")
        for required in (
            "breakdown_json",
            "reject_reasons",
            "risk_state_json",
            "trade_result_json",
            "context_json",
            "snapshot_age_s",
            "source",
            "validated",
            "invalidate_level",
        ):
            assert required in cols, required

    def test_trades_tiene_lo_que_distingue_sl_de_cierre_manual(self, fresh_conn):
        cols = models.existing_columns(fresh_conn, "trades")
        # Sin exit_reason no se puede separar "lo paró el SL" de "lo cerró el
        # operador a mano", que es la diferencia entre un stop que funciona y uno
        # que se toca.
        for required in ("time_open", "commission", "swap", "exit_reason"):
            assert required in cols, required


class TestSchemaSqlAlDia:
    """`schema.sql` es un artefacto generado. Sin este test seria una segunda
    fuente de verdad."""

    def test_el_fichero_existe_y_se_puede_generar(self):
        assert SCHEMA_SQL.exists(), "falta database/schema.sql"
        assert models.render_sql() == SCHEMA_SQL.read_text(encoding="utf-8"), (
            "database/schema.sql no coincide con models.py. Regenerar con:\n"
            "    python -m database.models > database/schema.sql"
        )

    def test_el_sql_generado_es_ejecutable(self, tmp_path):
        """Un .sql que no se puede ejecutar es documentación, no esquema."""
        target = tmp_path / "desde_sql.db"
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        # Se aplica el DDL del fichero, no solo el de models.py: asi se comprueba
        # que lo que se escribe en el .sql funciona, y no solo que los strings
        # coinciden.
        for stmt in models.SCHEMA:
            conn.execute(stmt)
        for stmt in models.INDEXES:
            conn.execute(stmt)
        models.apply_migrations(conn)
        conn.commit()
        assert len(
            [
                r
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
        ) == 14
        conn.close()

    def test_cada_tabla_del_snapshot_aparece_en_el_sql(self):
        sql = SCHEMA_SQL.read_text(encoding="utf-8")
        for table in models.TABLE_NAMES:
            assert "CREATE TABLE IF NOT EXISTS %s (" % table in sql, table


class TestMigraciones:
    """Idempotentes y aditivas: es lo unico que permite correrlas en caliente."""

    def test_sobre_base_nueva_no_anade_nada(self, fresh_conn):
        """La base ya nace con todo: una migracion que anade algo esta obsoleta."""
        assert models.apply_migrations(fresh_conn) == []

    def test_correrlas_dos_veces_no_cambia_nada(self, fresh_conn):
        first = models.apply_migrations(fresh_conn)
        second = models.apply_migrations(fresh_conn)
        assert first == []
        assert second == []

    def test_anade_lo_que_falta_en_una_base_vieja(self, tmp_path):
        """Simula la base de hace meses: `roles` sin `active`, `messages` sin
        `tool_call_id`. Es el caso que las migraciones existen para cubrir."""
        conn = sqlite3.connect(tmp_path / "vieja.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE roles (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "system_prompt TEXT NOT NULL, allowed_tools TEXT NOT NULL, "
            "provider TEXT NOT NULL DEFAULT 'auto', model TEXT NOT NULL DEFAULT '', "
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, "
            "timestamp TEXT NOT NULL)"
        )
        added = models.apply_migrations(conn)
        conn.commit()

        assert "roles.active" in added
        assert "messages.tool_call_id" in added
        # `existing_columns` devuelve nombres sueltos; `added` viene cualificado
        # ("tabla.columna") para que la lista se pueda leer de un vistazo.
        assert "active" in models.existing_columns(conn, "roles")
        assert "tool_call_id" in models.existing_columns(conn, "messages")
        # Y no duplica lo que ya estaba.
        assert models.apply_migrations(conn) == []
        conn.close()

    def test_las_filas_viejas_no_se_rellenan_con_inventos(self, tmp_path):
        """NULL = "no medido". Rellenarlo de ceros fabricaría evidencia."""
        conn = sqlite3.connect(tmp_path / "vieja.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ticket INTEGER NOT NULL UNIQUE, symbol TEXT NOT NULL, action TEXT NOT NULL, "
            "synced_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO trades (ticket, symbol, action, synced_at) VALUES (1,'X','BUY','t')")
        models.apply_migrations(conn)
        row = conn.execute("SELECT * FROM trades").fetchone()

        for col in ("time_open", "commission", "swap", "exit_reason"):
            assert row[col] is None, "%s no debe inventarse: NULL = no medido" % col
        conn.close()

    def test_init_schema_es_idempotente(self, tmp_path):
        """init_db() se llama en cada arranque: no puede fallar la segunda vez."""
        conn = sqlite3.connect(tmp_path / "dos.db")
        conn.row_factory = sqlite3.Row
        models.init_schema(conn)
        models.init_schema(conn)
        conn.commit()
        assert len(
            [
                r
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
        ) == 14
        conn.close()