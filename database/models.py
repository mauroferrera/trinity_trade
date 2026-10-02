"""Esquema de la base de datos: la fuente de verdad, en un solo sitio.

Extraído de `REF/store.py::init_db()`, que mezclaba el DDL con el sembrado de
filas, las migraciones y el reloj. Con las cuatro cosas en una función, la
pregunta "¿qué columnas tiene `setup_log`?" solo se respondía leyendo 330 líneas
en medio de otra lógica, y el esquema no se podía regenerar para un entorno
nuevo sin ejecutarla.

Aquí se separa por responsabilidad:

  - `SCHEMA`: los `CREATE TABLE`/`CREATE INDEX` de la base actual.
  - `INDEXES`: los `CREATE INDEX` explícitos, aparte de los DDL, porque SQLite los
    crea por su cuenta con nombres `sqlite_autoindex_*` y no se pueden pedir.
  - `MIGRATIONS`: las columnas que se fueron añadiendo. Se ejecutan SIEMPRE, pero
    son idempotentes (`if not in` sobre `PRAGMA table_info`), que es lo que las
    hace seguras de correr contra una base ya viva.
  - `init_schema(conn)`: aplica `SCHEMA` + `MIGRATIONS`.

Las filas iniciales (roles y settings del agente) NO viven aquí: necesitan la
config del agente, que es un seam perezoso de `store.py`. Ver `_seed_defaults`.

La migración de DATOS (normalizar horas legacy a UTC) no vive aquí: es del
dominio de `store.py`, no del esquema.

`.gitignore` dice que las DB "se regeneran desde schema.sql", y eso obliga a que
el esquema sea legible por una persona y no solo por Python. `schema.sql` se
genera desde aquí con `python -m database.models > database/schema.sql`, y
`tests/unit/test_schema.py` falla si se desincronizan. Sin ese test, el fichero
.sql sería una segunda fuente de verdad que nadie actualiza.

NOTA SOBRE `trades.exit_reason` y los `NULL`: las columnas añadidas por migración
se quedan en `NULL` en las filas viejas. Es deliberado. `NULL` significa "no
medido"; `0` significaría "medido y valía cero", y para el post-mortem no es lo
mismo. Rellenar con ceros fabricaría evidencia.
"""

from __future__ import annotations

import sqlite3

# ============================================================
# Tablas
# ============================================================

SCHEMA: tuple[str, ...] = (
    # --- Agente: roles, conversaciones, mensajes ---
    """
    CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        system_prompt TEXT NOT NULL,
        allowed_tools TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'auto',
        model TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )
    """,
    # --- Espejo de operaciones del broker ---
    # `trades` decía cuánto se ganó y nada más: sin la hora de apertura no hay
    # duración, sin comisión/swap el profit neto no es el neto, y sin el motivo
    # de cierre no se puede separar "lo paró el SL" de "lo cerró el operador a
    # mano" — que es la diferencia entre un stop que funciona y uno que se toca.
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        volume REAL,
        price_open REAL,
        price_close REAL,
        profit REAL,
        time_close TEXT,
        synced_at TEXT NOT NULL
    )
    """,
    # --- Bitácora / journal ---
    """
    CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT,
        ticket TEXT,
        symbol TEXT NOT NULL DEFAULT 'EURUSD',
        action TEXT,
        poi_type TEXT,
        liquidity_swept TEXT,
        cme_confirmation TEXT,
        setup_json TEXT NOT NULL DEFAULT '{}',
        emotion TEXT,
        plan_compliance INTEGER,
        tags TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        timestamp TEXT NOT NULL
    )
    """,
    # --- Ajustes del agente (clave/valor) ---
    """
    CREATE TABLE IF NOT EXISTS agent_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # --- Alertas de precio del gráfico ---
    """
    CREATE TABLE IF NOT EXISTS chart_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        price REAL NOT NULL,
        label TEXT,
        side TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        triggered_at TEXT
    )
    """,
    # --- Informes COT (macro) ---
    """
    CREATE TABLE IF NOT EXISTS cot_reports (
        report_date TEXT PRIMARY KEY,
        am_net INTEGER,
        lf_net INTEGER,
        nc_net INTEGER,
        cot_index_26w REAL,
        macro_bias TEXT,
        delta_am REAL,
        delta_lf REAL,
        created_at TEXT NOT NULL
    )
    """,
    # --- Registro append-only del motor de riesgo (el post-mortem) ---
    """
    CREATE TABLE IF NOT EXISTS setup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT NOT NULL,
        verdict TEXT NOT NULL,
        score REAL NOT NULL,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        validated INTEGER,
        reject_reasons TEXT NOT NULL DEFAULT '[]',
        risk_state_json TEXT NOT NULL DEFAULT '{}',
        trade_result_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        snapshot_age_s REAL,
        source TEXT NOT NULL DEFAULT '',
        timestamp TEXT NOT NULL
    )
    """,
    # Excursión observada de cada posición abierta. MT5 no expone MFE/MAE en la
    # API de deals, así que esto NO es la excursión real: es el extremo entre las
    # muestras que se observaron mientras la posición vivía, y por eso se guarda
    # también cuántas fueron. `best_price`/`worst_price` van ya orientados a la
    # DIRECCIÓN (best = extremo más a favor), no un high/low crudo, para que
    # convertirlos a R no tenga que adivinar el signo.
    """
    CREATE TABLE IF NOT EXISTS trade_excursions (
        ticket INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        price_open REAL,
        best_price REAL,
        worst_price REAL,
        samples INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT,
        updated_at TEXT
    )
    """,
    # Estado del watcher (bot a la escucha): dedup por symbol/timeframe.
    """
    CREATE TABLE IF NOT EXISTS setup_state (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT,
        score REAL,
        verdict TEXT,
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        status TEXT NOT NULL DEFAULT 'active',
        notified INTEGER NOT NULL DEFAULT 0,
        auto_executed INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    )
    """,
    # Overrides del watcher persistidos (p.ej. auto_execute conmutado en la UI).
    """
    CREATE TABLE IF NOT EXISTS watcher_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Estado del gate de prop-firm (baseline, picos de equity diario/total).
    # Filas JSON clave->valor para no tocar el esquema por cada métrica nueva
    # (mismo patrón que watcher_settings).
    """
    CREATE TABLE IF NOT EXISTS prop_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # Dibujos manuales del gráfico, por symbol/timeframe. Cada item lleva
    # origin: "user"|"agent".
    """
    CREATE TABLE IF NOT EXISTS chart_drawings (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        drawings_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    )
    """,
)


INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_setup_log_time ON setup_log(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_setup_log_verdict ON setup_log(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_setup_log_age ON setup_log(snapshot_age_s);",
    "CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);",
    "CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_close);",
    "CREATE INDEX IF NOT EXISTS idx_journal_time ON journal(timestamp);",
)


# ============================================================
# Migraciones de esquema (idempotentes)
# ============================================================
#
# Cada entrada es (tabla, columna, definición del ALTER). Todas se comprueban
# antes de aplicarse, así que correrlas sobre una base ya actualizada no hace
# nada y sobre una base nueva tampoco rompe.
#
# Se conservan aunque la base nazca con la columna ya declarada arriba: una base
# en producción se crea desde un `init_db()` de hace meses, no desde este
# fichero, y quitar la migración significaría que esa base no levanta.

MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # `trades`: cerrar el post-mortem (ver la nota de la tabla).
    ("trades", "time_open", "REAL"),
    ("trades", "commission", "REAL"),
    ("trades", "swap", "REAL"),
    ("trades", "exit_reason", "INTEGER"),
    # `roles`: roles legados conservados pero inactivos.
    ("roles", "active", "INTEGER NOT NULL DEFAULT 1"),
    # `messages`: multi-turno con tools.
    ("messages", "tool_call_id", "TEXT"),
    ("messages", "name", "TEXT"),
    # `chart_alerts`: alertas multicondición.
    ("chart_alerts", "conditions", "TEXT NOT NULL DEFAULT '[]'"),
    ("chart_alerts", "timeframe", "TEXT"),
    ("chart_alerts", "expires_at", "TEXT"),
    ("chart_alerts", "last_check", "TEXT"),
    # `journal`: precios y tiempos para la proyección gráfica de operaciones.
    ("journal", "entry_price", "REAL"),
    ("journal", "sl_price", "REAL"),
    ("journal", "tp_price", "REAL"),
    ("journal", "time_open", "TEXT"),
    ("journal", "time_close", "TEXT"),
    # `setup_log`: procedencia de la fila. '' = producción.
    # 'test_fixture' = residuo de una ejecución manual o de un test que escribió
    # contra la DB real. No se borran: una fila que no se puede explicar se
    # marca, y el audit la excluye de las métricas para que no contamine.
    ("setup_log", "source", "TEXT NOT NULL DEFAULT ''"),
    # Contexto de decisión: la fila decía QUÉ se decidió pero no EN QUÉ
    # CONDICIONES, así que el post-mortem no podía separar "el setup no
    # funcionó" de "el score era viejo" ni de "el spread ya se había comido el
    # SL". `snapshot_age_s` va en columna propia porque es la única por la que se
    # filtra y ordena; el resto del contexto (procedencia, spread, fracción del
    # SL) vive en el JSON. Las filas anteriores se quedan con '{}' y edad NULL.
    ("setup_log", "context_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("setup_log", "snapshot_age_s", "REAL"),
)


TABLE_NAMES: tuple[str, ...] = (
    "agent_settings",
    "chart_alerts",
    "chart_drawings",
    "conversations",
    "cot_reports",
    "journal",
    "messages",
    "prop_state",
    "roles",
    "setup_log",
    "setup_state",
    "trade_excursions",
    "trades",
    "watcher_settings",
)


# ============================================================
# Aplicación del esquema
# ============================================================


def existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Columnas actuales de `table` (vacío si la tabla no existe)."""
    return {r["name"] for r in conn.execute('PRAGMA table_info("%s")' % table)}


def _known_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Aplica las columnas que falten. Devuelve las que realmente añadió.

    Salta las tablas que no existen. En `init_schema` es irrelevante (el DDL va
    primero), pero `apply_migrations` es pública y se usa sola: contra una base a
    medio migrar, o una donde alguien dropeó una tabla, un `ALTER TABLE`
    inexistente aborta el arranque entero. Saltarse la tabla es lo correcto: si
    no está, `SCHEMA` la creará con sus columnas ya dentro.
    """
    added: list[str] = []
    for table, column, decl in MIGRATIONS:
        present = existing_columns(conn, table)
        if not present and table not in _known_tables(conn):
            continue
        if column in present:
            continue
        conn.execute('ALTER TABLE "%s" ADD COLUMN "%s" %s' % (table, column, decl))
        added.append("%s.%s" % (table, column))
    return added


def init_schema(conn: sqlite3.Connection) -> list[str]:
    """Crea el esquema completo y aplica las migraciones pendientes."""
    for statement in SCHEMA:
        conn.execute(statement)
    for statement in INDEXES:
        conn.execute(statement)
    return apply_migrations(conn)


def render_sql() -> str:
    """El esquema como `schema.sql` legible.

    Se genera desde las MISMAS tuplas que ejecuta `init_schema`, no se escribe a
    mano: un `.sql` mantenido a mano es una segunda fuente de verdad, y el test
    `test_schema_sql_al_dia` compara ambos para que no se separen.
    """
    out = [
        "-- GENERADO, no editar a mano:",
        "--   python -m database.models > database/schema.sql",
        "-- Fuente de verdad: database/models.py (SCHEMA, INDEXES, MIGRATIONS).",
        "-- `tests/unit/test_schema.py::test_schema_sql_al_dia` falla si se separan.",
        "",
    ]
    for statement in SCHEMA:
        out.append("%s;" % statement.strip())
        out.append("")
    out.append("-- Índices")
    for statement in INDEXES:
        out.append(statement)
        out.append("")
    out.append("-- Migraciones idempotentes (ALTER TABLE ... ADD COLUMN)")
    out.append("-- Se aplican en init_schema(); aquí solo como referencia.")
    for table, column, decl in MIGRATIONS:
        out.append(
            '-- %-14s ADD COLUMN %-14s %s' % (table, column, decl)
        )
    out.append("")
    return "\n".join(out)


def write_sql(path: "os.PathLike[str] | str") -> None:
    """Escribe `schema.sql` como UTF-8 con finales de linea LF.

    Existe como función, y no como `python -m database.models > schema.sql`, por
    un motivo concreto: la redireccion `>` de PowerShell 5.1 escribe **UTF-16LE**.
    Generado así, `schema.sql` era un fichero que `sqlite3` no podía leer y que
    ningún editor de texto mostraba bien; el test de sincronía lo detectó, pero
    mejor no depender de que lo detecte.
    """
    text = render_sql()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


if __name__ == "__main__":  # pragma: no cover - generador de schema.sql
    import os.path
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "schema.sql"
    )
    write_sql(target)
    print("escrito %s" % target, file=sys.stderr)